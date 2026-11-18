"""Render the implemented extraction decisions as a branching workflow.

The branches describe business control flow. They do not claim that the
extraction procedure is currently scheduled by LangGraph.
"""

from __future__ import annotations

import html
import json
import xml.etree.ElementTree as ET

from tools import draw_extraction_workflow as base
from matplotlib.patches import Polygon

base.mpl.rcParams.update({
    "font.family": "sans-serif", "font.sans-serif": ["Microsoft YaHei", "Arial"],
    "font.size": 11, "svg.fonttype": "none", "pdf.fonttype": 42,
})


class BranchCanvas(base.Canvas):
    def __init__(self):
        super().__init__()
        self.connections = {}

    def decision(self, identifier, x, y, w, h, title, family="rule", size=12.4):
        color = base.PALETTE[family]
        self.ax.add_patch(Polygon([(x+w/2, y), (x+w, y+h/2), (x+w/2, y+h), (x, y+h/2)],
            closed=True, facecolor=base.PALETTE[family+"_bg"], edgecolor=color, linewidth=1.2, zorder=3))
        self.nodes.append({"id": identifier, "x": x, "y": y, "w": w, "h": h,
            "title": title, "lines": [], "family": family, "shape": "rhombus"})
        self.text(x+w/2, y+h/2, title, size, color, True,
                  owner=(x+32, y+12, x+w-32, y+h-12))

    def link(self, identifier, source, target, points, anchors, dashed=False, family=None):
        self.arrow(identifier, points, dashed=dashed, color=base.PALETTE[family] if family else None)
        self.connections[identifier] = (source, target, anchors)

    def drawio(self, path):
        super().drawio(path)
        tree = ET.parse(path)
        root = tree.getroot()
        cells = {cell.get("id"): cell for cell in root.findall(".//mxCell")}
        for node in self.nodes:
            if node.get("shape") == "rhombus":
                cell = cells[node["id"]]
                cell.set("style", cell.get("style").replace("rounded=1;arcSize=6;", "rhombus;"))
                cell.set("value", '<b style="font-size:23px">' + html.escape(node["title"]).replace("\n", "<br>") + "</b>")
        for identifier, (source, target, anchors) in self.connections.items():
            ex, ey, ix, iy = anchors
            cell = cells[identifier]
            cell.set("source", source)
            cell.set("target", target)
            cell.set("style", cell.get("style") + f"exitX={ex};exitY={ey};entryX={ix};entryY={iy};")
        ET.indent(root, space="  ")
        tree.write(path, encoding="utf-8", xml_declaration=True)

    def save(self, stem):
        fit = self.validate_text_fit()
        alignment = base.require_matplotlib_panel_alignment(self.fig,
            json_out=base.BUILD / (stem+".alignment.json"), tolerance_pt=1.5, strict=True)
        self.fig.savefig(base.OUT / f"{stem}.svg", facecolor="white")
        self.fig.savefig(base.OUT / f"{stem}.pdf", facecolor="white")
        self.fig.savefig(base.OUT / f"{stem}.png", dpi=600, facecolor="white")
        self.fig.savefig(base.BUILD / f"{stem}.preview.png", dpi=300, facecolor="white")
        self.drawio(base.OUT / f"{stem}.drawio")
        (base.BUILD / f"{stem}.geometry.json").write_text(json.dumps({
            "nodes": self.nodes, "edges": self.edges, "connections": self.connections,
            "text_fit": fit, "alignment": alignment["verdict"],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        base.plt.close(self.fig)
        print(stem+": rendered", flush=True)


def make_figure():
    c = BranchCanvas()
    p = base.PALETTE
    c.text(60, 964, "技术指标与结题条件提取：条件分支工作流", 25, bold=True, ha="left")
    c.text(60, 923, "展开结构校验、有限重试、类型选路与人工确认；各分支依据条件触发", 11.8, p["muted"], ha="left")
    c.text(60, 890, "提取与质量控制", 12.5, bold=True, ha="left")
    c.text(1060, 868, "按条目类型选择处理路径", 16, bold=True, ha="left")
    c.text(1060, 830, "每条要求进入对应问句模板，结果汇总后人工确认", 10.8, p["muted"], ha="left")

    c.node("input", 60, 765, 410, 100, "依据读取与分批", ["段落编号与分批；逐批执行"])
    c.node("llm", 60, 575, 410, 125, "LangChain 约束式提取", ["ChatOpenAI · 输出指标与结题条件", "提示词限定字段、范围与来源编号"], "llm")
    c.decision("json", 105, 400, 320, 120, "JSON 结构\n是否有效？")
    c.decision("retry", 570, 400, 250, 120, "可继续\n二分拆批？", "warn")
    c.node("split", 550, 575, 300, 125, "二分批次重试", ["仅对可拆分批次", "最多拆分一层"], "warn")
    c.node("preserve", 550, 225, 300, 130, "保留有效项与异常", ["无法再拆分或仍然失败", "记录问题，允许人工补充"], "warn")
    c.node("validate", 60, 150, 410, 180, "字段、来源与范围校验", ["无效项隔离，原文回填", "技术范围过滤与联合去重", "保留有效条目及来源依据"])
    c.decision("available", 785, 715, 230, 130, "有有效条目？", size=11.6)
    c.node("stop", 520, 785, 230, 70, "无有效条目", [], "warn")
    c.text(635, 800, "提示检查材料", 9.4, p["warn"])
    c.decision("type", 1200, 570, 290, 130, "条目类型", size=13)
    c.node("numeric", 950, 315, 230, 125, "数值指标问句", ["参数／单位／方向", "及适用条件"])
    c.node("functional", 1230, 315, 230, 125, "功能条件问句", ["功能实现与依据", "区分要求和已实现"])
    c.node("document", 1510, 315, 230, 125, "技术文档问句", ["指定技术交付物", "是否存在对应文件"])
    c.node("confirmed", 1020, 100, 650, 140, "人工确认与结构化清单输出", [
        "修改、增删与补充条目，同步更新编号和问句", "输出数值指标、功能条件与技术交付物清单"], "human")

    c.link("b01", "input", "llm", [(265, 763), (265, 702)], (.5, 1, .5, 0))
    c.link("b02", "llm", "json", [(265, 573), (265, 522)], (.5, 1, .5, 0))
    c.link("b03", "json", "validate", [(265, 398), (265, 332)], (.5, 1, .5, 0))
    c.text(307, 365, "通过", 10.3, p["muted"])
    c.link("b04", "json", "retry", [(427, 460), (568, 460)], (1, .5, 0, .5), family="warn")
    c.text(495, 484, "失败", 10.3, p["warn"])
    c.link("b05", "retry", "split", [(695, 522), (695, 573)], (.5, 0, .5, 1), family="warn")
    c.text(735, 550, "是", 10.3, p["warn"])
    c.link("b06", "split", "llm", [(548, 637.5), (472, 637.5)], (0, .5, 1, .5), dashed=True, family="warn")
    c.text(509, 668, "重试", 9.7, p["warn"])
    c.link("b07", "retry", "preserve", [(695, 398), (695, 357)], (.5, 1, .5, 0), family="warn")
    c.text(735, 378, "否", 10.3, p["warn"])
    c.link("b08", "preserve", "validate", [(548, 290), (472, 290)], (0, .5, 1, .22), family="warn")
    c.link("b09", "validate", "available", [(472, 180), (900, 180), (900, 713)], (1, .833, .5, 1))
    c.text(655, 200, "汇总有效条目", 10.3, p["muted"])
    c.link("b10", "available", "stop", [(783, 780), (765, 780), (765, 820), (752, 820)], (0, .5, 1, .5), family="warn")
    c.text(735, 755, "否", 10.3, p["warn"])
    c.link("b11", "available", "type", [(1017, 780), (1345, 780), (1345, 702)], (1, .5, .5, 0))
    c.text(1090, 804, "是", 10.3, p["muted"])
    c.link("b12", "type", "numeric", [(1198, 635), (1065, 635), (1065, 442)], (0, .5, .5, 0))
    c.link("b13", "type", "functional", [(1345, 568), (1345, 442)], (.5, 1, .5, 0))
    c.link("b14", "type", "document", [(1492, 635), (1625, 635), (1625, 442)], (1, .5, .5, 0))
    c.text(1110, 510, "数值", 10.3, p["rule"])
    c.text(1390, 510, "功能", 10.3, p["rule"])
    c.text(1670, 510, "文档", 10.3, p["rule"])
    c.link("b15", "numeric", "confirmed", [(1065, 313), (1065, 275), (1345, 275), (1345, 242)], (.5, 1, .5, 0))
    c.link("b16", "functional", "confirmed", [(1345, 313), (1345, 242)], (.5, 1, .5, 0))
    c.link("b17", "document", "confirmed", [(1625, 313), (1625, 275), (1345, 275), (1345, 242)], (.5, 1, .5, 0))
    c.text(1480, 295, "逐项汇总", 10.1, p["muted"])
    c.text(60, 57, "菱形表示条件判断；类型分支为逐条选路，不表示三个模型并行运行。", 9.6, p["muted"], ha="left")
    c.text(60, 28, "当前提取模块由业务代码编排；LangGraph 已实现的检索—生成工作流属于系统人机协审分支。", 9.5, p["muted"], ha="left")
    return c


if __name__ == "__main__":
    make_figure().save("03_指标与结题条件提取_条件分支修订版")
