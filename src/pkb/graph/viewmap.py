"""Evidence Map: 개념 관계의 근거 확인용 자급식 HTML 스냅샷.

SQLite가 SSOT — 이 모듈은 query.py의 read model을 시각화용 view model로
정규화하고, CDN·외부 요청 없는 단일 HTML 파일로 렌더한다.
사용자 절대 경로·원문 전체는 포함하지 않는다 (doc_id/chunk_index/제목/섹션만).
"""

from __future__ import annotations

import html as html_mod
import json
import sqlite3

from pkb.graph import query as gquery
from pkb.graph import store as gstore

_CONF_ORDER = ["explicit", "inferred", "weak", "legacy"]


def _node_mentions(conn: sqlite3.Connection, slug: str, *, limit: int = 10) -> list[dict]:
    row = gstore.get_concept(conn, slug)
    return gquery._mentions(conn, row["id"], limit=limit) if row else []


def _norm_nodes(conn: sqlite3.Connection, raw_nodes: list[dict], mode: str) -> list[dict]:
    nodes = []
    for raw in raw_nodes:
        role = "seed" if raw.get("seed") else ("path" if mode == "path" else "normal")
        nodes.append(
            {
                "slug": raw["slug"],
                "name": raw["name"],
                "category": raw["category"] or "",
                "description": raw.get("description", ""),
                "mention_count": raw["mention_count"],
                "depth": raw.get("depth", 0),
                "role": role,
                "mentions": _node_mentions(conn, raw["slug"]),
            }
        )
    return sorted(nodes, key=lambda n: (n["depth"], n["slug"]))


def _norm_edges(raw_edges: list[dict]) -> list[dict]:
    edges = [
        {
            "source": raw["source"]["slug"],
            "source_name": raw["source"]["name"],
            "target": raw["target"]["slug"],
            "target_name": raw["target"]["name"],
            "relation": raw["relation"],
            "confidence": raw["confidence"],
            "confidence_label": raw["confidence_label"],
            "evidence_count": raw["evidence_count"],
            "evidence": [
                {
                    "doc_id": ev["doc_id"],
                    "chunk_index": ev["chunk_index"],
                    "title": ev["title"],
                    "section_path": ev["section_path"],
                    "confidence_label": ev["confidence_label"],
                }
                for ev in raw["evidence"]
            ],
        }
        for raw in raw_edges
    ]
    return sorted(edges, key=lambda e: (e["source"], e["target"], e["relation"]))


def build(
    conn: sqlite3.Connection,
    *,
    concept: str | None = None,
    query: str | None = None,
    query_embedding: list[float] | None = None,
    path: tuple[str, str] | None = None,
    depth: int = 1,
    max_nodes: int = 30,
    relations: list[str] | None = None,
    evidence_limit: int = 5,
) -> dict:
    """concept/query/path 중 한 진입 방식으로 시각화 view model을 조립한다."""
    chosen = [m for m, v in (("concept", concept), ("query", query), ("path", path)) if v]
    if len(chosen) != 1:
        raise ValueError("concept, query, path 중 정확히 하나만 지정해야 합니다.")
    mode = chosen[0]
    seeds: list[dict] = []
    message = None

    if mode == "concept":
        raw = gquery.concept_subgraph(
            conn, concept, depth=depth, max_nodes=max_nodes,
            relations=relations, evidence_limit=evidence_limit,
        )
        input_echo = {"concept": concept}
        raw_nodes, raw_edges, found = raw["nodes"], raw["edges"], True
        if not raw_edges:
            message = "이 개념과 연결된 관계가 없습니다 (고아 개념)."
    elif mode == "query":
        raw = gquery.query_subgraph(
            conn, query, query_embedding=query_embedding, depth=depth,
            max_nodes=max_nodes, relations=relations, evidence_limit=evidence_limit,
        )
        input_echo = {"query": query}
        raw_nodes, raw_edges, found = raw["nodes"], raw["edges"], raw["found"]
        seeds = [
            {"slug": s["concept"]["slug"], "name": s["concept"]["name"],
             "score": s["score"], "match": s["match"]}
            for s in raw["seeds"]
        ]
        if not found:
            message = "질문과 매칭되는 개념 시드를 찾지 못했습니다."
        elif not raw_edges:
            message = "시드 개념 주변에 표시할 관계가 없습니다."
    else:
        source, target = path
        raw = gquery.shortest_path(
            conn, source, target, relations=relations, evidence_limit=evidence_limit,
        )
        input_echo = {"source": source, "target": target}
        found, raw_edges = raw["found"], raw["edges"]
        if found:
            last = len(raw["nodes"]) - 1
            raw_nodes = [
                {**n, "depth": i, "seed": i in (0, last)}
                for i, n in enumerate(raw["nodes"])
            ]
        else:
            endpoints = {raw["source"]["slug"]: raw["source"], raw["target"]["slug"]: raw["target"]}
            raw_nodes = [
                {**n, "depth": i, "seed": True} for i, n in enumerate(endpoints.values())
            ]
            message = f"두 개념 사이에 경로가 없습니다 (max_hops={raw['max_hops']})."

    nodes = _norm_nodes(conn, raw_nodes, mode)
    edges = _norm_edges(raw_edges)
    confidences = sorted(
        {e["confidence_label"] for e in edges}, key=_CONF_ORDER.index
    )
    if not nodes and message is None:
        message = "표시할 그래프가 없습니다."
    return {
        "mode": mode,
        "input": input_echo,
        "found": found,
        "message": message,
        "seeds": seeds,
        "nodes": nodes,
        "edges": edges,
        "relations": sorted({e["relation"] for e in edges}),
        "confidences": confidences,
        "categories": sorted({n["category"] for n in nodes}),
    }


def render(model: dict) -> str:
    """view model을 오프라인 단일 HTML로 렌더 (결정적 출력)."""
    subtitle = " · ".join(f"{k}: {v}" for k, v in model["input"].items())
    data = json.dumps(model, ensure_ascii=False, sort_keys=True).replace("</", "<\\/")
    return (
        _TEMPLATE
        .replace("__TITLE__", html_mod.escape(f"Evidence Map — {subtitle}"))
        .replace("__SUBTITLE__", html_mod.escape(subtitle))
        .replace("/*__DATA__*/null", data)
    )


# 팔레트: dataviz 스킬 기준 검증된 카테고리 8색(light/dark) — relation 타입에 고정 순서 배정.
_TEMPLATE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{
  color-scheme:light;
  --bg:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
  --grid:#e1e0d9;--border:rgba(11,11,11,.10);
  --c0:#2a78d6;--c1:#008300;--c2:#e87ba4;--c3:#eda100;--c4:#1baf7a;--c5:#eb6834;--c6:#4a3aa7;--c7:#e34948;--cx:#898781;
}
@media (prefers-color-scheme:dark){:root{
  color-scheme:dark;
  --bg:#0d0d0d;--surface:#1a1a19;--ink:#ffffff;--ink2:#c3c2b7;--grid:#2c2c2a;--border:rgba(255,255,255,.10);
  --c0:#3987e5;--c1:#008300;--c2:#d55181;--c3:#c98500;--c4:#199e70;--c5:#d95926;--c6:#9085e9;--c7:#e66767;
}}
*{box-sizing:border-box}
body{margin:0;height:100vh;display:flex;flex-direction:column;background:var(--bg);color:var(--ink);
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
header{display:flex;flex-wrap:wrap;align-items:center;gap:12px;padding:10px 16px;border-bottom:1px solid var(--grid)}
header h1{font-size:16px;margin:0}
#subtitle{color:var(--ink2);font-size:13px}
#filters{display:flex;flex-wrap:wrap;gap:12px;margin-left:auto;font-size:12px}
#filters fieldset{border:1px solid var(--grid);border-radius:6px;margin:0;padding:2px 8px;
  display:flex;gap:8px;align-items:center}
#filters legend{color:var(--muted);padding:0 4px}
#filters label{display:inline-flex;align-items:center;gap:4px;cursor:pointer;color:var(--ink2)}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block}
main{flex:1;display:flex;min-height:0}
#map{flex:1;min-width:0;background:var(--surface)}
#map svg{width:100%;height:100%;display:block}
aside{width:320px;flex:none;border-left:1px solid var(--grid);overflow-y:auto;padding:14px 16px;font-size:13px}
aside h2{font-size:13px;margin:0 0 8px;color:var(--muted);font-weight:600}
aside h3{font-size:15px;margin:0 0 4px}
.kv{margin:2px 0;color:var(--ink2)}
.kv b{color:var(--ink);font-weight:600}
.ev{margin:8px 0;padding:8px;border:1px solid var(--grid);border-radius:6px;background:var(--bg)}
.ev .loc{font-family:ui-monospace,monospace;font-size:12px}
.ev .sec{color:var(--muted);font-size:12px}
.banner{margin:0 0 10px;padding:8px 10px;border:1px solid var(--border);border-radius:6px;
  background:var(--bg);color:var(--ink2)}
footer{padding:6px 16px;border-top:1px solid var(--grid);color:var(--muted);font-size:12px;
  display:flex;flex-wrap:wrap;gap:14px}
.edge-hit{stroke:transparent;stroke-width:12;fill:none;cursor:pointer}
.edge{fill:none;cursor:pointer;pointer-events:none}
.edge.sel,.node.sel circle{filter:drop-shadow(0 0 3px var(--ink))}
.node circle{fill:var(--surface);cursor:pointer}
.node text{fill:var(--ink2);font-size:11px;text-anchor:middle;pointer-events:none}
.node.seed text{fill:var(--ink);font-weight:600}
.node.dim,.hidden-edge{opacity:.25}
.hidden{display:none}
#edgelabel{fill:var(--ink);font-size:11px;text-anchor:middle;paint-order:stroke;stroke:var(--surface);stroke-width:3px}
</style>
</head>
<body>
<header>
  <h1>Evidence Map</h1>
  <span id="subtitle">__SUBTITLE__</span>
  <div id="filters"></div>
</header>
<main>
  <div id="map"></div>
  <aside id="panel"></aside>
</main>
<footer>
  <span>실선 = explicit·inferred</span><span>점선 = weak·legacy</span>
  <span>엣지 굵기 = evidence 수</span><span>엣지 투명도 = confidence</span>
  <span>노드 크기 = mention 수</span><span>굵은 테두리 = 시드·경로 노드</span>
</footer>
<script>
"use strict";
const DATA = /*__DATA__*/null;
const COLORS = 8, esc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const relColor = {};
DATA.relations.forEach((r, i) => relColor[r] = i < COLORS ? `var(--c${i})` : "var(--cx)");
const CONF_STYLE = {
  explicit:{op:1,dash:""}, inferred:{op:.75,dash:""},
  weak:{op:.5,dash:"6 4"}, legacy:{op:.6,dash:"3 5"},
};
const nodeR = n => Math.min(26, 7 + 2.2 * Math.sqrt(n.mention_count || 1));

