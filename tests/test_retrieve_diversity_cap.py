"""_cap_per_doc(R4b) 문서당 다양성 캡 단위 테스트.

실제 ES/리랭커 호출 없이 fake 후보 dict 리스트로 캡 동작만 검증.
"""

from __future__ import annotations

from pkb.retrieve import _cap_per_doc


def _cand(doc_id: str, idx: int) -> dict:
    return {"doc_id": doc_id, "chunk_index": idx}


def test_cap_limits_same_doc_and_fills_from_other_doc():
    # A 3개 + B 1개, top_k=3 → cap(2)에 걸린 A 세 번째는 건너뛰고 B로 채움
    candidates = [_cand("A", 0), _cand("A", 1), _cand("A", 2), _cand("B", 0)]
    result = _cap_per_doc(candidates, top_k=3)
    assert [c["doc_id"] for c in result] == ["A", "A", "B"]


def test_cap_falls_back_to_skipped_when_short():
    # 후보가 전부 같은 문서면 cap만으로는 top_k 미달 → 건너뛴 것들로 순서대로 채움
    candidates = [_cand("A", 0), _cand("A", 1), _cand("A", 2)]
    result = _cap_per_doc(candidates, top_k=3)
    assert [c["chunk_index"] for c in result] == [0, 1, 2]
