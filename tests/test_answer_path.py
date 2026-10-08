"""Search-to-answer integration boundaries, without Elasticsearch or model downloads."""

import json
from copy import deepcopy

from typer.testing import CliRunner

from pkb.answer_eval import citation_present, compare_modes, digest
from pkb.cli import app
from pkb.config import settings
from pkb.eval import evaluate
from pkb.query import analyze_query

QUOTE = "Entry requires a badge."
HIT = {"_id": "entry-0", "doc_id": "entry.md", "chunk_index": 0, "score": 1.0,
       "content": QUOTE, "source_path": "entry.md", "section_path": "Access",
       "legal_kind": "statute", "law_id": "fixture-law", "article_id": "제12조",
       "legal_version": "revision-1", "effective_from": "2024-01-01",
       "original_url": "https://example.org/law", "original_location": "제12조 제1항"}
CITATION = {"doc_id": "entry.md", "chunk_index": 0, "quote": QUOTE}


def _gold():
    return {"version": 2, "id": "access", "query": "Access requirements?", "query_type": "exact-evidence",
            "answerable": True, "relevant": [CITATION | {"relevance": 2}],
            "expected_claims": [{"id": "badge", "text": "A badge is required.", "evidence": [CITATION]}]}


def _stub_search(monkeypatch):
    calls = []
    plan = analyze_query("기준일 2024-06-01 제12조", issues=["적용 요건"], needed_sources=["statute"])

    def search(es, question, **kwargs):
        calls.append((question, kwargs))
        kwargs["query_plan_out"].append(deepcopy(plan))
        return [deepcopy(HIT)]

    monkeypatch.setattr("pkb.store.get_client", lambda: object())
    monkeypatch.setattr("pkb.retrieve.hybrid_search", search)
    return calls, plan


def _local_search(monkeypatch, hits=None):
    batches = []

    def embed(texts):
        batches.append(list(texts))
        return [[1.0] for _ in texts]

    monkeypatch.setattr("pkb.retrieve.embed", embed)
    monkeypatch.setattr("pkb.retrieve._rrf_search", lambda *args, **kwargs: deepcopy(hits or [HIT]))
    monkeypatch.setattr("pkb.eval._git_revision", lambda: {"commit": "test", "dirty": False})
    return batches


def test_cli_query_forwards_analysis_and_legal_flags_and_displays_plan(monkeypatch):
    calls, plan = _stub_search(monkeypatch)
    result = CliRunner().invoke(app, [
        "query", "법률 질문", "--analyze", "--issue", "적용 요건", "--needed-source", "statute",
        "--query-variant", "원문 조문", "--as-of", "2024-06-01", "--law-id", "fixture-law",
        "--article-id", "제12조", "--case-id", "2024다1234", "--legal-version", "revision-1",
        "--legal-kind", "statute", "--context-tokens", "2000",
    ])
    assert result.exit_code == 0, result.output
    options = calls[0][1]
    assert options["analyze"] is True
    assert options["issues"] == ["적용 요건"]
    assert options["needed_sources"] == ["statute"]
    assert options["variants"] == ["원문 조문"]
    for name, value in {"as_of": "2024-06-01", "law_id": "fixture-law", "article_id": "제12조",
                        "case_id": "2024다1234", "legal_version": "revision-1", "legal_kind": "statute"}.items():
        assert options[name] == value
    visible_plan = result.output.split("질문 분석: ", 1)[1].split("\n", 1)[0]
    assert json.loads(visible_plan) == plan


def test_mcp_search_forwards_flags_and_keeps_original_source_metadata(monkeypatch, tmp_path):
    from pkb.mcp_server import search_knowledge

    calls, _ = _stub_search(monkeypatch)
    monkeypatch.setattr(settings, "graph_db_path", str(tmp_path / "absent.sqlite"))
    rendered = search_knowledge(
        "법률 질문", analyze=True, issues=["적용 요건"], needed_sources=["statute"],
        query_variants=["원문 조문"], as_of="2024-06-01", law_id="fixture-law", article_id="제12조",
        case_id="2024다1234", legal_version="revision-1", legal_kind="statute", max_context_tokens=2000,
    )
    options = calls[0][1]
    assert options["analyze"] is True
    assert options["issues"] == ["적용 요건"]
    assert options["needed_sources"] == ["statute"]
    assert options["variants"] == ["원문 조문"]
    assert options["as_of"] == "2024-06-01"
    assert options["case_id"] == "2024다1234"
    for field in ("doc_id", "law_id", "article_id", "legal_version", "original_url", "original_location"):
        assert HIT[field] in rendered
    assert "질문 분석:" in rendered


