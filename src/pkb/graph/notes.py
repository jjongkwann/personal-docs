"""SQLite 개념그래프 → data/concepts/<slug>.md 볼트 노트 렌더러/동기화.

단방향(SQLite→노트), 결정적·멱등. 개념 엣지를 [[위키링크]]로 되써서
Obsidian 그래프뷰가 개념그래프를 그리게 한다. 노트→SQLite 역승격은 v1 범위 밖.

위키링크 타깃은 논리 doc_id가 아니라 **볼트 루트 기준 물리 경로**다 —
Obsidian은 볼트 루트 기준으로 링크를 해소하므로 doc_id 접두어(data/, obsidian/)를
물리 경로(예: PKB/, 루트)로 변환해야 클릭 이동이 된다. DATA_ROOT가 볼트 밖이면
개념 링크는 basename([[slug|name]])으로, 출처는 평문으로 폴백한다.
"""

import re
import sqlite3
from pathlib import Path

import yaml

AUTO_START = "<!-- pkb:auto:start -->"
AUTO_END = "<!-- pkb:auto:end -->"

_PROSE_PLACEHOLDER_RE = re.compile(r"\[\[c:([^|\]]+)(?:\|([^\]]+))?\]\]")


def _vault_prefixes() -> tuple[str | None, str | None]:
    """(concepts_prefix, data_prefix) — 볼트 루트 기준 물리 경로 접두어.

    예: DATA_ROOT=<vault>/PKB → ("PKB/concepts", "PKB").
    OBSIDIAN_PATH 미설정이거나 DATA_ROOT가 볼트 밖이면 (None, None).
    """
    from pkb.config import data_dir, settings
    from pkb.ingest import CONCEPTS_DIR_NAME

    if not settings.obsidian_path:
        return None, None
    vault = Path(settings.obsidian_path).expanduser().resolve()
    root = data_dir()
    if not root.is_relative_to(vault):
        return None, None
    rel = root.relative_to(vault)
    data_prefix = "" if str(rel) == "." else str(rel)
    concepts_prefix = f"{data_prefix}/{CONCEPTS_DIR_NAME}" if data_prefix else CONCEPTS_DIR_NAME
    return concepts_prefix, data_prefix


def _concept_link(slug: str, name: str, concepts_prefix: str | None) -> str:
    """개념→개념 위키링크. 볼트 매핑 불가 시 basename 링크로 폴백."""
    target = f"{concepts_prefix}/{slug}" if concepts_prefix else slug
    return f"[[{target}|{name}]]"


def _source_line(doc_id: str, title: str, data_prefix: str | None) -> str:
    """출처 한 줄. doc_id를 볼트 물리 경로로 변환, 불가하면 평문."""
    if doc_id.startswith("obsidian/"):
        path = doc_id[len("obsidian/"):]
    elif doc_id.startswith("data/") and data_prefix is not None:
        rel = doc_id[len("data/"):]
        path = f"{data_prefix}/{rel}" if data_prefix else rel
    else:
        return f"- {doc_id}"
    # Obsidian 관례: .md는 확장자 생략, 그 외(.pdf 등)는 유지
    target = path[:-3] if path.endswith(".md") else path
    return f"- [[{target}|{title}]]"

def _render_prose(prose: str, projected: set[str] | None, concepts_prefix: str | None) -> str:
    """산문 속 [[c:slug|표시명]] 플레이스홀더를 해소.

    투영된 개념(projected is None 또는 slug in projected)만 위키링크,
    나머지는 표시명 평문으로 치환.
    """

    def _sub(m: re.Match) -> str:
        slug = m.group(1)
        name = m.group(2) or slug
        if concepts_prefix and (projected is None or slug in projected):
            return f"[[{concepts_prefix}/{slug}|{name}]]"
        return name

    return _PROSE_PLACEHOLDER_RE.sub(_sub, prose)


# 관계 화이트리스트 (출력 순서 고정). 그 외 관계는 "기타 관계" 섹션.
_RELATION_SECTIONS = [
    ("part_of", "상위 개념 (part_of)"),
    ("prerequisite_of", "선행 개념 (prerequisite_of)"),
    ("related_to", "관련 개념 (related_to)"),
]
_WHITELISTED_RELATIONS = {rel for rel, _ in _RELATION_SECTIONS}


