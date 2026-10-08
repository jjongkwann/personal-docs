"""Operational retrieval evaluation with versioned multi-relevance gold rows."""
from __future__ import annotations

import hashlib
import json
import subprocess
from math import isfinite, log2
from pathlib import Path
from time import perf_counter

from elasticsearch import Elasticsearch

from pkb.answer_eval import digest, validate_gold_answer
from pkb.config import settings
from pkb.context import render_query_plan, render_search_results
from pkb.retrieve import RETRIEVAL_PROFILES, hybrid_search

TOP_K = settings.default_top_k
SEARCH_OPTIONS = {
    "candidate_k", "rerank", "expand_context", "profile", "canonical_group",
    "canonical_boost", "fusion", "lexical_weight", "rerank_context",
    "exclude_doc_prefix", "include_archived",
    "analyze", "use_variants", "context_tokens",
}


def load_gold(path: Path) -> list[dict]:
    """Read v2 JSONL. Legacy rows require explicit migration."""
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict) or row.get("version") != 2:
                raise ValueError("version 2 row required; run migrate_legacy_gold first")
            if not isinstance(row.get("query"), str) or not row["query"].strip():
                raise ValueError("query must be a nonempty string")
            if not isinstance(row.get("query_type"), str) or not row["query_type"].strip():
                raise ValueError("query_type must be a nonempty string")
            if not isinstance(row.get("answerable"), bool):
                raise ValueError("answerable must be boolean")
            relevant = row.get("relevant")
            if not isinstance(relevant, list) or bool(relevant) != row["answerable"]:
                raise ValueError("relevant must be nonempty iff answerable is true")
            variants = row.get("variants", [])
            if not isinstance(variants, list) or any(not isinstance(v, str) for v in variants):
                raise ValueError("variants must be a list of strings")
            seen = set()
            for item in relevant:
                if not isinstance(item, dict):
                    raise ValueError("relevant items must be objects")
                for key in ("doc_id", "canonical_id"):
                    if key in item and (not isinstance(item[key], str) or not item[key].strip()):
                        raise ValueError(f"{key} must be a nonempty string")
                if not (item.get("doc_id") or item.get("canonical_id")):
                    raise ValueError("relevant item needs doc_id or canonical_id")
                if "chunk_index" in item and (type(item["chunk_index"]) is not int or item["chunk_index"] < 0):
                    raise ValueError("chunk_index must be a nonnegative integer")
                if "chunk_index" in item and not item.get("doc_id"):
                    raise ValueError("chunk_index requires doc_id")
                relevance = item.get("relevance", 1)
                if (isinstance(relevance, bool) or not isinstance(relevance, (int, float))
                        or not isfinite(relevance) or relevance <= 0):
                    raise ValueError("relevance must be a positive finite number")
                key = ("doc_id", item["doc_id"], item.get("chunk_index")) if item.get("doc_id") else (
                    "canonical_id", item["canonical_id"], None
                )
                if key in seen:
                    raise ValueError("duplicate relevant item")
                seen.add(key)
                if "quote" in item and (not isinstance(item["quote"], str) or not item["quote"].strip()):
                    raise ValueError("evidence quote must be nonempty")
            validate_gold_answer(row)
            rows.append(row)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{path}:{number}: {exc}") from exc
    ids = [row.get("id", str(i)) for i, row in enumerate(rows)]
    if any(not isinstance(value, str) or not value.strip() for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("gold query ids must be unique nonempty strings")
    return rows


def migrate_legacy_gold(source: Path, destination: Path) -> int:
    """Convert one-document rows into a new v2 file without changing source."""
    converted = []
    for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict) or set(row) != {"query", "doc_id"}:
                raise ValueError("expected legacy query/doc_id row")
            if not all(isinstance(row[key], str) and row[key].strip() for key in ("query", "doc_id")):
                raise ValueError("query and doc_id must be nonempty strings")
            converted.append({"version": 2, "query": row["query"], "query_type": "legacy",
                              "answerable": True, "relevant": [{"doc_id": row["doc_id"]}]})
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{source}:{number}: {exc}") from exc
    with destination.open("x", encoding="utf-8") as output:
        for row in converted:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(converted)


def doc_ranking(hits: list[dict]) -> list[str]:
    return list(dict.fromkeys(hit["doc_id"] for hit in hits))


def _matches(item: dict, hit: dict) -> bool:
    if item.get("doc_id"):
        return item["doc_id"] == hit.get("doc_id")
    return item["canonical_id"] == hit.get("canonical_id")