def test_eval_captures_only_delivered_clipped_evidence(monkeypatch):
    hidden_quote = "The secret appendix grants an exemption."
    _local_search(monkeypatch, [HIT | {"content": "Visible introduction. " * 700 + hidden_quote}])
    report = evaluate(object(), [_gold()], configurations={"small": {
        "rerank": False, "expand_context": 0, "context_tokens": 256,
    }})
    row = report["modes"]["small"]["queries"][0]
    assert row["context"]["evidence"]
    visible = row["context"]["evidence"][0]["content"]
    assert visible in row["context"]["rendered"]
    assert hidden_quote not in row["context"]["rendered"]
    assert not citation_present(CITATION | {"quote": hidden_quote}, row["context"]["evidence"])
    assert row["context_sha256"] == digest(row["context"])


def test_replay_cli_scores_and_writes_metrics_without_an_es_client(monkeypatch, tmp_path):
    _local_search(monkeypatch)
    gold = [_gold()]
    report = evaluate(object(), gold, configurations={"baseline": {"rerank": False, "expand_context": 0}})
    frozen = report["modes"]["baseline"]["queries"][0]
    answer = {"status": "answer", "claims": [{"id": "a1", "text": "A badge is required.",
                                                "citations": [CITATION]}]}
    review = {"reviewer": "independent-test-review", "answer_sha256": digest(answer),
              "context_sha256": frozen["context_sha256"], "claims": [{
                  "id": "a1", "correct": True, "covers": ["badge"], "fully_supported": True,
                  "supported_citations": [0],
              }]}
    submission = {"mode": "baseline", "query_id": "access", "context_sha256": frozen["context_sha256"],
                  "answer": answer, "review": review}
    gold_path, report_path, answers_path, output = [tmp_path / name for name in (
        "gold.jsonl", "frozen.json", "answers.jsonl", "scored.json",
    )]
    gold_path.write_text(json.dumps(gold[0]) + "\n")
    report_path.write_text(json.dumps(report))
    answers_path.write_text(json.dumps(submission) + "\n")

    def forbidden_client():
        raise AssertionError("Replay must not initialize Elasticsearch")

    monkeypatch.setattr("pkb.store.get_client", forbidden_client)
    result = CliRunner().invoke(app, ["eval", "--gold", str(gold_path), "--replay-report", str(report_path),
                                    "--answers", str(answers_path), "--output", str(output)])
    assert result.exit_code == 0, result.output
    summary = json.loads(output.read_text())["modes"]["baseline"]["answer_summary"]
    assert summary["answer_accuracy"] == summary["citation_precision"] == 1
    assert "reviewed answers 1" in result.output


def test_eval_controls_variants_and_saves_the_exact_visible_analysis_plan(monkeypatch):
    batches = _local_search(monkeypatch)
    gold = _gold() | {"query": "기준일 2024-06-01 제12조", "variants": ["원문 조문"],
                      "issues": ["적용 요건"], "needed_sources": ["statute"]}
    report = evaluate(object(), [gold], configurations={
        "baseline": {"rerank": False, "expand_context": 0, "analyze": False, "use_variants": False},
        "variants": {"rerank": False, "expand_context": 0, "analyze": False, "use_variants": True},
        "analyzed": {"rerank": False, "expand_context": 0, "analyze": True, "use_variants": True},
    })
    assert batches[0] == [gold["query"]]
    assert batches[1] == [gold["query"], "원문 조문"]
    analyzed = report["modes"]["analyzed"]["queries"][0]
    expected_plan = analyze_query(gold["query"], variants=gold["variants"], issues=gold["issues"],
                                  needed_sources=gold["needed_sources"])
    assert analyzed["query_plan"] == expected_plan
    assert batches[2] == [gold["query"], *expected_plan["variants"]]
    visible_plan = analyzed["context"]["rendered"].split("질문 분석: ", 1)[1].split("\n", 1)[0]
    assert json.loads(visible_plan) == analyzed["query_plan"]
    assert report["modes"]["baseline"]["queries"][0]["query_plan"] is None


def test_mode_comparison_pairs_only_jointly_reviewed_questions_and_keeps_none_precision():
    def row(key, accuracy, precision, evidence):
        result = {"query_id": key, "recall_at_k": 1, "ndcg_at_k": 1,
                  "context": {"evidence": [{"doc_id": evidence, "chunk_index": 0, "content": "visible text"}]}}
        if accuracy is not None:
            result["answer_metrics"] = {"answer_accuracy": accuracy, "citation_precision": precision,
                                        "status": "answer" if precision is not None else "abstain"}
        return result

    report = {"modes": {
        "baseline": {"queries": [row("q1", 0, None, "old"), row("q2", None, None, "old"),
                                  row("q3", 1, 1, "same")]},
        "analyzed": {"queries": [row("q1", 1, 1, "new"), row("q2", 1, 1, "new"),
                                  row("q3", 1, 0.5, "same")]},
    }}
    compared = compare_modes(report)[0]
    assert compared["paired_answers"] == 2
    assert compared["answer_accuracy_delta"] == 0.5
    assert [row["query_id"] for row in compared["queries"]] == ["q1", "q3"]
    assert compared["queries"][0]["citation_precision_delta"] is None
    assert compared["queries"][0]["evidence_added"] == [("new", 0)]
    assert compared["queries"][1]["citation_precision_delta"] == -0.5
