"""Synthetic packets and fake BLE only. No physical devices or live sockets."""
import asyncio
import contextlib
import importlib.util
import io
import logging
from pathlib import Path
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

import numpy as np
ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parents[1] / 'monitor'
SUPPORT = SOURCE
sys.path.insert(0,str(SUPPORT))
sys.path.insert(0,str(SOURCE))
from frame_buffer import MuseFrameBuffer
from rolling_buffer import RollingBuffer

with patch.dict(sys.modules, {
    'bleak': types.SimpleNamespace(BleakClient=object),
    'decoder': types.SimpleNamespace(decode_eeg_packet=lambda data:data),
    'processor': types.SimpleNamespace(EEGProcessor=object),
}):
    spec = importlib.util.spec_from_file_location('tested_muse_notify',SOURCE/'muse_notify.py')
    muse = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(muse)
muse.logger.addHandler(logging.NullHandler())
UUIDS = list(MuseFrameBuffer.CHANNEL_NAMES)


def feed(session,start,stop):
    for number in range(start,stop):
        for uuid in UUIDS:
            session.callback(types.SimpleNamespace(uuid=uuid),(number,np.full(12,float(number))))


class FrameTests(unittest.TestCase):
    def test_requires_all_four_matching_channels(self):
        b=MuseFrameBuffer()
        for uuid in UUIDS[:3]:
            self.assertIsNone(b.add_packet(uuid,7,[1]*12))
        frame=b.add_packet(UUIDS[3],7,[2]*12)
        self.assertEqual(frame[0],7)
        self.assertEqual(set(frame[1]),{'TP9','AF7','AF8','TP10'})
        self.assertFalse(b.frames)

    def test_incomplete_frames_are_bounded(self):
        b=MuseFrameBuffer(max_pending=3,max_age_s=1000,clock=lambda:0)
        for number in range(10000):
            b.add_packet(UUIDS[0],number,[1]*12)
        self.assertEqual(len(b.frames),3)
        self.assertEqual(len(b.first_seen),3)
        self.assertEqual(b.overflow_count,9997)

    def test_expired_partial_frames_cannot_supply_old_channels(self):
        clock=[0.]
        b=MuseFrameBuffer(clock=lambda:clock[0])
        b.add_packet(UUIDS[0],65535,['old']*12)
        clock[0]=4000  # Includes reuse of the 16-bit packet counter after wrap.
        for uuid in UUIDS[1:]:
            self.assertIsNone(b.add_packet(uuid,65535,['fresh']*12))
        frame=b.add_packet(UUIDS[0],65535,['fresh']*12)
        self.assertEqual(frame[1]['TP9'],['fresh']*12)
        self.assertEqual(b.expired_count,1)


