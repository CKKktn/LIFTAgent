"""Build synthetic quick-review fixtures; never read the user's runtime data."""
from __future__ import annotations

from collections import Counter
import csv
import hashlib
from html import escape
import json
from pathlib import Path
import zipfile

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "output" / "pdf" / "快速审查测试包"
PDF_NAME = "移动监测终端采购与技术服务合同.pdf"
BASELINE = "01_公共审查依据/" + PDF_NAME
NOTICE = "合成软件测试资料。项目、单位、编号及数据均为虚构，不用于实际签约或交付。"
PROJECT = "项目编号 DEMO-X7-20261025；对象为 X7 移动监测终端及配套监控软件，交付批次 A。"

REQUIREMENTS = [
    ("T01", "numeric", "连续续航时间", "在环境温度 25±2 ℃、额定负载条件下，终端方案设计的连续续航时间不得低于 8 小时。"),
    ("T02", "numeric", "工作温度范围", "终端方案设计的工作温度范围应覆盖 -10 ℃ 至 +50 ℃，上下端点均包含在内。"),
    ("T03", "numeric", "额定输出功率", "在交流 220 V 输入条件下，终端电源模块方案设计的额定输出功率不得低于 2 kW。"),
    ("T04", "numeric", "本地告警响应时间", "在 10 Mbps 有线局域网内，本地告警响应时间应不超过 200 ms；计时从终端检测到故障开始，到监控页面显示告警结束。"),
    ("T05", "numeric", "并发在线终端容量", "在单台监控服务器配置下，方案设计应支持不少于 100 台终端同时在线并上报状态。"),
    ("T06", "numeric", "整机质量", "终端方案设计的整机质量应不超过 45 kg；质量包含电池，不包含外置充电器。"),
    ("T07", "functional", "远程状态监控", "监控软件应实现远程状态监控，操作人员能够在监控页面查看终端位置、电池电量和在线状态。"),
    ("T08", "functional", "故障告警推送", "监控软件应实现故障告警推送，检测到终端故障后自动向已登录操作人员显示告警消息。"),
    ("T09", "functional", "CSV 数据导出", "监控软件应支持将选定时间段的历史监测数据导出为 CSV 文件。"),
    ("T10", "document", "设备测试报告", "乙方应提交设备测试报告。"),
    ("T11", "document", "部署操作手册", "乙方应提交部署操作手册。"),
    ("T12", "document", "接口设计说明书", "乙方应提交接口设计说明书。"),
]

BUSINESS = [
    "B01 合同总价为人民币 120000 元；税费参数以测试双方另行记录为准。",
    "B02 首付款为合同总价的 30%，即 36000 元；剩余 84000 元在双方确认结算后支付。",
    "B03 双方为测试设置逾期违约金参数为每日合同总价的 0.03%；该数值仅为虚构占位。",
    "B04 乙方应提交保密承诺书及合作伙伴主动申报事项表；以上均为商务行政材料。",
]


def write(relative: str, text: str):
    path = PACK / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")


def material(title: str, body: str):
    return f"{title}\n{NOTICE}\n{PROJECT}\n\n{body.strip()}\n"


