"""pkb watch의 snapshot/snapshot_diff 순수 함수 단위 테스트 — 추가/수정/삭제/임계 초과 판정."""

from __future__ import annotations

from pathlib import Path

from pkb.cli import snapshot, snapshot_diff
from pkb.store import PRUNE_CONFIRM_THRESHOLD


def test_snapshot_diff_detects_add_modify_delete(tmp_path):
    study = tmp_path / "study"
    study.mkdir()
    a = study / "a.md"
    a.write_text("v1", encoding="utf-8")
    b = study / "b.md"
    b.write_text("b", encoding="utf-8")

    old = snapshot(tmp_path)
    assert set(old) == {a, b}

    a.write_text("v2-longer", encoding="utf-8")  # 수정 (mtime 해상도 무관하게 size 변화)
    c = study / "c.md"
    c.write_text("c", encoding="utf-8")  # 추가
    b.unlink()  # 삭제

    changed, deleted = snapshot_diff(old, snapshot(tmp_path))
    assert set(changed) == {a, c}
    assert deleted == [b]


def test_snapshot_diff_no_change(tmp_path):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    snap = snapshot(tmp_path)
    assert snapshot_diff(snap, snap) == ([], [])


def test_mass_delete_trips_prune_threshold():
    # 삭제 수가 임계 초과면 watch는 자동 삭제하지 않고 pkb sync를 유도한다
    old = {
        Path(f"/vault/{i}.md"): (1.0, 1)
        for i in range(PRUNE_CONFIRM_THRESHOLD + 1)
    }
    changed, deleted = snapshot_diff(old, {})
    assert changed == []
    assert len(deleted) > PRUNE_CONFIRM_THRESHOLD
