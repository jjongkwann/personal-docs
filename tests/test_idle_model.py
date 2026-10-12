"""실제 모델을 올리지 않고 IdleModel의 지연 적재·유휴 해제·재적재를 검증."""

from __future__ import annotations

import time
from types import SimpleNamespace

from pkb import idle_model
from pkb.config import settings
from pkb.idle_model import IdleModel


class _Loader:
    def __init__(self):
        self.count = 0

    def __call__(self):
        self.count += 1
        return object()


def test_loads_on_first_use_and_reuses(monkeypatch):
    monkeypatch.setattr(settings, "model_idle_seconds", 0)
    load = _Loader()
    holder = IdleModel(load)
    assert load.count == 0

    with holder.use() as first:
        pass
    with holder.use() as second:
        pass

    assert first is second
    assert load.count == 1
    assert holder._timer is None  # 0이면 유휴 타이머를 걸지 않는다


def test_expire_unloads_only_after_idle_period(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(idle_model, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(settings, "model_idle_seconds", 60)
    monkeypatch.setattr(IdleModel, "_arm", lambda self, delay: None)  # 타이머 스레드 없이 _expire를 직접 호출
    load = _Loader()
    holder = IdleModel(load)
    with holder.use():
        pass

    now[0] += 59
    holder._expire()
    with holder.use():
        pass
    assert load.count == 1  # 유휴 시간 전 — 유지

    now[0] += 60
    holder._expire()
    assert holder._model is None
    with holder.use():
        pass
    assert load.count == 2  # 내려갔다가 다음 사용 때 다시 적재


def test_idle_timer_unloads_and_next_use_reloads(monkeypatch):
    monkeypatch.setattr(settings, "model_idle_seconds", 0.05)
    load = _Loader()
    holder = IdleModel(load)
    with holder.use():
        pass

    deadline = time.monotonic() + 30
    while holder._model is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert holder._model is None

    with holder.use():
        pass
    assert load.count == 2
