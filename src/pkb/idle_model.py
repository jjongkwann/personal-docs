"""쓸 때만 올리고, 쓰지 않으면 내리는 모델 홀더."""

import gc
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Generic, TypeVar

from pkb.config import settings

T = TypeVar("T")


class IdleModel(Generic[T]):
    """load()를 첫 use()까지 미루고, 마지막 사용 뒤 MODEL_IDLE_SECONDS가 지나면 모델을 내린다.

    한 락이 적재·사용·해제를 직렬화한다 — 사용 중에는 내려가지 않고, 이전 모델의 메모리를
    돌려준 뒤에야 다음 적재가 시작된다(8GB 머신에서 두 벌이 겹치지 않게).
    """

    def __init__(self, load: Callable[[], T]) -> None:
        self._load = load
        self._model: T | None = None
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._last_used = 0.0

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @contextmanager
    def use(self) -> Iterator[T]:
        with self._lock:
            if self._model is None:
                self._model = self._load()
            try:
                yield self._model
            finally:
                self._last_used = time.monotonic()
                if self._timer is None:
                    self._arm(settings.model_idle_seconds)

    def _arm(self, delay: float) -> None:
        if settings.model_idle_seconds <= 0:
            return
        self._timer = threading.Timer(delay, self._expire)
        self._timer.daemon = True  # 유휴 타이머가 CLI 프로세스 종료를 붙잡지 않게
        self._timer.start()

    def _expire(self) -> None:
        with self._lock:
            self._timer = None
            remaining = self._last_used + settings.model_idle_seconds - time.monotonic()
            if remaining > 0:  # 기다리는 동안 다시 쓰였다 — 남은 시간만큼 더 기다린다
                self._arm(remaining)
                return
            self._model = None
            gc.collect()
            import torch

            if torch.backends.mps.is_available():
                torch.mps.empty_cache()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