def render_concept_note(
    conn: sqlite3.Connection, slug: str, projected: set[str] | None = None
) -> str:
    """개념 slug 하나를 결정적·멱등하게 노트 텍스트로 렌더링.

    projected: 투영된(real) slug 집합. None이면 store.projected_slugs로 조회
    (큐레이션 미실시면 다시 None → 전량 투영, v1 호환). 미투영 dst는 평문 렌더.
    """
    from pkb.graph import store as gstore

    row = gstore.get_concept(conn, slug)
    if row is None:
        raise ValueError(f"개념을 찾을 수 없습니다: {slug}")

    if projected is None:
        projected = gstore.projected_slugs(conn)

    aliases = sorted(gstore.list_aliases(conn, row["id"]))
    concepts_prefix, data_prefix = _vault_prefixes()

    def _dst_label(e: sqlite3.Row) -> str:
        if projected is None or e["dst_slug"] in projected:
            return _concept_link(e["dst_slug"], e["dst_name"], concepts_prefix)
        return e["dst_name"]

    # 콜론 포함 name("LLM01: ...")·쉼표 포함 alias·따옴표 시작 name 등은 f-string 조립 시
    # invalid YAML이 되므로 safe_dump로 인용 처리를 맡긴다 (결정적: 같은 입력 → 같은 바이트).
    meta = {
        "slug": row["slug"],
        "name": row["name"],
        "aliases": aliases,
        "category": row["category"] or "",
        "mention_count": row["mention_count"],
        "pkb_generated": True,
    }
    fm = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=False).strip()
    lines = [
        "---",
        fm,
        "---",
        "",
        AUTO_START,
        row["description"] or "",
        "",
    ]

    prose = gstore.get_prose(conn, slug)
    if prose:
        lines.append(_render_prose(prose, projected, concepts_prefix))
        lines.append("")

    out_edges = gstore.list_edges(conn, row["id"])
    for relation, heading in _RELATION_SECTIONS:
        items = sorted(
            [e for e in out_edges if e["relation"] == relation],
            key=lambda e: e["dst_slug"],
        )
        if not items:
            continue
        lines.append(f"## {heading}")
        lines.extend(f"- {_dst_label(e)}" for e in items)

    other = sorted(
        [e for e in out_edges if e["relation"] not in _WHITELISTED_RELATIONS],
        key=lambda e: (e["dst_slug"], e["relation"]),
    )
    if other:
        lines.append("## 기타 관계")
        lines.extend(f"- [{e['relation']}] {_dst_label(e)}" for e in other)

    # 출처는 문서 단위 — 청크 위치는 기계용 근거(SQLite concept_mentions)일 뿐
    # 독자가 이동할 수 없는 정보라 표시하지 않고, 같은 문서 다중 언급은 1줄로 dedup.
    mention_docs = sorted(
        {m["doc_id"] for m in gstore.list_mentions(conn, row["id"], limit=10)}
    )
    if mention_docs:
        lines.append("## 출처")
        for doc_id in mention_docs:
            title = gstore.get_document_title(conn, doc_id) or doc_id
            lines.append(_source_line(doc_id, title, data_prefix))

    lines.append(AUTO_END)

    return "\n".join(lines) + "\n"


def merge_concept_note(existing_text: str, rendered_text: str) -> str:
    """기존 노트의 auto:end 마커 이후 사용자 산문을 보존하며 auto 구간을 rendered로 교체.

    existing_text에 마커가 없으면 rendered_text 그대로 반환.
    """
    idx = existing_text.find(AUTO_END)
    if idx == -1:
        return rendered_text
    preserved = existing_text[idx + len(AUTO_END):]
    new_idx = rendered_text.find(AUTO_END)
    return rendered_text[: new_idx + len(AUTO_END)] + preserved


def _frontmatter_slug(path: Path) -> str | None:
    """노트 파일의 frontmatter에서 slug 필드를 읽는다 (없으면 None)."""
    from pkb.ingest import parse_frontmatter

    metadata, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    slug = metadata.get("slug")
    return slug if isinstance(slug, str) else None


def sync_concept_notes(conn: sqlite3.Connection, confirm_prune: bool = False) -> dict:
    """SQLite 개념그래프를 data/concepts/<slug>.md 노트로 동기화 (전역, 카테고리 스코프 없음).

    반환: {"created": n, "updated": n, "skipped": n, "pruned": n, "pending_prune": [...]}
    """
    from pkb.config import data_dir
    from pkb.graph import store as gstore
    from pkb.ingest import CONCEPTS_DIR_NAME
    from pkb.store import PRUNE_CONFIRM_THRESHOLD

    target_dir = data_dir() / CONCEPTS_DIR_NAME
    target_dir.mkdir(parents=True, exist_ok=True)

    concepts = gstore.list_concepts(conn)
    concept_slugs = {c["slug"] for c in concepts}
    projected = gstore.projected_slugs(conn)
    keep_slugs = concept_slugs if projected is None else projected

    created = updated = skipped = failed = 0
    for c in concepts:
        slug = c["slug"]
        if not slug or not slug.strip():
            failed += 1  # 빈 slug 방어 — concepts/.md 방지
            continue
        if projected is not None and slug not in projected:
            continue  # vocab(비실개념) — 노트 미생성
        try:
            rendered = render_concept_note(conn, slug, projected=projected)
        except Exception:
            # 개념 하나의 렌더 실패가 전체 sync를 막지 않게 건너뛴다.
            failed += 1
            continue
        path = target_dir / f"{slug}.md"
        if path.exists():
            existing_text = path.read_text(encoding="utf-8")
            merged = merge_concept_note(existing_text, rendered)
            if merged == existing_text:
                skipped += 1
                continue
            path.write_text(merged, encoding="utf-8")
            updated += 1
        else:
            path.write_text(rendered, encoding="utf-8")
            created += 1

    prune_candidates = [
        p for p in sorted(target_dir.glob("*.md"))
        if (_frontmatter_slug(p) or p.stem) not in keep_slugs
    ]

    pruned = 0
    pending_prune: list[str] = []
    if prune_candidates:
        if len(prune_candidates) > PRUNE_CONFIRM_THRESHOLD and not confirm_prune:
            pending_prune = [str(p) for p in prune_candidates]
        else:
            for p in prune_candidates:
                p.unlink()
                pruned += 1

    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "pruned": pruned,
        "pending_prune": pending_prune,
    }
