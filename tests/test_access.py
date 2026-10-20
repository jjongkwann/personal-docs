"""접근 제어 정책: 해석, 가장 긴 규칙 판정, ES 필터 번역의 동치성, 토큰, 경로 정규화."""

from __future__ import annotations

import asyncio
import hashlib
import random

import pytest
import yaml

from pkb import access
from pkb.access import PolicyError, Principal, parse_policy

TOKEN = "alice-token"
POLICY = {
    "version": 1,
    "default": {"read": ["group:staff"]},
    "principals": {
        "owner": {"admin": True},
        "alice": {"groups": ["staff"], "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest()},
        "bob": {"groups": ["study"]},
    },
    "rules": [
        {"path": "data/career", "read": ["user:alice"], "write": ["user:alice"]},
        {"path": "data/career/public", "read": ["*"]},
        {"path": "data/study", "read": ["group:study", "group:staff"]},
        {"path": "data/study/private", "read": []},
        {"path": "data/study/private/shared.md", "write": ["user:bob"]},
    ],
}


@pytest.fixture
def policy():
    return parse_policy(POLICY)


def _principal(policy, principal_id):
    return policy.principal(principal_id)


@pytest.mark.parametrize(
    ("principal_id", "resource", "read", "write"),
    [
        ("alice", "data/career/cv.md", True, True),
        ("bob", "data/career/cv.md", False, False),
        ("bob", "data/career/public/intro.md", True, False),  # 하위 규칙이 상위 거부를 덮는다
        ("alice", "data/career/public/intro.md", True, False),  # 하위 규칙에 write가 없다
        ("bob", "data/study/rag.md", True, False),
        ("alice", "data/study/private/x.md", False, False),
        ("bob", "data/study/private/shared.md", True, True),  # write는 read를 포함한다
        ("alice", "data/writing/a.md", True, False),  # default
        ("bob", "data/writing/a.md", False, False),
        ("alice", "data/careerist.md", True, False),  # data/career는 접두사가 아니라 경로 단위다
        ("owner", "data/study/private/x.md", True, True),  # admin
    ],
)
def test_longest_rule_decides(policy, principal_id, resource, read, write):
    principal = _principal(policy, principal_id)
    assert policy.allows(principal, "read", resource) is read
    assert policy.allows(principal, "write", resource) is write


def test_dot_segments_cannot_reach_a_sibling_rule(policy):
    bob = _principal(policy, "bob")
    with pytest.raises(ValueError):
        policy.allows(bob, "read", "data/study/../career/cv.md")
    with pytest.raises(ValueError):
        access.normalize_resource("data//x.md")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda p: p["rules"].append({"path": "data/x", "read": ["user:nobody"]}), "principals에 없는"),
        (lambda p: p["rules"].append({"path": "data/x", "read": ["team:eng"]}), "주체는"),
        (lambda p: p["rules"].append({"path": "data/career", "read": []}), "두 번"),
        (lambda p: p["rules"].append({"path": "", "read": []}), "default"),
        (lambda p: p["principals"]["bob"].update(token_sha256="xyz"), "SHA-256"),
        (lambda p: p.update(version=2), "version"),
        (lambda p: p["principals"]["bob"].update(
            token_sha256=p["principals"]["alice"]["token_sha256"]), "같은 토큰"),
    ],
)
def test_policy_validation_rejects_mistakes(mutate, message):
    data = yaml.safe_load(yaml.safe_dump(POLICY))
    mutate(data)
    with pytest.raises(PolicyError, match=message):
        parse_policy(data)


# ---------- ES 필터 번역: 작은 DSL 평가기로 파이썬 판정과 같은 집합을 고르는지 본다 ----------

def _matches(query: dict, doc_id: str) -> bool:
    (kind, body), = query.items()
    if kind == "match_all":
        return True
    if kind == "match_none":
        return False
    if kind == "term":
        return doc_id == body["doc_id"]
    if kind == "prefix":
        return doc_id.startswith(body["doc_id"])
    if kind == "bool":
        if not all(_matches(q, doc_id) for q in body.get("filter", [])):
            return False
        if any(_matches(q, doc_id) for q in body.get("must_not", [])):
            return False
        should = body.get("should", [])
        if should:
            return sum(_matches(q, doc_id) for q in should) >= body.get("minimum_should_match", 1)
        return True
    raise AssertionError(f"unexpected query: {query}")


def _filter_allows(clauses: list[dict], doc_id: str) -> bool:
    return all(_matches(clause, doc_id) for clause in clauses)


