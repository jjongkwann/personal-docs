"""Consumer-answer evaluation using independent, synthetic source/review cases."""

from copy import deepcopy

import pytest

from pkb.answer_eval import answer_summary, digest, evaluate_answer, score_answers, validate_gold_answer

BADGE = {"doc_id": "entry.md", "chunk_index": 0, "quote": "Entry requires a badge."}
REGISTER = {"doc_id": "visitors.md", "chunk_index": 2, "quote": "Visitors must register in advance."}


def _case(*, multi=False, policy="answer"):
    evidence = [deepcopy(BADGE), deepcopy(REGISTER)] if multi else [deepcopy(BADGE)]
    gold = {"id": "access", "answerable": True, "answer_policy": policy, "expected_claims": [
        {"id": "access-rule", "text": "A badge and advance registration are required." if multi
         else "A badge is required.", "evidence": evidence},
    ]}
    context = {"evidence": [
        {"doc_id": "entry.md", "chunk_index": 0, "content": "Entry requires a badge. The lobby closes at six."},
        {"doc_id": "visitors.md", "chunk_index": 2, "content": "Visitors must register in advance."},
    ]}
    answer = {"status": policy, "claims": [{
        "id": "a1", "text": gold["expected_claims"][0]["text"], "citations": deepcopy(evidence),
    }]}
    review = {"reviewer": "independent-test-review", "context_sufficient": True, "answer_sha256": digest(answer),
              "context_sha256": digest(context), "claims": [{
                  "id": "a1", "correct": True, "covers": ["access-rule"], "fully_supported": True,
                  "supported_citations": list(range(len(evidence))),
              }]}
    return gold, context, answer, review


def _score(gold, context, answer, review):
    review = deepcopy(review)
    review.update(answer_sha256=digest(answer), context_sha256=digest(context))
    return evaluate_answer(gold, context, answer, review)


def test_correct_reviewed_answer_has_independent_perfect_metrics():
    metrics = evaluate_answer(*_case())
    assert metrics["answer_accuracy"] == 1
    assert metrics["citation_precision"] == 1
    assert metrics["citation_completeness"] == 1
    assert metrics["counts"]["quote_matches"] == 1


@pytest.mark.parametrize("citation", [
    BADGE | {"quote": "Entry never requires a badge."},
    BADGE | {"doc_id": "wrong.md"},
    BADGE | {"chunk_index": 9},
])
def test_wrong_quote_document_or_chunk_cannot_receive_citation_credit(citation):
    gold, context, answer, review = _case()
    answer["claims"][0]["citations"] = [citation]
    metrics = _score(gold, context, answer, review)
    assert metrics["citation_precision"] == 0
    assert metrics["citation_completeness"] == 0
    assert metrics["counts"]["quote_matches"] == 0


def test_quote_outside_visible_truncated_context_cannot_receive_credit():
    gold, context, answer, review = _case()
    context["evidence"][0]["content"] = "Entry requires a"
    review["context_sufficient"] = False
    metrics = _score(gold, context, answer, review)
    assert metrics["citation_precision"] == 0
    assert metrics["sufficient_evidence"] is False


def test_exact_quote_does_not_substitute_for_entailment_review():
    gold, context, answer, review = _case()
    answer["claims"][0]["text"] = "A badge guarantees access after midnight."
    review["claims"][0].update(correct=False, covers=[], fully_supported=False, supported_citations=[])
    metrics = _score(gold, context, answer, review)
    assert metrics["counts"]["quote_matches"] == 1
    assert metrics["citation_precision"] == 0
    assert metrics["answer_accuracy"] == 0
    with pytest.raises(ValueError):
        evaluate_answer(gold, context, answer, {})


def test_correct_claim_without_citations_keeps_correctness_separate_from_support():
    gold, context, answer, review = _case()
    answer["claims"][0]["citations"] = []
    review["claims"][0].update(fully_supported=False, supported_citations=[])
    metrics = _score(gold, context, answer, review)
    assert metrics["answer_accuracy"] == 1
    assert metrics["citation_precision"] is None
    assert metrics["citation_completeness"] == 0
    assert metrics["unsupported_claim_rate"] == 1


