"""개념 그래프의 형식 온톨로지(TBox): 개념 종류(클래스)와 관계(속성)의 어휘·특성·시그니처.

데이터와 순수 함수만 있고 I/O는 없다. 저장 시 검사(graph.store), 검증 보고(graph.validate),
질의 시 추론(graph.query), RDF 내보내기(graph.export), 추출 프롬프트가 모두 이 정의를 읽는다.
정의를 바꾸면 TBOX_VERSION을 올리고 필요한 데이터 이관을 schema.py에 더한다.
"""

from __future__ import annotations

from dataclasses import dataclass

TBOX_VERSION = "pkb-tbox/1"
ROOT_KIND = "concept"


@dataclass(frozen=True)
class KindGroup:
    id: str
    label_ko: str
    definition_ko: str
    definition_en: str


@dataclass(frozen=True)
class Kind:
    """개념 종류(leaf 클래스). 개념마다 정확히 하나이며 leaf끼리는 서로 배타적이다."""

    id: str
    group: str
    label_ko: str
    definition_ko: str
    definition_en: str


@dataclass(frozen=True)
class Relation:
    """관계 하나의 형식 특성과 시그니처.

    시그니처는 src kind가 domain에, dst kind가 range에 들고 constraint(두 끝의 쌍 조건)를
    만족해야 성립한다. constraint: same_kind | same_group | same_group_or_field_target | None.
    inverse는 표시용 역방향 이름이고 대칭 관계는 None(자기 자신이 역)이다.
    """

    id: str
    inverse: str | None
    transitive: bool
    symmetric: bool
    asymmetric: bool
    irreflexive: bool
    acyclic: bool
    domain: frozenset[str]
    range: frozenset[str]
    constraint: str | None
    skos: str  # broader | related — SKOS 매핑
    label_ko: str
    definition_ko: str
    definition_en: str
    fallback: bool = False


GROUPS: dict[str, KindGroup] = {
    group.id: group
    for group in (
        KindGroup("abstract", "추상", "방법이나 산출물이 아닌 분야·문제·원리", "Fields, problems and principles."),
        KindGroup("method", "방법", "무엇을 하는 방식", "Ways of doing something."),
        KindGroup("artifact", "산출물", "만들어져 이름으로 가리키는 대상", "Concrete, named things that were built."),
        KindGroup("measure", "척도", "무엇을 재는 기준", "Criteria for measuring something."),
    )
}

KINDS: dict[str, Kind] = {
    kind.id: kind
    for kind in (
        Kind("field", "abstract", "분야", "연구·실무의 영역 (예: 정보 검색, 분산 시스템)",
             "An area of study or practice (e.g. information retrieval, distributed systems)."),
        Kind("problem", "abstract", "문제", "풀어야 할 과제나 바라지 않는 현상 (예: 환각, 콜드 스타트)",
             "A task to solve or an unwanted phenomenon (e.g. hallucination, cold start)."),
        Kind("principle", "abstract", "원리", "방법이 기대는 개념·이론·법칙·성질 (예: CAP 정리, 의존성 역전 원칙)",
             "A concept, theory, law or property that methods rest on (e.g. CAP theorem, dependency inversion)."),
        Kind("technique", "method", "기법", "절차가 하나로 정해지지 않은 일반적 접근법 (예: 파인튜닝, 캐싱)",
             "A general approach without one fixed procedure (e.g. fine-tuning, caching)."),
        Kind("algorithm", "method", "알고리즘", "단계가 정해진 계산 절차 (예: BM25, Dijkstra)",
             "A computational procedure with defined steps (e.g. BM25, Dijkstra)."),
        Kind("pattern", "method", "패턴", "되풀이되는 설계·구조 해법 (예: Dependency Injection, ReAct)",
             "A recurring design or structural solution (e.g. dependency injection, ReAct)."),
        Kind("software", "artifact", "소프트웨어", "제품·라이브러리·프레임워크·서비스 (예: Elasticsearch, PyTorch)",
             "A product, library, framework or service (e.g. Elasticsearch, PyTorch)."),
        Kind("model", "artifact", "모델", "학습된 특정 모델·모델 계열·이름 있는 아키텍처 (예: BERT, Transformer)",
             "A specific trained model, model family or named architecture (e.g. BERT, Transformer)."),
        Kind("dataset", "artifact", "데이터셋", "이름 있는 데이터 모음이나 벤치마크 (예: MS MARCO, ImageNet)",
             "A named data collection or benchmark (e.g. MS MARCO, ImageNet)."),
        Kind("protocol", "artifact", "프로토콜", "형식·인터페이스 규격이나 표준 (예: HTTP, MCP)",
             "A format, interface specification or standard (e.g. HTTP, MCP)."),
        Kind("metric", "measure", "지표", "측정·평가 기준 (예: nDCG, 지연 시간)",
             "A measurement or evaluation criterion (e.g. nDCG, latency)."),
    )
}