MIXED_PLAN = material("项目实施方案", """
文件编号 X7-PLAN-R1；当前交付批次 A 的实施设计，适用型号 X7。

一 设备参数
连续续航时间：在环境温度 25±2 ℃、额定负载条件下，采用双电池供电，方案设计连续续航时间为 10 小时。这是设计参数，不是实测结果。
工作温度范围：当前 X7 终端方案设计为 -5 ℃ 至 +45 ℃，上下端点均包含；未配置低温加热和高温扩展模块。
额定输出功率：交流 220 V 输入条件下，电源模块选型额定输出功率为 2500 W。
本地告警响应时间：在 10 Mbps 有线局域网内，设计时延上限为 250 ms；计时从终端检测到故障开始，到监控页面显示告警结束。
并发在线终端容量：当前材料未给出单台监控服务器同时在线终端数量，也未确定容量上限，尚待容量选型。不要将软件界面中的页大小当作容量参数。
整机质量：方案核算为 45 kg，包含电池，不包含外置充电器。

二 软件实现
远程状态监控已在当前版本实现。终端周期上报位置、电池电量和心跳；后台保存最新状态，监控页面通过 WebSocket 更新位置、电量和在线状态。
故障告警推送在当前版本尚未实现。当前版本没有自动向已登录操作人员显示故障告警消息的通道，需由操作人员主动查询日志；推送功能列入后续开发计划。
历史监测数据导出在当前版本仅支持 PDF，不支持 CSV 文件导出。

三 技术资料
本方案说明设计参数与实现现状。设备测试报告和部署操作手册分别形成独立文件；接口设计说明书尚未形成独立交付文件。
""")

GOOD_PLAN = material("项目实施方案", """
文件编号 X7-PLAN-R2；当前交付批次 A 的实施设计，适用型号 X7。

一 设备参数
连续续航时间：环境温度 25±2 ℃、额定负载条件下，双电池方案设计连续续航时间为 10 小时，属于设计参数。
工作温度范围：终端方案设计的工作温度范围为 -10 ℃ 至 +50 ℃，上下端点均包含，配置低温加热与高温散热模块。
额定输出功率：在交流 220 V 输入条件下，电源模块选型额定输出功率为 2500 W。
本地告警响应时间：在 10 Mbps 有线局域网内，设计响应时间上限为 180 ms，计时从终端检测到故障开始，到监控页面显示告警结束。
并发在线终端容量：单台监控服务器设计支持 120 台终端同时在线并上报状态。
整机质量：方案核算为 45 kg，包含电池，不包含外置充电器。

二 软件实现
远程状态监控已实现。终端周期上报位置、电池电量与心跳，监控页面通过 WebSocket 显示终端位置、电池电量与在线状态。
故障告警推送已实现。故障事件进入消息队列，由告警服务通过 WebSocket 自动向已登录操作人员显示告警消息。
CSV 数据导出已实现。历史数据查询接口接收起止时间，服务端按时间范围查询记录，生成带字段名的 UTF-8 CSV 文件并提供下载。

三 技术资料
设备测试报告、部署操作手册、接口设计说明书均形成独立交付文件。是否存在文件应由交付清单与实际文件对应确认。
""")

MIXED_REPORT = material("设备测试报告", """
文件编号 X7-TEST-01；报告日期 2026年10月20日；样机 X7-A001。
本报告为合成记录，记录实测结果，不修改项目实施方案中的设计参数。

连续续航试验：环境温度 25 ℃，额定负载，电池充满，连续运行至自动关机；实测连续续航时间为 7.6 小时。试验日期 2026年10月19日。
本次报告未开展工作温度边界、并发在线终端容量和质量测量试验，其余项目无实测结论。
设计续航 10 小时与实测 7.6 小时分属方案参数与测试观测，不得把设计值写成实测值。实测差异需另行分析。
""")

GOOD_REPORT = material("设备测试报告", """
文件编号 X7-TEST-02；报告日期 2026年10月20日；样机 X7-A002。
测试条件：环境温度 25 ℃，额定负载，电池充满，持续运行至自动关机。
实测连续续航时间为 8.4 小时；试验日期 2026年10月19日。
本报告只记录本次续航试验，不代表其他设计参数已经实测或项目已整体验收。
""")

MIXED_MANUAL = material("部署操作手册", """
文件编号 X7-MANUAL-01；适用软件版本 R1。
部署：将终端接入有线局域网，在监控服务器录入终端编号并启用心跳采集。
远程状态监控：登录监控页面后，选择终端即可查看位置、电池电量和在线状态。
故障处置：当前版本故障消息不会自动推送到操作人员页面，操作人员需手动打开日志查询故障事件。
数据导出：历史记录页当前提供 PDF 下载按钮，没有 CSV 导出入口。
本手册不提供并发容量参数，也不提供任何实测性能结论。
""")