def eval_query(row: dict, hits: list[dict], k: int = TOP_K) -> dict:
    """Score returned hits; evidence requires the exact chunk when labeled."""
    if not row["answerable"]:
        return {"rank": None, "recall_at_k": None, "ndcg_at_k": None,
                "evidence_recall_at_k": None, "noanswer_false_positive": bool(hits),
                "top1": hits[0].get("doc_id") if hits else None}
    selected = hits[:k]
    first_hits = []
    seen_docs = set()
    for hit in selected:
        if hit["doc_id"] not in seen_docs:
            seen_docs.add(hit["doc_id"])
            first_hits.append(hit)
    # Several evidence chunks of one document contribute one document gain.
    documents = {}
    for item in row["relevant"]:
        key = ("doc_id", item["doc_id"]) if item.get("doc_id") else ("canonical_id", item["canonical_id"])
        if key not in documents or item.get("relevance", 1) > documents[key].get("relevance", 1):
            documents[key] = item
    relevant = list(documents.values())
    matched = [next((i + 1 for i, hit in enumerate(first_hits) if _matches(item, hit)), None) for item in relevant]
    credited = set()
    gains = []
    for hit in first_hits:
        uncredited = [(i, item.get("relevance", 1)) for i, item in enumerate(relevant)
                      if i not in credited and _matches(item, hit)]
        if uncredited:
            index, gain = max(uncredited, key=lambda pair: pair[1])
            credited.add(index)
            gains.append(gain)
        else:
            gains.append(0)
    dcg = sum(gain / log2(i + 2) for i, gain in enumerate(gains))
    ideal = sorted((item.get("relevance", 1) for item in relevant), reverse=True)[:k]
    idcg = sum(gain / log2(i + 2) for i, gain in enumerate(ideal))
    evidence = [item for item in row["relevant"] if "chunk_index" in item]
    evidence_found = sum(any(_matches(item, hit) and item["chunk_index"] == hit.get("chunk_index")
                             and (not item.get("content_hash") or item["content_hash"] == hit.get("content_hash"))
                             and (not item.get("quote") or item["quote"] in hit.get("content", ""))
                             for hit in selected) for item in evidence)
    return {"rank": min((rank for rank in matched if rank is not None), default=None),
            "recall_at_k": sum(rank is not None for rank in matched) / len(relevant),
            "ndcg_at_k": dcg / idcg if idcg else 0.0,
            "evidence_recall_at_k": evidence_found / len(evidence) if evidence else None,
            "noanswer_false_positive": None,
            "top1": first_hits[0]["doc_id"] if first_hits else None}


def mrr(ranks: list[int | None]) -> float:
    return sum(1 / rank for rank in ranks if rank is not None) / len(ranks) if ranks else 0.0


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * percentile
    low = int(pos)
    return round(ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * (pos - low), 2)


def _summary(rows: list[dict]) -> dict:
    answerable = [row for row in rows if row["answerable"]]
    noanswer = [row for row in rows if not row["answerable"]]
    evidence = [row["evidence_recall_at_k"] for row in answerable if row["evidence_recall_at_k"] is not None]
    return {
        "queries": len(rows), "answerable_queries": len(answerable), "noanswer_queries": len(noanswer),
        "evidence_queries": len(evidence),
        "mrr_at_k": mrr([row["rank"] for row in answerable]) if answerable else None,
        "final_recall_at_k": sum(row["recall_at_k"] for row in answerable) / len(answerable) if answerable else None,
        "ndcg_at_k": sum(row["ndcg_at_k"] for row in answerable) / len(answerable) if answerable else None,
        "evidence_recall_at_k": sum(evidence) / len(evidence) if evidence else None,
        "noanswer_false_positive_rate": (
            sum(row["noanswer_false_positive"] for row in noanswer) / len(noanswer) if noanswer else None
        ),
        "latency_p50_ms": _percentile([row["latency_ms"] for row in rows], 0.5),
        "latency_p95_ms": _percentile([row["latency_ms"] for row in rows], 0.95),
    }


def _git_revision() -> dict:
    root = Path(__file__).resolve().parents[2]
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False)
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=False)
    return {"commit": revision.stdout.strip() if revision.returncode == 0 else None,
            "dirty": bool(dirty.stdout.strip()) if dirty.returncode == 0 else None}


