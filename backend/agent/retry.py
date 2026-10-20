"""일시 오류 재시도 (지수 백오프). 영구 오류(ValueError 등)는 재시도하지 않는다."""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")
TRANSIENT = (asyncio.TimeoutError, ConnectionError, OSError)


async def with_retry(fn: Callable[[], Awaitable[T]], attempts: int = 3, base_delay: float = 0.3,
                     retry_on: tuple = TRANSIENT) -> T:
    last: BaseException | None = None
    for i in range(attempts):
        try:
            return await fn()
        except retry_on as e:  # noqa: PERF203
            last = e
            if i < attempts - 1:
                await asyncio.sleep(base_delay * (2 ** i))
    assert last is not None
    raise last
