#!/usr/bin/env python3
"""Standalone Ace 25c test/control. No changes to the wearable project."""
from __future__ import annotations
import argparse
import asyncio
from dataclasses import asdict
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent/'vendor'))
from amaran_protocol.protocol import (
    normalize_hex_key, build_mesh_proxy_pdu, build_proxy_filter_pdu,
    decode_mesh_proxy_access, decode_secure_network_beacon,
    status_request_payload, brightness_payload_percent, cct_payload_percent,
    hsi_payload, power_payload,
)

PROXY_IN = '00002add-0000-1000-8000-00805f9b34fb'
PROXY_OUT = '00002ade-0000-1000-8000-00805f9b34fb'
ROOT = Path(__file__).resolve().parent


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class SequenceStore:
    """Caller holds the process lock throughout connection and writes."""
    def __init__(self, path, fingerprint):
        self.path = path
        if path.exists():
            self.value = json.loads(path.read_text())
            if self.value.get('fingerprint') != fingerprint:
                raise ValueError('The light network changed; existing counters were preserved. Reconfigure explicitly.')
        else:
            self.value = dict(fingerprint=fingerprint, source=0x7000+secrets.randbelow(0xFFF),
                              next_sequence=100000, iv_index=0)
            atomic_json(path, self.value)
        if (not 0x7000 <= self.value['source'] < 0x7FFF or
                not 0 <= self.value['next_sequence'] <= 0x1000000 or
                not 0 <= self.value['iv_index'] <= 0xFFFFFFFF):
            raise ValueError('Invalid counter state; refusing to reset it.')

    def reserve(self):
        seq = self.value['next_sequence']
        if seq > 0xFFFFFF:
            raise ValueError('Mesh sequence exhausted; reconfiguration required.')
        self.value['next_sequence'] = seq+1
        atomic_json(self.path, self.value)  # Persist BEFORE using a nonce.
        return seq

    def note_beacon(self, beacon):
        # This small test tool deliberately does not implement IV-update procedures.
        if beacon.iv_update:
            raise ValueError('The mesh is updating its IV index; wait and use the app for now.')
        if beacon.iv_index < self.value['iv_index']:
            raise ValueError('Network IV index moved backwards; refusing to reuse old state.')
        if beacon.iv_index != self.value['iv_index']:
            self.value['iv_index'] = beacon.iv_index
            atomic_json(self.path, self.value)


def fragments(pdu, limit=20):
    if len(pdu) <= limit:
        return [pdu]
    kind = pdu[0] & 0x3F
    chunks = [pdu[i:i+limit-1] for i in range(1, len(pdu), limit-1)]
    return [bytes([kind | (0x40 if i == 0 else 0xC0 if i == len(chunks)-1 else 0x80)])+part
            for i, part in enumerate(chunks)]


class Reassembler:
    def __init__(self):
        self.parts = b''
        self.kind = None
        self.started = 0

    def feed(self, raw):
        if not raw:
            return None
        sar, kind = raw[0] >> 6, raw[0] & 0x3F
        if sar == 0:
            self.parts, self.kind = b'', None
            return bytes(raw)
        if sar == 1:
            self.parts, self.kind, self.started = bytes(raw[1:]), kind, time.monotonic()
            return None
        if self.kind != kind or time.monotonic()-self.started > 10 or len(self.parts)+len(raw)-1 > 512:
            self.parts, self.kind = b'', None
            return None
        self.parts += bytes(raw[1:])
        if sar == 3:
            result = bytes([kind])+self.parts
            self.parts, self.kind = b'', None
            return result
        return None