def test_extra_false_claim_reduces_answer_accuracy_and_claim_precision():
    gold, context, answer, review = _case()
    answer["claims"].append({"id": "false", "text": "Admission is free.", "citations": []})
    review["claims"].append({"id": "false", "correct": False, "covers": [],
                             "fully_supported": False, "supported_citations": []})
    metrics = _score(gold, context, answer, review)
    assert metrics["answer_accuracy"] == 0
    assert metrics["claim_precision"] == 0.5
    assert metrics["claim_recall"] == 1
    assert metrics["citation_completeness"] == 0.5


def test_multi_document_answer_with_only_one_support_is_incomplete():
    gold, context, answer, review = _case(multi=True)
    context["evidence"] = context["evidence"][:1]
    review["context_sufficient"] = False
    answer["claims"][0]["citations"] = [deepcopy(BADGE)]
    review["claims"][0].update(fully_supported=False, supported_citations=[0])
    metrics = _score(gold, context, answer, review)
    assert metrics["citation_precision"] == 1
    assert metrics["citation_completeness"] == 0
    assert metrics["sufficient_evidence"] is False
    assert metrics["abstention_when_insufficient"] is False


def test_abstention_is_scored_for_insufficient_evidence_and_no_answer_questions():
    gold, context, _, review = _case(multi=True)
    context["evidence"] = context["evidence"][:1]
    review["context_sufficient"] = False
    answer = {"status": "abstain", "claims": [], "reason": "Registration policy is missing."}
    review["claims"] = []
    metrics = _score(gold, context, answer, review)
    assert metrics["abstention_when_insufficient"] is True
    assert metrics["unnecessary_abstention"] is None
    gold.update(answerable=False, answer_policy="abstain", expected_claims=[])
    metrics = _score(gold, context, answer, review)
    assert metrics["answer_accuracy"] == 1
    assert metrics["noanswer_abstention"] is True
    assert metrics["claim_precision"] is None
    assert metrics["claim_recall"] is None
    assert metrics["citation_precision"] is None
    assert metrics["citation_completeness"] is None
    assert metrics["unsupported_claim_rate"] is None


def test_abstention_with_sufficient_context_is_unnecessary():
    gold, context, _, review = _case()
    review["claims"] = []
    metrics = _score(gold, context, {"status": "abstain", "claims": [], "reason": "Unsure."}, review)
    assert metrics["answer_accuracy"] == 0
    assert metrics["unnecessary_abstention"] is True


def test_alternative_support_and_unreviewed_sufficiency_are_not_gold_chunk_misses():
    gold, context, answer, review = _case()
    context["evidence"][0]["doc_id"] = "equivalent-source.md"
    answer["claims"][0]["citations"][0]["doc_id"] = "equivalent-source.md"
    metrics = _score(gold, context, answer, review)
    assert metrics["gold_evidence_complete"] is False
    assert metrics["sufficient_evidence"] is True
    assert metrics["abstention_when_insufficient"] is None
    review.pop("context_sufficient")
    metrics = _score(gold, context, answer, review)
    assert metrics["sufficient_evidence"] is None
    assert metrics["abstention_when_insufficient"] is None


def test_conflict_policy_requires_explicit_conflict_status():
    gold, context, answer, review = _case(multi=True, policy="conflict")
    contradiction = REGISTER | {"quote": "Entry does not require a badge."}
    context["evidence"][1]["content"] = contradiction["quote"]
    gold["expected_claims"][0].update(
        text="The two access notices disagree about whether a badge is required.",
        evidence=[deepcopy(BADGE), contradiction],
    )
    answer["claims"][0].update(text=gold["expected_claims"][0]["text"],
                               citations=deepcopy(gold["expected_claims"][0]["evidence"]))
    review.update(answer_sha256=digest(answer), context_sha256=digest(context))
    assert evaluate_answer(gold, context, answer, review)["answer_accuracy"] == 1
    answer["status"] = "answer"
    assert _score(gold, context, answer, review)["answer_accuracy"] == 0


@pytest.mark.parametrize("change", ["answer", "context"])
def test_stale_review_hash_is_rejected(change):
    gold, context, answer, review = _case()
    if change == "answer":
        answer["claims"][0]["text"] += " changed"
    else:
        context["evidence"][0]["content"] += " changed"
    with pytest.raises(ValueError, match="hashes"):
        evaluate_answer(gold, context, answer, review)