GOOD_MANUAL = material("部署操作手册", """
文件编号 X7-MANUAL-02；适用软件版本 R2。
部署：将终端接入有线局域网，在监控服务器录入终端编号并启用心跳采集。
远程状态监控：操作人员登录后，可在监控页面查看终端位置、电池电量与在线状态。
故障告警推送：保持页面消息连接，终端故障上报后，页面自动弹出告警消息。
CSV 数据导出：在历史记录页选择起止时间并点击导出 CSV，下载 UTF-8 CSV 文件。
本手册说明操作路径，不声称全部设计指标已经实测达标。
""")

INTERFACE = material("接口设计说明书", """
文件编号 X7-IF-01；接口版本 R2。
状态接口：GET /terminals/status，返回终端位置、电池电量、在线状态。
告警消息：WebSocket /events/alarms，服务端推送故障事件编号与描述。
导出接口：GET /history/export?format=csv，按起止时间查询并输出 UTF-8 CSV 文件。
本文件定义软件接口，不给出终端硬件工作温度或实测性能结论。
""")

HISTORICAL = """历史产品参考参数
合成软件测试资料，全部数据均为虚构。
历史项目编号 OLD-Z9-2024，产品型号 Z9。该产品不属于 DEMO-X7-20261025，也不属于 X7 交付批次 A，未作为本项目实施设计。
历史 Z9 产品参数：设计续航 16 小时，工作温度 -20 ℃ 至 +60 ℃，并发在线终端容量 300 台。
该资料只供历史选型背景参考，不能代表当前 X7 交付产品参数。
"""


def make_contract():
    pdfmetrics.registerFont(TTFont("SampleChinese", "C:/Windows/Fonts/simsun.ttc", subfontIndex=0))
    font = "SampleChinese"
    body = ParagraphStyle("Body", fontName=font, fontSize=11, leading=19, spaceAfter=8, wordWrap="CJK", textColor=colors.black)
    heading = ParagraphStyle("Heading", parent=body, fontSize=13, leading=21, spaceBefore=10, spaceAfter=9, keepWithNext=True)
    title = ParagraphStyle("Title", parent=body, fontSize=19, leading=28, alignment=TA_CENTER, spaceAfter=15)
    meta = ParagraphStyle("Meta", parent=body, fontSize=10, leading=17, spaceAfter=6)
    story = []
    p = lambda text, style=body: Paragraph(escape(text), style)
    story.extend([p("移动监测终端采购与技术服务合同", title), p("合同编号 DEMO-X7-20261025", meta),
        p("甲方 测试采购单位    乙方 测试技术单位", meta), p(NOTICE, meta), Spacer(1, 6)])
    story.extend([p("一 项目对象与资料范围", heading), p(PROJECT),
        p("乙方提供终端设计与软件实现方案，以及约定的技术交付文件。各项要求适用于本项目当前交付对象；历史产品、其他项目及未采用选型资料不代表本项目的设计与实现。"),
        p("二 方案设计数值要求", heading)])
    for code, _, name, criterion in REQUIREMENTS[:6]:
        story.append(p(f"{code} {name}  {criterion}"))
    story.extend([PageBreak(), p("三 软件功能要求", heading)])
    for code, _, name, criterion in REQUIREMENTS[6:9]:
        story.append(p(f"{code} {name}  {criterion}"))
    story.append(p("四 技术交付文件", heading))
    for code, _, name, criterion in REQUIREMENTS[9:]:
        story.append(p(f"{code} {name}  {criterion}"))
    story.append(p("五 商务与行政约定", heading))
    for text in BUSINESS:
        story.append(p(text))
    story.extend([p("六 设计与验收的区分", heading), p("方案设计符合约定，仅表示设计参数或实现描述满足对应要求。实际测试、交付签收及最终验收由双方另行依据测试记录和验收程序确认。本合同本身不证明任何设计、功能或交付已经完成。"),
        p("甲方 测试采购单位    乙方 测试技术单位", meta), p("编制日期 2026年10月25日    本样例不含真实签章", meta)])
    target = PACK / BASELINE
    target.parent.mkdir(parents=True, exist_ok=True)

    def page(canvas, doc):
        canvas.saveState()
        canvas.setFont(font, 8)
        canvas.setFillColor(colors.HexColor("#666666"))
        canvas.drawString(22 * mm, A4[1] - 14 * mm, "DEMO-X7    合成测试合同")
        canvas.drawString(22 * mm, 14 * mm, "仅供软件测试    不用于真实签约")
        canvas.drawRightString(A4[0] - 22 * mm, 14 * mm, f"第 {doc.page} 页")
        canvas.restoreState()

    SimpleDocTemplate(str(target), pagesize=A4, rightMargin=22 * mm, leftMargin=22 * mm,
        topMargin=24 * mm, bottomMargin=23 * mm, title="移动监测终端采购与技术服务合同", author="合成测试资料").build(story, onFirstPage=page, onLaterPages=page)
    lines = ["移动监测终端采购与技术服务合同", "合同编号 DEMO-X7-20261025", NOTICE, PROJECT]
    lines += [f"{code} {name} {criterion}" for code, _, name, criterion in REQUIREMENTS]
    lines += BUSINESS
    lines += ["方案符合不等同于实测达标或最终验收通过。合同本身不证明任何要求已经完成。"]
    write("03_核对资料_不要上传/合同可编辑原文.txt", "\n\n".join(lines))


