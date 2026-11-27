"""Source budgets and verbatim excerpts shared by the review workflows."""
import re

from .models import terms

SOURCES_PER_SEARCH = 16
QUICK_ROUND_CHARACTERS = 32_000
MAX_SOURCE_CHARACTERS = 6_000
COUNTEREVIDENCE_TERMS = (
    "同一对象 型号 版本 批次 可比工况 实测 复测 不合格 未达标 异常 偏差 变更 更正 失效 复验"
)
_RECORD_TERMS = re.compile(r"实测|复测|复验|测得|不合格|未达标|异常|偏差|变更|更正|型号|批次|版本|工况|载荷|环境|签收|延期")
_VALUE = re.compile(r"\d+(?:\.\d+)?\s*(?:小时|分钟|毫米|千米|毫秒|千克|度|℃|%|％|[a-zA-Zμ]+)")
_IDENTITY = re.compile(r"(?:设备型号|型号|批次号|批次|批号|序列号|版本号|版本|工况)\s*[:：]?\s*([\w.-]{1,28})")


def counterevidence_query(question, cited_text="", limit=2000):
    # Keep the original target first, the search terms within the character
    # limit, and exact identity clues from the initially cited source.
    identities = list(dict.fromkeys(m.group() for m in _IDENTITY.finditer(cited_text)))
    suffix = "\n补查：" + COUNTEREVIDENCE_TERMS + " " + " ".join(identities[:8])
    return question[:limit - len(suffix)].rstrip() + suffix


def source_limits(texts, total):
    """Distribute unused capacity from short sources among longer ones."""
    limits = [0] * len(texts)
    remaining = total
    ordered = sorted(range(len(texts)), key=lambda index: len(texts[index]))
    for left, index in enumerate(ordered):
        limits[index] = min(len(texts[index]), MAX_SOURCE_CHARACTERS, remaining // (len(ordered) - left))
        remaining -= limits[index]
    return limits


def evidence_ranges(text, limit, question):
    """Select original spans, with a header and nearby context around key text."""
    if limit <= 0 or not text:
        return []
    if len(text) <= limit:
        return [(0, len(text))]
    if limit < 300:
        return [(0, limit)]

    header = min(240, limit // 6)
    query_terms = {token for token in terms(question) if len(token) > 1}
    spans = [(0, header)]
    remaining = limit - header
    width = max(240, remaining // 3)
    stride = width // 2
    starts = list(range(header, len(text) - width + 1, stride))
    starts.append(max(header, len(text) - width))
    scored = []
    for start in starts:
        section = text[start:start + width]
        # Give more weight to passages that mention both a relevant term and a
        # measurement or condition. Keep the surrounding original wording.
        overlap = len(query_terms.intersection(terms(section)))
        records = len(set(_RECORD_TERMS.findall(section)))
        values = bool(_VALUE.search(section))
        scored.append((3 * overlap + 2 * min(4, records) + 3 * values, start))
    for score, start in sorted(scored, key=lambda item: (-item[0], item[1])):
        if remaining < width or (score == 0 and len(spans) > 1):
            break
        end = min(len(text), start + width)
        if all(end <= lo or start >= hi for lo, hi in spans):
            spans.append((start, end))
            remaining -= end - start

    merged = []
    for start, end in sorted(spans):
        if merged and merged[-1][1] == start:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged
