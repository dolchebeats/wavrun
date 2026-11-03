"""A bounded history mapping queued PCM frames onto the output DAC clock."""
import math
import time


class OutputClock:
    def __init__(self, rate, *, monotonic=time.monotonic):
        self.rate = rate
        self._now = monotonic
        self.submitted = 0
        self._anchors = [None] * 512
        self._written = 0
        self._base = self._last = 0
        self._frozen = False

    def record(self, frames, timing=None, latency=0):
        delay = latency
        if timing is not None:
            delay = timing.outputBufferDacTime - timing.currentTime
        if not math.isfinite(delay):
            delay = 0
        sequence = self._written
        self._anchors[sequence % len(self._anchors)] = (
            sequence, self._now() + max(0, delay), self.submitted, frames)
        self.submitted += frames
        self._written = sequence + 1

    def position(self):
        if self._frozen:
            return self._last
        now = self._now()
        written = self._written
        position = self._base
        for sequence in range(max(0, written - len(self._anchors)), written):
            anchor = self._anchors[sequence % len(self._anchors)]
            if anchor is None or anchor[0] != sequence:
                continue
            _, dac_time, first, count = anchor
            if dac_time <= now:
                position = first + min(count, max(0, int((now - dac_time) * self.rate)))
        self._last = max(self._last, min(position, self.submitted))
        return self._last

    def freeze(self, frames=None):
        self._last = self.position() if frames is None else frames
        self._frozen = True

    def resume(self):
        # stop() drained the hardware queue; future callbacks start at this frame.
        self._base = self._last = self.submitted
        self._written = 0
        self._frozen = False
