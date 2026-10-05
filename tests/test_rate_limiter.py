import collections
import time
import unittest
from typing import Callable, Dict, Tuple


class _rate_deque(collections.deque):
    pass


def make_rate_limiter(
    per_minute_limit: int, per_key: bool = False
) -> Callable[[str], Tuple[bool, float]]:
    _buckets: Dict[str, _rate_deque] = {}
    _global_bucket: _rate_deque = _rate_deque(maxlen=per_minute_limit * 10)

    def _check(key: str = "global") -> Tuple[bool, float]:
        now = time.time()
        cutoff = now - 60.0
        if per_key:
            bucket = _buckets.get(key)
            if bucket is None:
                bucket = _rate_deque(maxlen=per_minute_limit * 10)
                _buckets[key] = bucket
        else:
            bucket = _global_bucket
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= per_minute_limit:
            retry_after = 60.0 - (now - bucket[0]) if bucket else 1.0
            return False, max(0.1, retry_after)
        bucket.append(now)
        return True, 0.0

    return _check


class TestRateLimiter(unittest.TestCase):

    def test_rate_limit_5_per_min(self):
        limiter = make_rate_limiter(5)
        for i in range(5):
            ok, retry = limiter("x")
            self.assertTrue(ok, f"iteration {i} should pass")
        ok, retry = limiter("x")
        self.assertFalse(ok, "6th should fail 429")
        time.sleep(0.05)
        ok, retry2 = limiter("x")
        self.assertFalse(ok, "still should fail after tiny sleep")


if __name__ == "__main__":
    unittest.main()
