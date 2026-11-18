"""Export the actual compiled quick-review graph without loading user data."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langgraph.checkpoint.memory import InMemorySaver

from backend.review_graph import NODE_LABELS, WORKFLOW_VERSION, build_review_graph


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "workflow" / "快速审查LangGraph实际结构")
    parser.add_argument("--stdout-json", action="store_true", help="Print the compiled graph JSON without exporting files")
    parser.add_argument("--node", default=shutil.which("node"), help="Optional Node.js path for SVG rendering")
    args = parser.parse_args()
    # Construction does not execute nodes. No app, credentials or model calls
    # are needed to inspect the same compiled graph used by the task runner.
    diagram = build_review_graph(None, InMemorySaver()).get_graph(xray=True)
    value = {
        "workflow_version": WORKFLOW_VERSION,
        "nodes": [{"id": str(key), "label": NODE_LABELS.get(str(key).split(":")[-1], str(node.name))}
                  for key, node in diagram.nodes.items()],
        "edges": [{"source": str(edge.source), "target": str(edge.target),
                   "conditional": bool(edge.conditional),
                   **({"label": str(edge.data)} if edge.data is not None else {})}
                  for edge in diagram.edges],
    }
    if args.stdout_json:
        print(json.dumps(value, ensure_ascii=False))
        return

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix(".mmd").write_text(diagram.draw_mermaid(), encoding="utf-8")
    args.output.with_suffix(".json").write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.node:
        script = """import fs from 'node:fs';
import vm from 'node:vm';
const {workflowGraphSvg,workflowOverviewSvg,workflowAcademicSvg} = await import(process.argv[1]);
const graph=JSON.parse(fs.readFileSync(0, 'utf8'));
const context={structuredClone};vm.runInNewContext(fs.readFileSync(new URL('./vendor/dagre/dagre.min.js',process.argv[1]),'utf8'),context);
const academic=await workflowAcademicSvg(graph,{layoutEngine:context.dagre.default||context.dagre});
process.stdout.write(JSON.stringify({complete:workflowGraphSvg(graph),overview:workflowOverviewSvg(graph),academic}));
"""
        rendered = subprocess.run([args.node, "--input-type=module", "-e", script,
                                   (ROOT / "frontend" / "review-jobs.js").as_uri()],
                                  input=json.dumps(value, ensure_ascii=False), text=True,
                                  encoding="utf-8", capture_output=True, check=True)
        diagrams = json.loads(rendered.stdout)
        args.output.with_suffix(".svg").write_text(diagrams["complete"], encoding="utf-8")
        args.output.with_name(args.output.name + "-总览.svg").write_text(diagrams["overview"], encoding="utf-8")
        args.output.with_name(args.output.name + "-流程图.svg").write_text(diagrams["academic"], encoding="utf-8")
        legacy_diagram = args.output.with_name(args.output.name + "-学术流程图.svg")
        if legacy_diagram.exists():
            legacy_diagram.write_text(diagrams["academic"], encoding="utf-8")
    print(f"Exported actual graph: {len(value['nodes'])} nodes, {len(value['edges'])} edges")
    print(args.output)


if __name__ == "__main__":
    main()