class LightConnection:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.net_key = normalize_hex_key(config['net_key'])
        self.app_key = normalize_hex_key(config['app_key'])
        self.node = config['node_address']
        self.reassembler = Reassembler()
        self.status = None
        self.status_version = 0
        self.beacon_error = None
        self.active_iv = store.value['iv_index']
        self.client = None

    def notification(self, sender, data):
        packet = self.reassembler.feed(bytes(data))
        if packet is None:
            return
        beacon = decode_secure_network_beacon(net_key=self.net_key, proxy_pdu=packet)
        if beacon is not None:
            try:
                self.store.note_beacon(beacon)
                if self.client is not None and self.active_iv != beacon.iv_index:
                    self.beacon_error = 'Network IV changed during the connection; run the command again.'
            except ValueError as error:
                self.beacon_error = str(error)
            return
        message = decode_mesh_proxy_access(net_key=self.net_key, app_key=self.app_key,
                                          iv_index=self.store.value['iv_index'], proxy_pdu=packet)
        if message is not None and message.source_address == self.node and message.sidus_status is not None:
            self.status = message.sidus_status
            self.status_version += 1

    async def write(self, pdu):
        if self.beacon_error:
            raise RuntimeError(self.beacon_error)
        for fragment in fragments(pdu):
            await asyncio.wait_for(self.client.write_gatt_char(PROXY_IN, fragment, response=False), 5)
            await asyncio.sleep(0.04)

    async def send(self, payload):
        pdu = build_mesh_proxy_pdu(net_key=self.net_key, app_key=self.app_key,
                                  src=self.store.value['source'], dst=self.node,
                                  seq=self.store.reserve(), iv_index=self.active_iv, sidus_payload=payload)
        await self.write(pdu)

    async def connect(self):
        from bleak import BleakClient, BleakScanner
        found = asyncio.get_running_loop().create_future()
        def detected(device, ad):
            if device.address.upper() == self.config['address'].upper() and not found.done():
                print('Found Ace 25c; signal=%s dBm' % ad.rssi, flush=True)
                found.set_result(device)
        print('Scanning for Ace 25c (up to 15 seconds)...', flush=True)
        async with BleakScanner(detection_callback=detected):
            device = await asyncio.wait_for(found, 15)
            client = BleakClient(device, timeout=20)
            self.client = client
            await asyncio.wait_for(client.connect(), 25)
        print('Connected; requesting authenticated status...', flush=True)
        await asyncio.wait_for(self.client.start_notify(PROXY_OUT, self.notification), 5)
        # Collect any initial authenticated network beacon before building messages.
        self.client = client
        await asyncio.sleep(2)
        if self.beacon_error:
            # A changed IV before the first message is safe to adopt.
            if self.beacon_error.startswith('Network IV changed'):
                self.beacon_error = None
            else:
                raise RuntimeError(self.beacon_error)
        self.active_iv = self.store.value['iv_index']
        filter_pdu = build_proxy_filter_pdu(net_key=self.net_key, app_key=self.app_key,
                                           src=self.store.value['source'], seq=self.store.reserve(),
                                           iv_index=self.active_iv)
        await self.write(filter_pdu)
        await asyncio.sleep(0.3)

    async def read_status(self, timeout=5):
        version = self.status_version
        await self.send(status_request_payload())
        deadline = time.monotonic()+timeout
        while self.status_version == version:
            if self.beacon_error:
                raise RuntimeError(self.beacon_error)
            if not self.client.is_connected:
                raise RuntimeError('The light disconnected.')
            if time.monotonic() >= deadline:
                raise RuntimeError('Connected, but no authenticated light status arrived. Quit the Mac app and retry nearby.')
            await asyncio.sleep(0.05)
        return self.status

    async def close(self):
        if self.client is not None:
            try:
                await asyncio.wait_for(self.client.disconnect(), 5)
            except Exception:
                print('Light disconnect cleanup did not finish normally.', file=sys.stderr)


def command_payload(args):
    if args.command == 'brightness':
        return brightness_payload_percent(args.percent)
    if args.command == 'white':
        return cct_payload_percent(percent=args.percent, kelvin=args.kelvin)
    if args.command == 'color':
        return hsi_payload(hue=args.hue, saturation=args.saturation, intensity=args.percent*10)
    if args.command in ('on', 'off'):
        return power_payload(args.command == 'on')
    return None