def evaluate(es: Elasticsearch, gold: list[dict], *, configurations: dict[str, dict] | None = None,
             top_k: int = TOP_K) -> dict:
    """Benchmark named settings through hybrid_search, with only gold-supplied variants."""
    if type(top_k) is not int or top_k < 1 or not gold:
        raise ValueError("top_k must be positive and gold nonempty")
    defaults = {"candidate_k": settings.candidate_k, "rerank": settings.rerank_enabled,
                "expand_context": settings.expand_context, "profile": "all", "canonical_group": True,
                "canonical_boost": 0.15, "fusion": "rrf", "lexical_weight": 0.5,
                "rerank_context": False, "exclude_doc_prefix": None, "include_archived": False,
                "analyze": False, "use_variants": True, "context_tokens": 4000}
    if configurations is None:
        configurations = {"operational": {}}
    if not isinstance(configurations, dict) or not configurations:
        raise ValueError("configurations must be a nonempty mapping")
    prepared = {}
    for name, overrides in configurations.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(overrides, dict):
            raise ValueError("configuration names must be nonempty strings and values must be objects")
        unknown = set(overrides) - SEARCH_OPTIONS
        if unknown:
            raise ValueError(f"unknown search options for {name}: {sorted(unknown)}")
        options = {**defaults, **overrides}
        for key in ("candidate_k", "expand_context"):
            minimum = 1 if key == "candidate_k" else 0
            if type(options[key]) is not int or options[key] < minimum:
                raise ValueError(f"{name}.{key} must be an integer >= {minimum}")
        for key in ("rerank", "canonical_group", "rerank_context", "include_archived", "analyze", "use_variants"):
            if type(options[key]) is not bool:
                raise ValueError(f"{name}.{key} must be boolean")
        for key in ("canonical_boost", "lexical_weight"):
            value = options[key]
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not isfinite(value) or value < 0 or (key == "lexical_weight" and value > 1)
                    or (key == "canonical_boost" and value >= 1)):
                raise ValueError(f"{name}.{key} must be a finite number in range")
        if not isinstance(options["profile"], str) or options["profile"] not in RETRIEVAL_PROFILES:
            raise ValueError(f"{name}.profile must be one of {sorted(RETRIEVAL_PROFILES)}")
        if not isinstance(options["fusion"], str) or options["fusion"] not in {"rrf", "linear"}:
            raise ValueError(f"{name}.fusion must be rrf or linear")
        if options["exclude_doc_prefix"] is not None and not isinstance(options["exclude_doc_prefix"], str):
            raise ValueError(f"{name}.exclude_doc_prefix must be string or null")
        render_search_results([], max_tokens=options["context_tokens"])
        prepared[name] = options
    modes = {}
    for name, options in prepared.items():
        rows = []
        for i, gold_row in enumerate(gold):
            query_plan = []
            search_options = {key: value for key, value in options.items() if key not in {
                "use_variants", "context_tokens",
            }}
            search_options.update({key: gold_row[key] for key in (
                "as_of", "law_id", "article_id", "case_id", "legal_version", "legal_kind", "category",
            ) if key in gold_row})
            if options["analyze"]:
                search_options.update({key: gold_row[key] for key in ("issues", "needed_sources") if key in gold_row})
            start = perf_counter()
            hits = hybrid_search(es, gold_row["query"], top_k=top_k,
                                 variants=gold_row.get("variants") if options["use_variants"] else None,
                                 query_plan_out=query_plan, log=False, **search_options)
            latency_ms = round((perf_counter() - start) * 1000, 2)
            evidence = []
            rendered = render_search_results(
                hits, max_tokens=options["context_tokens"], evidence_out=evidence,
                preamble=render_query_plan(query_plan[0]) if options["analyze"] and query_plan else "",
            )
            context = {"question": gold_row["query"], "rendered": rendered, "evidence": evidence}
            rows.append({"query_id": gold_row.get("id", str(i)),
                         "query": gold_row["query"], "query_type": gold_row["query_type"],
                         "answerable": gold_row["answerable"], "latency_ms": latency_ms,
                         "query_plan": query_plan[0] if query_plan else None,
                         "context": context, "context_sha256": digest(context),
                         "ranking": [{key: hit[key] for key in (
                             "doc_id", "chunk_index", "content_hash", "score", "rerank_score",
                         ) if key in hit} for hit in hits],
                         **eval_query(gold_row, hits, top_k)})
        modes[name] = {"config": {"top_k": top_k, **options}, "summary": _summary(rows),
                       "by_query_type": {kind: _summary([row for row in rows if row["query_type"] == kind])
                                         for kind in sorted({row["query_type"] for row in rows})},
                       "queries": rows}
    payload = json.dumps(gold, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"schema_version": 1, "metric_cutoff": top_k,
            "metadata": {"git": _git_revision(), "index": settings.es_index,
                         "source_sha256": digest({str(path.relative_to(Path(__file__).parent)): path.read_text()
                                                  for path in sorted(Path(__file__).parent.rglob("*.py"))}),
                         "embedding_model": settings.embedding_model,
                         "embedding_revision": getattr(settings, "embedding_revision", None),
                         "rerank_model": settings.rerank_model,
                         "rerank_revision": getattr(settings, "rerank_revision", None),
                         "queryset_sha256": hashlib.sha256(payload).hexdigest()},
            "modes": modes}


def format_report(report: dict) -> str:
    """Concise human view; report dict is the machine artifact."""
    k = report["metric_cutoff"]
    lines = [f"mode  MRR@{k}  final R@{k}  nDCG@{k}  evidence R@{k}  no-answer FP  p50/p95 ms"]
    for name, mode in report["modes"].items():
        metrics = mode["summary"]
        def display(key: str, metrics: dict = metrics) -> str:
            value = metrics[key]
            return "-" if value is None else f"{value:.3f}"
        lines.append(f"{name}  {display('mrr_at_k')}  {display('final_recall_at_k')}  "
                     f"{display('ndcg_at_k')}  {display('evidence_recall_at_k')}  "
                     f"{display('noanswer_false_positive_rate')}  "
                     f"{metrics['latency_p50_ms']}/{metrics['latency_p95_ms']}")
        if "answer_summary" in mode:
            a = mode["answer_summary"]
            lines.append(f"  reviewed answers {a['reviewed_answers']} | accuracy {a['answer_accuracy']} | "
                         f"citation precision {a['citation_precision']} | completeness {a['citation_completeness']}")
    return "\n".join(lines)
