"""Bounded single-process limiter. One polling process per Telegram bot."""
import time
from collections import defaultdict, deque

_calls = defaultdict(deque)


def allow(user_id, operation, limit=6, seconds=60):
    key = (user_id, operation)
    now = time.monotonic()
    queue = _calls[key]
    while queue and queue[0] <= now - seconds:
        queue.popleft()
    if len(queue) >= limit:
        return False
    queue.append(now)
    return True
