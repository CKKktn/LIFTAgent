"""Render source-grounded extraction workflow figures with editable exports.

This is an explanatory figure generator. It does not run the application,
load user data, read model settings, or send model requests.
"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "figures" / "quick-review-workflow"
BUILD = OUT / "_build"
sys.path.insert(0, str(BUILD / "python-libs"))
os.environ.setdefault("MPLCONFIGDIR", str(BUILD / "matplotlib-config"))

import matplotlib as mpl
mpl.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch
from matplotlib.path import Path as MplPath

SKILL = Path.home() / ".codex" / "skills" / "nature-figure"
sys.path.insert(0, str(SKILL / "scripts"))
from audit_panel_alignment import require_matplotlib_panel_alignment


# Figure contract: show how the implemented extraction algorithm turns technical
# source text into a traceable, human-confirmed checklist. Single-canvas workflow;
# no quantitative claims or invented performance results. Python-only rendering.
W, H = 1800.0, 1012.5
PALETTE = {
    "ink": "#19324B", "muted": "#647588", "line": "#7A8B9B",
    "rule": "#446784", "rule_bg": "#F5F8FC",
    "llm": "#71558E", "llm_bg": "#F4F0F8",
    "human": "#28796F", "human_bg": "#EDF7F3",
    "warn": "#A76D38", "warn_bg": "#FBF6ED", "border": "#C5D1DC",
}
font_manager.fontManager.addfont("C:/Windows/Fonts/msyh.ttc")
font_manager.fontManager.addfont("C:/Windows/Fonts/msyhbd.ttc")
FONT = font_manager.FontProperties(fname="C:/Windows/Fonts/msyh.ttc")
BOLD = font_manager.FontProperties(fname="C:/Windows/Fonts/msyhbd.ttc")
mpl.rcParams.update({
    "font.family": "sans-serif", "font.sans-serif": ["Microsoft YaHei", "Arial", "DejaVu Sans"],
    "font.size": 11, "svg.fonttype": "none", "pdf.fonttype": 42,
    "axes.unicode_minus": False, "savefig.facecolor": "white", "figure.facecolor": "white",
})


class Canvas:
    def __init__(self):
        self.fig, self.ax = plt.subplots(figsize=(40 / 3, 7.5))
        self.fig.subplots_adjust(left=0, right=1, bottom=0, top=1)
        self.ax.set(xlim=(0, W), ylim=(0, H))
        self.ax.set_axis_off()
        self.nodes, self.edges, self.labels, self.text_checks = [], [], [], []

    def text(self, x, y, value, size=11, color=None, bold=False, ha="center", owner=None):
        artist = self.ax.text(x, y, value, fontsize=size, fontproperties=BOLD if bold else FONT,
                              color=color or PALETTE["ink"], ha=ha, va="center", linespacing=1.55,
                              zorder=5, clip_on=False)
        self.labels.append({"x": x, "y": y, "value": value, "size": size,
                            "color": color or PALETTE["ink"], "bold": bold, "align": ha})
        self.text_checks.append((artist, owner))
        return artist

    def node(self, identifier, x, y, w, h, title, lines, family="rule"):
        color, fill = PALETTE[family], PALETTE[family + "_bg"]
        patch = FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=8",
                               facecolor=fill, edgecolor=color, linewidth=1.15, zorder=3)
        self.ax.add_patch(patch)
        self.nodes.append({"id": identifier, "x": x, "y": y, "w": w, "h": h,
                           "title": title, "lines": lines, "family": family})
        owner = (x + 10, y + 7, x + w - 10, y + h - 7)
        self.text(x + w/2, y + h - 29, title, 13.6, color, True, owner=owner)
        line_y = y + h - 68
        for line in lines:
            self.text(x + w/2, line_y, line, 10.65, owner=owner)
            line_y -= 25

    def arrow(self, identifier, points, dashed=False, color=None):
        path = MplPath(points, [MplPath.MOVETO] + [MplPath.LINETO] * (len(points)-1))
        arrow = FancyArrowPatch(path=path, arrowstyle="-|>", mutation_scale=10,
                                linewidth=1.25 if not dashed else 1.0,
                                linestyle=(0, (4, 3)) if dashed else "solid",
                                color=color or PALETTE["line"], zorder=2)
        self.ax.add_patch(arrow)
        self.edges.append({"id": identifier, "points": points, "dashed": dashed,
                           "color": color or PALETTE["line"]})

    def validate_text_fit(self):
        self.fig.canvas.draw()
        renderer = self.fig.canvas.get_renderer()
        problems = []
        for artist, owner in self.text_checks:
            bbox = artist.get_window_extent(renderer).transformed(self.ax.transData.inverted())
            if bbox.x0 < 10 or bbox.y0 < 5 or bbox.x1 > W-10 or bbox.y1 > H-5:
                problems.append({"text": artist.get_text(), "reason": "outside figure"})
            if owner and (bbox.x0 < owner[0] or bbox.y0 < owner[1] or bbox.x1 > owner[2] or bbox.y1 > owner[3]):
                problems.append({"text": artist.get_text(), "reason": "outside node"})
        if problems:
            raise ValueError(json.dumps(problems, ensure_ascii=False))
        return {"text_runs": len(self.text_checks), "outside_figure_or_node": 0}

    def drawio(self, path):
        root = ET.Element("mxfile", {"host": "app.diagrams.net", "version": "24.7.17", "type": "device"})
        diagram = ET.SubElement(root, "diagram", {"id": "extraction", "name": "指标与结题条件提取"})
        model = ET.SubElement(diagram, "mxGraphModel", {"dx": "1800", "dy": "1013", "grid": "1",
            "gridSize": "10", "page": "1", "pageWidth": str(W), "pageHeight": str(H), "pageScale": "1"})
        cells = ET.SubElement(model, "root")
        ET.SubElement(cells, "mxCell", {"id": "0"})
        ET.SubElement(cells, "mxCell", {"id": "1", "parent": "0"})
        # Independent native vertices and native orthogonal edges remain editable.
        for n in self.nodes:
            value = '<b style="font-size:25px">' + html.escape(n["title"]) + "</b><br><br>"
            value += '<span style="font-size:20px">' + "<br>".join(html.escape(t) for t in n["lines"]) + "</span>"
            style = ("rounded=1;arcSize=6;whiteSpace=wrap;html=1;align=center;verticalAlign=middle;"
                "fontFamily=Microsoft YaHei;spacing=12;strokeWidth=2;fillColor=" + PALETTE[n["family"]+"_bg"] +
                ";strokeColor=" + PALETTE[n["family"]] + ";fontColor=" + PALETTE["ink"] + ";")
            cell = ET.SubElement(cells, "mxCell", {"id": n["id"], "value": value, "style": style, "vertex": "1", "parent": "1"})
            ET.SubElement(cell, "mxGeometry", {"x": str(n["x"]), "y": str(H-n["y"]-n["h"]),
                "width": str(n["w"]), "height": str(n["h"]), "as": "geometry"})
        for e in self.edges:
            style = "endArrow=block;endFill=1;html=1;rounded=0;strokeWidth=2;strokeColor=" + e["color"] + ";"
            if e["dashed"]:
                style += "dashed=1;dashPattern=5 4;"
            connections = {
                "e1": ("text", "batch", 1, 0.5, 0, 0.5),
                "e2": ("batch", "prompt", 1, 0.5, 0, 0.5),
                "e3": ("prompt", "llm", 1, 0.5, 0, 0.5),
                "e4": ("llm", "parse", 0.5, 1, 0.5, 0),
                "e5": ("parse", "source", 0, 0.5, 1, 0.5),
                "e6": ("source", "filter", 0, 0.5, 1, 0.5),
                "e7": ("filter", "dedup", 0, 0.5, 1, 0.5),
                "e8": ("dedup", "question", 0, 0.5, 0, 0.5),
                "e9": ("question", "human", 1, 0.5, 0, 0.5),
                "e10": ("human", "output", 1, 0.5, 0, 0.5),
                "retry": ("parse", "llm", 1, 0.5, 1, 0.5),
            }
            attributes = {"id": e["id"], "style": style, "edge": "1", "parent": "1"}
            if e["id"] in connections:
                src, dst, ex, ey, ix, iy = connections[e["id"]]
                attributes.update({"source": src, "target": dst})
                attributes["style"] += f"exitX={ex};exitY={ey};entryX={ix};entryY={iy};"
            cell = ET.SubElement(cells, "mxCell", attributes)
            geom = ET.SubElement(cell, "mxGeometry", {"relative": "1", "as": "geometry"})
            points = e["points"]
            ET.SubElement(geom, "mxPoint", {"x": str(points[0][0]), "y": str(H-points[0][1]), "as": "sourcePoint"})
            ET.SubElement(geom, "mxPoint", {"x": str(points[-1][0]), "y": str(H-points[-1][1]), "as": "targetPoint"})
            if len(points) > 2:
                middle = ET.SubElement(geom, "Array", {"as": "points"})
                for x, y in points[1:-1]:
                    ET.SubElement(middle, "mxPoint", {"x": str(x), "y": str(H-y)})
        # Non-node headings, branch annotations and footnotes are editable text.
        for index, label in enumerate(self.labels):
            if any(label["value"] == n["title"] or label["value"] in n["lines"] for n in self.nodes):
                continue
            width = min(1680, max(180, len(label["value"])*label["size"]*1.8))
            height = max(35, 28 * (label["value"].count("\n")+1))
            left = label["x"] if label["align"] == "left" else label["x"] - width/2
            style = ("text;html=1;strokeColor=none;fillColor=none;whiteSpace=wrap;verticalAlign=middle;"
                f"align={label['align']};fontFamily=Microsoft YaHei;fontSize={label['size']*1.875};"
                f"fontColor={label['color']};fontStyle={1 if label['bold'] else 0};")
            cell = ET.SubElement(cells, "mxCell", {"id": f"label-{index}", "value": html.escape(label["value"]).replace("\n", "<br>"),
                "style": style, "vertex": "1", "parent": "1"})
            ET.SubElement(cell, "mxGeometry", {"x": str(left), "y": str(H-label["y"]-height/2),
                "width": str(width), "height": str(height), "as": "geometry"})
        ET.indent(root, space="  ")
        ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)

    def save(self, stem):
        fit_report = self.validate_text_fit()
        alignment = require_matplotlib_panel_alignment(self.fig,
            json_out=BUILD / (stem + ".alignment.json"), tolerance_pt=1.5, strict=True)
        self.fig.savefig(OUT / f"{stem}.svg", facecolor="white")
        self.fig.savefig(OUT / f"{stem}.pdf", facecolor="white")
        self.fig.savefig(OUT / (stem + ".png"), dpi=600, facecolor="white")
        self.fig.savefig(BUILD / (stem + ".preview.png"), dpi=300, facecolor="white")
        self.drawio(OUT / (stem + ".drawio"))
        (BUILD / (stem + ".geometry.json")).write_text(json.dumps({"nodes": self.nodes, "edges": self.edges,
            "labels": self.labels, "text_fit": fit_report, "alignment": alignment["verdict"]}, ensure_ascii=False, indent=2), encoding="utf-8")
        plt.close(self.fig)
        print(stem + ": rendered", flush=True)


def make_figure(detail=False):
    c = Canvas()
    c.text(60, 958, "技术指标与结题条件提取工作流", 26, bold=True, ha="left")
    subtitle = "段落级来源锚定、约束式结构化提取与人机协同确认"
    if detail:
        subtitle = "算法详图：处理粒度、校验规则、有限重试与业务状态保存"
    c.text(60, 916, subtitle, 12, PALETTE["muted"], ha="left")
    c.text(60, 850, "01   文本组织与约束式提取", 12.8, bold=True, ha="left")
    c.text(60, 575, "02   结果校验与可追溯建模", 12.8, bold=True, ha="left")
    c.text(60, 290, "03   人机协同确认与清单输出", 12.8, bold=True, ha="left")

    text_lines = ["合同／技术协议及已有解析", "LangChain Document · 来源保留"]
    batch_lines = ["P1…Pn 原文段落编号", "按段落切分并组织输入批次"]
    prompt_lines = ["明确技术范围与核查对象", "约束输出字段及来源编号"]
    llm_lines = ["ChatOpenAI · 异步流式调用", "输出指标与结题条件 JSON"]
    parse_lines = ["解析 JSON 并检查输出数组", "调用或解析异常进入有限重试"]
    source_lines = ["按 P 编号回填真实原文", "核验对象、标题、条件及类型"]
    filter_lines = ["数值指标 · 功能条件", "技术交付物（文档存在）"]
    dedup_lines = ["类型、对象、条件联合去重", "生成 R001…Rn 条目编号"]
    question_lines = ["按类型生成专用核查问句", "保留数值、条件与核查边界"]
    human_lines = ["修改、增删与人工补充要求", "确认范围并同步更新核查问句"]
    output_lines = ["条目编号 · 核查对象 · 类型 · 达标条件", "筛选理由 · 依据原文 · 类型化核查问句"]
    if detail:
        text_lines = ["复用已解析切片／提取正文", "Document 与文件来源元数据"]
        batch_lines = ["每段≤600 字符并分配 P 编号", "每批≤6000 字符，顺序处理"]
        prompt_lines = ["限定数值、功能与技术文档", "保留量值、单位、方向与条件"]
        llm_lines = ["ChatOpenAI.astream()", "单批 max_tokens=4096"]
        parse_lines = ["technical_indicators 数组", "closure_conditions 数组"]
        source_lines = ["source_ids 映射与原文回填", "核验对象、条件、类型及理由"]
        filter_lines = ["排除商务、财务与行政事项", "数值／功能／技术文档三类"]
        dedup_lines = ["类型、对象、条件联合去重", "R 编号、来源与依据绑定"]
        question_lines = ["数值比较／功能实现／文件存在", "逐项生成 question 字段"]
        human_lines = ["修订类别、条件及纳入范围", "重编号、更新问句并确认"]
        output_lines = ["id · title · subject · criterion · type", "rationale · source · quote · question"]

    c.node("text", 60, 675, 330, 125, "依据文本准备", text_lines)
    c.node("batch", 450, 675, 330, 125, "段落锚定与批次构建", batch_lines)
    c.node("prompt", 840, 675, 330, 125, "技术语义约束提示词", prompt_lines, "llm")
    c.node("llm", 1230, 675, 330, 125, "LangChain 模型调用", llm_lines, "llm")
    c.node("parse", 1230, 405, 330, 135, "JSON 解析与结构检查", parse_lines)
    c.node("source", 840, 405, 330, 135, "原文回填与字段校验", source_lines)
    c.node("filter", 450, 405, 330, 135, "技术范围筛选与分类", filter_lines)
    c.node("dedup", 60, 405, 330, 135, "去重与条目规范化", dedup_lines)
    c.node("question", 60, 120, 330, 135, "类型化核查问句生成", question_lines)
    c.node("human", 450, 120, 330, 135, "人工确认与范围修订", human_lines, "human")
    c.node("output", 840, 120, 720, 135, "已确认的指标与结题条件清单", output_lines, "human")

    c.arrow("e1", [(392, 737.5), (448, 737.5)])
    c.arrow("e2", [(782, 737.5), (838, 737.5)])
    c.arrow("e3", [(1172, 737.5), (1228, 737.5)])
    c.arrow("e4", [(1395, 673), (1395, 542)])
    c.arrow("e5", [(1228, 472.5), (1172, 472.5)])
    c.arrow("e6", [(838, 472.5), (782, 472.5)])
    c.arrow("e7", [(448, 472.5), (392, 472.5)])
    c.arrow("e8", [(58, 472.5), (28, 472.5), (28, 187.5), (58, 187.5)])
    c.arrow("e9", [(392, 187.5), (448, 187.5)])
    c.arrow("e10", [(782, 187.5), (838, 187.5)])
    c.arrow("retry", [(1562, 472.5), (1730, 472.5), (1730, 737.5), (1562, 737.5)],
            dashed=True, color=PALETTE["warn"])
    c.text(1645, 645, "可拆分批次\n二分重试\n仅拆分一层", 10.8, PALETTE["warn"])
    c.text(1415, 612, "模型响应", 9.8, PALETTE["muted"], ha="left")

    # Invalid records do not automatically trigger another full model pass.
    # They are isolated, valid records survive, and users can supplement them.
    c.arrow("invalid", [(1005, 403), (1005, 365)], dashed=True, color=PALETTE["warn"])
    warning_text = "无效项隔离，保留有效项并记录异常，供人工核对与补充"
    if detail:
        warning_text = "无效记录隔离；持续失败保留有效项并记入 issues；无有效条目时终止并提示"
    c.text(1200, 345, warning_text, 10.0, PALETTE["warn"])

    c.text(60, 76, "技术组件：LangChain Document、ChatOpenAI；格式解析、JSON 校验、模板问句与业务状态保存", 9.8,
           PALETTE["muted"], ha="left")
    c.text(60, 47, "实现范围：本图对应快速审查提取模块。LangGraph 的检索—生成图用于系统人机协审分支。", 9.5,
           PALETTE["muted"], ha="left")
    note = "实线表示处理顺序，虚线表示异常处理；人工补充项保留标记。"
    if detail:
        note = "过程状态：extractedParts 保存完成批次，splitParts 记录拆分批次；已完成批次可跳过。"
    c.text(60, 24, note, 9.5, PALETTE["muted"], ha="left")
    return c


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    BUILD.mkdir(parents=True, exist_ok=True)
    make_figure().save("01_指标与结题条件提取_汇报版")
    make_figure(detail=True).save("02_指标与结题条件提取_算法详版")


if __name__ == "__main__":
    main()
