import json
import sqlite3
import uuid
from typing import Any, Dict, Optional


def _rule_value(rule: Dict[str, Any], key: str, default: Any) -> Any:
    return rule.get(key, default)


def generate_design_spec_from_rule(
    series_id: str,
    index: int,
    rule: Dict[str, Any],
    is_standby: bool = False,
) -> Dict[str, Any]:
    headboards = _rule_value(rule, "headboard_types", [])
    bed_frames = _rule_value(rule, "bed_frames", [])
    nightstands = _rule_value(rule, "nightstands", [])
    wardrobes = _rule_value(rule, "wardrobe_types", [])
    materials = _rule_value(rule, "materials", [])
    colors = _rule_value(rule, "color_palette", [])
    if not headboards or not bed_frames or not nightstands or not wardrobes:
        raise ValueError(f"系列 {series_id} 的冻结规则缺少必备家具字段")

    return {
        "series_id": series_id,
        "series_name": rule.get("name", series_id),
        "model_title": f"{rule.get('name', series_id)} - 方案 {index + 1}{' (候补备选)' if is_standby else ''}",
        "template_version": rule["template_version"],
        "headboard": headboards[index % len(headboards)],
        "bed_frame": bed_frames[(index + 1) % len(bed_frames)],
        "nightstand": nightstands[index % len(nightstands)],
        "wardrobe": wardrobes[index % len(wardrobes)],
        "main_materials": materials[:2],
        "accent_material": materials[2] if len(materials) > 2 else materials[0] if materials else "",
        "color_scheme": colors[:3],
        "soft_upholstery_usage": _rule_value(rule, "soft_upholstery_usage", ""),
        "metal_usage": _rule_value(rule, "metal_usage", ""),
        "allowed_structural_changes": _rule_value(rule, "allowed_structural_changes", []),
        "forbidden_elements": _rule_value(rule, "forbidden_elements", []),
        "forbidden_processes": _rule_value(rule, "forbidden_processes", []),
        "hotel_mood": _rule_value(rule, "hotel_mood", ""),
        "twin_adapter_rule": _rule_value(rule, "twin_adapter_rule", ""),
        "white_bg_rule": _rule_value(rule, "white_bg_rule", ""),
        "planning_basis": "batch_approved_rule_snapshot",
    }


def get_batch_rule_snapshot(conn: sqlite3.Connection, batch_id: str) -> Optional[Dict[str, Dict[str, Any]]]:
    cur = conn.cursor()
    cur.execute("SELECT approved_series_rules_json FROM batches WHERE id = ?", (batch_id,))
    row = cur.fetchone()
    if not row or not row["approved_series_rules_json"]:
        return None
    return json.loads(row["approved_series_rules_json"])


def build_slot_prompt(
    slot_type: str,
    structured_spec: Dict[str, Any],
    design_code: str,
    template_version: str,
    panorama_version_id: Optional[str] = None,
) -> tuple[str, Dict[str, Any]]:
    objectives = {
        "panorama": "Hotel room panorama; keep scale, structure and approved series harmony.",
        "white_isolated": "White-background full suite recomposition; keep approved materials and structure.",
        "twin_bed": "Same-series twin-bed adaptation; adapt width and headboard without claiming identical structure.",
        "nightstand_closeup": "Nightstand and headboard closeup; focus on bedside area, not unrelated texture.",
    }
    metadata = {
        "mock_only": True,
        "offline_local_generator": True,
        "no_network": True,
        "slot_objective": objectives.get(slot_type, "Approved hotel furniture slot."),
        "design_code": design_code,
        "template_version": template_version,
        "depends_on_panorama_version_id": panorama_version_id,
        "rule_snapshot_locked": True,
    }
    prompt = (
        "LOCAL MOCK VISUAL PROTOTYPE - OFFLINE ONLY. DO NOT SEND TO NETWORK.\n"
        f"Slot: {slot_type}\nObjective: {metadata['slot_objective']}\n"
        f"Design: {design_code}; Template: {template_version}\n"
        "Approved structured design JSON:\n"
        f"{json.dumps(structured_spec, ensure_ascii=False, sort_keys=True)}"
    )
    return prompt, metadata


_SLOT_OBJECTIVES = {
    "panorama": "酒店客房场景全景实拍图：完整呈现家具组合与空间关系，比例真实、透视自然",
    "white_isolated": "纯白背景整套家具重组展示图：家具分立摆放、立体透视平铺，可重新组合与调整朝向",
    "twin_bed": "同系列双床配置白底展示图：两张单人床对称布局，保持原款材料与设计语言，床宽与床屏分段做必要适配",
    "nightstand_closeup": "床头区域白底中近景特写：聚焦床头柜、床屏与床头局部结构，带少量床体",
}


def _white_isolated_layout(companion: list) -> str:
    """白底清单图的固定三行版式（对照用户确认的布局模板）；
    家具项从系列规则 companion_items 动态填充。"""
    def pick(i, default):
        return companion[i] if i < len(companion) else default

    return (
        "布局（严格遵循以下三行版式，从上到下，行高比例约为 45% / 30% / 25%）：\n"
        "第一行（画面上半，家具尺寸最大）：左侧为全套大床组——床（床屏+床体+床品，正面微俯视角）"
        f"、左右各一个床头柜（含台灯）{('；' + pick(5, '床尾凳搭搭毯')) if True else ''}；"
        f"中部为{pick(0, '转角沙发配圆茶几')}；右侧为{pick(1, '单人扶手椅')}。\n"
        f"第二行（画面居中，尺寸中等）：左侧为{pick(3, '电视柜（含壁挂电视与装饰背板）')}；"
        f"中部为{pick(2, '书桌配座椅与台灯')}；右侧为{pick(1, '单人扶手椅配小圆边几')}。\n"
        f"第三行（画面下方，尺寸较小）：从左到右依次为双门衣柜、{pick(4, '金属行李架（挂浴袍）')}、"
        f"{pick(5, '布艺长凳')}、{pick(6, '落地灯')}。"
    )


