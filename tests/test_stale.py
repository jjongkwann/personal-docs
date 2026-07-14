"""pkb stale 판정 순수 함수(count_stale) 단위 테스트. ES 불필요."""

from __future__ import annotations

import os

from pkb.cli import count_stale


def test_count_stale_counts_files_newer_than_marker(tmp_path):
    old = tmp_path / "old.md"
    old.write_text("x", encoding="utf-8")
    new = tmp_path / "new.md"
    new.write_text("y", encoding="utf-8")
    os.utime(old, (1000, 1000))
    os.utime(new, (3000, 3000))

    assert count_stale([old, new], last_sync_epoch=2000) == 1
    assert count_stale([old, new], last_sync_epoch=500) == 2
    assert count_stale([old, new], last_sync_epoch=5000) == 0
    assert count_stale([], last_sync_epoch=0) == 0
