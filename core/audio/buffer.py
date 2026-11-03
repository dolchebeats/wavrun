"""A bounded PCM ring with a non-blocking audio callback reader."""
import threading
import numpy as np


class PCMBuffer:
    def __init__(self, capacity, channels):
        if capacity <= 0 or channels not in (1, 2):
            raise ValueError("Invalid PCM buffer dimensions")
        self.data = np.empty((capacity, channels), dtype=np.float32)
        self.capacity = capacity
        self.channels = channels
        self._condition = threading.Condition()
        self._read = self._write = self._count = 0
        self._eof = False
        self._error = None
        self._cancelled = False

    @property
    def available(self):
        with self._condition:
            return self._count

    def write(self, samples):
        if samples.ndim != 2 or samples.shape[1] != self.channels:
            raise ValueError("PCM channel layout does not match output")
        offset = 0
        with self._condition:
            while offset < len(samples):
                while self._count == self.capacity and not self._cancelled:
                    self._condition.wait(timeout=0.1)
                if self._cancelled:
                    return False
                count = min(len(samples) - offset, self.capacity - self._count,
                            self.capacity - self._write)
                self.data[self._write:self._write + count] = samples[offset:offset + count]
                self._write = (self._write + count) % self.capacity
                self._count += count
                offset += count
                self._condition.notify_all()
        return True

    def read_into(self, output, gain=1.0):
        output.fill(0)
        # Never wait for decoder/producer work on PortAudio's callback thread.
        if not self._condition.acquire(blocking=False):
            return 0, False, None
        try:
            count = min(len(output), self._count)
            first = min(count, self.capacity - self._read)
            np.multiply(self.data[self._read:self._read + first], gain, out=output[:first])
            if first < count:
                np.multiply(self.data[:count - first], gain, out=output[first:count])
            self._read = (self._read + count) % self.capacity
            self._count -= count
            self._condition.notify_all()
            return count, self._eof and self._count == 0, self._error
        finally:
            self._condition.release()

    def finish(self, error=None):
        with self._condition:
            self._error = error
            self._eof = True
            self._condition.notify_all()

    def cancel(self):
        with self._condition:
            self._cancelled = True
            self._condition.notify_all()
