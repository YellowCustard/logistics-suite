"""UUIDv7 (RFC 9562) generation.

Generated in the application rather than the database so offline PWA devices and the server
share one scheme, and because the stock PostgreSQL in some environments predates uuidv7().
"""

import os
import threading
import time
import uuid

_lock = threading.Lock()
_last_ms = 0
_last_rand_a = 0


def uuid7() -> uuid.UUID:
    """Return a UUIDv7 that is strictly increasing within this process.

    Layout: 48-bit Unix ms timestamp | ver(4)=7 | rand_a(12) | var(2)=0b10 | rand_b(62).
    Within one millisecond rand_a is incremented (RFC 9562 method 1) so ids sort in creation order.
    """
    global _last_ms, _last_rand_a
    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms > _last_ms:
            rand_a = int.from_bytes(os.urandom(2), "big") & 0x0FFF
        else:
            ms = _last_ms
            rand_a = _last_rand_a + 1
            if rand_a > 0x0FFF:  # counter overflow: borrow the next millisecond
                ms += 1
                rand_a = int.from_bytes(os.urandom(2), "big") & 0x0FFF
        _last_ms, _last_rand_a = ms, rand_a
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | rand_a << 64 | 0b10 << 62 | rand_b
    return uuid.UUID(int=value)


def uuid7_timestamp_ms(value: uuid.UUID) -> int:
    return value.int >> 80
