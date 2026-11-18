"""Academic explanatory figures grounded in the implemented quick-review graphs.

Only graph construction is used to export topology. No live workspace, model,
account, stored job or source document is read by this drawing program.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
from pathlib import Path
import xml.etree.ElementTree as ET

os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
from tools import draw_extraction_workflow as base
from matplotlib.patches import FancyBboxPatch, Polygon, Rectangle
from matplotlib.path import Path as MplPath
from matplotlib.patches import FancyArrowPatch

base.OUT = base.ROOT / "output" / "figures" / "quick-review-frameworks" / "revision-03"
base.BUILD = base.OUT / "_build"
base.W, base.H = 2400.0, 1350.0
base.mpl.rcParams.update({"font.family": "sans-serif", "font.sans-serif": ["Microsoft YaHei", "Arial"],
    "font.size": 11, "svg.fonttype": "none", "pdf.fonttype": 42})
P = base.PALETTE
P.update({"neutral": "#697B8B", "neutral_bg": "#F8F9FB"})


class AcademicCanvas(base.Canvas):
    def __init__(self):
        self.fig, self.ax = base.plt.subplots(figsize=(16, 9))
        self.fig.subplots_adjust(left=0, right=1, bottom=0, top=1)
        self.ax.set(xlim=(0, base.W), ylim=(0, base.H))
        self.ax.set_axis_off()
        self.nodes, self.edges, self.labels, self.text_checks = [], [], [], []
        self.frames, self.mappings, self.swatches = [], {}, []

    def legend_item(self, identifier, x, y, label, family, size=13):
        self.ax.add_patch(Rectangle((x, y-14), 28, 28,
            facecolor=P[family], edgecolor="none", zorder=3))
        self.swatches.append({"id": identifier, "x": x, "y": y-14,
            "w": 28, "h": 28, "color": P[family]})
        self.text(x+45, y, label, size, P["ink"], True, ha="left")

    def frame(self, identifier, x, y, w, h, title, subtitle="", family="rule"):
        self.ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=8",
            facecolor="white", edgecolor=P["border"], linewidth=.9, zorder=0))
        self.text(x+24, y+h-32, title, 14.4, P[family], True, ha="left")
        if subtitle:
            self.text(x+24, y+h-66, subtitle, 9.8, P["muted"], ha="left")
        self.frames.append({"id": identifier, "x": x, "y": y, "w": w, "h": h})

    def box(self, identifier, x, y, w, h, title, lines=(), family="rule", title_size=12.2, body_size=9.8, source=(), emphasis=False):
        if h < 100:
            y, h = y-(100-h), 100
        fill = ({"llm": "#EBE2F4", "human": "#E2F1EB"}.get(family, P[family+"_bg"])
            if emphasis else P[family+"_bg"])
        self.ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=5",
            facecolor=fill, edgecolor=P[family], linewidth=1.65 if emphasis else 1.05, zorder=3))
        owner = (x+9, y+6, x+w-9, y+h-6)
        self.text(x+w/2, y+h-30, title, title_size, P[family], True, owner=owner)
        for index, line in enumerate(lines):
            self.text(x+w/2, y+h-75-index*27, line, body_size, owner=owner)
        self.nodes.append({"id": identifier, "x": x, "y": y, "w": w, "h": h, "title": title,
            "lines": list(lines), "family": family, "fill": fill, "emphasis": emphasis,
            "title_size": title_size, "body_size": body_size, "shape": "roundRect"})
        self.mappings[identifier] = list(source)

    def diamond(self, identifier, x, y, w, h, title, family="rule", size=11.2, source=()):
        self.ax.add_patch(Polygon([(x, y+h/2), (x+w/2, y+h), (x+w, y+h/2), (x+w/2, y)],
            closed=True, facecolor=P[family+"_bg"], edgecolor=P[family], linewidth=1.05, zorder=3))
        self.text(x+w/2, y+h/2, title, size, P[family], True, owner=(x+25,y+8,x+w-25,y+h-8))
        self.nodes.append({"id": identifier, "x": x, "y": y, "w": w, "h": h, "title": title,
            "lines": [], "family": family, "title_size": size, "body_size": 10, "shape": "rhombus"})
        self.mappings[identifier] = list(source)

    def edge(self, identifier, source, target, points, family=None, dashed=False, association=False, width=None):
        color = P[family] if family else P["line"]
        path = MplPath(points, [MplPath.MOVETO]+[MplPath.LINETO]*(len(points)-1))
        self.ax.add_patch(FancyArrowPatch(path=path, arrowstyle="-" if association else "-|>", mutation_scale=9,
            linewidth=width if width is not None else (.9 if dashed or association else 1.15),
            linestyle=(0,(4,3)) if dashed or association else "solid",
            color=color, zorder=2))
        self.edges.append({"id": identifier,"source":source,"target":target,"points":points,
            "color":color,"dashed":dashed or association,"association":association,
            "width": width if width is not None else (.9 if dashed or association else 1.15)})

    def drawio(self, path):
        root = ET.Element("mxfile", {"host":"app.diagrams.net","type":"device"})
        diagram=ET.SubElement(root,"diagram",{"id":"quick-review","name":"快速审查技术工作流"})
        model=ET.SubElement(diagram,"mxGraphModel",{"page":"1","pageWidth":"2400","pageHeight":"1350","grid":"1","gridSize":"10"})
        cells=ET.SubElement(model,"root")
        ET.SubElement(cells,"mxCell",{"id":"0"}); ET.SubElement(cells,"mxCell",{"id":"1","parent":"0"})
        for frame in self.frames:
            cell=ET.SubElement(cells,"mxCell",{"id":"frame-"+frame["id"],"vertex":"1","parent":"1",
                "style":"rounded=1;arcSize=3;fillColor=#FFFFFF;strokeColor="+P["border"]+";strokeWidth=1;"})
            ET.SubElement(cell,"mxGeometry",{"x":str(frame["x"]),"y":str(base.H-frame["y"]-frame["h"]),"width":str(frame["w"]),"height":str(frame["h"]),"as":"geometry"})
        for s in self.swatches:
            cell=ET.SubElement(cells,"mxCell",{"id":s["id"],"vertex":"1","parent":"1",
                "style":"shape=rectangle;strokeColor=none;fillColor="+s["color"]+";"})
            ET.SubElement(cell,"mxGeometry",{"x":str(s["x"]),"y":str(base.H-s["y"]-s["h"]),
                "width":str(s["w"]),"height":str(s["h"]),"as":"geometry"})
        for n in self.nodes:
            scale=1/.48
            title=html.escape(n["title"]).replace("\n","<br>")
            value=f'<b style="font-size:{n["title_size"]*scale}px">{title}</b>'
            if n["lines"]:
                value+='<br><br><span style="font-size:'+str(n["body_size"]*scale)+'px">'+"<br>".join(html.escape(s) for s in n["lines"])+"</span>"
            style=("shape="+n["shape"]+";rounded=1;arcSize=4;html=1;whiteSpace=wrap;align=center;verticalAlign=middle;spacing=9;"
                "fontFamily=Microsoft YaHei;strokeWidth="+str(3.4 if n.get("emphasis") else 2)+";fillColor="+n.get("fill",P[n["family"]+"_bg"])+";strokeColor="+P[n["family"]]+";fontColor="+P["ink"]+";")
            cell=ET.SubElement(cells,"mxCell",{"id":n["id"],"value":value,"style":style,"vertex":"1","parent":"1"})
            ET.SubElement(cell,"mxGeometry",{"x":str(n["x"]),"y":str(base.H-n["y"]-n["h"]),"width":str(n["w"]),"height":str(n["h"]),"as":"geometry"})
        node_ids={n["id"] for n in self.nodes}
        node_lookup={n["id"]:n for n in self.nodes}
        for e in self.edges:
            style="html=1;rounded=0;strokeWidth="+str(e["width"]/.48)+";endArrow="+("none" if e["association"] else "block")+";strokeColor="+e["color"]+";"
            if e["dashed"]: style+="dashed=1;dashPattern=5 4;"
            attr={"id":e["id"],"style":style,"edge":"1","parent":"1"}
            for endpoint, point, anchor in (("source",e["points"][0],"exit"),("target",e["points"][-1],"entry")):
                if e[endpoint] in node_ids:
                    attr[endpoint]=e[endpoint]
                    n=node_lookup[e[endpoint]]
                    ax=max(0,min(1,(point[0]-n["x"])/n["w"]))
                    ay=max(0,min(1,1-(point[1]-n["y"])/n["h"]))
                    attr["style"]+=f"{anchor}X={ax};{anchor}Y={ay};{anchor}Perimeter=0;"
            cell=ET.SubElement(cells,"mxCell",attr)
            geom=ET.SubElement(cell,"mxGeometry",{"relative":"1","as":"geometry"})
            pts=e["points"]
            for point,kind in ((pts[0],"sourcePoint"),(pts[-1],"targetPoint")):
                ET.SubElement(geom,"mxPoint",{"x":str(point[0]),"y":str(base.H-point[1]),"as":kind})
            if len(pts)>2:
                middle=ET.SubElement(geom,"Array",{"as":"points"})
                for x,y in pts[1:-1]: ET.SubElement(middle,"mxPoint",{"x":str(x),"y":str(base.H-y)})
        for i,l in enumerate(self.labels):
            if any(l["value"]==n["title"] or l["value"] in n["lines"] for n in self.nodes): continue
            width=min(2250,max(100,len(l["value"])*l["size"]*2.1)); height=34*(l["value"].count("\n")+1)
            left=l["x"] if l["align"]=="left" else l["x"]-width if l["align"]=="right" else l["x"]-width/2
            cell=ET.SubElement(cells,"mxCell",{"id":f"label-{i}","value":html.escape(l["value"]).replace("\n","<br>"),
                "vertex":"1","parent":"1","style":f'text;html=1;strokeColor=none;fillColor=none;whiteSpace=wrap;align={l["align"]};fontFamily=Microsoft YaHei;fontSize={l["size"]/.48};fontColor={l["color"]};fontStyle={1 if l["bold"] else 0};'})
            ET.SubElement(cell,"mxGeometry",{"x":str(left),"y":str(base.H-l["y"]-height/2),"width":str(width),"height":str(height),"as":"geometry"})
        ET.indent(root,space="  "); ET.ElementTree(root).write(path,encoding="utf-8",xml_declaration=True)

    def save(self, stem):
        fit=self.validate_text_fit()
        alignment=base.require_matplotlib_panel_alignment(self.fig,json_out=base.BUILD/(stem+".alignment.json"),strict=True)
        self.fig.savefig(base.OUT/f"{stem}.svg",facecolor="white")
        self.fig.savefig(base.OUT/f"{stem}.pdf",facecolor="white")
        self.fig.savefig(base.OUT/f"{stem}.png",dpi=600,facecolor="white")
        self.fig.savefig(base.BUILD/f"{stem}.preview.png",dpi=300,facecolor="white")
        self.drawio(base.OUT/f"{stem}.drawio")
        (base.BUILD/(stem+".mapping.json")).write_text(json.dumps({"nodes":self.nodes,"edges":self.edges,
            "source_mapping":self.mappings,"text_fit":fit,"alignment":alignment["verdict"]},ensure_ascii=False,indent=2),encoding="utf-8")
        base.plt.close(self.fig); print(stem+": rendered",flush=True)


def graph_figure():
    c=AcademicCanvas()
    c.text(60,1295,"基于 LangGraph 的快速审查流程编排与状态管理",25.5,bold=True,ha="left")
    c.text(60,1248,"阶段化编排、类型路由与持久状态管理；A–E 标识 LangChain 执行模块",12,P["muted"],ha="left")
    c.text(2340,1248,"已实现 · quick-review-v1",10.2,P["muted"],ha="right")
    c.text(60,1212,"快速审查总体流程",10.6,P["rule"],True,ha="left")
    parent=[("root-init",70,300,"固定输入与版本","资料／模型配置签名",["initialize"]),
        ("root-extract",430,320,"指标与结题条件提取","extract_requirements",["extract_requirements"]),
        ("root-human",810,330,"人工确认与版本复核","interrupt / Command",["await_confirmation","validate_confirmation"]),
        ("root-check",1200,330,"按类型逐条核查","check_requirements",["check_requirements"]),
        ("root-report",1590,330,"原始报告与展示整理","report_workflow",["report_workflow"]),
        ("root-end",1980,330,"结束／异常项重试","END / await_retry",["__end__","await_retry","restart_retry"])]
    for i,(key,x,w,title,line,mapping) in enumerate(parent):
        c.box(key,x,1095,w,95,title,[line],"human" if key=="root-human" else "rule",source=mapping)
        if i: c.edge("parent-"+str(i),parent[i-1][0],key,[(parent[i-1][1]+parent[i-1][2]+2,1142.5),(x-2,1142.5)])
    c.edge("empty-end","root-extract","root-end",[(590,1192),(590,1212),(2145,1212),(2145,1192)],family="warn",dashed=True)
    c.text(1310,1228,"无有效要求：结束本轮",9.4,P["warn"])
    c.edge("user-retry","root-end","root-check",[(2145,1093),(2145,1064),(1365,1064),(1365,1093)],family="warn",dashed=True)
    c.text(1940,1080,"用户选择：仅重试异常项",9.2,P["warn"])
    c.frame("extract",60,395,740,620,"Ⅰ  指标提取与来源建模","extract_requirements · 批次循环与有限重试")
    c.frame("checking",835,395,760,620,"Ⅱ  类型路由与证据约束核查","check_requirements · 按条目选路，单任务串行核查")
    c.frame("report",1630,395,710,620,"Ⅲ  报告整理与失败回退","report_workflow · 原始判定与证据保持")
    for key,x,center in (("root-extract",590,430),("root-check",1365,1215),("root-report",1755,1985)):
        c.edge("expand-"+key,key,None,[(x,1093),(x,1038),(center,1038),(center,1017)],association=True)
    c.box("batch",100,825,300,100,"选择待处理批次",["select_batch"],source=["extract_requirements:select_batch"])
    c.box("finish-extract",470,825,280,100,"保存提取清单",["批次耗尽后完成提取"],source=["extract_requirements:extraction_finish"])
    c.box("extract-llm",100,700,300,100,"A  提取与原文核验",["LangChain＋Pydantic"],"llm",source=["extract_requirements:extract_batch"])
    c.diamond("batch-ok",135,555,230,105,"批次通过？",source=["extract_requirements:extract_batch"])
    c.box("merge",100,425,300,90,"保留有效项与去重",["过滤技术范围，汇入清单"],source=["extract_requirements:merge_batch"])
    c.box("split",470,705,280,95,"拆批／有限重试",["二分一层；叶批最多两次"],"warn",source=["extract_requirements:split_or_retry"])
    c.box("batch-issue",470,445,280,90,"隔离批次异常",["保留已成功批次"],"warn",source=["extract_requirements:record_batch_issue"])
    c.edge("x1","batch","extract-llm",[(250,823),(250,802)])
    c.text(290,815,"有",9.4,P["muted"])
    c.edge("x2","batch","finish-extract",[(402,875),(468,875)])
    c.text(435,900,"无",9.4,P["muted"])
    c.edge("x3","extract-llm","batch-ok",[(250,698),(250,662)])
    c.edge("x4","batch-ok","merge",[(250,553),(250,517)])
    c.text(286,536,"通过",9.4,P["muted"])
    c.edge("x5","merge","batch",[(98,470),(82,470),(82,875),(98,875)])
    c.edge("x6","batch-ok","split",[(367,607.5),(430,607.5),(430,752.5),(468,752.5)],family="warn")
    c.text(389,632,"失败",9.2,P["warn"])
    c.edge("x7","split","extract-llm",[(610,802),(610,825),(250,825),(250,802)],family="warn",dashed=True)
    c.edge("x8","split","batch-issue",[(610,703),(610,537)],family="warn")
    c.text(656,620,"到限",9.3,P["warn"])
    c.edge("x9","batch-issue","batch",[(610,433),(610,405),(82,405),(82,875),(98,875)],family="warn",dashed=True)
    c.diamond("route",1065,820,280,100,"要求类型",source=["check_requirements:select_requirement"])
    for key,x,title,line,mapping in (("doc",855,"B  文档存在性","等价识别按需调用",["check_requirements:document_check"]),
        ("num",1100,"C  数值核查","检索＋参数条件判定",["check_requirements:numeric_retrieve","check_requirements:judge_numeric"]),
        ("fun",1345,"D  功能核查","检索＋实现机制判定",["check_requirements:functional_retrieve","check_requirements:judge_functional"])):
        c.box(key,x,705,210,100,title,[line],"llm",body_size=9.2,source=mapping)
    c.edge("c1","route","doc",[(1063,870),(960,870),(960,807)])
    c.edge("c2","route","num",[(1205,818),(1205,807)])
    c.edge("c3","route","fun",[(1347,870),(1450,870),(1450,807)])
    c.diamond("evidence",1080,550,250,90,"需补检索？",source=["check_requirements:assess_evidence"])
    c.box("supplement",1375,550,185,90,"补充检索",["最多两轮"],"warn",source=["check_requirements:supplement_retrieval"])
    c.box("save-item",1095,420,340,90,"按条目编号保存结果",["异常项保留为待人工复核"],source=["check_requirements:save_item","check_requirements:item_failure"])
    c.edge("c4","num","evidence",[(1205,703),(1205,642)])
    c.edge("c5","fun","evidence",[(1450,703),(1450,670),(1370,670),(1370,595),(1332,595)])
    c.edge("c6","doc","save-item",[(960,703),(960,445),(1093,445)])
    c.edge("c7","evidence","save-item",[(1205,548),(1205,525),(1265,525),(1265,512)])
    c.text(1060,530,"结束本项",9.3,P["muted"])
    c.edge("c8","evidence","supplement",[(1332,595),(1373,595)],family="warn")
    c.text(1351,620,"是",9.2,P["warn"])
    c.edge("c9","supplement","num",[(1562,595),(1580,595),(1580,830),(1325,830),(1325,755),(1312,755)],family="warn",dashed=True)
    c.edge("c10","supplement","fun",[(1562,595),(1580,595),(1580,755),(1557,755)],family="warn",dashed=True)
    c.edge("c11","save-item","route",[(1093,490),(850,490),(850,815),(915,815),(915,915),(1135,915),(1135,897)],dashed=True)
    c.text(990,893,"下一条",9.3,P["muted"])
    c.box("collect",1755,825,460,100,"固定原始报告与资料版本",["collect_report"],source=["report_workflow:collect_report"])
    c.box("polish",1755,700,460,100,"E  整理并校验报告内容",["LangChain：条目、判定、数字与引用"],"llm",source=["report_workflow:polish_one"])
    c.diamond("polish-ok",1845,555,280,100,"整理通过？",source=["report_workflow:polish_one"])
    c.box("fallback",1680,430,300,85,"回退原始分析",["保持原判定与证据"],"warn",source=["report_workflow:polish_fallback"])
    c.box("finalize",2045,430,275,85,"批次推进与最终保存",["全部完成后输出报告"],title_size=11.7,source=["report_workflow:advance_report","report_workflow:finalize_report"])
    c.edge("r1","collect","polish",[(1985,823),(1985,802)])
    c.edge("r2","polish","polish-ok",[(1985,698),(1985,657)])
    c.edge("r3","polish-ok","finalize",[(1985,553),(1985,530),(2182.5,530),(2182.5,517)])
    c.text(2190,550,"通过",9.3,P["muted"])
    c.edge("r4","polish-ok","fallback",[(1843,605),(1812,605),(1812,517)],family="warn")
    c.text(1720,550,"到限回退",9.2,P["warn"])
    c.edge("r5","polish-ok","polish",[(2127,605),(2255,605),(2255,750),(2217,750)],family="warn",dashed=True)
    c.text(2295,670,"重试",9.3,P["warn"])
    c.edge("r6","fallback","finalize",[(1982,472.5),(2043,472.5)])
    c.edge("r7","finalize","polish",[(2322,472.5),(2328,472.5),(2328,780),(2217,780)],dashed=True)
    c.text(2270,815,"后续批次",9.3,P["muted"])
    c.frame("lc-support",60,120,740,205,"指标提取与来源约束",family="llm")
    c.text(84,239,"ChatPromptTemplate 约束技术指标与结题条件",10.5,P["ink"],ha="left")
    c.text(84,195,"Pydantic 结构解析 · P 编号原文映射与核验",10.5,P["ink"],ha="left")
    c.text(84,155,"技术范围过滤 · 有效要求归并与去重",10.1,P["muted"],ha="left")
    c.frame("state",835,120,760,205,"统一状态与持久化",family="rule")
    c.text(859,239,"ReviewState · task_id/thread_id · 资料与模型版本签名",10.2,P["ink"],ha="left")
    c.text(859,195,"SqliteSaver 检查点 ＋ 按条目编号保存结果",10.3,P["ink"],ha="left")
    c.frame("runner",1630,120,710,205,"后台运行与人工命令",family="human")
    c.text(1654,239,"独立任务运行器 · SSE 节点事件与游标重连",10.1,P["ink"],ha="left")
    c.text(1654,195,"继续／停止／重试；服务重启后由用户显式恢复",10.1,P["ink"],ha="left")
    for i,x in enumerate((430,1215,1985)):
        c.edge("support-"+str(i),None,None,[(x,393),(x,327)],association=True)
    for i,(x,label,family) in enumerate(((60,"流程与规则处理","rule"),
        (610,"LangChain 执行模块","llm"),(1215,"人工确认与控制","human"),
        (1770,"有限重试与异常回退","warn"))):
        c.legend_item("legend-"+str(i),x,65,label,family)
    return c


def chain_figure():
    c=AcademicCanvas()
    c.text(60,1295,"基于 LangChain 的快速审查模块执行流程",25.5,bold=True,ha="left")
    c.text(60,1248,"模块 A–E 与 LangGraph 编排节点对应；C／D 通过检索增强实现证据约束判定",11.8,P["muted"],ha="left")
    columns=[(60,315,"对应执行模块"),(430,440,"输入与任务准备"),
        (925,330,"提示词构造"),(1310,220,"模型执行"),
        (1585,360,"文本提取与结构解析"),(2000,340,"业务校验与返回")]
    for x,w,title in columns:
        c.text(x+w/2,1204,title,10.8,P["muted"],True)
    rows=[
        {"code":"A","y":1020,"title":"提取与原文核验","phase":"指标提取阶段",
         "input":"依据段落与提取约束","input_lines":["原文 P 编号＋技术范围","技术指标与结题条件"],
         "prompt":"提取字段与原文约束","schema":"ExtractionOutput",
         "output":"要求候选项","output_lines":["原文编号核验／范围过滤","结果返回 A → 批次校验"],
         "source":["extract_requirements:extract_batch"]},
        {"code":"B","y":815,"title":"文档存在性","phase":"等价识别按需调用",
         "input":"真实文件清单","input_lines":["先执行文件名直接匹配","未命中：以 F 编号调用模型"],
         "prompt":"简称／同义名称等价识别","schema":"DocumentMatches",
         "output":"文件匹配结果","output_lines":["真实 F 编号与等价依据","结果返回 B → 保存条目"],
         "source":["check_requirements:document_check"]},
        {"code":"C","y":610,"title":"数值核查","phase":"分类核查阶段",
         "input":"指标检索与证据准备","input_lines":["KnowledgeRetriever.invoke","0.3 向量＋0.7 词项","E 编号＋条件／单位上下文"],
         "prompt":"比较数值、方向及适用条件","schema":"JudgmentOutput",
         "output":"数值核查结果","output_lines":["来源引用与判定支持校验","结果返回 C → 证据评估"],
         "source":["check_requirements:numeric_retrieve","check_requirements:judge_numeric"]},
        {"code":"D","y":405,"title":"功能核查","phase":"分类核查阶段",
         "input":"功能检索与证据准备","input_lines":["KnowledgeRetriever.invoke","0.3 向量＋0.7 词项","E 编号＋功能实现上下文"],
         "prompt":"分析实现机制与支持程度","schema":"JudgmentOutput",
         "output":"功能核查结果","output_lines":["来源引用与判定支持校验","结果返回 D → 证据评估"],
         "source":["check_requirements:functional_retrieve","check_requirements:judge_functional"]},
        {"code":"E","y":200,"title":"整理并校验报告内容","phase":"报告整理阶段",
         "input":"原始报告与整理约束","input_lines":["原条目、判定、数值及引用","忠实整理，保持原有事实"],
         "prompt":"忠实概括与展示内容整理","schema":"PolishedOutput",
         "output":"报告展示条目","output_lines":["条目／判定／数字一致性","结果返回 E → 整理校验"],
         "source":["report_workflow:polish_one"]}]
    for row in rows:
        code,y=row["code"],row["y"]
        c.box("module-"+code,60,y,315,160,code+"  "+row["title"],
            [row["phase"]],"llm",title_size=11.8,body_size=10.0,source=row["source"],emphasis=True)
        c.edge(code+"-expand","module-"+code,code+"-input",[(377,y+80),(428,y+80)],association=True)
        steps=[
            ("input",430,440,row["input"],row["input_lines"],"rule"),
            ("prompt",925,330,"ChatPromptTemplate",[row["prompt"],"任务规则＋输出格式"],"llm"),
            ("model",1310,220,"模型推理",["ChatOpenAI","同步 invoke"],"llm"),
            ("parse",1585,360,"结构化解析",["StrOutputParser","PydanticOutputParser",row["schema"]],"llm"),
            ("return",2000,340,row["output"],row["output_lines"],"rule")]
        for index,(suffix,x,w,title,lines,family) in enumerate(steps):
            c.box(code+"-"+suffix,x,y,w,160,title,lines,family,title_size=11.5,
                body_size=9.7,source=row["source"])
            if index:
                previous=steps[index-1]
                c.edge(code+"-flow-"+str(index),code+"-"+previous[0],code+"-"+suffix,
                    [(previous[1]+previous[2]+2,y+80),(x-2,y+80)])
    c.text(60,148,"模型链：ChatPromptTemplate | ChatOpenAI | StrOutputParser；随后执行 PydanticOutputParser.parse()。",10.4,P["muted"],ha="left")
    c.text(60,108,"结构化结果返回同标识 LangGraph 节点；状态图组织补充检索、有限重试、异常回退与人工确认。",10.4,P["muted"],ha="left")
    c.legend_item("legend-rule",60,54,"输入准备、证据检索与业务校验","rule",12.5)
    c.legend_item("legend-llm",1250,54,"LangChain 模型链与结构解析","llm",12.5)
    return c


def validate_module_correspondence(graph_canvas, chain_canvas):
    pairs=[("A","extract-llm"),("B","doc"),("C","num"),("D","fun"),("E","polish")]
    mapping=[]
    for code,identifier in pairs:
        first=next(node for node in graph_canvas.nodes if node["id"]==identifier)
        second=next(node for node in chain_canvas.nodes if node["id"]=="module-"+code)
        assert first["title"]==second["title"] and first["family"]==second["family"]=="llm"
        assert graph_canvas.mappings[identifier]==chain_canvas.mappings["module-"+code]
        mapping.append({"module":code,"title":first["title"],"figure1_node":identifier,
            "figure2_node":"module-"+code,"actual_graph_nodes":graph_canvas.mappings[identifier]})
    (base.OUT/"04_两图模块对应表.json").write_text(json.dumps(mapping,ensure_ascii=False,indent=2),encoding="utf-8")


def export_actual_graph():
    from backend.review_graph import build_review_graph, WORKFLOW_VERSION, NODE_LABELS
    from langgraph.checkpoint.memory import InMemorySaver
    graph=build_review_graph(None,InMemorySaver())
    diagram=graph.get_graph(xray=True)
    value={"workflow_version":WORKFLOW_VERSION,"nodes":[{"id":str(key),"label":NODE_LABELS.get(str(key).split(":")[-1],str(node.name))} for key,node in diagram.nodes.items()],
        "edges":[{"source":str(e.source),"target":str(e.target),"conditional":bool(e.conditional),"label":str(e.data) if e.data is not None else None} for e in diagram.edges]}
    (base.OUT/"03_实际编译图_完整拓扑.mmd").write_text(diagram.draw_mermaid(),encoding="utf-8")
    (base.OUT/"03_实际编译图_完整拓扑.json").write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding="utf-8")
    (base.BUILD/"source-hashes.json").write_text(json.dumps({name:hashlib.sha256((base.ROOT/name).read_bytes()).hexdigest() for name in (
        "backend/review_graph.py","backend/review_engine.py","backend/review_jobs.py","backend/review_schemas.py")},indent=2),encoding="utf-8")
    assert len(value["nodes"])==31 and len(value["edges"])==59
    print(f"Actual topology: {len(value['nodes'])} nodes / {len(value['edges'])} edges",flush=True)


if __name__=="__main__":
    base.OUT.mkdir(parents=True,exist_ok=True); base.BUILD.mkdir(parents=True,exist_ok=True)
    export_actual_graph()
    graph_canvas,chain_canvas=graph_figure(),chain_figure()
    validate_module_correspondence(graph_canvas,chain_canvas)
    graph_canvas.save("01_LangGraph快速审查_状态编排与条件工作流")
    chain_canvas.save("02_LangChain快速审查_模型链与证据增强")