KIND_IDS: tuple[str, ...] = tuple(KINDS)


def kinds_of(*groups: str) -> frozenset[str]:
    """그룹들에 속한 leaf kind 집합."""
    return frozenset(kind.id for kind in KINDS.values() if kind.group in groups)


_ALL = frozenset(KIND_IDS)
_METHOD_ARTIFACT = kinds_of("method", "artifact")
_HIERARCHY = {"transitive": True, "irreflexive": True, "acyclic": True}

RELATIONS: dict[str, Relation] = {
    relation.id: relation
    for relation in (
        Relation(
            "is_a", inverse="has_subtype", **_HIERARCHY, symmetric=False, asymmetric=False,
            domain=_ALL, range=_ALL, constraint="same_kind", skos="broader", label_ko="하위 유형",
            definition_ko="src는 dst의 하위 유형이다. 두 끝의 kind가 같다.",
            definition_en="src is a subtype of dst; both ends share one kind.",
        ),
        Relation(
            "part_of", inverse="has_part", **_HIERARCHY, symmetric=False, asymmetric=False,
            domain=_ALL, range=_ALL, constraint="same_group_or_field_target", skos="broader", label_ko="구성 요소",
            definition_ko="src는 dst의 실제 구성 요소이거나 분야 dst에 속한다. 문서 절·목록 소속은 아니다.",
            definition_en="src is a real component of dst, or belongs to the field dst; never mere section membership.",
        ),
        Relation(
            "prerequisite_of", inverse="requires", **_HIERARCHY, symmetric=False, asymmetric=False,
            domain=_ALL, range=_ALL, constraint=None, skos="related", label_ko="선행 지식",
            definition_ko="dst를 알거나 하기 전에 src가 먼저 필요하다.",
            definition_en="src must be known or done before dst.",
        ),
        Relation(
            "uses", inverse="used_by", transitive=False, symmetric=False, asymmetric=False, irreflexive=True,
            acyclic=False, domain=_METHOD_ARTIFACT, range=kinds_of("method", "artifact", "measure"),
            constraint=None, skos="related", label_ko="사용",
            definition_ko="src가 동작하거나 만들어질 때 dst를 쓴다.",
            definition_en="src uses dst to work or to be built.",
        ),
        Relation(
            "implements", inverse="implemented_by", transitive=False, symmetric=False, asymmetric=True,
            irreflexive=True, acyclic=False, domain=frozenset({"software", "model"}),
            range=frozenset({"algorithm", "technique", "pattern", "protocol"}),
            constraint=None, skos="related", label_ko="구현",
            definition_ko="소프트웨어·모델 src가 방법이나 프로토콜 dst를 구현한다.",
            definition_en="The software or model src implements the method or protocol dst.",
        ),
        Relation(
            "addresses", inverse="addressed_by", transitive=False, symmetric=False, asymmetric=False,
            irreflexive=True, acyclic=False, domain=_METHOD_ARTIFACT, range=frozenset({"problem"}),
            constraint=None, skos="related", label_ko="해결",
            definition_ko="src가 문제 dst를 풀거나 줄인다.",
            definition_en="src solves or mitigates the problem dst.",
        ),
        Relation(
            "alternative_to", inverse=None, transitive=False, symmetric=True, asymmetric=False, irreflexive=True,
            acyclic=False, domain=_ALL, range=_ALL, constraint="same_group", skos="related", label_ko="대안",
            definition_ko="src와 dst는 같은 목적에 서로 대신 쓸 수 있다.",
            definition_en="src and dst can replace each other for the same purpose.",
        ),
        Relation(
            "related_to", inverse=None, transitive=False, symmetric=True, asymmetric=False, irreflexive=True,
            acyclic=False, domain=_ALL, range=_ALL, constraint=None, skos="related", label_ko="관련",
            definition_ko="텍스트가 밝힌 의미 연결. 다른 관계가 맞지 않을 때만 쓴다.",
            definition_en="An explicit semantic link stated by the text; use only when no other relation fits.",
            fallback=True,
        ),
    )
}