def confirmed(args, status):
    expected = round(args.percent/100*255) if hasattr(args, 'percent') else None
    if args.command in ('brightness', 'white', 'color'):
        if status.brightness is None or abs(status.brightness-expected) > 2:
            return False
    if args.command == 'white':
        return status.color_temp_kelvin is not None and abs(status.color_temp_kelvin-args.kelvin) <= 10
    if args.command == 'color':
        return (status.hs_color is not None and abs(status.hs_color[0]-args.hue) <= 1
                and abs(status.hs_color[1]-args.saturation) <= 1)
    if args.command in ('on', 'off'):
        return status.power is (args.command == 'on')
    return True


async def run(args, config, store):
    light = LightConnection(config, store)
    try:
        await light.connect()
        before = await light.read_status()
        print('Authenticated status received from the selected light.', flush=True)
        payload = command_payload(args)
        if payload is not None:
            if args.command not in ('on', 'off') and before.power is False:
                raise RuntimeError('The light output is off. Turn it on using its button, then retry.')
            await light.send(payload)
            await asyncio.sleep(0.4)
            after = await light.read_status()
            if not confirmed(args, after):
                print('Command sent, but the returned status did not confirm the requested setting. Check the light visually.', flush=True)
                return 2
            print('Light reported the requested setting.', flush=True)
        else:
            after = before
        print(json.dumps(dict(power=after.power, brightness_percent=None if after.brightness is None else round(after.brightness/255*100, 1),
                              kelvin=after.color_temp_kelvin, hue_saturation=after.hs_color), indent=2))
        return 0
    finally:
        await light.close()


def bounded(low, high):
    def parse(value):
        number = int(value)
        if not low <= number <= high:
            raise argparse.ArgumentTypeError('Must be between %d and %d' % (low, high))
        return number
    return parse


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT/'light_config.json')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status')
    commands.add_parser('on')
    commands.add_parser('off')
    p = commands.add_parser('brightness'); p.add_argument('percent', type=bounded(0, 100))
    p = commands.add_parser('white'); p.add_argument('kelvin', type=bounded(2300, 10000)); p.add_argument('--percent', type=bounded(0, 100), default=10)
    p = commands.add_parser('color'); p.add_argument('hue', type=bounded(0, 360)); p.add_argument('--saturation', type=bounded(0, 100), default=60); p.add_argument('--percent', type=bounded(0, 100), default=10)
    return parser.parse_args(argv)


def main():
    args = parse_args()
    args.config = args.config.resolve()  # Preserve the original private counter/lock directory.
    config = json.loads(args.config.read_text())
    # Validate before opening a connection. Never include key values in errors.
    net = normalize_hex_key(config['net_key']); app = normalize_hex_key(config['app_key'])
    if not 1 <= int(config['node_address']) < 0x7000:
        raise ValueError('Invalid light mesh address.')
    fingerprint = hashlib.sha256(net+app).hexdigest()
    lockpath = args.config.with_name('light.lock')
    fd = os.open(str(lockpath), os.O_RDWR|os.O_CREAT, 0o600)
    with os.fdopen(fd, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another light command is running. Wait for it to finish.')
        store = SequenceStore(args.config.with_name('sequence.json'), fingerprint)
        return asyncio.run(asyncio.wait_for(run(args, config, store), 55))


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit('\nLight command stopped.')
    except (TimeoutError, asyncio.TimeoutError):
        raise SystemExit('Light scan or connection timed out. Quit amaran Desktop and retry with the light nearby.')
    except Exception as error:
        # Avoid third-party exception repr containing config/keys.
        if type(error) in (RuntimeError, ValueError) and not isinstance(error, json.JSONDecodeError):
            raise SystemExit(str(error))
        raise SystemExit('Light test failed (%s). The wearable project was not changed.' % type(error).__name__)
