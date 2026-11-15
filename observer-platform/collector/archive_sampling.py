"""Clock-slot sampling for the recording tap; sensor and preview streams are untouched."""
import math


class FrameSampler:
    def __init__(self, fps=0):
        if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or not 0 <= fps <= 120:
            raise ValueError('recording fps must be finite and between 0 and 120')
        self.fps = fps
        self.interval_ns = round(1_000_000_000/fps) if fps else 0
        self.deadlines = {}
        self.previous = {}

    def clear(self):
        self.deadlines.clear()
        self.previous.clear()

    def accept(self, key, timestamp_ns):
        if not self.interval_ns:
            return True
        if type(timestamp_ns) is not int or timestamp_ns <= 0:
            return False
        before = self.previous.get(key)
        self.previous[key] = timestamp_ns
        deadline = self.deadlines.get(key)
        if deadline is None or (before is not None and timestamp_ns < before):
            deadline = timestamp_ns
        if timestamp_ns < deadline:
            return False
        slots = max(1, (timestamp_ns-deadline)//self.interval_ns+1)
        self.deadlines[key] = deadline+slots*self.interval_ns
        return True
