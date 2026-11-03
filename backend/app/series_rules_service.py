import json
import re
import sqlite3
import uuid
from typing import Any, Dict
from .config import SERIES_RULES

# 新建系列必须提供的规划字段：缺一无法生成结构化设计方案
REQUIRED_SERIES_KEYS = [
    "headboard_types", "bed_frames", "nightstands", "wardrobe_types",
    "materials", "color_palette",
]
SERIES_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,31}$")


def get_configured_series_rules(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """Load persisted rule versions, seeding defaults on first use."""
    cur = conn.cursor()
    cur.execute("SELECT series_id, rule_json, template_version FROM series_rule_configs")
    rows = {row["series_id"]: row for row in cur.fetchall()}
    for series_id, default_rule in SERIES_RULES.items():
        if series_id not in rows:
            template_version = "tpl_2026_v1.0"
            conn.execute(
                """INSERT INTO series_rule_configs (series_id, rule_json, template_version)
                   VALUES (?, ?, ?, 'system')""",
                (series_id, json.dumps(default_rule, ensure_ascii=False), template_version),
            )
            rows[series_id] = {
                "series_id": series_id,
                "rule_json": json.dumps(default_rule, ensure_ascii=False),
                "template_version": template_version,
            }

    result = {}
    for series_id, row in rows.items():
        rule = json.loads(row["rule_json"])
        rule["id"] = series_id
        rule["template_version"] = row["template_version"]
        result[series_id] = rule
    return result


def create_series_rule(
    conn: sqlite3.Connection,
    series_id: str,
    name: str,
    rule: Dict[str, Any],
    updated_by: str,
) -> Dict[str, Any]:
    """
    负责人自定义新增产品系列：
    - series_id 必须是小写英文标识（用于批次快照与设计编号）
    - 必填六个规划字段齐全且非空；其余字段沿用内置模板的默认值
    - 规划提示词后续由系统按槽位目标自动生成，人工只维护这套结构化规则
    """
    if not SERIES_ID_PATTERN.match(series_id or ""):
        raise ValueError("系列标识必须是 3-32 位小写英文/数字/下划线，且以字母开头")
    if series_id in SERIES_RULES:
        raise ValueError("该标识与内置系列冲突")
    configured = get_configured_series_rules(conn)
    if series_id in configured:
        raise ValueError("该系列标识已存在")

    merged = dict(rule or {})
    # 未显式提供的描述性字段沿用内置模板默认值，保证规划提示词结构完整
    for key, value in SERIES_RULES["modern_business"].items():
        merged.setdefault(key, value)
    merged["id"] = series_id
    merged["name"] = name
    missing = [k for k in REQUIRED_SERIES_KEYS
               if not isinstance(merged.get(k), list) or not merged[k]]
    if missing:
        raise ValueError(f"缺少必填规划字段或为空: {missing}")

    template_version = f"tpl_{series_id}_{uuid.uuid4().hex[:10]}"
    merged["template_version"] = template_version
    conn.execute(
        """INSERT INTO series_rule_configs (series_id, rule_json, template_version, updated_by)
           VALUES (?, ?, ?, ?)""",
        (series_id, json.dumps(merged, ensure_ascii=False), template_version, updated_by),
    )
    return merged


def update_series_rule(
    conn: sqlite3.Connection,
    series_id: str,
    rules: Dict[str, Any],
    updated_by: str,
) -> Dict[str, Any]:
    """Persist a new rule revision. Existing designs keep their stored template_version."""
    configured = get_configured_series_rules(conn)
    if series_id not in configured:
        raise ValueError("series not found")

    rule = dict(configured[series_id])
    rule.update(rules)
    missing = [k for k in REQUIRED_SERIES_KEYS if not isinstance(rule.get(k), list) or not rule[k]]
    if missing:
        raise ValueError(f"缺少必填规划字段或为空: {missing}")
    rule["id"] = series_id
    # 内置系列名称固定；自定义系列保留其存储名称
    if series_id in SERIES_RULES:
        rule["name"] = SERIES_RULES[series_id]["name"]
    elif not rule.get("name"):
        rule["name"] = series_id
    new_version = f"tpl_{series_id}_{uuid.uuid4().hex[:10]}"
    rule["template_version"] = new_version

    conn.execute(
        """UPDATE series_rule_configs
           SET rule_json = ?, template_version = ?, updated_by = ?,
               updated_at = datetime('now', 'localtime')
           WHERE series_id = ?""",
        (json.dumps(rule, ensure_ascii=False), new_version, updated_by, series_id),
    )
    return rule