CASES = [
    {"id": "mixed", "folder": "01_混合问题", "kb": "QA-01-混合问题", "files": {"项目实施方案.txt": MIXED_PLAN, "设备测试报告.txt": MIXED_REPORT, "部署操作手册.txt": MIXED_MANUAL, "历史产品参考参数.txt": HISTORICAL}},
    {"id": "conforming", "folder": "02_方案全部符合", "kb": "QA-02-方案全部符合", "files": {"项目实施方案.txt": GOOD_PLAN, "设备测试报告.txt": GOOD_REPORT, "部署操作手册.txt": GOOD_MANUAL, "接口设计说明书.txt": INTERFACE}},
    {"id": "baseline_only", "folder": "03_仅有合同", "kb": "QA-03-仅有合同", "files": {}},
    {"id": "conflicting", "folder": "04_方案冲突", "kb": "QA-04-方案冲突", "files": {
        "项目实施方案_设计组.txt": GOOD_PLAN.replace("文件编号 X7-PLAN-R2", "文件编号 X7-PLAN-DESIGN-R1").replace("当前交付批次 A 的实施设计", "当前交付批次 A 的实施设计；与集成组方案同时流转，未声明优先级"),
        "项目实施方案_集成组.txt": GOOD_PLAN.replace("文件编号 X7-PLAN-R2", "文件编号 X7-PLAN-INTEGRATION-R1").replace("当前交付批次 A 的实施设计", "当前交付批次 A 的实施设计；与设计组方案同时流转，未声明优先级").replace("-10 ℃ 至 +50 ℃，上下端点均包含，配置低温加热与高温散热模块", "-5 ℃ 至 +45 ℃，上下端点均包含，未配置低温加热和高温扩展模块"),
        "设备测试报告.txt": GOOD_REPORT, "部署操作手册.txt": GOOD_MANUAL, "接口设计说明书.txt": INTERFACE}},
]

