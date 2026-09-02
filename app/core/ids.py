"""UUIDv7 generation (RFC 9562).

Python 3.12 has no `uuid.uuid7`, and we deliberately avoid a third-party
dependency for something this small and this central.

Layout (128 bits):

    | 48 bits unix_ts_ms | 4 bits version=7 | 12 bits counter |
    | 2 bits variant=0b10 | 62 bits random |

The 12-bit counter guarantees strictly increasing values inside the same
millisecond within a process, which keeps index inserts local even under
load. See `docs/architecture.md` for why UUIDv7 is the identifier strategy.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

_lock = threading.Lock()
_last_ms: int = -1
_counter: int = 0

_MAX_COUNTER = 0xFFF


def uuid7() -> uuid.UUID:
    """Return a time-ordered UUIDv7."""
    global _last_ms, _counter

    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms == _last_ms:
            _counter += 1
            if _counter > _MAX_COUNTER:
                # Counter exhausted inside this millisecond: borrow the next one
                # rather than emit a non-monotonic value.
                ms = _last_ms + 1
                _last_ms = ms
                _counter = 0
        else:
            if ms < _last_ms:  # clock moved backwards; never regress
                ms = _last_ms
            _last_ms = ms
            _counter = int.from_bytes(os.urandom(2), "big") & 0x0FF

        counter = _counter

    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)

    value = (
        (ms & 0xFFFF_FFFF_FFFF) << 80
        | 0x7 << 76
        | (counter & _MAX_COUNTER) << 64
        | 0b10 << 62
        | rand_b
    )
    return uuid.UUID(int=value)


def uuid7_timestamp_ms(value: uuid.UUID) -> int:
    """Extract the embedded millisecond timestamp from a UUIDv7."""
    if value.version != 7:
        raise ValueError(f"not a UUIDv7: {value}")
    return value.int >> 80
