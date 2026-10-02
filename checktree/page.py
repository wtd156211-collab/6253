"""差异页面渲染（README 4.5）。

页面上的坐标、块号、计数全部取自引擎的 CheckResult，页面本身不重算哈希。
"""
from __future__ import annotations

import html


_DX = 100
_DY = 90
_MARGIN_X = 70
_MARGIN_TOP = 60
_MARGIN_BOTTOM = 60


def _num(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _layout(result):
    nodes = set(result.recomputed)
    by_layer: dict[int, list[int]] = {}
    for layer, index in result.recomputed:
        by_layer.setdefault(layer, []).append(index)

    xs: dict[tuple[int, int], float] = {}
    for rank, index in enumerate(by_layer.get(0, [])):
        xs[(0, index)] = _MARGIN_X + rank * _DX
    for layer in range(1, result.depth + 1):
        previous = None
        for index in by_layer.get(layer, []):
            children = [
                xs[(layer - 1, child)]
                for child in range(index * result.branch, index * result.branch + result.branch)
                if (layer - 1, child) in nodes
            ]
            if children:
                position = sum(children) / len(children)
            else:
                position = _MARGIN_X if previous is None else previous + _DX
            xs[(layer, index)] = position
            previous = position

    def y(layer: int) -> int:
        return _MARGIN_TOP + (result.depth - layer) * _DY

    return nodes, xs, y


def render_page(result) -> str:
    nodes, xs, y = _layout(result)
    changed_leaves = {b for b in result.changed_blocks if b not in result.deleted_blocks}

    edges = []
    for layer, index in result.recomputed:
        parent = (layer + 1, index // result.branch)
        if parent in nodes:
            edges.append(((layer, index), parent))
    edges.sort()

    edge_tags = []
    for child, parent in edges:
        edge_tags.append(
            f'<line data-edge="{parent[0]}:{parent[1]}/{child[0]}:{child[1]}" '
            f'x1="{_num(xs[child])}" y1="{y(child[0])}" '
            f'x2="{_num(xs[parent])}" y2="{y(parent[0])}"/>'
        )

    node_tags = []
    for layer, index in result.recomputed:
        klass = "node changed-block" if layer == 0 and index in changed_leaves else "node changed"
        coord = f"{layer}:{index}"
        node_tags.append(
            f'<g class="{klass}" data-node="{coord}" '
            f'transform="translate({_num(xs[(layer, index)])},{y(layer)})">'
            f"<circle r=\"9\"/><text y=\"24\">{html.escape(coord)}</text></g>"
        )

    if xs:
        width = max(xs.values()) + _MARGIN_X
    else:
        width = _MARGIN_X * 2
    height = _MARGIN_TOP + result.depth * _DY + _MARGIN_BOTTOM

    path_items = []
    for block in result.changed_blocks:
        if block in result.deleted_blocks:
            path_items.append(f"<li>块 {block}：删除（仅祖先路径）</li>")
        else:
            path_items.append(f"<li>块 {block}：{html.escape(result.paths[block])}</li>")

    values = {
        "块数": str(result.blocks),
        "变化块": result.changed_text,
        "重算数": str(len(result.recomputed)),
        "根": result.root,
    }
    summary_items = "".join(
        f"<dt>{html.escape(name)}</dt><dd>{html.escape(value)}</dd>"
        for name, value in values.items()
    )

    return (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8">\n'
        "<title>校验树差异定位</title>\n"
        "<style>\n"
        "body{font-family:system-ui,'PingFang SC','Microsoft YaHei',sans-serif;"
        "margin:24px;color:#263238}\n"
        "h1{font-size:20px}\n"
        "h2{font-size:15px;margin-top:24px}\n"
        "#summary{display:grid;grid-template-columns:max-content 1fr;"
        "gap:4px 16px;max-width:900px}\n"
        "#summary dt{font-weight:600}\n"
        "#summary dd{margin:0;font-family:ui-monospace,monospace;word-break:break-all}\n"
        "#tree{border:1px solid #eceff1;background:#fafafa;width:100%;"
        "max-width:1200px;height:auto}\n"
        "line{stroke:#90a4ae;stroke-width:1.5}\n"
        ".node circle{fill:#ffb300;stroke:#e65100;stroke-width:1.5}\n"
        ".node.changed-block circle{fill:#e53935;stroke:#b71c1c}\n"
        ".node text{font:10px ui-monospace,monospace;text-anchor:middle;fill:#37474f}\n"
        "#paths li{font-family:ui-monospace,monospace}\n"
        "</style>\n</head>\n<body>\n"
        "<h1>校验树差异定位</h1>\n"
        f'<dl id="summary">{summary_items}</dl>\n'
        f'<svg id="tree" viewBox="0 0 {_num(width)} {height}" '
        'role="img" aria-label="变化路径">\n'
        + ("\n".join(edge_tags) + ("\n" if edge_tags else ""))
        + ("\n".join(node_tags) + ("\n" if node_tags else ""))
        + "</svg>\n"
        "<section><h2>变化块路径</h2>\n"
        f'<ul id="paths">{"".join(path_items)}</ul>\n'
        "</section>\n</body>\n</html>\n"
    )