// ---- 레이아웃: path는 수평 일렬, 그 외는 깊이별 방사형 ----
const pos = {};
if (DATA.mode === "path") {
  const order = [...DATA.nodes].sort((a, b) => a.depth - b.depth);
  order.forEach((n, i) => pos[n.slug] = {
    x: order.length === 1 ? 0 : (i / (order.length - 1) - 0.5) * (170 * (order.length - 1)),
    y: 0,
  });
} else {
  // 방사형 트리: BFS 트리 백본으로 자식을 부모 각도 근처에 배치해 교차선을 줄인다.
  const adj = new Map();
  DATA.nodes.forEach(n => adj.set(n.slug, []));
  DATA.edges.forEach(e => {
    if (adj.has(e.source) && adj.has(e.target) && e.source !== e.target) {
      adj.get(e.source).push(e.target);
      adj.get(e.target).push(e.source);
    }
  });
  const roots = DATA.nodes.filter(n => n.depth === 0).map(n => n.slug);
  const children = new Map(), visited = new Set(roots), queue = [...roots];
  const depthCount = {0: roots.length}, depthBFS = new Map(roots.map(r => [r, 0]));
  while (queue.length) {
    const u = queue.shift();
    for (const v of adj.get(u)) {
      if (visited.has(v)) continue;
      visited.add(v);
      const dv = depthBFS.get(u) + 1;
      depthBFS.set(v, dv);
      depthCount[dv] = (depthCount[dv] || 0) + 1;
      if (!children.has(u)) children.set(u, []);
      children.get(u).push(v);
      queue.push(v);
    }
  }
  DATA.nodes.forEach(n => { if (!visited.has(n.slug)) roots.push(n.slug); }); // 고립 노드도 배치
  const leaves = u => (children.get(u) || []).reduce((s, c) => s + leaves(c), 0) || 1;
  const place = (u, start, end, depth) => {
    const a = (start + end) / 2;
    // 링 반지름은 그 깊이의 노드 수에 비례해 넓혀 라벨 겹침을 막는다 (노드당 호길이 ~95px)
    const r = depth === 0
      ? (roots.length > 1 ? 80 : 0)
      : Math.max(60 + 160 * depth, ((depthCount[depth] || 1) * 95) / (2 * Math.PI));
    pos[u] = {x: r * Math.cos(a), y: r * Math.sin(a)};
    let cursor = start;
    for (const c of children.get(u) || []) {
      const span = (end - start) * leaves(c) / leaves(u);
      place(c, cursor, cursor + span, depth + 1);
      cursor += span;
    }
  };
  const total = roots.reduce((s, r) => s + leaves(r), 0);
  let cursor = -Math.PI / 2;
  for (const r of roots) {
    const span = 2 * Math.PI * leaves(r) / total;
    place(r, cursor, cursor + span, 0);
    cursor += span;
  }
}
const xs = Object.values(pos).map(p => p.x), ys = Object.values(pos).map(p => p.y);
const pad = 140; // 긴 개념명 라벨이 노드 밖으로 뻗는 폭까지 흡수
const vb = xs.length
  ? [Math.min(...xs) - pad, Math.min(...ys) - pad,
     Math.max(...xs) - Math.min(...xs) + 2 * pad, Math.max(...ys) - Math.min(...ys) + 2 * pad]
  : [-200, -100, 400, 200];

// ---- SVG ----
const NS = "http://www.w3.org/2000/svg";
const el = (tag, attrs, parent) => {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  parent && parent.appendChild(e);
  return e;
};
const svg = el("svg", {viewBox: vb.join(" ")}, document.getElementById("map"));
const defs = el("defs", {}, svg);
DATA.relations.forEach((r, i) => {
  const m = el("marker", {id: `arrow-${i}`, viewBox: "0 0 10 10", refX: 9, refY: 5,
    markerWidth: 11, markerHeight: 11, markerUnits: "userSpaceOnUse",
    orient: "auto-start-reverse"}, defs);
  el("path", {d: "M0,0 L10,5 L0,10 z", style: `fill:${relColor[r]}`}, m);
});
const edgeLayer = el("g", {}, svg), nodeLayer = el("g", {}, svg);

const nodeBy = {};
DATA.nodes.forEach(n => nodeBy[n.slug] = n);
const pairCount = {}, pairSeen = {};
DATA.edges.forEach(e => {
  const k = [e.source, e.target].sort().join("|");
  pairCount[k] = (pairCount[k] || 0) + 1;
});

const edgeEls = DATA.edges.map((e, idx) => {
  const s = pos[e.source], t = pos[e.target];
  if (!s || !t || e.source === e.target) return null; // ponytail: 자기순환 엣지는 미표시 (패널 목록엔 없음)
  const k = [e.source, e.target].sort().join("|");
  const nth = pairSeen[k] = (pairSeen[k] || 0) + 1;
  const bend = (nth - 1 - (pairCount[k] - 1) / 2) * 26;
  const dx = t.x - s.x, dy = t.y - s.y, len = Math.hypot(dx, dy) || 1;
  const ux = dx / len, uy = dy / len, px = -uy, py = ux;
  const rs = nodeR(nodeBy[e.source]) + 2, rt = nodeR(nodeBy[e.target]) + 6;
  const x1 = s.x + ux * rs, y1 = s.y + uy * rs;
  const x2 = t.x - ux * rt, y2 = t.y - uy * rt;
  const mx = (x1 + x2) / 2 + px * bend, my = (y1 + y2) / 2 + py * bend;
  const d = `M${x1},${y1} Q${mx},${my} ${x2},${y2}`;
  const conf = CONF_STYLE[e.confidence_label] || CONF_STYLE.legacy;
  const ri = DATA.relations.indexOf(e.relation);
  const path = el("path", {d, class: "edge", stroke: relColor[e.relation],
    "stroke-width": Math.min(5, 1 + 1.1 * Math.sqrt(e.evidence_count || 1)),
    "stroke-opacity": conf.op, "marker-end": `url(#arrow-${ri})`}, edgeLayer);
  if (conf.dash) path.setAttribute("stroke-dasharray", conf.dash);
  el("title", {}, path).textContent = `${e.source_name} -[${e.relation}]-> ${e.target_name}`;
  const hit = el("path", {d, class: "edge-hit"}, edgeLayer);
  hit.addEventListener("click", () => select("edge", idx));
  return {e, path, hit, mid: {x: mx, y: my}};
}).filter(Boolean);

const ROLE_STROKE = {seed: ["var(--ink)", 2.5], path: ["var(--ink2)", 2], normal: ["var(--muted)", 1.2]};
const nodeEls = DATA.nodes.map((n, idx) => {
  const p = pos[n.slug], [stroke, width] = ROLE_STROKE[n.role];
  const g = el("g", {class: `node ${n.role}`}, nodeLayer);
  const c = el("circle", {cx: p.x, cy: p.y, r: nodeR(n), stroke, "stroke-width": width}, g);
  el("title", {}, c).textContent = `${n.name} (mentions: ${n.mention_count})`;
  const label = el("text", {x: p.x, y: p.y + nodeR(n) + 13}, g);
  label.textContent = n.name;
  g.addEventListener("click", () => select("node", idx));
  return {n, g};
});
const edgeLabel = el("text", {id: "edgelabel", class: "hidden"}, svg);