@pytest.mark.parametrize("defect", ["answer_id", "review_id", "citation", "dangling_index", "missing_review"])
def test_ambiguous_or_dangling_records_are_rejected(defect):
    gold, context, answer, review = _case()
    if defect == "answer_id":
        answer["claims"].append(deepcopy(answer["claims"][0]))
    elif defect == "review_id":
        review["claims"].append(deepcopy(review["claims"][0]))
    elif defect == "citation":
        answer["claims"][0]["citations"].append(deepcopy(BADGE))
    elif defect == "dangling_index":
        review["claims"][0]["supported_citations"] = [1]
    else:
        review["claims"] = []
    with pytest.raises(ValueError):
        _score(gold, context, answer, review)


def test_duplicate_gold_claim_ids_are_rejected():
    gold, _, _, _ = _case()
    gold["expected_claims"].append(deepcopy(gold["expected_claims"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        validate_gold_answer(gold)


def test_unanswerable_gold_cannot_require_an_answer_or_conflict():
    gold, _, _, _ = _case()
    gold.update(answerable=False, expected_claims=[])
    for policy in ("answer", "conflict"):
        with pytest.raises(ValueError, match="answer_policy"):
            validate_gold_answer(gold | {"answer_policy": policy})


def test_summary_uses_all_submitted_citations_and_preserves_empty_denominators():
    gold, context, answer, review = _case()
    answer["claims"][0]["citations"].append(BADGE | {"doc_id": "missing.md"})
    reviewed = _score(gold, context, answer, review)
    summary = answer_summary([{"answer_metrics": reviewed}, {}])
    assert summary["citation_precision"] == 0.5
    assert summary["reviewed_answers"] == 1
    assert summary["unreviewed_answers"] == 1
    empty = answer_summary([{}])
    assert empty["answer_accuracy"] is None
    assert empty["citation_precision"] is None


def _replay():
    gold, context, answer, review = _case()
    report = {"metadata": {"queryset_sha256": digest([gold])}, "modes": {"hybrid": {"queries": [{
        "query_id": "access", "context": context, "context_sha256": digest(context),
    }]}}}
    submission = {"mode": "hybrid", "query_id": "access", "context_sha256": digest(context),
                  "answer": answer, "review": review}
    return report, [gold], [submission]


def test_replay_scores_only_the_frozen_context():
    report, gold, submissions = _replay()
    scored = score_answers(report, gold, submissions)
    assert scored["modes"]["hybrid"]["answer_summary"]["citation_precision"] == 1


@pytest.mark.parametrize("tamper", ["gold", "frozen_context", "submission_context", "duplicate_submission"])
def test_replay_rejects_other_context_or_gold_and_duplicate_submissions(tamper):
    report, gold, submissions = _replay()
    if tamper == "gold":
        gold[0]["expected_claims"][0]["text"] = "Different rule."
    elif tamper == "frozen_context":
        report["modes"]["hybrid"]["queries"][0]["context"]["evidence"] = []
    elif tamper == "submission_context":
        submissions[0]["context_sha256"] = digest({"evidence": []})
    else:
        submissions.append(deepcopy(submissions[0]))
    with pytest.raises(ValueError):
        score_answers(report, gold, submissions)


@pytest.mark.parametrize("duplicate", ["gold_query", "report_query"])
def test_replay_rejects_ambiguous_query_ids(duplicate):
    report, gold, submissions = _replay()
    if duplicate == "gold_query":
        gold.append(deepcopy(gold[0]))
        report["metadata"]["queryset_sha256"] = digest(gold)
    else:
        queries = report["modes"]["hybrid"]["queries"]
        queries.append(deepcopy(queries[0]))
    with pytest.raises(ValueError, match="ambiguous"):
        score_answers(report, gold, submissions)


def test_rejected_replay_batch_does_not_partially_mutate_original_report():
    report, gold, submissions = _replay()
    original = deepcopy(report)
    submissions.append(deepcopy(submissions[0]))
    submissions[1]["query_id"] = "missing-question"
    with pytest.raises(ValueError):
        score_answers(report, gold, submissions)
    assert report == original
