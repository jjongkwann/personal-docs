"""Evaluate consumer answers against frozen, actually delivered search evidence.

Quote checks are mechanical. Entailment and correctness require a separate review
bound to the exact answer/context hashes; retrieval success is never answer accuracy.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Citation(_Record):
    doc_id: str = Field(min_length=1)
    chunk_index: StrictInt = Field(ge=0)
    quote: str = Field(min_length=1)


class Claim(_Record):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    citations: list[Citation]


class Answer(_Record):
    status: str
    claims: list[Claim]
    reason: str = ""


class ClaimReview(_Record):
    id: str
    correct: StrictBool
    covers: list[str]
    fully_supported: StrictBool
    supported_citations: list[StrictInt]


class Review(_Record):
    reviewer: str = Field(min_length=1)
    answer_sha256: str
    context_sha256: str
    claims: list[ClaimReview]
    context_sufficient: StrictBool | None = None
    note: str = ""


def validate_gold_answer(row: dict) -> None:
    """Additive v2 labels: old retrieval-only rows remain retrieval-only."""
    if "expected_claims" not in row:
        return
    claims = row["expected_claims"]
    if not isinstance(claims, list) or bool(claims) != row["answerable"]:
        raise ValueError("expected_claims must be nonempty iff answerable")
    ids = set()
    for claim in claims:
        if not isinstance(claim, dict) or not isinstance(claim.get("id"), str) or not claim["id"].strip():
            raise ValueError("expected claim needs an id")
        if claim["id"] in ids:
            raise ValueError("duplicate expected claim id")
        ids.add(claim["id"])
        if not isinstance(claim.get("text"), str) or not claim["text"].strip():
            raise ValueError("expected claim needs text")
        evidence = claim.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("expected claim needs exact evidence")
        for citation in evidence:
            Citation.model_validate(citation)
    policy = row.get("answer_policy", "answer" if row["answerable"] else "abstain")
    if policy not in ({"answer", "conflict"} if row["answerable"] else {"abstain"}):
        raise ValueError("invalid answer_policy")


def citation_present(citation: dict, evidence: list[dict]) -> bool:
    return bool(citation.get("quote", "").strip()) and any(
        citation["doc_id"] == source.get("doc_id")
        and citation["chunk_index"] == source.get("chunk_index")
        and citation["quote"] in source.get("content", "")
        for source in evidence
    )


def evaluate_answer(row: dict, context: dict, answer_data: dict, review_data: dict) -> dict:
    """Fail closed on incomplete/stale reviews; count every submitted citation."""
    validate_gold_answer(row)
    if "expected_claims" not in row:
        raise ValueError("answer evaluation requires expected_claims labels")
    answer = Answer.model_validate(answer_data)
    review = Review.model_validate(review_data)
    if answer.status not in {"answer", "abstain", "conflict"}:
        raise ValueError("invalid answer status")
    if answer.status == "abstain" and (answer.claims or not answer.reason.strip()):
        raise ValueError("abstention needs a reason and no claims")
    if answer.status != "abstain" and not answer.claims:
        raise ValueError("non-abstention needs claims")
    ids = [claim.id for claim in answer.claims]
    review_ids = [claim.id for claim in review.claims]
    if len(ids) != len(set(ids)) or len(review_ids) != len(set(review_ids)) or set(ids) != set(review_ids):
        raise ValueError("review must cover each unique answer claim exactly once")
    if review.answer_sha256 != digest(answer_data) or review.context_sha256 != digest(context):
        raise ValueError("review hashes do not match the answer/context")
    expected = {claim["id"] for claim in row["expected_claims"]}
    verdicts = {claim.id: claim for claim in review.claims}
    covered = set()
    correct_claims = supported_claims = valid_citations = total_citations = quote_matches = 0
    details = []
    for claim in answer.claims:
        verdict = verdicts[claim.id]
        if not set(verdict.covers) <= expected:
            raise ValueError("review covers unknown expected claim")
        positions = verdict.supported_citations
        if len(positions) != len(set(positions)) or any(i < 0 or i >= len(claim.citations) for i in positions):
            raise ValueError("invalid supported citation index")
        citations = [citation.model_dump() for citation in claim.citations]
        if len({digest(citation) for citation in citations}) != len(citations):
            raise ValueError("duplicate citation within a claim")
        present = [citation_present(citation, context["evidence"]) for citation in citations]
        valid = [exists and i in positions for i, exists in enumerate(present)]
        supported = verdict.fully_supported and bool(positions) and all(present[i] for i in positions)
        correct_claims += verdict.correct
        supported_claims += supported
        if verdict.correct:
            covered.update(verdict.covers)
        quote_matches += sum(present)
        valid_citations += sum(valid)
        total_citations += len(citations)
        details.append({"id": claim.id, "correct": verdict.correct, "fully_supported": supported,
                        "quote_present": present, "citation_supported": valid, "covers": verdict.covers})
    gold_evidence_complete = row["answerable"] and all(
        citation_present(citation, context["evidence"])
        for claim in row["expected_claims"] for citation in claim["evidence"]
    )
    sufficient = review.context_sufficient
    if sufficient and (not row["answerable"] or not context["evidence"]):
        raise ValueError("sufficient context conflicts with no-answer gold or empty evidence")
    policy = row.get("answer_policy", "answer" if row["answerable"] else "abstain")
    accuracy = answer.status == policy and len(covered) == len(expected) and correct_claims == len(ids)
    return {
        "answer_accuracy": float(accuracy),
        "claim_precision": correct_claims / len(ids) if ids else None,
        "claim_recall": len(covered) / len(expected) if expected else None,
        "citation_precision": valid_citations / total_citations if total_citations else None,
        "citation_completeness": supported_claims / len(ids) if ids else None,
        "unsupported_claim_rate": (len(ids) - supported_claims) / len(ids) if ids else None,
        "abstention_when_insufficient": answer.status == "abstain" if sufficient is False else None,
        "unnecessary_abstention": answer.status == "abstain" if sufficient is True else None,
        "noanswer_abstention": answer.status == "abstain" if not row["answerable"] else None,
        "sufficient_evidence": sufficient, "gold_evidence_complete": bool(gold_evidence_complete),
        "status": answer.status,
        "counts": {"claims": len(ids), "correct_claims": correct_claims, "supported_claims": supported_claims,
                   "citations": total_citations, "valid_citations": valid_citations,
                   "quote_matches": quote_matches}, "claims": details,
        "reviewer": review.reviewer,
    }


def answer_summary(rows: list[dict]) -> dict:
    metrics = [row["answer_metrics"] for row in rows if row.get("answer_metrics") is not None]
    counts = {key: sum(m["counts"][key] for m in metrics) for key in (
        "claims", "correct_claims", "supported_claims", "citations", "valid_citations", "quote_matches",
    )}
    result = {"reviewed_answers": len(metrics), "unreviewed_answers": len(rows) - len(metrics), "counts": counts}
    for key in ("answer_accuracy", "claim_recall", "abstention_when_insufficient", "unnecessary_abstention",
                "noanswer_abstention"):
        values = [m[key] for m in metrics if m[key] is not None]
        result[key] = sum(values) / len(values) if values else None
    for key, numerator, denominator in (
        ("claim_precision", "correct_claims", "claims"),
        ("citation_precision", "valid_citations", "citations"),
        ("citation_completeness", "supported_claims", "claims"),
    ):
        result[key] = counts[numerator] / counts[denominator] if counts[denominator] else None
    result["unsupported_claim_rate"] = (
        1 - result["citation_completeness"] if counts["claims"] else None
    )
    return result


def score_answers(report: dict, gold: list[dict], submissions: list[dict]) -> dict:
    """Attach reviewed consumer answers to a frozen retrieval report, never rerun retrieval."""
    if report["metadata"]["queryset_sha256"] != digest(gold):
        raise ValueError("gold does not match the frozen report")
    report = deepcopy(report)
    seen = set()
    for entry in submissions:
        key = entry["mode"], entry["query_id"]
        if key in seen:
            raise ValueError("duplicate answer submission")
        seen.add(key)
        if key[0] not in report["modes"]:
            raise ValueError("unknown answer mode")
        targets = [r for r in report["modes"][key[0]]["queries"] if r["query_id"] == key[1]]
        labels = [r for i, r in enumerate(gold) if r.get("id", str(i)) == key[1]]
        if len(targets) != 1 or len(labels) != 1:
            raise ValueError("unknown or ambiguous query_id")
        target = targets[0]
        if target.get("context_sha256") != digest(target["context"]):
            raise ValueError("frozen context hash mismatch")
        if entry["context_sha256"] != target["context_sha256"]:
            raise ValueError("answer was not generated from this context")
        target["answer_metrics"] = evaluate_answer(labels[0], target["context"], entry["answer"], entry["review"])
        target["answer"] = entry["answer"]
        target["answer_review"] = entry["review"]
        target["answer_generator"] = entry.get("generator")
    for mode in report["modes"].values():
        mode["answer_summary"] = answer_summary(mode["queries"])
    report["answer_evaluation"] = {
        "method": "independently reviewed claims plus exact delivered-quote checks",
        "note": "Citation precision includes entailment review; quote presence alone is not support.",
    }
    report["paired_comparisons"] = compare_modes(report)
    return report


def export_contexts(report: dict) -> list[dict]:
    """Blinded input for an answer consumer: no gold, scores, labels or reviews."""
    return [{"mode": name, "query_id": row["query_id"], "context_sha256": row["context_sha256"],
             "context": row["context"]}
            for name, mode in report["modes"].items() for row in mode["queries"]]


def compare_modes(report: dict) -> list[dict]:
    """Pair the same questions; expose evidence changes behind answer deltas."""
    names = list(report["modes"])
    baseline = {row["query_id"]: row for row in report["modes"][names[0]]["queries"]}
    comparisons = []
    for name in names[1:]:
        pairs = []
        for row in report["modes"][name]["queries"]:
            original = baseline[row["query_id"]]
            left, right = original.get("answer_metrics"), row.get("answer_metrics")
            if left is None or right is None:
                continue
            before = {(e["doc_id"], e["chunk_index"]) for e in original["context"]["evidence"]}
            after = {(e["doc_id"], e["chunk_index"]) for e in row["context"]["evidence"]}
            before_text = {(e["doc_id"], e["chunk_index"]): e["content"] for e in original["context"]["evidence"]}
            after_text = {(e["doc_id"], e["chunk_index"]): e["content"] for e in row["context"]["evidence"]}
            pairs.append({
                "query_id": row["query_id"],
                "recall_delta": (row["recall_at_k"] - original["recall_at_k"])
                if row["recall_at_k"] is not None else None,
                "ndcg_delta": (row["ndcg_at_k"] - original["ndcg_at_k"])
                if row["ndcg_at_k"] is not None else None,
                "answer_accuracy_delta": right["answer_accuracy"] - left["answer_accuracy"],
                "citation_precision_delta": right["citation_precision"] - left["citation_precision"]
                if right["citation_precision"] is not None and left["citation_precision"] is not None else None,
                "evidence_added": sorted(after - before), "evidence_removed": sorted(before - after),
                "visible_evidence_changed": sorted(k for k in before & after if before_text[k] != after_text[k]),
                "status_before": left["status"], "status_after": right["status"],
            })
        comparisons.append({"baseline": names[0], "mode": name, "paired_answers": len(pairs),
                            "answer_accuracy_delta": sum(p["answer_accuracy_delta"] for p in pairs) / len(pairs)
                            if pairs else None, "queries": pairs})
    return comparisons
