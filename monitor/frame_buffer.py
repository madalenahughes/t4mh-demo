"""Assemble matching four-channel frames, with bounded incomplete state."""
import time


class MuseFrameBuffer:
    CHANNEL_NAMES = {
        "273e0003-4c4d-454d-96be-f03bac821358": "TP9",
        "273e0004-4c4d-454d-96be-f03bac821358": "AF7",
        "273e0005-4c4d-454d-96be-f03bac821358": "AF8",
        "273e0006-4c4d-454d-96be-f03bac821358": "TP10",
    }

    def __init__(self, max_pending=128, max_age_s=2.0, clock=time.monotonic):
        self.frames = {}
        self.first_seen = {}
        self.max_pending = max_pending
        self.max_age_s = max_age_s
        self.clock = clock
        self.expired_count = 0
        self.overflow_count = 0

    def add_packet(self, uuid, packet_number, samples):
        channel = self.CHANNEL_NAMES[uuid]
        now = self.clock()
        for number, first in list(self.first_seen.items()):
            if now - first > self.max_age_s:
                self.frames.pop(number, None)
                self.first_seen.pop(number, None)
                self.expired_count += 1
        if packet_number not in self.frames:
            if len(self.frames) >= self.max_pending:
                oldest = next(iter(self.frames))
                self.frames.pop(oldest)
                self.first_seen.pop(oldest)
                self.overflow_count += 1
            self.frames[packet_number] = {}
            self.first_seen[packet_number] = now
        self.frames[packet_number][channel] = samples
        if len(self.frames[packet_number]) == 4:
            self.first_seen.pop(packet_number)
            return packet_number, self.frames.pop(packet_number)
        return None
