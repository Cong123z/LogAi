"""Simple exponential backoff retry helper (plan section 7).

Deliberately dependency-free (no tenacity) to keep the runtime footprint
small; behaviour: retry up to `max_retries` times with backoff
min(base * 2**attempt, max_backoff), then re-raise the last exception so the
caller can route the item to the DLQ.
"""
from __future__ import annotations

import logging
import random
import time
from functools import wraps
from typing import Callable, Tuple, Type

logger = logging.getLogger("logai.reliability.retry")


def retry_with_backoff(
    max_retries: int = 5,
    base_seconds: float = 1.0,
    max_seconds: float = 60.0,
    exceptions: Tuple[Type[BaseException], ...] = (Exception,),
):
    def decorator(fn: Callable):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            attempt = 0
            while True:
                try:
                    return fn(*args, **kwargs)
                except exceptions as exc:  # noqa: BLE001
                    attempt += 1
                    if attempt > max_retries:
                        logger.error(
                            "%s failed after %d attempts: %s", fn.__name__,
                            attempt - 1, exc,
                        )
                        raise
                    delay = min(base_seconds * (2 ** (attempt - 1)), max_seconds)
                    delay += random.uniform(0, delay * 0.1)  # jitter
                    logger.warning(
                        "%s attempt %d/%d failed (%s), retrying in %.2fs",
                        fn.__name__, attempt, max_retries, exc, delay,
                    )
                    time.sleep(delay)
        return wrapper
    return decorator