RELATION_IDS: tuple[str, ...] = tuple(RELATIONS)
SYMMETRIC_RELATIONS = frozenset(r.id for r in RELATIONS.values() if r.symmetric)
TRANSITIVE_RELATIONS = frozenset(r.id for r in RELATIONS.values() if r.transitive)
ACYCLIC_RELATIONS = frozenset(r.id for r in RELATIONS.values() if r.acyclic)
HIERARCHY_RELATIONS = frozenset(r.id for r in RELATIONS.values() if r.skos == "broader")
# 어휘에서 빠진 옛 라벨 → 대체 관계. schema 이관이 기존 행을 옮기고 쓰기 거부 메시지가 안내한다.
LEGACY_RELATIONS = {"example_of": "is_a"}


def get_kind(kind_id: str | None) -> Kind | None:
    return KINDS.get(kind_id) if isinstance(kind_id, str) else None


def get_relation(relation_id: str | None) -> Relation | None:
    return RELATIONS.get(relation_id) if isinstance(relation_id, str) else None


def group_of(kind_id: str) -> str | None:
    kind = get_kind(kind_id)
    return kind.group if kind else None


def inverse_label(relation_id: str) -> str | None:
    """inbound 관계를 대상 쪽에서 읽을 때의 이름. 대칭 관계는 자기 자신, 모르는 라벨은 None."""
    relation = get_relation(relation_id)
    if relation is None:
        return None
    return relation.id if relation.symmetric else relation.inverse


def canonical_pair(relation_id: str, src_id: int, dst_id: int) -> tuple[int, int]:
    """대칭 관계는 src_id < dst_id 한 방향으로만 저장한다."""
    if relation_id in SYMMETRIC_RELATIONS and src_id > dst_id:
        return dst_id, src_id
    return src_id, dst_id


def check_signature(relation_id: str, src_kind: str | None, dst_kind: str | None) -> str | None:
    """관계가 두 끝의 kind에 맞으면 None, 아니면 이유. 한쪽이라도 미분류(None)면 검사하지 않는다."""
    relation = get_relation(relation_id)
    if relation is None:
        return f"모르는 관계 '{relation_id}'"
    if src_kind is None or dst_kind is None:
        return None
    for side, kind in (("src", src_kind), ("dst", dst_kind)):
        if get_kind(kind) is None:
            return f"{side}의 kind '{kind}'는 TBox에 없습니다"
    if src_kind not in relation.domain:
        return f"{relation.id}의 src는 {_kinds_text(relation.domain)}여야 합니다 (src kind: {src_kind})"
    if dst_kind not in relation.range:
        return f"{relation.id}의 dst는 {_kinds_text(relation.range)}여야 합니다 (dst kind: {dst_kind})"
    same_group = group_of(src_kind) == group_of(dst_kind)
    if relation.constraint == "same_kind" and src_kind != dst_kind:
        return f"{relation.id}는 두 끝의 kind가 같아야 합니다 ({src_kind} → {dst_kind})"
    if relation.constraint == "same_group" and not same_group:
        return f"{relation.id}는 두 끝이 같은 그룹이어야 합니다 ({src_kind} → {dst_kind})"
    if relation.constraint == "same_group_or_field_target" and not same_group and dst_kind != "field":
        return f"{relation.id}는 두 끝이 같은 그룹이거나 dst가 field여야 합니다 ({src_kind} → {dst_kind})"
    return None


