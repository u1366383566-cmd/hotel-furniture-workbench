"""LLM 风格规划服务：风格方向一句话 → 整套系列规则 JSON。

原则（对应计划第四节）：LLM 只负责提出方案，不修改任务状态、不批准设计；
生成的规则必须经负责人在界面上确认后才进入批次规划（本服务直接落库并记审计，
但生图仍需走完整批次流程与人工审图）。

上游契约：反代 /v1/chat/completions，model=auto；返回文本可能被 <escape> 标签
或 markdown 代码块包裹，解析时统一剥离。
"""
import json
import re
import uuid

import sqlite3

from .image_adapter import chat_completion_real
from .series_rules_service import REQUIRED_SERIES_KEYS

_LIST_KEYS = [
    "headboard_types", "bed_frames", "nightstands", "wardrobe_types",
    "materials", "color_palette", "forbidden_elements", "forbidden_processes",
    "allowed_structural_changes", "companion_items",
]
_TEXT_KEYS = [
    "soft_upholstery_usage", "metal_usage", "hotel_mood",
    "twin_adapter_rule", "white_bg_rule",
]
_SERIES_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,31}$")

_SYSTEM_PROMPT = """你是高端酒店家具系列的首席规则规划师。用户给出一个风格方向（一句话），
你输出一套完整的系列规则 JSON，用于驱动 AI 生图提示词的自动拼装。

只输出一个严格合法的 JSON 对象（不要 markdown 代码块、不要解释文字），
必须包含以下全部键，值一律使用中文：

{
  "series_id": "系列英文标识：3-32位小写字母数字下划线，字母开头，体现风格（如 neo_oriental）",
  "name": "系列中文名（4-10字，如：新中式禅意）",
  "headboard_types": ["床屏款式描述（含材质工艺细节）", "至少3款供组合轮换"],
  "bed_frames": ["床体款式描述", "至少2款"],
  "nightstands": ["床头柜款式描述", "至少2款"],
  "wardrobe_types": ["衣柜/柜体款式描述", "至少2款"],
  "materials": ["主要材质", "4-6种"],
  "color_palette": ["配色（主色+辅助色+点缀色）", "3-5种"],
  "soft_upholstery_usage": "软包使用范围一句话",
  "metal_usage": "金属使用范围一句话",
  "forbidden_elements": ["禁止出现的元素", "3-5条"],
  "forbidden_processes": ["禁止工艺", "2-4条"],
  "allowed_structural_changes": ["允许的结构变化", "3-5条"],
  "hotel_mood": "空间气质与光照氛围（30-60字，含时段与光线描述）",
  "twin_adapter_rule": "双床适配规则一句话",
  "white_bg_rule": "白底重组规则一句话",
  "companion_items": ["配套家具清单：沙发+茶几、扶手椅+边几、书桌椅、电视柜、行李架、床尾凳、落地灯等", "共6-9件"]
}

要求：所有描述具体到材质工艺与造型细节（供生图提示词直接引用），风格内部高度统一，
配套家具与核心家具同材质同色系。"""


def _strip_wrappers(text: str) -> str:
    """剥掉反代的 <escape> 包裹与 markdown 代码块，提取 JSON 文本。"""
    text = text.strip()
    text = re.sub(r"</?escape>", "", text, flags=re.I)
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("LLM 返回中未找到 JSON 对象")
    return text[start:end + 1]


def build_series_schema_system_prompt() -> str:
    """LLM 规划师的 System Prompt：描述系列规则 JSON schema 与质量要求。"""
    return _SYSTEM_PROMPT


def parse_llm_rules(raw: str) -> dict:
    """解析并规整 LLM 返回的系列规则：剥包裹 → 提取 JSON → 校验必填 → 类型规整。"""
    try:
        rules = json.loads(_strip_wrappers(raw))
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM 返回的不是合法 JSON，请重试或换个风格描述（{e}）") from e
    if not isinstance(rules, dict):
        raise ValueError("LLM 返回的 JSON 不是对象")

    series_id = str(rules.get("series_id") or "").strip()
    name = str(rules.get("name") or "").strip()
    if not _SERIES_ID_RE.match(series_id):
        series_id = "ai_" + uuid.uuid4().hex[:8]
    if not name:
        name = "AI 生成系列"

    for key in _LIST_KEYS:
        val = rules.get(key)
        if isinstance(val, list):
            cleaned = [str(v).strip() for v in val if str(v).strip()]
        elif isinstance(val, str) and val.strip():
            cleaned = [val.strip()]
        else:
            cleaned = []
        rules[key] = cleaned
    for key in _TEXT_KEYS:
        rules[key] = str(rules.get(key) or "").strip()

    missing = [k for k in REQUIRED_SERIES_KEYS if not rules.get(k)]
    if missing:
        raise ValueError(f"LLM 生成的规则缺少必填字段: {missing}，请重试")
    if not rules.get("companion_items"):
        raise ValueError("LLM 生成的规则缺少配套家具清单(companion_items)，请重试")
    return rules
