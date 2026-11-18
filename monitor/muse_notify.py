"""Muse acquisition with bounded queues, stall diagnostics and reconnection."""
import asyncio
from datetime import datetime
import logging
import os
from pathlib import Path
import time

# All plots are saved to files; the processing thread must not open a GUI.
os.environ["MPLBACKEND"] = "Agg"

from bleak import BleakClient
from decoder import decode_eeg_packet
from frame_buffer import MuseFrameBuffer
from rolling_buffer import RollingBuffer
from processor import EEGProcessor

ADDRESS = "00:55:DA:B8:2D:64"
CONTROL = "273e0001-4c4d-454d-96be-f03bac821358"
EEG_UUIDS = list(MuseFrameBuffer.CHANNEL_NAMES)
STALE_SECONDS = 8.0
RETRY_SECONDS = 3.0
logger = logging.getLogger("t4mh.muse.stream")


class StreamStalled(RuntimeError):
    pass


class ProcessingFailure(RuntimeError):
    pass


class WorkerState:
    def __init__(self):
        self.busy_since = None
        self.last_finished = None
        self.skipped_old = 0


class MuseSession:
    def __init__(self, queue, generation, clock=time.monotonic):
        self.queue = queue
        self.generation = generation
        self.clock = clock
        self.started = clock()
        self.last_raw = None
        self.last_frame = None
        self.last_channel = {}
        self.raw_packets = 0
        self.channel_packets = {name: 0 for name in MuseFrameBuffer.CHANNEL_NAMES.values()}
        self.complete_frames = 0
        self.ready_frames = 0
        self.queued_windows = 0
        self.dropped_windows = 0
        self.decode_errors = 0
        self.disconnected = False
        self.buffer = MuseFrameBuffer(clock=clock)
        self.rolling = RollingBuffer()

    def on_disconnect(self, _client):
        self.disconnected = True

    def callback(self, sender, data):
        self.raw_packets += 1
        self.last_raw = self.clock()
        try:
            packet, samples = decode_eeg_packet(data)
            channel = MuseFrameBuffer.CHANNEL_NAMES[sender.uuid]
            self.last_channel[channel] = self.clock()
            self.channel_packets[channel] += 1
            frame = self.buffer.add_packet(sender.uuid, packet, samples)
            if frame is None:
                return
            self.complete_frames += 1
            self.last_frame = self.clock()
            _, eeg = frame
            self.rolling.add_frame(eeg)
            if self.rolling.is_full():
                self.ready_frames += 1
                if self.ready_frames % 5 == 0:
                    # Independent arrays let one worker process while BLE keeps receiving.
                    snapshot = {name: values.copy() for name, values in self.rolling.get_data().items()}
                    if self.queue.full():
                        self.queue.get_nowait()
                        self.dropped_windows += 1
                    self.queue.put_nowait((self.generation, self.clock(), snapshot))
                    self.queued_windows += 1
        except Exception:
            self.decode_errors += 1
            if self.decode_errors <= 3 or self.decode_errors % 100 == 0:
                logger.exception("Muse notification/frame error (%d)", self.decode_errors)

    def summary(self):
        return (f"raw={self.raw_packets}, channels={self.channel_packets}, "
                f"frames={self.complete_frames}, windows={self.queued_windows}, "
                f"queue_drops={self.dropped_windows}, decode_errors={self.decode_errors}, "
                f"expired_frames={self.buffer.expired_count}, pending={len(self.buffer.frames)}")

    def health_problem(self, connected, timeout=STALE_SECONDS):
        now = self.clock()
        if self.disconnected or not connected:
            return "Bluetooth connection lost"
        if now - (self.last_raw if self.last_raw is not None else self.started) > timeout:
            return "No EEG notifications arriving"
        if now - self.started > timeout:
            missing = [ch for ch in self.channel_packets
                       if now - self.last_channel.get(ch, self.started) > timeout]
            if missing:
                return "EEG channel notifications missing: " + ", ".join(missing)
            if self.last_frame is None or now - self.last_frame > timeout:
                return "Notifications arriving but no matching four-channel EEG frames"
        return None