MIXED_EXPECTED = [
    ("已满足", "项目实施方案.txt", "设计续航 10 小时达到 8 小时；报告实测 7.6 小时应作为额外风险，不能把方案符合写成实测达标。"),
    ("不满足", "项目实施方案.txt", "本项目 -5 至 +45 ℃ 未覆盖 -10 至 +50 ℃；其他项目的历史参数不能补足本项目范围。"),
    ("已满足", "项目实施方案.txt", "2500 W = 2.5 kW，达到 2 kW，输入条件一致。"),
    ("不满足", "项目实施方案.txt", "设计上限 250 ms 大于允许上限 200 ms，不能把比较方向反转。"),
    ("证据不足", "项目实施方案.txt", "没有本项目明确的容量数值；历史 Z9 产品的 300 台不能证明 X7 达到要求。"),
    ("已满足", "项目实施方案.txt", "45 kg 等于允许上限，且质量范围一致。"),
    ("已满足", "项目实施方案.txt；部署操作手册.txt", "位置、电量与在线状态具有明确实现机制和操作入口。"),
    ("不满足", "项目实施方案.txt；部署操作手册.txt", "资料明确当前版本未实现故障告警推送，后续计划不能当作已实现。"),
    ("不满足", "项目实施方案.txt；部署操作手册.txt", "当前只支持 PDF，明确不支持 CSV。"),
    ("已满足", "设备测试报告.txt", "文件名匹配，仅确认存在；不评价报告内容充分性或指标是否通过。"),
    ("已满足", "部署操作手册.txt", "文件名匹配，仅确认存在。"),
    ("证据不足", "完整文件清单", "未找到接口设计说明书；正文提到该名称不等于实际文件存在。"),
]


