"""접근 제어가 걸린 주체에게 개념 그래프를 그 주체가 읽을 수 있는 문서 근거만으로 보여 준다.

그래프는 여러 문서에서 뽑은 파생 데이터라, 결과 목록만 거르면 개념 설명·별칭·관계 수·경로가
읽을 수 없는 문서의 존재와 내용을 드러낸다. 그래서 읽기 연결의 temp 스키마에 같은 이름의 뷰를
만들어 main 테이블을 가린다. SQLite는 한정자 없는 이름을 temp → main 순으로 찾으므로 기존 조회
코드를 고치지 않아도 모든 읽기가 걸러진 그래프를 본다. 뷰는 쓰기를 거부하므로 이 연결로는
그래프를 바꿀 수 없다.

가시성 규칙
- 언급·관계 근거·문서·추출 마커: doc_id를 읽을 수 있는 행만.
- 개념: 읽을 수 있는 언급이나 관계 근거가 하나라도 있으면 보인다. 이름·slug·범주처럼 그 근거
  청크에서 바로 나온 열만 보이고, 설명·벡터·별칭·증류 산문처럼 여러 문서에서 합쳐 만든 값은
  개념의 근거 전부를 읽을 수 있을 때만 보인다. mention_count는 보이는 언급 수로 다시 센다.
- 관계: 읽을 수 있는 근거가 있는 관계만. weight·evidence_count·confidence는 보이는 근거로 다시 계산.
- 별칭 충돌 기록은 숨긴다.
새 열이 생기면 PUBLIC 목록에 넣기 전까지 근거 전부를 읽을 수 있을 때만 보인다(fail closed).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from pkb import access
from pkb.graph.schema import graph_connection

# 근거 청크에서 직접 나온 열 — 근거 하나만 읽을 수 있어도 보여 준다.
PUBLIC_CONCEPT_COLUMNS = frozenset(
    {"id", "name", "slug", "namespace", "base_slug", "category", "concept_type", "created_at", "updated_at"}
)
PUBLIC_EDGE_COLUMNS = frozenset({"src_id", "dst_id", "relation"})
PUBLIC_CURATION_COLUMNS = frozenset({"slug", "label", "updated_at"})

_DOC_TABLES = ("concept_mentions", "concept_edge_evidence", "documents", "extracted_chunks")


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA main.table_info({table})")]


def _select(
    columns: list[str], alias: str, public: frozenset[str], overrides: dict[str, str], *, guarded: bool = True
) -> str:
    """뷰의 SELECT 목록. public이 아닌 열은 guarded면 근거 전부를 읽을 수 있을 때(v.full)만 값이 있다."""
    parts = []
    for column in columns:
        if column in overrides:
            parts.append(f"{overrides[column]} AS {column}")
        elif column in public or not guarded:
            parts.append(f"{alias}.{column}")
        else:
            parts.append(f"CASE WHEN v.full THEN {alias}.{column} END AS {column}")
    return ", ".join(parts)


def restrict_to_readable(conn: sqlite3.Connection, principal: access.Principal) -> None:
    """이 연결의 그래프 테이블을 principal이 읽을 수 있는 근거만 남긴 temp 뷰로 가린다."""
    doc_ids = [row[0] for row in conn.execute(
        " UNION ".join(f"SELECT doc_id FROM main.{table}" for table in _DOC_TABLES)
    )]
    conn.execute("CREATE TEMP TABLE pkb_readable_docs (doc_id TEXT PRIMARY KEY) WITHOUT ROWID")
    conn.executemany(
        "INSERT INTO pkb_readable_docs VALUES (?)",
        [(doc_id,) for doc_id in doc_ids if access.can("read", doc_id, principal)],
    )
    for table in _DOC_TABLES:
        conn.execute(
            f"CREATE TEMP VIEW {table} AS SELECT * FROM main.{table} "
            "WHERE doc_id IN (SELECT doc_id FROM pkb_readable_docs)"
        )

    # 개념마다 보이는 언급 수와 '근거 전부를 읽을 수 있는가'(full)
    conn.executescript(
        """
        CREATE TEMP TABLE pkb_visible_concepts (
            id INTEGER PRIMARY KEY, mentions INTEGER NOT NULL, full INTEGER NOT NULL
        );
        INSERT INTO pkb_visible_concepts
        WITH refs AS (
            SELECT concept_id AS id, doc_id, 1 AS is_mention FROM main.concept_mentions
            UNION ALL SELECT src_id, doc_id, 0 FROM main.concept_edge_evidence
            UNION ALL SELECT dst_id, doc_id, 0 FROM main.concept_edge_evidence
        ), marked AS (
            SELECT id, is_mention, doc_id IN (SELECT doc_id FROM pkb_readable_docs) AS ok FROM refs
        )
        SELECT id, SUM(is_mention AND ok), MIN(ok) FROM marked GROUP BY id HAVING MAX(ok) = 1;

        CREATE TEMP TABLE pkb_visible_edges AS
        SELECT src_id, dst_id, relation, COUNT(*) AS n, MAX(confidence) AS confidence
        FROM concept_edge_evidence GROUP BY src_id, dst_id, relation;
        CREATE UNIQUE INDEX temp.pkb_visible_edges_key ON pkb_visible_edges (src_id, dst_id, relation);
        CREATE INDEX temp.pkb_visible_edges_dst ON pkb_visible_edges (dst_id);
        """
    )

    concept_select = _select(
        _columns(conn, "concepts"), "c", PUBLIC_CONCEPT_COLUMNS, {"mention_count": "v.mentions"}
    )
    conn.execute(
        f"CREATE TEMP VIEW concepts AS SELECT {concept_select} "
        "FROM main.concepts c JOIN pkb_visible_concepts v ON v.id = c.id"
    )
    # 관계는 근거 행 단위로 이미 걸렀다 — 집계 열만 다시 세고 나머지 열은 그대로 둔다.
    edge_select = _select(
        _columns(conn, "concept_edges"), "e", PUBLIC_EDGE_COLUMNS,
        {"weight": "CAST(v.n AS REAL)", "evidence_count": "v.n", "confidence": "v.confidence"},
        guarded=False,
    )
    conn.execute(
        f"CREATE TEMP VIEW concept_edges AS SELECT {edge_select} FROM main.concept_edges e "
        "JOIN pkb_visible_edges v ON v.src_id = e.src_id AND v.dst_id = e.dst_id AND v.relation = e.relation"
    )
    conn.execute(
        "CREATE TEMP VIEW concept_aliases AS SELECT a.* FROM main.concept_aliases a "
        "JOIN pkb_visible_concepts v ON v.id = a.concept_id WHERE v.full"
    )
    curation_select = _select(_columns(conn, "concept_curation"), "cc", PUBLIC_CURATION_COLUMNS, {})
    conn.execute(
        f"CREATE TEMP VIEW concept_curation AS SELECT {curation_select} FROM main.concept_curation cc "
        "JOIN main.concepts c ON c.slug = cc.slug JOIN pkb_visible_concepts v ON v.id = c.id"
    )
    conn.execute("CREATE TEMP VIEW concept_alias_conflicts AS SELECT * FROM main.concept_alias_conflicts WHERE 0")


@contextmanager
def graph_read_connection(db_path: str) -> Iterator[sqlite3.Connection]:
    """읽기 전용 그래프 연결. 지금 주체가 모든 문서를 읽지 못하면 걸러진 그래프를 본다."""
    principal = access.current_principal()
    with graph_connection(db_path, normalize=False) as conn:
        if access.is_restricted(principal):
            restrict_to_readable(conn, principal)
        yield conn