// ---- 선택 → evidence 패널 ----
const panel = document.getElementById("panel");
let selected = null;
function evidenceHtml(list, aggregateCount) {
  if (!list.length) {
    return aggregateCount > 0
      ? '<div class="kv">청크 단위 evidence 미기록 — 집계 수만 존재 (graph reset-evidence 재구축 전 데이터)</div>'
      : '<div class="kv">저장된 evidence 없음</div>';
  }
  return list.map((ev, i) => `<div class="ev">
    <div class="loc">${i + 1}. ${esc(ev.doc_id)} #${esc(ev.chunk_index)}</div>
    ${ev.title ? `<div>${esc(ev.title)}</div>` : ""}
    ${ev.section_path ? `<div class="sec">section: ${esc(ev.section_path)}</div>` : ""}
  </div>`).join("");
}
function showDefault() {
  const seeds = DATA.seeds.length
    ? `<div class="kv">시드: ${DATA.seeds.map(s => `<b>${esc(s.name)}</b> (${esc(s.match)})`).join(", ")}</div>` : "";
  panel.innerHTML = `<h2>개요</h2>
    ${DATA.message ? `<div class="banner">${esc(DATA.message)}</div>` : ""}
    <div class="kv">mode: <b>${esc(DATA.mode)}</b></div>${seeds}
    <div class="kv">노드 ${DATA.nodes.length} · 관계 ${DATA.edges.length}</div>
    <div class="kv">노드나 엣지를 클릭하면 근거가 표시됩니다.</div>`;
}
function select(kind, idx) {
  document.querySelectorAll(".sel").forEach(x => x.classList.remove("sel"));
  edgeLabel.classList.add("hidden");
  if (selected && selected.kind === kind && selected.idx === idx) { selected = null; showDefault(); return; }
  selected = {kind, idx};
  if (kind === "edge") {
    const {e, path, mid} = edgeEls.find(x => DATA.edges.indexOf(x.e) === idx) || {};
    if (path) path.classList.add("sel");
    if (mid) {
      edgeLabel.setAttribute("x", mid.x); edgeLabel.setAttribute("y", mid.y - 6);
      edgeLabel.textContent = e.relation; edgeLabel.classList.remove("hidden");
    }
    const d = DATA.edges[idx];
    panel.innerHTML = `<h2>선택한 관계</h2>
      <h3>${esc(d.source_name)} → ${esc(d.target_name)}</h3>
      <div class="kv">relation: <b>${esc(d.relation)}</b></div>
      <div class="kv">confidence: <b>${esc(d.confidence_label)}</b>${
        d.confidence != null ? ` (${d.confidence})` : ""}</div>
      <div class="kv">evidence: <b>${d.evidence_count}건</b></div>
      ${evidenceHtml(d.evidence, d.evidence_count)}`;
  } else {
    const n = DATA.nodes[idx];
    nodeEls[idx].g.classList.add("sel");
    panel.innerHTML = `<h2>선택한 개념</h2>
      <h3>${esc(n.name)}</h3>
      ${n.category ? `<div class="kv">category: <b>${esc(n.category)}</b></div>` : ""}
      <div class="kv">mentions: <b>${n.mention_count}</b> · depth: ${n.depth} · ${esc(n.role)}</div>
      ${n.description ? `<div class="kv">${esc(n.description)}</div>` : ""}
      <h2 style="margin-top:12px">언급 출처</h2>
      ${evidenceHtml(n.mentions)}`;
  }
}
showDefault();

// ---- 필터: relation / confidence / category ----
const active = {
  relation: new Set(DATA.relations),
  confidence: new Set(DATA.confidences),
  category: new Set(DATA.categories),
};
function applyFilters() {
  const nodeVisible = {};
  DATA.nodes.forEach((n, i) => {
    const ok = active.category.has(n.category);
    nodeVisible[n.slug] = ok;
    nodeEls[i].g.classList.toggle("hidden", !ok);
  });
  const nodeHasEdge = {};
  edgeEls.forEach(({e, path, hit}) => {
    const ok = active.relation.has(e.relation) && active.confidence.has(e.confidence_label)
      && nodeVisible[e.source] && nodeVisible[e.target];
    path.classList.toggle("hidden", !ok);
    hit.classList.toggle("hidden", !ok);
    if (ok) nodeHasEdge[e.source] = nodeHasEdge[e.target] = true;
  });
  nodeEls.forEach(({n, g}) =>
    g.classList.toggle("dim", DATA.edges.length > 0 && !nodeHasEdge[n.slug]));
}
function fieldset(title, key, values, dot) {
  if (!values.length) return "";
  const boxes = values.map(v => `<label><input type="checkbox" checked data-k="${esc(key)}" data-v="${esc(v)}">
    ${dot ? `<span class="dot" style="background:${relColor[v]}"></span>` : ""}${esc(v) || "(없음)"}</label>`).join("");
  return `<fieldset><legend>${esc(title)}</legend>${boxes}</fieldset>`;
}
const filters = document.getElementById("filters");
filters.innerHTML = fieldset("관계", "relation", DATA.relations, true)
  + fieldset("신뢰도", "confidence", DATA.confidences, false)
  + (DATA.categories.filter(Boolean).length > 1
     ? fieldset("카테고리", "category", DATA.categories, false) : "");
filters.addEventListener("change", ev => {
  const {k, v} = ev.target.dataset;
  ev.target.checked ? active[k].add(v) : active[k].delete(v);
  applyFilters();
});
applyFilters();
</script>
</body>
</html>
"""