def test_es_filter_selects_exactly_the_allowed_documents_on_random_policies():
    rng = random.Random(7)
    segments = ["a", "b", "c"]
    paths = sorted({"/".join(["data", *rng.choices(segments, k=depth)]) for depth in (1, 2, 3) for _ in range(6)})
    docs = [f"{path}/{name}.md" for path in paths for name in ("x", "y")] + paths
    subjects = ["*", "user:u1", "user:u2", "group:g1", "group:g2"]
    principals = [
        Principal("u1", frozenset({"g1"})),
        Principal("u2", frozenset({"g2"})),
        Principal("u3"),
    ]
    for _ in range(300):
        rule_paths = rng.sample(paths, rng.randint(0, len(paths)))
        data = {
            "version": 1,
            "default": {"read": rng.sample(subjects, rng.randint(0, 2))},
            "principals": {"u1": {"groups": ["g1"]}, "u2": {"groups": ["g2"]}, "u3": {}},
            "rules": [
                {"path": path, "read": rng.sample(subjects, rng.randint(0, 2)),
                 "write": rng.sample(subjects, rng.randint(0, 1))}
                for path in rule_paths
            ],
        }
        policy = parse_policy(data)
        for principal in principals:
            clauses = policy.es_filter(principal)
            for doc_id in docs:
                assert _filter_allows(clauses, doc_id) == policy.allows(principal, "read", doc_id), (
                    data, principal, doc_id,
                )


def test_es_filter_is_empty_only_when_everything_is_readable(policy):
    assert policy.es_filter(_principal(policy, "owner")) == []
    # '*'는 인증된 모든 주체다 — 그룹이 없어도 data/career/public은 읽는다.
    assert _filter_allows(policy.es_filter(Principal("nobody")), "data/career/public/a.md")
    assert parse_policy({"version": 1}).es_filter(Principal("nobody")) == [{"match_none": {}}]


# ---------- 요청 주체와 토큰 ----------

@pytest.fixture
def policy_file(tmp_path, monkeypatch):
    path = tmp_path / "access.yml"
    path.write_text(yaml.safe_dump(POLICY), encoding="utf-8")
    monkeypatch.setattr("pkb.config.settings.access_policy", str(path))
    return path


def test_without_policy_every_call_is_system(monkeypatch):
    monkeypatch.setattr("pkb.config.settings.access_policy", "")
    assert access.current_principal() is access.SYSTEM
    assert access.search_filter() == []


def test_with_policy_a_request_needs_a_principal(policy_file):
    with pytest.raises(access.AccessDeniedError):
        access.current_principal()
    with access.acting_as(access.load_policy().principal("bob")):
        assert access.current_principal().id == "bob"
        assert access.search_filter() != []


def test_token_verifier_maps_token_to_principal_and_follows_policy_changes(policy_file):
    verifier = access.PolicyTokenVerifier()
    token = asyncio.run(verifier.verify_token(TOKEN))
    assert token.client_id == "alice" and token.token != TOKEN
    assert asyncio.run(verifier.verify_token("wrong")) is None

    revoked = yaml.safe_load(yaml.safe_dump(POLICY))
    del revoked["principals"]["alice"]["token_sha256"]
    policy_file.write_text(yaml.safe_dump(revoked) + "\n# revoked\n", encoding="utf-8")
    assert asyncio.run(verifier.verify_token(TOKEN)) is None


def test_unreadable_policy_fails_closed(policy_file):
    policy_file.write_text("version: [", encoding="utf-8")
    with pytest.raises(PolicyError):
        access.current_principal()
    assert asyncio.run(access.PolicyTokenVerifier().verify_token(TOKEN)) is None


def test_denials_are_audited(policy_file, tmp_path):
    from pkb import search_log

    with access.acting_as(access.load_policy().principal("bob")), pytest.raises(access.AccessDeniedError):
        access.require("read", "data/career/cv.md")
    lines = (search_log.LOG_DIR / "access.jsonl").read_text(encoding="utf-8").splitlines()
    assert '"principal": "bob"' in lines[-1] and '"decision": "deny"' in lines[-1]


# ---------- 파일시스템 자원: 대소문자만 바꾼 경로로 하위 거부 규칙을 피할 수 없다 ----------

def test_file_resource_uses_real_entry_names(tmp_path):
    root = tmp_path / "data"
    (root / "career").mkdir(parents=True)
    (root / "career" / "cv.md").write_text("x", encoding="utf-8")

    assert access.file_resource("data/", root, root / "career" / "cv.md") == "data/career/cv.md"
    assert access.file_resource("data/", root, root / "career" / "new.md") == "data/career/new.md"
    with pytest.raises(ValueError, match="대소문자"):
        access.file_resource("data/", root, root / "CAREER" / "cv.md")