def build_real_generation_prompt(
    slot_type: str,
    structured_spec: Dict[str, Any],
    design_code: str,
    prompt_metadata: Optional[Dict[str, Any]] = None,
    has_reference_image: bool = False,
) -> str:
    """真实生图提示词：由结构化规则自动生成（人工不写提示词，只维护规则库）。

    has_reference_image=True 时为以图生图（下游三图，参考图=已通过的全景）：
    白底/去环境指令放最前作为强指令，这是防止模型画成房间的关键。
    """
    meta = prompt_metadata or {}
    materials = structured_spec.get("main_materials") or []
    colors = structured_spec.get("color_scheme") or []
    forbidden = structured_spec.get("forbidden_elements") or []
    processes = structured_spec.get("forbidden_processes") or []

    if has_reference_image:
        # ---- 以图生图提示词（下游三图）：视觉锚点 = 参考全景 ----
        if slot_type == "white_isolated":
            companion = structured_spec.get("companion_items") or []
            lines = [
                "任务：制作一张【酒店客房家具套装清单图】——把参考图房间里的家具逐件提取，在纯白背景上按固定三行版式陈列，"
                "作为参考图场景的「家具拆解说明书」，让看图人能把清单里的每一件与参考图场景一一对应。",
                "硬性要求（最高优先级）：背景纯净纯白色 #FFFFFF；每件家具独立完整呈现、互不遮挡；"
                "无窗帘/地毯/地板/墙面/窗户/吊灯/挂画/绿植等任何环境元素。",
                _white_isolated_layout(companion),
                "所有家具保留参考图中的材质、颜色与设计语言，不得更换款式；全部正面微俯视角，彼此留白间距，风格如家具产品目录的全套一览页。",
            ]
        elif slot_type == "twin_bed":
            companion = structured_spec.get("companion_items") or []
            sofa_item = companion[0] if companion else "简约沙发配茶几"
            lines = [
                "以参考图片中的家具为设计依据，生成一张高端酒店双床房家具系列白底产品展示图。",
                "保留参考图中所有家具原有的外观、款式、结构、比例、材质、颜色、木纹及细节，不随意修改产品设计。",
                "画面以两张单人床为核心，每张床配各自独立的完整床屏背板——两块背板彼此分开、中间留出明显间隔，"
                "严禁连接成一整块通长背板，严禁共用床屏。",
                f"搭配{sofa_item}及独立式衣架，其设计需与参考图的整体风格协调，不添加衣柜。",
                "重新组织家具的朝向、间距与前后关系，突出双床配置与独立背板产品，整体构图有层次、留白充足、产品完整。",
                "纯白无缝背景，真实材质与自然接触阴影，无室内环境、无人物、无文字、无标识、无水印。",
            ]
        elif slot_type == "nightstand_closeup":
            headboard_desc = structured_spec.get("headboard", "软包床屏")
            lines = [
                f"以参考图片中的床、床头柜及{headboard_desc}为设计主体，严格保留原有家具的造型、结构、比例、材质、纹理、配色及细节，不进行任何重新设计。",
                "采用纯白无缝背景的商业产品摄影形式，不出现任何室内场景、墙面环境或装饰背景。",
                "镜头聚焦床头区域，采用45°侧前方中近景构图，仅展示床头柜、床屏及少量床体边缘，不拍摄床尾，不展示完整床体，不出现大面积床面。",
                f"重点呈现床头柜的柜体造型与细节、床屏的分块、包边、缝线及材质纹理，以及床、床头柜与床屏之间的组合关系。",
                "家具自然摆放于纯白背景中，轮廓清晰，比例真实，柔和均匀的摄影棚光线，保留轻微自然接触阴影，避免悬浮感与过度修饰。",
                "画面简洁、高端、真实，8K超写实商业家具摄影，无其他家具、无场景布置、无人物、无文字、无标识、无水印。",
            ]
        else:
            lines = [
                f"任务：基于参考图生成{_SLOT_OBJECTIVES.get(slot_type, '产品展示图')}，纯白背景，移除一切环境元素。",
            ]
        if materials:
            lines.append(f"材质（与参考图保持一致）：{'、'.join(materials)}")
        if colors:
            lines.append(f"配色（与参考图保持一致）：{'、'.join(colors)}")
        lines.append("构图要求：正方形 1:1，写实商业产品摄影质感，均匀柔和的影棚布光，轻微投影。")
    else:
        # ---- 文生图提示词（全景）----
        if slot_type == 'panorama' and structured_spec.get('panorama_prompt'):
            custom = structured_spec['panorama_prompt'].strip()
            reason = meta.get('redo_reason_category')
            if reason:
                custom += '\n本次重做需修正：' + str(reason) + ' ' + str(meta.get('detail_comment') or '')
            return custom + '\n画面中不得出现任何文字、水印、LOGO 或人物。'
        lines = [f"为酒店家具定制方案「{design_code}」生成{_SLOT_OBJECTIVES.get(slot_type, ' Approved hotel furniture visual.')}",
                 "构图要求：正方形 1:1，写实商业摄影质感，柔和均匀布光，高级酒店样板间水准。"]

        furniture = (
            f"家具组合：床屏={structured_spec.get('headboard', '')}；"
            f"床体={structured_spec.get('bed_frame', '')}；"
            f"床头柜={structured_spec.get('nightstand', '')}；"
            f"柜体={structured_spec.get('wardrobe', '')}"
        )
        lines.append(furniture)
        companion = structured_spec.get("companion_items") or []
        if companion:
            lines.append(
                "房间内还需完整布置以下配套家具（与核心家具同材质同色系，自然融入场景）："
                + "；".join(companion)
            )
        if materials:
            lines.append(f"主要材质：{'、'.join(materials)}")
        if colors:
            lines.append(f"配色方案：{'、'.join(colors)}")
        if structured_spec.get("soft_upholstery_usage"):
            lines.append(f"软包使用范围：{structured_spec['soft_upholstery_usage']}")
        if structured_spec.get("metal_usage"):
            lines.append(f"金属使用范围：{structured_spec['metal_usage']}")
        if structured_spec.get("hotel_mood"):
            lines.append(f"空间气质：{structured_spec['hotel_mood']}")

    if slot_type == "twin_bed" and structured_spec.get("twin_adapter_rule") and has_reference_image:
        lines.append(f"系列适配参考：{structured_spec['twin_adapter_rule']}")
    elif slot_type == "white_isolated" and structured_spec.get("white_bg_rule") and has_reference_image:
        lines.append(f"白底重组参考：{structured_spec['white_bg_rule']}")

    if forbidden or processes:
        lines.append(f"禁止出现：{'、'.join(forbidden)}；禁止工艺：{'、'.join(processes)}")

    reason = meta.get("redo_reason_category")
    if reason:
        comment = meta.get("detail_comment") or ""
        lines.append(f"本次为重做，需重点修正：{reason}" + (f" — {comment}" if comment else ""))

    lines.append("画面中不得出现任何文字、水印、LOGO 或人物。")
    return "\n".join(lines)


def build_mock_visual_features(
    structured_spec: Dict[str, Any],
    slot_type: str,
    version_id: str,
) -> Dict[str, Any]:
    return {
        "mock_generator": "local_pillow_geometry_v1",
        "version_id": version_id,
        "slot_type": slot_type,
        "series_id": structured_spec.get("series_id"),
        "template_version": structured_spec.get("template_version"),
        "primary_materials": structured_spec.get("main_materials", []),
        "color_scheme": structured_spec.get("color_scheme", []),
        "layout_marker": f"{slot_type}:{uuid.uuid5(uuid.NAMESPACE_URL, version_id).hex[:8]}",
        "visible_mock_labels": ["SLOT", "LOCAL MOCK", "OFFLINE GENERATED"],
    }
