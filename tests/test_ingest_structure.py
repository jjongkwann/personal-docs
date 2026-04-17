"""frontmatter 파싱 + 헤딩 기반 청킹(section_path) 테스트."""

from __future__ import annotations

from pkb.ingest import chunk_markdown_hierarchical, parse_frontmatter

# ---------- parse_frontmatter ----------


def test_frontmatter_none_returns_original():
    text = "# hello\nbody"
    meta, body = parse_frontmatter(text)
    assert meta == {}
    assert body == text


def test_frontmatter_basic():
    text = "---\ntitle: x\ntags: [a, b]\n---\nbody text"
    meta, body = parse_frontmatter(text)
    assert meta == {"title": "x", "tags": ["a", "b"]}
    assert body == "body text"


def test_frontmatter_crlf_line_endings():
    text = "---\r\ntitle: x\r\n---\r\nbody"
    meta, body = parse_frontmatter(text)
    assert meta == {"title": "x"}
    assert body == "body"


def test_frontmatter_malformed_yaml_falls_back():
    # unclosed bracket → YAMLError로 fallback
    text = "---\ntitle: [unclosed\n---\nbody"
    meta, body = parse_frontmatter(text)
    assert meta == {}
    assert body == text


def test_frontmatter_non_dict_falls_back():
    # YAML이 리스트로 해석되는 경우 → 원문 그대로 반환
    text = "---\n- a\n- b\n---\nbody"
    meta, body = parse_frontmatter(text)
    assert meta == {}
    assert body == text


def test_frontmatter_only_delimiters_without_close_ignored():
    # 종결 `---`이 없으면 frontmatter 아님
    text = "---\ntitle: x\nbody"
    meta, body = parse_frontmatter(text)
    assert meta == {}
    assert body == text


# ---------- chunk_markdown_hierarchical ----------


def test_chunk_no_heading_single_section_empty_path():
    text = "본문만 있는 짧은 문서."
    chunks = chunk_markdown_hierarchical(text)
    assert len(chunks) == 1
    section_path, chunk_text = chunks[0]
    assert section_path == ""
    assert text.strip() in chunk_text.strip()


def test_chunk_single_h1_section_path():
    text = "# Intro\n본문 1\n본문 2"
    chunks = chunk_markdown_hierarchical(text)
    assert len(chunks) == 1
    assert chunks[0][0] == "Intro"


def test_chunk_h1_h2_h3_hierarchy_path():
    text = (
        "# Top\n"
        "lead paragraph\n"
        "## Middle\n"
        "mid paragraph\n"
        "### Leaf\n"
        "leaf paragraph\n"
    )
    chunks = chunk_markdown_hierarchical(text)
    paths = [c[0] for c in chunks]
    assert "Top" in paths
    assert "Top > Middle" in paths
    assert "Top > Middle > Leaf" in paths


def test_chunk_sibling_h2_resets_h3():
    text = (
        "# Root\n"
        "## A\n"
        "### a1\n"
        "body a1\n"
        "## B\n"
        "body b\n"
    )
    chunks = chunk_markdown_hierarchical(text)
    paths = [c[0] for c in chunks]
    # B 아래는 h3 없음 → path에 leaf 없음
    assert "Root > A > a1" in paths
    assert "Root > B" in paths
    # B가 A의 h3을 물려받지 않음
    assert "Root > B > a1" not in paths


def test_chunk_h4_not_in_path_stack():
    # _split_by_headings_hierarchical은 H1~H3까지만 스택에 쌓음
    text = "# Top\n## Mid\n#### Deep\nbody\n"
    chunks = chunk_markdown_hierarchical(text)
    paths = [c[0] for c in chunks]
    assert "Top > Mid" in paths
    # H4는 path에 포함되지 않아야 함
    assert not any("Deep" in p for p in paths)
