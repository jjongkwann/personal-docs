"""권한 기반 접근 제어: 경로 규칙 정책, 요청 주체, 검색 필터, 감사 로그.

정책 파일(ACCESS_POLICY, YAML)이 없으면 접근 제어가 꺼진 단일 사용자 모드다. 모든 호출이
관리자(SYSTEM)로 실행되고 필터도 붙지 않는다. 정책이 있으면 MCP 요청의 주체는 Bearer
토큰으로 정하고, 어떤 규칙에도 맞지 않는 자원은 거부한다(default deny).

자원은 doc_id 형식의 경로(data/…, obsidian/…, attachments/…)다. 규칙은 경로 단위이고 자원에
맞는 가장 긴 규칙 하나가 결정한다 — 하위 폴더 규칙이 상위 규칙을 덮어쓴다. write는 read를
포함한다. admin 주체는 모든 자원을 읽고 쓰며 관리 도구(동기화·그래프 변경·doctor)를 부른다.

정책이 곧 단일 진실 공급원이다. 검색은 같은 규칙을 ES 필터로 번역해 BM25·kNN 양쪽에 미리
걸고(pre-filter), 결과는 한 번 더 파이썬으로 검사한다. 청크에 ACL을 복제해 두지 않으므로
정책을 고치면 재색인 없이 다음 요청부터 적용된다.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from threading import Lock
from typing import Literal

import yaml

_log = logging.getLogger(__name__)

Action = Literal["read", "write"]
Capability = Literal["read", "write", "admin"]
CAPABILITIES: frozenset[str] = frozenset({"read", "write", "admin"})

_PRINCIPAL_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_GROUP_RE = _PRINCIPAL_ID_RE
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
ANY_AUTHENTICATED = "*"


class AccessDeniedError(PermissionError):
    """접근 거부. 메시지는 자원 존재 여부를 드러내지 않는다 — 없는 자원과 같은 문장을 쓴다."""


class PolicyError(ValueError):
    """정책 파일 형식 오류. 정책을 읽을 수 없으면 모든 요청을 거부한다(fail closed)."""


@dataclass(frozen=True)
class Principal:
    id: str
    groups: frozenset[str] = frozenset()
    admin: bool = False

    @property
    def subjects(self) -> frozenset[str]:
        """규칙의 read/write 목록과 비교할 이 주체의 이름들."""
        return frozenset({ANY_AUTHENTICATED, f"user:{self.id}", *(f"group:{g}" for g in self.groups)})


# CLI·운영 스크립트와 접근 제어가 꺼진 서버의 주체. 셸에서 파일과 ES를 직접 다룰 수 있는 사람이다.
SYSTEM = Principal("system", admin=True)


def normalize_resource(resource: str) -> str:
    """doc_id 형식 자원 경로를 NFC·슬래시 정규형으로. '.'·'..'·빈 성분·역슬래시·NUL은 거부.

    규칙 비교는 문자열 접두사라 data/study/../career 같은 경로가 study 규칙에 걸리면 안 된다.
    """
    if not isinstance(resource, str):
        raise ValueError(f"자원 경로는 문자열이어야 합니다: {resource!r}")
    text = unicodedata.normalize("NFC", resource.strip()).strip("/")
    parts = text.split("/") if text else []
    if any(not part or part in {".", ".."} or "\\" in part or "\0" in part for part in parts):
        raise ValueError(f"'.'·'..'·빈 성분이 있는 경로는 쓸 수 없습니다: {resource!r}")
    return "/".join(parts)


def _subject_list(value: object, *, where: str, users: set[str]) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PolicyError(f"{where}: 주체 목록은 문자열 배열이어야 합니다")
    subjects = set()
    for raw in value:
        item = raw.strip()
        if item == ANY_AUTHENTICATED:
            subjects.add(item)
            continue
        kind, _, name = item.partition(":")
        if kind == "user" and _PRINCIPAL_ID_RE.fullmatch(name):
            if name not in users:
                raise PolicyError(f"{where}: principals에 없는 사용자입니다: {item}")
        elif not (kind == "group" and _GROUP_RE.fullmatch(name)):
            raise PolicyError(f"{where}: 주체는 user:<id>, group:<이름>, * 중 하나여야 합니다: {raw!r}")
        subjects.add(item)
    return frozenset(subjects)


@dataclass(frozen=True)
class Rule:
    path: str  # 정규화한 자원 경로. 빈 문자열은 default 규칙(모든 자원)
    read: frozenset[str] = frozenset()
    write: frozenset[str] = frozenset()

    def matches(self, resource: str) -> bool:
        return not self.path or resource == self.path or resource.startswith(self.path + "/")

    def contains(self, other: Rule) -> bool:
        """other가 이 규칙의 하위 경로 규칙인가 (자신 제외)."""
        return other.path != self.path and self.matches(other.path)

    def grants(self, principal: Principal, action: Action) -> bool:
        allowed = self.write if action == "write" else self.read | self.write
        return bool(allowed & principal.subjects)

    def es_match(self) -> dict:
        if not self.path:
            return {"match_all": {}}
        return {"bool": {"should": [
            {"term": {"doc_id": self.path}},
            {"prefix": {"doc_id": self.path + "/"}},
        ], "minimum_should_match": 1}}


@dataclass(frozen=True)
class _PrincipalEntry:
    principal: Principal
    token_hashes: tuple[str, ...]


@dataclass(frozen=True)
class Policy:
    rules: tuple[Rule, ...]  # default 규칙(path="")을 맨 앞에 포함
    principals: dict[str, _PrincipalEntry] = field(default_factory=dict)
    sha256: str = ""

    def principal(self, principal_id: str) -> Principal | None:
        entry = self.principals.get(principal_id)
        return entry.principal if entry else None

    def principal_for_token(self, token: str) -> Principal | None:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        found = None
        for entry in self.principals.values():
            for expected in entry.token_hashes:
                # 일치를 찾아도 끝까지 비교해 주체마다 걸리는 시간을 같게 둔다.
                if hmac.compare_digest(digest, expected) and found is None:
                    found = entry.principal
        return found

    def rule_for(self, resource: str) -> Rule:
        """자원에 맞는 가장 긴 규칙. default 규칙이 항상 맞으므로 None이 없다."""
        return max((rule for rule in self.rules if rule.matches(resource)), key=lambda rule: len(rule.path))

    def allows(self, principal: Principal, action: Action, resource: str) -> bool:
        if principal.admin:
            return True
        return self.rule_for(normalize_resource(resource)).grants(principal, action)

    def reads_any_under(self, principal: Principal, resource: str) -> bool:
        """resource 자체나 그 아래 어딘가를 읽을 수 있는가 — 폴더 목록에 그 폴더를 보일지."""
        if self.allows(principal, "read", resource):
            return True
        base = Rule(normalize_resource(resource))
        return any(base.contains(rule) and rule.grants(principal, "read") for rule in self.rules)

    def reads_everything(self, principal: Principal) -> bool:
        return principal.admin or all(rule.grants(principal, "read") for rule in self.rules)

    def es_filter(self, principal: Principal) -> list[dict]:
        """allows(principal, "read", doc_id)와 같은 집합을 고르는 ES 필터.

        가장 긴 규칙이 이기므로, 허용하는 규칙 r마다 "r에 맞고, r 아래에서 거부하는 더 긴 규칙에는
        맞지 않음"을 만들고 OR로 묶는다. 거부 규칙 아래 다시 허용하는 규칙은 자기 절이 고른다.
        """
        if self.reads_everything(principal):
            return []
        clauses = []
        for rule in self.rules:
            if not rule.grants(principal, "read"):
                continue
            denied = [other.es_match() for other in self.rules
                      if rule.contains(other) and not other.grants(principal, "read")]
            match = rule.es_match()
            clauses.append({"bool": {"filter": [match], "must_not": denied}} if denied else match)
        if not clauses:
            return [{"match_none": {}}]
        return [{"bool": {"should": clauses, "minimum_should_match": 1}}]


def parse_policy(data: object, *, sha256: str = "") -> Policy:
    if not isinstance(data, dict):
        raise PolicyError("정책 파일 최상위는 매핑이어야 합니다")
    unknown = set(data) - {"version", "default", "principals", "rules"}
    if unknown:
        raise PolicyError(f"알 수 없는 최상위 키: {sorted(unknown)}")
    if data.get("version") != 1:
        raise PolicyError("version: 1 이어야 합니다")

    raw_principals = data.get("principals") or {}
    if not isinstance(raw_principals, dict):
        raise PolicyError("principals는 <id>: {...} 매핑이어야 합니다")
    principals: dict[str, _PrincipalEntry] = {}
    for principal_id, spec in raw_principals.items():
        if not isinstance(principal_id, str) or not _PRINCIPAL_ID_RE.fullmatch(principal_id):
            raise PolicyError(f"주체 id는 소문자·숫자·._- 64자 이내여야 합니다: {principal_id!r}")
        spec = spec or {}
        if not isinstance(spec, dict) or set(spec) - {"groups", "admin", "token_sha256"}:
            raise PolicyError(f"principals.{principal_id}: groups, admin, token_sha256만 쓸 수 있습니다")
        groups = spec.get("groups") or []
        if not isinstance(groups, list) or not all(isinstance(g, str) and _GROUP_RE.fullmatch(g) for g in groups):
            raise PolicyError(f"principals.{principal_id}.groups는 그룹 이름 배열이어야 합니다")
        admin = spec.get("admin", False)
        if not isinstance(admin, bool):
            raise PolicyError(f"principals.{principal_id}.admin은 true/false여야 합니다")
        hashes = spec.get("token_sha256") or []
        hashes = [hashes] if isinstance(hashes, str) else hashes
        if not isinstance(hashes, list) or not all(isinstance(h, str) and _SHA256_RE.fullmatch(h) for h in hashes):
            raise PolicyError(f"principals.{principal_id}.token_sha256은 소문자 16진수 SHA-256(64자)이어야 합니다")
        principals[principal_id] = _PrincipalEntry(
            Principal(principal_id, frozenset(groups), admin), tuple(hashes)
        )
    all_hashes = [h for entry in principals.values() for h in entry.token_hashes]
    if len(all_hashes) != len(set(all_hashes)):
        raise PolicyError("같은 토큰 해시를 두 주체가 쓸 수 없습니다")

    users = set(principals)

    def _rule(spec: object, where: str, *, path: str) -> Rule:
        if not isinstance(spec, dict) or set(spec) - {"path", "read", "write"}:
            raise PolicyError(f"{where}: path, read, write만 쓸 수 있습니다")
        return Rule(
            path,
            _subject_list(spec.get("read"), where=f"{where}.read", users=users),
            _subject_list(spec.get("write"), where=f"{where}.write", users=users),
        )

    rules = [_rule(data.get("default") or {}, "default", path="")]
    raw_rules = data.get("rules") or []
    if not isinstance(raw_rules, list):
        raise PolicyError("rules는 배열이어야 합니다")
    seen = {""}
    for index, spec in enumerate(raw_rules):
        where = f"rules[{index}]"
        raw_path = spec.get("path") if isinstance(spec, dict) else None
        if not isinstance(raw_path, str):
            raise PolicyError(f"{where}.path가 필요합니다")
        try:
            path = normalize_resource(raw_path)
        except ValueError as exc:
            raise PolicyError(f"{where}.path: {exc}") from None
        if not path:
            raise PolicyError(f"{where}: 전체 경로 규칙은 default에 씁니다")
        if path in seen:
            raise PolicyError(f"{where}: 같은 경로 규칙이 두 번 있습니다: {path}")
        seen.add(path)
        rules.append(_rule(spec, where, path=path))
    return Policy(tuple(rules), principals, sha256)


# ---------- 정책 파일 로딩 (mtime·크기가 바뀌면 다시 읽는다) ----------

_cache_lock = Lock()
_cache: tuple[str, int, int, Policy] | None = None


def policy_path() -> Path | None:
    from pkb.config import settings

    return Path(settings.access_policy).expanduser() if settings.access_policy else None


def enabled() -> bool:
    return policy_path() is not None


def load_policy() -> Policy | None:
    """현재 정책. ACCESS_POLICY가 비면 None(접근 제어 꺼짐). 설정됐는데 읽을 수 없으면 PolicyError."""
    global _cache
    path = policy_path()
    if path is None:
        return None
    try:
        info = path.stat()
    except OSError as exc:
        raise PolicyError(f"정책 파일을 읽을 수 없습니다: {path} ({exc})") from None
    key = (str(path), info.st_mtime_ns, info.st_size)
    with _cache_lock:
        if _cache is not None and _cache[:3] == key:
            return _cache[3]
    raw = path.read_bytes()
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise PolicyError(f"정책 YAML 파싱 실패: {exc}") from None
    policy = parse_policy(data, sha256=hashlib.sha256(raw).hexdigest())
    with _cache_lock:
        _cache = (*key, policy)
    return policy


# ---------- 요청 주체 ----------

_principal: ContextVar[Principal | None] = ContextVar("pkb_principal", default=None)


@contextmanager
def acting_as(principal: Principal) -> Iterator[Principal]:
    """이 블록(과 거기서 띄운 작업 스레드)을 principal로 실행한다. CLI·테스트·예열용."""
    token = _principal.set(principal)
    try:
        yield principal
    finally:
        _principal.reset(token)


def set_process_principal(principal: Principal) -> None:
    """CLI 진입점이 프로세스 전체 주체를 정한다 — 셸 사용자는 SYSTEM이다."""
    _principal.set(principal)


def current_principal() -> Principal:
    """지금 요청의 주체. 정책이 없으면 SYSTEM, 있으면 명시 주체 → MCP 토큰 순으로 찾고 없으면 거부."""
    explicit = _principal.get()
    if explicit is not None:
        return explicit
    policy = load_policy()
    if policy is None:
        return SYSTEM
    from mcp.server.auth.middleware.auth_context import get_access_token

    token = get_access_token()
    if token is not None:
        # 토큰 검증 뒤 정책이 바뀌었을 수 있다 — 지운 주체·바꾼 그룹을 지금 정책으로 다시 읽는다.
        principal = policy.principal(token.client_id)
        if principal is not None:
            return principal
    raise AccessDeniedError("인증된 주체가 없습니다. Bearer 토큰으로 요청하세요.")


class PolicyTokenVerifier:
    """MCP SDK TokenVerifier: 정적 Bearer 토큰의 SHA-256을 정책의 주체 토큰 해시와 비교한다."""

    async def verify_token(self, token: str):
        from mcp.server.auth.provider import AccessToken

        try:
            policy = load_policy()
        except PolicyError as exc:
            _log.error("access policy unreadable; rejecting token: %s", exc)
            return None
        principal = policy.principal_for_token(token) if policy else None
        if principal is None:
            return None
        return AccessToken(
            token=hashlib.sha256(token.encode("utf-8")).hexdigest(),  # 원문 토큰을 메모리에 남기지 않는다
            client_id=principal.id,
            subject=principal.id,
            scopes=[],
        )


# ---------- 판정 ----------

def _deny(action: str, resource: str, principal: Principal) -> AccessDeniedError:
    audit(principal, action=action, resource=resource, decision="deny")
    return AccessDeniedError(f"접근 권한이 없거나 존재하지 않습니다: {resource}")


def is_restricted(principal: Principal | None = None) -> bool:
    """이 주체의 읽기에 필터가 필요한가. 정책이 없거나 admin이면 False."""
    principal = principal or current_principal()
    policy = load_policy()
    return policy is not None and not policy.reads_everything(principal)


def can(action: Action, resource: str, principal: Principal | None = None) -> bool:
    principal = principal or current_principal()
    policy = load_policy()
    if policy is None or principal.admin:
        return True
    try:
        return policy.allows(principal, action, resource)
    except ValueError:
        return False


def require(action: Action, resource: str) -> None:
    """자원에 대한 action 권한을 요구한다. 거부·잘못된 경로 모두 같은 AccessDeniedError."""
    principal = current_principal()
    if not can(action, resource, principal):
        raise _deny(action, resource, principal)


def file_resource(prefix: str, root: Path, target: Path) -> str:
    """디스크 경로를 실제 디렉터리 항목 이름(대소문자·NFC)에 맞춘 doc_id 형식 자원으로 바꾼다.

    macOS 기본 파일시스템은 대소문자를 구분하지 않아 data/CAREER/x.md도 data/career/x.md를 연다.
    요청 문자열 그대로 규칙에 대면 대소문자만 바꾼 경로가 하위 거부 규칙을 피한다. 그래서 각
    성분을 실제 항목과 NFC로 정확히 맞추고, 대소문자만 다른 항목이 있으면 거부한다. 아직 없는
    성분(새 파일)은 요청한 이름을 쓴다.
    """
    current = root
    names: list[str] = []
    for part in target.relative_to(root).parts:
        wanted = unicodedata.normalize("NFC", part)
        entries = {}
        if current.is_dir():
            entries = {unicodedata.normalize("NFC", name): name for name in os.listdir(current)}
        if wanted in entries:
            current = current / entries[wanted]
        elif any(name.casefold() == wanted.casefold() for name in entries):
            raise ValueError(f"대소문자만 다른 기존 항목이 있습니다: {part}")
        else:
            current = current / part
        names.append(wanted)
    return "/".join([prefix.strip("/"), *names]) if names else prefix.strip("/")


def require_file(action: Action, prefix: str, root: Path, target: Path, *, label: str) -> None:
    """파일시스템 자원에 대한 권한 검사. 접근 제어가 꺼졌거나 admin이면 디스크를 보지 않는다."""
    principal = current_principal()
    if load_policy() is None or principal.admin:
        return
    try:
        resource = file_resource(prefix, root, target)
    except ValueError:
        raise _deny(action, label, principal) from None
    if not can(action, resource, principal):
        raise _deny(action, label, principal)


def can_list(resource: str, principal: Principal | None = None) -> bool:
    principal = principal or current_principal()
    policy = load_policy()
    if policy is None or principal.admin:
        return True
    try:
        return policy.reads_any_under(principal, resource)
    except ValueError:
        return False


def search_filter() -> list[dict]:
    """지금 주체가 읽을 수 있는 문서만 고르는 ES 필터 절 목록. 제한이 없으면 []."""
    principal = current_principal()
    policy = load_policy()
    if policy is None:
        return []
    return policy.es_filter(principal)


def readable(items: Iterable[dict], key: str = "doc_id") -> list[dict]:
    """ES 필터를 통과한 결과를 파이썬 판정으로 한 번 더 거른다 (필터 번역 오류 방어).

    걸러지는 항목이 있으면 필터와 판정이 어긋난 것이므로 경고를 남긴다.
    """
    principal = current_principal()
    items = list(items)
    if load_policy() is None or principal.admin:
        return items
    kept = [item for item in items if can("read", str(item.get(key) or ""), principal)]
    if len(kept) != len(items):
        _log.warning("access post-check dropped %d rows the ES filter returned", len(items) - len(kept))
    return kept


def tool_access(capability: Capability) -> Callable:
    """MCP 도구의 권한 등급. read/write 도구는 자원별 검사를 본문에서, admin은 여기서 한다.

    모든 호출을 감사 로그에 남긴다. 등급은 pkb_access 속성으로 남아 등록 검사 테스트가 읽는다.
    """
    if capability not in CAPABILITIES:
        raise ValueError(f"알 수 없는 권한 등급: {capability!r}")

    def decorate(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            principal = current_principal()
            if capability == "admin" and not principal.admin:
                audit(principal, tool=func.__name__, capability=capability, decision="deny")
                raise AccessDeniedError(f"관리자 전용 도구입니다: {func.__name__}")
            audit(principal, tool=func.__name__, capability=capability, decision="allow")
            return func(*args, **kwargs)

        wrapper.pkb_access = capability
        return wrapper

    return decorate


# ---------- 감사 로그 ----------

def audit(principal: Principal, *, decision: str, **detail: str) -> None:
    """접근 판정을 <DATA_ROOT>/.logs/access.jsonl에 남긴다. 정책이 있을 때만. 실패는 삼킨다."""
    if not enabled():
        return
    try:
        from pkb.search_log import LOG_DIR

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        entry = {"ts": datetime.now(UTC).isoformat(), "principal": principal.id, "decision": decision, **detail}
        with (LOG_DIR / "access.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 감사 로그 실패가 요청을 막지 않는다
