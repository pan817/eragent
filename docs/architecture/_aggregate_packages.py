"""一次性脚本：把 pyreverse 78 模块的扁平图聚合成顶层包级依赖图。

读取 packages_eragent.dot（pyreverse 产出），按"前 2 段路径"聚合
（如 core.orchestrator.dag.executor → core.orchestrator），
输出 packages_aggregated.dot 用于人类阅读。

仅作为本目录架构图生成的辅助工具，不在生产代码路径里。
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

INPUT = Path(__file__).parent / "packages_eragent.dot"
OUTPUT_2 = Path(__file__).parent / "packages_aggregated.dot"
OUTPUT_1 = Path(__file__).parent / "packages_toplevel.dot"


def aggregate_name(full: str, depth: int = 2) -> str:
    """core.orchestrator.dag.executor → core.orchestrator (depth=2)。"""
    parts = full.split(".")
    return ".".join(parts[:depth])


def parse(text: str, depth: int) -> tuple[set[str], dict[tuple[str, str], int]]:
    nodes: set[str] = set()
    edges: dict[tuple[str, str], int] = defaultdict(int)
    node_re = re.compile(r'^"([^"]+)"\s*\[(?:.*color="([^"]+)")?')
    edge_re = re.compile(r'^"([^"]+)"\s*->\s*"([^"]+)"')

    for line in text.splitlines():
        m = node_re.match(line.strip())
        if m and "->" not in line:
            agg = aggregate_name(m.group(1), depth)
            nodes.add(agg)
            continue
        m = edge_re.match(line.strip())
        if m:
            src = aggregate_name(m.group(1), depth)
            tgt = aggregate_name(m.group(2), depth)
            if src != tgt:
                edges[(src, tgt)] += 1
    return nodes, edges


def main() -> None:
    text = INPUT.read_text(encoding="utf-8")

    nodes, edges = parse(text, depth=2)
    nodes_top, edges_top = parse(text, depth=1)

    # 为顶层包定色（覆盖 pyreverse 给的随机色）
    color_map = {
        "api": "#7FA7D4",          # 蓝
        "config": "#D4B886",        # 米
        "core.database": "#AAAAAA",
        "core.memory": "#AAAAAA",
        "core.observability": "#AAAAAA",
        "core.knowledge": "#AAAAAA",
        "core.ontology": "#AAAAAA",
        "core.chat": "#AAAAAA",
        "core.tasks": "#C9A96E",    # 异步基础设施 — 黄
        "core.orchestrator": "#C9A96E",  # 编排 — 黄
        "core": "#AAAAAA",
        "modules.p2p": "#8EB56E",   # P2P 业务 — 绿
        "modules": "#8EB56E",
    }

    def write_dot(out: Path, nodes_: set[str], edges_: dict[tuple[str, str], int],
                  title: str) -> None:
        with out.open("w", encoding="utf-8") as f:
            f.write(f'digraph "{out.stem}" {{\n')
            f.write('rankdir=TB\n')
            f.write('charset="utf-8"\n')
            f.write('nodesep=0.4\n')
            f.write('ranksep=0.7\n')
            f.write(
                f'graph [fontname="Helvetica", fontsize=14, labelloc="t", label="{title}"]\n'
            )
            f.write('node [fontname="Helvetica", fontsize=11, '
                    'shape=box, style="rounded,filled"]\n')
            f.write('edge [fontname="Helvetica", fontsize=9, color="#555555"]\n\n')

            for n in sorted(nodes_):
                color = color_map.get(n, "#DDDDDD")
                f.write(f'  "{n}" [fillcolor="{color}"];\n')

            f.write('\n')
            for (src, tgt), count in sorted(edges_.items()):
                penwidth = min(1.0 + count * 0.25, 5.0)
                label = f' [label="{count}", penwidth={penwidth:.1f}]'
                f.write(f'  "{src}" -> "{tgt}"{label};\n')

            f.write('}\n')

    write_dot(
        OUTPUT_2, nodes, edges,
        title=f"eragent — pyreverse 聚合 (depth=2,{len(nodes)} 包,{sum(edges.values())} import)"
    )
    write_dot(
        OUTPUT_1, nodes_top, edges_top,
        title=f"eragent — pyreverse 聚合 (depth=1 顶层,{len(nodes_top)} 包,{sum(edges_top.values())} import)"
    )

    print(f"wrote {OUTPUT_2.name}: {len(nodes)} packages, {sum(edges.values())} imports")
    print(f"wrote {OUTPUT_1.name}: {len(nodes_top)} packages, {sum(edges_top.values())} imports")


if __name__ == "__main__":
    main()