def make_guides():
    truth = {"schema_version": 1, "created": "2026-10-25", "synthetic": True,
        "scope": "v2 当前技术方案符合性口径；不属于全面法律审查、实测验收或生产效果认证", "baseline": BASELINE,
        "requirements": [], "cases": []}
    for i, (code, kind, subject, criterion) in enumerate(REQUIREMENTS):
        mixed_status, sources, rationale = MIXED_EXPECTED[i]
        truth["requirements"].append({"id": code, "type": kind, "subject": subject, "criterion": criterion,
            "document_name": subject if kind == "document" else "", "expected": {
            "mixed": {"status": mixed_status, "sources": sources, "reason": rationale},
            "conforming": {"status": "已满足", "reason": "方案参数或实现机制符合；三份技术交付文件均存在，不等同于全部实测和验收通过。"},
            "baseline_only": {"status": "证据不足", "reason": "审查依据不可自证完成；本场景没有任何其他材料或技术交付文件。"},
            "conflicting": {"status": "证据冲突" if code == "T02" else "已满足", "reason": "同对象同批次的两份现行方案温度范围互相矛盾，未声明优先级，需引用两份并人工确认。" if code == "T02" else "除温度条款外与方案全部符合场景一致。"}}})
    for case in CASES:
        directory = PACK / "02_场景材料" / case["folder"]
        directory.mkdir(parents=True, exist_ok=True)
        for name, content in case["files"].items():
            write(f"02_场景材料/{case['folder']}/{name}", content)
        counts = Counter(row["expected"][case["id"]]["status"] for row in truth["requirements"])
        truth["cases"].append({"id": case["id"], "folder": "02_场景材料/" + case["folder"], "knowledge_base_name": case["kb"],
            "upload_files": [BASELINE] + [f"02_场景材料/{case['folder']}/{name}" for name in case["files"]],
            "baseline_files": [BASELINE], "expected_counts": dict(counts)})
    write("03_核对资料_不要上传/expected-results.json", json.dumps(truth, ensure_ascii=False, indent=2))

    table = ["| 编号 | 类型 | 核查对象 | 混合问题场景预期 | 依据与核对重点 |", "|---|---|---|---|---|"]
    for row in truth["requirements"]:
        exp = row["expected"]["mixed"]
        table.append(f"| {row['id']} | {row['type']} | {row['subject']} | {exp['status']} | {exp['reason']} |")
    write("03_核对资料_不要上传/预期结果与改进记录.md", """# 预期结果与改进记录

这是独立人工核对答案，不要上传到任何测试知识库，也不要把本文件内容填入审查关注点。预期根据合成原文与当前 v2 代码口径制定，不代表已经运行真实模型并获得这些结果。

## 第一阶段核对

合同包含 12 个技术语义核查点：6 个数值指标、3 个功能条件、3 个技术文档交付要求。T01 至 T12 是本测试包编号，不要求系统生成相同的 R 编号或提取顺序。若模型把一项拆成多项或合并多项，按语义逐项核对，不只按总条数评分。

B01 合同总价、B02 首付款与付款比例、B03 违约金、B04 保密承诺书与合作伙伴主动申报事项表均应排除，不能因为有数字或“应提交”而纳入技术核查。标题、虚构单位名称、项目编号与编制日期也不应成为指标。

## 混合问题场景

按本表 12 项语义核查点，预期 6 项已满足、4 项不满足、2 项证据不足。

""" + "\n".join(table) + """

## 其他三个场景

- 方案全部符合：12 项均已满足，含 6 个数值设计符合、3 个功能实现描述符合、3 个文件存在。不可写成全部实测达标或项目验收通过。
- 仅有合同：12 项均证据不足。不能把合同里的要求当作完成证明；没有找到文件也不能直接断言现实中从未交付。
- 方案冲突：T02 为证据冲突，其他 11 项已满足。两份方案针对相同型号、同一交付批次，均为现行流转版本且没有优先级。结论须同时引用设计组与集成组方案。若模型或检索只找到一份，记录为漏召回或冲突漏判，不能宣称已解决。

## 记录实际问题

先导出原始 JSON 汇报和 Markdown 报告，再按以下顺序定位：

1. 提取遗漏或范围错误：检查合同文字解析与第一阶段指标清单。
2. 错用历史产品或漏掉某份方案：检查召回片段、对象条件与实际引用。
3. 单位、端点、比较方向错误：核对 T03、T04、T06 的原始参数。
4. 把计划当实现或把缺证当不满足：核对 T05、T08、T09。
5. 文档存在判定错误：检查真实文件清单，不用正文提到的名称代替实际文件。
6. 整理阶段改变原始判定或数字：对照第二阶段原始汇报与第三阶段看板。

使用测试记录模板.csv 逐条填写实际状态、引用文件、错误类型和改进意见；记录所用模型、日期和第一阶段人工修改内容，便于同条件复测。不同模型措辞可以不同，事实、适用条件和来源必须一致。

## 可选的状态与恢复测试

在独立测试知识库运行，不动生产资料。保存第一阶段任务后刷新，检查指标及依据选择能否恢复；停止审查后继续，检查已完成条目是否保留。已有结论后修改一份方案并重新解析，再继续旧任务，应提示材料已变化并要求新建任务。测试前导出结果，测试结束后保留记录。

该测试包只覆盖中文文本 PDF 和 TXT、短合同及当前方案审查口径，不覆盖扫描 OCR、复杂表格、跨页条款、长合同穷尽性或法律有效性判断。
""")

    write("00_使用说明.md", """# 快速审查测试包

本包为全新编写的合成材料，用来检查当前 v2 的快速审查流程与结论边界，不含真实合同、账号、模型密钥或现有知识库资料。建议先做“01 混合问题”，出现疑问时用独立答案核对。

## 文件分工

- 01_公共审查依据：一份带可提取文本层的 PDF 合同，四个场景共用。合同有 12 项技术要求，并混入应排除的商务行政条款。
- 02_场景材料：每次只选择其中一个场景。03_仅有合同为空目录，是刻意不提供证明材料。
- 03_核对资料_不要上传：预期答案、机器可读答案、测试记录模板及合同可编辑原文。不要上传此目录。
- 素材验证记录.json：本地解析与检索检查记录，不是模型审查结论，不要上传。
- 文件清单.json：便于核对材料完整性，不要上传。

## 推荐操作

1. 使用项目根目录“启动已迁移系统.cmd”启动当前 v2，保留你已验证可用的模型设置。
2. 新建独立知识库 QA-01-混合问题，不要在已有真实资料库中测试。可选择“本地文本特征”嵌入和“通用”分块，块长 1000、重叠 120，避免为了此样例更改日常库配置。
3. 上传 01_公共审查依据 中的 PDF 合同，以及 02_场景材料/01_混合问题 下的全部 4 份 TXT。开启自动解析，等待解析完成。不要上传整个测试包。
4. 打开“快速审查”，选择该知识库。在“审查依据”中只选择 PDF 合同；方案、报告、手册和历史产品资料均不是审查依据。
5. 选择你已配置的真实对话模型；审查重点可留空。点击“提取指标与结题条件”，先记录自动提取结果，再人工核对类型、条件及遗漏；不要把参考答案提供给模型。
6. 确认指标后继续，查看原始汇报、第三阶段看板和每条引用原文。导出 Markdown 报告与原始 JSON，再对照 03_核对资料_不要上传 中的预期结果。
7. 测试其他场景时分别新建 QA-02-方案全部符合、QA-03-仅有合同、QA-04-方案冲突。每个库都上传同一份公共 PDF，再上传该场景文件。仅有合同场景不上传任何其他材料。

## 四种场景的操作差异

| 场景 | 上传内容 | 主要观察点 |
|---|---|---|
| 01 混合问题 | 公共合同 + 4 份 TXT | 正反证、缺证、单位换算、边界值、历史项目干扰、方案与实测区分 |
| 02 方案全部符合 | 公共合同 + 4 份 TXT | 设计参数和功能描述符合，三个技术交付文件均存在 |
| 03 仅有合同 | 仅公共合同 | 合同要求不能自证完成；无证据应保留不确定性 |
| 04 方案冲突 | 公共合同 + 5 份 TXT | 同一对象两份现行方案温度范围冲突，应引用双方并要求确认 |

本包没有自动写入你的日常系统，也没有触发真实模型调用。PDF 与 TXT 均为可上传格式，不依赖 Word 或 LibreOffice。合同原文可编辑版本位于核对资料目录，如需修改再测，请另存新样例，避免与同一合同 PDF 重复上传。

当前系统口径：数值指标对比项目方案，功能条件检查实现或设计机制，文档交付只检查实际文件名是否存在。文件存在不等于内容合格，方案符合不等于实测达标。模型抽取、检索和推理仍需用本包在实际界面验证。
""")

    csv_path = PACK / "03_核对资料_不要上传" / "测试记录模板.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["场景", "核查点编号", "核查对象", "预期状态", "实际状态", "实际引用文件", "错误类型", "改进意见", "模型", "测试日期", "第一阶段人工修改"])
        for case in CASES:
            for row in truth["requirements"]:
                writer.writerow([case["folder"], row["id"], row["subject"], row["expected"][case["id"]]["status"], "", "", "", "", "", "", ""])
    return truth


def finalize():
    files = []
    for path in sorted(PACK.rglob("*")):
        if not path.is_file() or path.name == "文件清单.json":
            continue
        if path.suffix.lower() not in {".pdf", ".txt", ".md", ".json", ".csv"} or path.name.startswith("."):
            raise ValueError("Unexpected file in sample pack: " + str(path))
        files.append({"path": path.relative_to(PACK).as_posix(), "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    write("文件清单.json", json.dumps({"synthetic": True, "files": files}, ensure_ascii=False, indent=2))
    archive = PACK.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for path in sorted(PACK.rglob("*")):
            relative = Path(PACK.name) / path.relative_to(PACK)
            if path.is_dir():
                output.writestr(relative.as_posix() + "/", b"")
            else:
                output.write(path, relative.as_posix())
    print(json.dumps({"pack": str(PACK), "archive": str(archive), "files": len(files) + 1}, ensure_ascii=True))


if __name__ == "__main__":
    PACK.mkdir(parents=True, exist_ok=True)
    make_contract()
    make_guides()
    finalize()