def allowed_relations(src_kind: str | None, dst_kind: str | None) -> list[str]:
    """두 kind 사이에 시그니처가 맞는 관계(카탈로그 순서, related_to가 마지막)."""
    return [relation_id for relation_id in RELATION_IDS if check_signature(relation_id, src_kind, dst_kind) is None]


def _kinds_text(kinds: frozenset[str], *, groups: bool = True) -> str:
    """kind 집합 표기. groups면 그룹 전체를 대문자 그룹 이름(Method)으로 줄인다."""
    if groups and kinds == _ALL:
        return "any"
    parts = []
    for group in GROUPS:
        members = kinds_of(group)
        if groups and members <= kinds:
            parts.append(group.capitalize())
        else:
            parts.extend(kind for kind in KIND_IDS if kind in kinds & members)
    return "|".join(parts)


def signature_text(relation: Relation) -> str:
    """시그니처를 언어 중립 표기로: 'Method|Artifact → problem', 'same kind → same kind' 등."""
    if relation.constraint == "same_kind":
        return "same kind → same kind"
    if relation.constraint == "same_group":
        return "same group → same group"
    if relation.constraint == "same_group_or_field_target":
        return "same group → same group, or any → field"
    return f"{_kinds_text(relation.domain)} → {_kinds_text(relation.range)}"


def describe() -> dict:
    """TBox 전체를 JSON으로 직렬화할 수 있는 dict로 (MCP 도구·프롬프트용)."""
    return {
        "version": TBOX_VERSION,
        "root": ROOT_KIND,
        "kind_rules": "개념마다 leaf kind 정확히 하나, leaf kind끼리는 서로 배타적. NULL은 미분류(레거시).",
        "groups": [
            {
                "id": group.id,
                "label_ko": group.label_ko,
                "definition_ko": group.definition_ko,
                "definition_en": group.definition_en,
                "kinds": [kind.id for kind in KINDS.values() if kind.group == group.id],
            }
            for group in GROUPS.values()
        ],
        "kinds": [
            {
                "id": kind.id,
                "group": kind.group,
                "label_ko": kind.label_ko,
                "definition_ko": kind.definition_ko,
                "definition_en": kind.definition_en,
            }
            for kind in KINDS.values()
        ],
        "relations": [
            {
                "id": relation.id,
                "inverse": inverse_label(relation.id),
                "transitive": relation.transitive,
                "symmetric": relation.symmetric,
                "asymmetric": relation.asymmetric,
                "irreflexive": relation.irreflexive,
                "acyclic": relation.acyclic,
                "fallback": relation.fallback,
                "signature": signature_text(relation),
                "domain": [kind for kind in KIND_IDS if kind in relation.domain],
                "range": [kind for kind in KIND_IDS if kind in relation.range],
                "constraint": relation.constraint,
                "skos": relation.skos,
                "label_ko": relation.label_ko,
                "definition_ko": relation.definition_ko,
                "definition_en": relation.definition_en,
            }
            for relation in RELATIONS.values()
        ],
        "legacy_relations": dict(LEGACY_RELATIONS),
    }


def prompt_rules() -> str:
    """추출 프롬프트에 넣을 kind·관계 규칙 (영문)."""
    kind_lines = [f"- {kind.id} ({kind.group}): {kind.definition_en}" for kind in KINDS.values()]
    relation_lines = [
        f"- {relation.id} [{signature_text(relation)}]: {relation.definition_en}"
        + (" Transitive; never create a cycle." if relation.acyclic else "")
        + (" Symmetric." if relation.symmetric else "")
        for relation in RELATIONS.values()
    ]
    return (
        "Concept kinds — give every concept exactly one kind:\n"
        + "\n".join(kind_lines)
        + "\nRelation types (src → dst, [allowed src kind → dst kind]; capitalized names are kind groups: "
        + "; ".join(f"{group.capitalize()} = {_kinds_text(kinds_of(group), groups=False)}" for group in GROUPS)
        + "). Use the most specific type whose signature fits both kinds; related_to is the fallback:\n"
        + "\n".join(relation_lines)
    )
