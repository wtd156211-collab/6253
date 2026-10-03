"""自包含差异定位页面：路径、块号、计数全部来自引擎结果。"""

from __future__ import annotations

import io
from html import escape

_WIDTH = 960
_MARGIN_X = 60
_MARGIN_Y = 40
_ROW_HEIGHT = 96
_RADIUS = 20

_HEAD = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>分块校验树核对结果</title>
<style>
body { font-family: system-ui, sans-serif; margin: 2em; color: #1e293b; }
h1 { font-size: 1.3em; }
#summary { display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px; }
#summary dt { font-weight: 600; }
#summary dd { margin: 0; font-family: ui-monospace, monospace; }
#tree { width: 100%; max-width: 960px; border: 1px solid #cbd5e1; background: #f8fafc; }
#tree line { stroke: #94a3b8; stroke-width: 1.5; }
.node circle { fill: #fff; stroke: #94a3b8; stroke-width: 1.5; }
.node text { font-size: 11px; text-anchor: middle; dominant-baseline: central; fill: #334155; }
.node .block-label { fill: #7f1d1d; font-weight: 600; }
.node.changed circle { fill: #fbbf24; stroke: #b45309; }
.node.changed-block circle { fill: #ef4444; stroke: #7f1d1d; }
.node.changed-block text { fill: #fff; }
#paths li { font-family: ui-monospace, monospace; }
</style>
</head>
<body>
  <h1>分块校验树核对结果</h1>
  <dl id="summary">
"""

_TAIL = """  </ul>
</body>
</html>
"""


def _summary_items(result):
    changed = (
        " ".join(str(item) for item in result.changed_blocks)
        if result.changed_blocks
        else "-"
    )
    return [
        ("块数", str(result.blocks)),
        ("变化块", changed),
        ("重算数", str(len(result.recomputed))),
        ("根", result.root.hex()),
    ]


def write_page(result, branch: int, depth: int, out) -> None:
    nodes = result.recomputed
    changed_leaves = {(0, block) for block in result.paths}

    positions = {}
    span = max(result.blocks, 1)
    for level, index in nodes:
        low = index * branch**level
        high = min(low + branch**level, span)
        center = (low + high) / 2
        x = _MARGIN_X + center / span * (_WIDTH - 2 * _MARGIN_X)
        y = _MARGIN_Y + (depth - level) * _ROW_HEIGHT
        positions[(level, index)] = (x, y)

    height = _MARGIN_Y * 2 + depth * _ROW_HEIGHT + _RADIUS

    out.write(_HEAD)
    for name, value in _summary_items(result):
        out.write(f"      <dt>{escape(name)}</dt><dd>{escape(value)}</dd>\n")
    out.write(
        "  </dl>\n  <h2>重算路径</h2>\n"
        f'  <svg id="tree" viewBox="0 0 {_WIDTH} {height}" role="img"\n'
        '       aria-label="变化路径高亮图">\n'
    )
    node_set = set(nodes)
    wrote_edge = False
    for level, index in nodes:
        if level >= depth:
            continue
        parent = (level + 1, index // branch)
        if parent not in node_set:
            continue
        if not wrote_edge:
            out.write('      <g class="edges">')
            wrote_edge = True
        x1, y1 = positions[parent]
        x2, y2 = positions[(level, index)]
        out.write(
            f'<line data-edge="{level + 1}:{index // branch}/{level}:{index}"'
            f' x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"/>'
        )
    if wrote_edge:
        out.write("</g>\n")
    for level, index in nodes:
        x, y = positions[(level, index)]
        if (level, index) in changed_leaves:
            klass = "node changed-block"
            extra = (
                f'<text class="block-label" y="{_RADIUS + 14}">'
                f"块 {index}</text>"
            )
        else:
            klass = "node changed"
            extra = ""
        label = f"{level}:{index}"
        out.write(
            f'      <g class="{klass}" data-node="{label}"'
            f' transform="translate({x:.1f},{y:.1f})">'
            f'<circle r="{_RADIUS}"/>'
            f"<text>{escape(label)}</text>{extra}</g>\n"
        )
    out.write("  </svg>\n  <h2>变化块路径</h2>\n  <ul id=\"paths\">\n")
    wrote_path = False
    for block in result.changed_blocks:
        if block in result.paths:
            out.write(
                f"      <li>路径 {block} {escape(result.paths[block])}</li>\n"
            )
            wrote_path = True
    if not wrote_path:
        out.write("      <li>路径 -</li>\n")
    out.write(_TAIL)


def render_page(result, branch: int, depth: int) -> str:
    buffer = io.StringIO()
    write_page(result, branch, depth, buffer)
    return buffer.getvalue()