class SessionTests(unittest.TestCase):
    def test_queue_is_latest_only_and_snapshots_independent(self):
        q=asyncio.Queue(maxsize=1)
        session=muse.MuseSession(q,1)
        feed(session,0,70)
        self.assertEqual(q.qsize(),1)
        self.assertGreater(session.dropped_windows,0)
        generation,when,old=q.get_nowait()
        self.assertEqual(generation,1)
        self.assertEqual(len(old['AF7']),512)
        before=old['AF7'].copy()
        feed(session,70,80)
        np.testing.assert_array_equal(before,old['AF7'])
        self.assertFalse(np.shares_memory(old['AF7'],session.rolling.get_data()['AF7']))

    def test_health_distinguishes_connection_packet_channel_and_frame_loss(self):
        clock=[0.]
        session=muse.MuseSession(asyncio.Queue(1),1,clock=lambda:clock[0])
        self.assertIsNone(session.health_problem(True))
        self.assertIn('Bluetooth',session.health_problem(False))
        clock[0]=9.
        self.assertIn('No EEG notifications',session.health_problem(True))
        session.last_raw=9.
        session.last_channel={ch:9. for ch in session.channel_packets}
        session.last_channel['AF8']=0.
        self.assertIn('AF8',session.health_problem(True))
        session.last_channel['AF8']=9.
        self.assertIn('no matching',session.health_problem(True))
        session.last_frame=9.
        self.assertIsNone(session.health_problem(True))

    def test_malformed_notification_is_counted_and_does_not_escape_callback(self):
        session=muse.MuseSession(asyncio.Queue(1),1)
        session.callback(types.SimpleNamespace(uuid=UUIDS[0]),None)
        self.assertEqual(session.raw_packets,1)
        self.assertEqual(session.decode_errors,1)
        self.assertEqual(session.complete_frames,0)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def wait_started(self,event):
        await asyncio.wait_for(asyncio.to_thread(event.wait),1)

    async def test_slow_processing_does_not_block_callbacks(self):
        started=threading.Event();release=threading.Event()
        class Processor:
            def process(self,eeg):
                started.set()
                if not release.wait(2): raise RuntimeError('test release not signaled')
        q=asyncio.Queue(1);session=muse.MuseSession(q,1)
        feed(session,0,50)
        worker=asyncio.create_task(muse.process_windows(q,Processor(),muse.WorkerState()))
        try:
            await self.wait_started(started)
            before=time.monotonic()
            feed(session,50,100)
            self.assertLess(time.monotonic()-before,.5)
            self.assertEqual(session.complete_frames,100)
            self.assertEqual(q.qsize(),1)
        finally:
            release.set();worker.cancel()
            await asyncio.gather(worker,return_exceptions=True)

    async def test_smoothing_restarts_after_a_new_connection(self):
        class Processor:
            def __init__(self):
                self.faa_smoothed=9;self.fba_smoothed=9;self.previous=[]
            def process(self,eeg):
                self.previous.append((self.faa_smoothed,self.fba_smoothed))
                self.faa_smoothed=self.fba_smoothed=5
        p=Processor();q=asyncio.Queue(1)
        worker=asyncio.create_task(muse.process_windows(q,p,muse.WorkerState()))
        async def wait_count(n):
            while len(p.previous)<n: await asyncio.sleep(.001)
        try:
            q.put_nowait((1,time.monotonic(),{}))
            await asyncio.wait_for(wait_count(1),1)
            q.put_nowait((2,time.monotonic(),{}))
            await asyncio.wait_for(wait_count(2),1)
            self.assertEqual(p.previous,[(None,None),(None,None)])
        finally:
            worker.cancel();await asyncio.gather(worker,return_exceptions=True)

    async def test_processing_failure_reaches_watchdog(self):
        class Processor:
            def process(self,eeg): raise RuntimeError('disk full')
        q=asyncio.Queue(1);q.put_nowait((1,time.monotonic(),{}))
        state=muse.WorkerState()
        worker=asyncio.create_task(muse.process_windows(q,Processor(),state))
        session=muse.MuseSession(q,1)
        try:
            with self.assertRaisesRegex(muse.ProcessingFailure,'disk full'):
                await asyncio.wait_for(muse.watch_stream(session,types.SimpleNamespace(is_connected=True),worker,state,interval=.001),1)
        finally:
            worker.cancel();await asyncio.gather(worker,return_exceptions=True)

    async def test_cancellation_waits_for_the_active_csv_writer(self):
        started=threading.Event();release=threading.Event();finished=threading.Event()
        class Processor:
            def process(self,eeg):
                started.set();release.wait(2);finished.set()
        q=asyncio.Queue(1);q.put_nowait((1,time.monotonic(),{}))
        worker=asyncio.create_task(muse.process_windows(q,Processor(),muse.WorkerState()))
        try:
            await self.wait_started(started)
            worker.cancel()
            await asyncio.sleep(.01)
            self.assertFalse(worker.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError): await worker
            self.assertTrue(finished.is_set())
        finally:
            release.set();worker.cancel();await asyncio.gather(worker,return_exceptions=True)

    async def test_old_queued_windows_are_skipped(self):
        class Processor:
            calls=0
            def process(self,eeg): self.calls+=1
        p=Processor();q=asyncio.Queue(1);q.put_nowait((1,time.monotonic()-10,{}))
        state=muse.WorkerState();worker=asyncio.create_task(muse.process_windows(q,p,state))
        try:
            await asyncio.sleep(.01)
            self.assertEqual(p.calls,0)
            self.assertEqual(state.skipped_old,1)
        finally:
            worker.cancel();await asyncio.gather(worker,return_exceptions=True)

    async def test_stall_reconnects_with_fresh_buffers_and_closes_clients(self):
        clients=[];sessions=[];second=asyncio.Event()
        class Client:
            is_connected=True
            def __init__(self,*args,**kwargs): self.subscribed=[];self.closed=False;clients.append(self)
            async def connect(self): pass
            async def start_notify(self,uuid,callback): self.subscribed.append(uuid)
            async def write_gatt_char(self,*args,**kwargs): pass
            async def disconnect(self): self.closed=True
        class Processor:
            def process(self,eeg): pass
        async def watch(session,client,worker,state):
            sessions.append(session)
            if len(sessions)==1:
                session.buffer.add_packet(UUIDS[0],1,[1]*12)
                raise muse.StreamStalled('injected packet loss')
            second.set()
            await asyncio.sleep(10)
        with patch.object(muse,'watch_stream',watch), contextlib.redirect_stdout(io.StringIO()):
            task=asyncio.create_task(muse.run_acquisition(Processor(),client_factory=Client,retry_s=0))
            try:
                await asyncio.wait_for(second.wait(),1)
                self.assertEqual(len(clients),2)
                self.assertEqual(len(clients[1].subscribed),4)
                self.assertFalse(sessions[1].buffer.frames)
                self.assertFalse(sessions[1].rolling.is_full())
                self.assertEqual(sessions[1].generation,2)
            finally:
                task.cancel();await asyncio.gather(task,return_exceptions=True)
            self.assertTrue(all(c.closed for c in clients))


if __name__=='__main__': unittest.main()