def discard_queue(queue):
    while not queue.empty():
        queue.get_nowait()


async def process_windows(queue, processor, state):
    previous_generation = None
    while True:
        generation, collected, eeg = await queue.get()
        if time.monotonic() - collected > 3.0:
            state.skipped_old += 1
            continue
        if generation != previous_generation:
            # Do not blend measurements across an outage into old smoothing state.
            processor.faa_smoothed = None
            processor.fba_smoothed = None
            previous_generation = generation
        state.busy_since = time.monotonic()
        future = asyncio.get_running_loop().run_in_executor(None, processor.process, eeg)
        try:
            # Cancellation must wait for an active write before closing the CSV.
            await asyncio.shield(future)
        except asyncio.CancelledError:
            await future
            raise
        except Exception as exc:
            logger.exception("EEG processing failed; acquisition will stop")
            raise ProcessingFailure(str(exc)) from exc
        finally:
            state.busy_since = None
        state.last_finished = time.monotonic()


async def watch_stream(session, client, worker, state, interval=1.0):
    last_report = time.monotonic()
    while True:
        await asyncio.sleep(interval)
        if worker.done():
            worker.result()  # Preserve the processing exception instead of silently stalling.
            raise ProcessingFailure("EEG processing worker ended unexpectedly")
        problem = session.health_problem(client.is_connected)
        if problem:
            raise StreamStalled(problem + "; " + session.summary())
        now = time.monotonic()
        if now - last_report >= 15:
            logger.info("Muse stream: %s; last_processed=%s", session.summary(), state.last_finished)
            if state.busy_since is not None and now - state.busy_since > 5:
                logger.warning("EEG processing is taking %.1fs; packets still being checked. "
                               "Check CPU, disk and terminal output. No extra workers started.",
                               now - state.busy_since)
            last_report = now


async def run_acquisition(processor, client_factory=BleakClient, retry_s=RETRY_SECONDS):
    queue = asyncio.Queue(maxsize=1)
    state = WorkerState()
    worker = asyncio.create_task(process_windows(queue, processor, state))
    generation = 0
    try:
        while True:
            generation += 1
            discard_queue(queue)
            session = MuseSession(queue, generation)
            client = client_factory(ADDRESS, timeout=30.0, disconnected_callback=session.on_disconnect)
            try:
                print(f"Connecting to Muse {ADDRESS} (attempt {generation})...", flush=True)
                await asyncio.wait_for(client.connect(), timeout=35)
                for uuid in EEG_UUIDS:
                    await asyncio.wait_for(client.start_notify(uuid, session.callback), timeout=10)
                await asyncio.wait_for(client.write_gatt_char(
                    CONTROL, bytearray([2, ord("d"), ord("\n")]), response=False), timeout=10)
                session.started = time.monotonic()
                logger.info("Connected and subscribed to all four channels; attempt=%d", generation)
                print("Muse connected. Waiting for fresh EEG windows...", flush=True)
                await watch_stream(session, client, worker, state)
            except ProcessingFailure:
                raise
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Muse stream interrupted; reconnecting after %.1fs", retry_s)
            finally:
                # These snapshots must not cross into the next connection.
                discard_queue(queue)
                try:
                    await asyncio.wait_for(client.disconnect(), timeout=5)
                except Exception:
                    logger.exception("Muse disconnect cleanup failed")
            await asyncio.sleep(retry_s)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


def configure_logging():
    directory = Path(os.environ.get("T4MH_DATA_DIR", str(Path(__file__).resolve().parents[1] / "data")))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("muse_stream_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".log")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    file_handler = logging.FileHandler(path)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(file_handler)
    logger.addHandler(console)
    print("Muse connection/packet diagnostics:", path, flush=True)
    return path


async def main():
    configure_logging()
    processor = EEGProcessor()
    try:
        await run_acquisition(processor)
    finally:
        # run_acquisition waits for the single active processing worker first.
        processor.integration_sock.close()
        processor.log_file.flush()
        processor.log_file.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nMuse acquisition stopped.")
