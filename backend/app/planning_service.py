import json
import sqlite3
import uuid
import random
import re
from typing import Any, Dict, Optional


def _rule_value(rule: Dict[str, Any], key: str, default: Any) -> Any:
    return rule.get(key, default)


PANORAMA_VIEWS = {
    'front': ('正面广角', '从床尾正面向床屏拍摄，适度广角呈现床与两侧家具'),
    'left': ('左侧斜向', '从客房左前侧向床区拍摄，展示床与配套家具的空间层次'),
    'right': ('右侧斜向', '从客房右前侧向床区拍摄，展示床与配套家具的空间层次'),
    'entry': ('入口方向', '从客房入口向室内拍摄，兼顾床区和办公收纳家具'),
    'window': ('临窗方向', '从临窗区域向室内拍摄，兼顾床区及入口侧家具'),
}


def choose_panorama_view(history, batch_id=None, exclude=None):
    known = [s for s in history if isinstance(s.get('panorama_view'), dict) and s['panorama_view'].get('id') in PANORAMA_VIEWS]
    recent = known[:30]
    candidates = [key for key in PANORAMA_VIEWS if key != exclude]
    def score(key):
        same_batch = sum(s.get('_batch_id') == batch_id and s['panorama_view']['id'] == key for s in known) if batch_id else 0
        total = sum(s['panorama_view']['id'] == key for s in recent)
        return same_batch, total
    key = min(candidates, key=score)
    label, instruction = PANORAMA_VIEWS[key]
    return {'id': key, 'label': label, 'instruction': instruction}


def redo_panorama_view(conn, spec, design_id, batch_id, series_id, version_id, comment):
    current = spec.get('panorama_view')
    if version_id:
        row = conn.execute('SELECT prompt_metadata FROM slot_versions WHERE id=?', (version_id,)).fetchone()
        if row and row['prompt_metadata']:
            current = json.loads(row['prompt_metadata']).get('panorama_view', current)
    text = (comment or '').strip()
    change = bool(re.search(r'(换|更换|改变|调整|变换|重新|改成|改为)[^，。；,;\n]{0,8}(视角|角度|机位|镜头|方向)|(视角|角度|机位|镜头|方向)[^，。；,;\n]{0,8}(换|更换|改变|调整|变换|重新|改成|改为)', text))
    if re.search(r'(不要|不需要|无需|不必|保持|保留).{0,6}(换|更换|改变|调整|变换|改).{0,6}(视角|角度|机位|镜头|方向)', text):
        change = False
    if change:
        history = [s for s in series_history(conn, series_id) if s.get('_design_id') != design_id]
        current = choose_panorama_view(history, batch_id, (current or {}).get('id'))
        spec['panorama_view'] = current
        conn.execute('UPDATE designs SET structured_spec=? WHERE id=?', (json.dumps(spec, ensure_ascii=False), design_id))
    return current, change


def _choose_combination(rule, history):
    fields = {'headboard': 'headboard_types', 'bed_frame': 'bed_frames',
              'nightstand': 'nightstands', 'wardrobe': 'wardrobe_types',
              'material': 'materials', 'color': 'color_palette'}
    options = {key: list(dict.fromkeys(rule.get(source) or [])) for key, source in fields.items()}
    rng = random.SystemRandom()
    previous = []
    for s in history:
        if isinstance(s.get('variation_combination'), dict):
            previous.append(s['variation_combination'])
        previous.append({
            'headboard': s.get('headboard'), 'bed_frame': s.get('bed_frame'),
            'nightstand': s.get('nightstand'), 'wardrobe': s.get('wardrobe'),
            'material': (s.get('main_materials') or [None])[0],
            'color': (s.get('color_scheme') or [None])[0],
        })
    candidates = [{key: rng.choice(values) if values else '' for key, values in options.items()} for _ in range(80)]
    def score(candidate):
        return max((sum(bool(candidate[k]) and candidate[k] == old.get(k) for k in fields) for old in previous), default=0)
    chosen = min(candidates, key=score)
    insufficient = [key for key, values in options.items() if len(values) < 2]
    return chosen, insufficient, score(chosen)


def series_history(conn, series_id):
    rows = conn.execute("SELECT id,batch_id,structured_spec FROM designs WHERE series_id=? AND status != 'eliminated' ORDER BY created_at DESC, rowid DESC", (series_id,)).fetchall()
    history = []
    for row in rows:
        try:
            spec = json.loads(row['structured_spec'])
            spec['_batch_id'] = row['batch_id']
            spec['_design_id'] = row['id']
            history.append(spec)
        except (TypeError, ValueError):
            continue
    return history


def generate_design_spec_from_rule(
    series_id: str,
    index: int,
    rule: Dict[str, Any],
    is_standby: bool = False,
    history: Optional[list] = None,
    batch_id: Optional[str] = None,
) -> Dict[str, Any]:
    headboards = _rule_value(rule, "headboard_types", [])
    bed_frames = _rule_value(rule, "bed_frames", [])
    nightstands = _rule_value(rule, "nightstands", [])
    wardrobes = _rule_value(rule, "wardrobe_types", [])
    materials = _rule_value(rule, "materials", [])
    colors = _rule_value(rule, "color_palette", [])
    if not headboards or not bed_frames or not nightstands or not wardrobes:
        raise ValueError(f"系列 {series_id} 的冻结规则缺少必备家具字段")

    combination, insufficient, similarity = _choose_combination(rule, history or [])

    return {
        "series_id": series_id,
        "series_name": rule.get("name", series_id),
        "model_title": f"{rule.get('name', series_id)} - 方案 {index + 1}{' (候补备选)' if is_standby else ''}",
        "template_version": rule["template_version"],
        "headboard": combination['headboard'],
        "bed_frame": combination['bed_frame'],
        "nightstand": combination['nightstand'],
        "wardrobe": combination['wardrobe'],
        "main_materials": [combination['material']] if combination['material'] else [],
        "accent_material": "",
        "color_scheme": [combination['color']] if combination['color'] else [],
        "variation_combination": combination,
        "panorama_view": choose_panorama_view(history or [], batch_id),
        "variation_warning": '部分系列选项不足或组合接近已有方案，建议补充风格规则' if insufficient or similarity >= 5 else '',
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
    if slot_type == 'panorama' and structured_spec.get('panorama_view'):
        metadata['panorama_view'] = structured_spec['panorama_view']
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
    "nightstand_closeup": "白底家具细节展板：左右并排展示两处床及床屏等局部特写",
}

_PANORAMA_ANCHOR = (
    "本款家具保持系列风格协调，同时清晰呈现本方案指定的床屏、床体、柜体造型及材质配色，让不同方案在家具外观上各有特点；严格遵循本系列实际的禁用元素和禁用工艺，不凭空增加统一的造型禁令。空间布局、镜头角度和柔和光线可适度变化，环境保持克制，家具完整清晰，整体氛围由本款家具的材质与工艺自然呈现。"
)


def _panorama_coverage_line(structured_spec: Dict[str, Any]) -> Optional[str]:
    """全景家具覆盖清单：逐件点名方案配套家具，防止生图遗漏或被遮挡。"""
    companion = [str(c).strip() for c in (structured_spec.get("companion_items") or []) if str(c).strip()]
    if not companion:
        return None
    return (
        "家具覆盖要求：画面必须把方案家具逐件完整呈现——床屏、床体、左右两只床头柜必须全部可见，"
        "配套家具的每一件也必须出现且可辨认：" + "；".join(companion) +
        "。任何一件都不得省略或被完全遮挡；空间不足时宁可缩小家具占比、拉开镜头，也不得删减家具种类。"
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
            lines = [
                "参考输入图片中的家具，分析并提取其中的产品设计特征，以原图家具为设计依据，重新创作一张高端酒店家具整套组合白底展示图。",
                "版式是家具产品目录式的多行分区排版，整张正方形画面只展示一套家具，不能保持原房间的透视和空间位置。将产品分组独立陈列，每组之间有明显白色间隔，不能相互遮挡或连接成一个房间。",
                "将参考全景中可辨认的全部家具逐件提取，按照数量灵活分成两至三行：床及床屏组合可作为较大的主视觉，其余家具独立分组均衡陈列。只展示参考图中实际出现且可辨认的家具，不凭空添加、重复或重新设计家具；未出现的家具无需补齐，缺少的位置自然留白。",
                "整个画面像家具目录的组合陈列，不画分格线、边框或文字；各组在同一白底上按行错落排列，彼此分离。每件家具包括柜体、桌脚、床尾均须完整入镜，四周留有足够空白，不要被画面边缘裁切。",
                "忠实保留参考图中家具的款式、造型、结构、比例、材质、木纹、软包、颜色及装饰细节。重新排列不等于重新设计，不得擅自修改原有产品外观；局部被遮挡的结构只做必要的合理补全。",
                "产品以合理的三分之四视角展示，床组最大，其他组按版面适当缩小以完整展示；不同产品组分别投下轻微接触阴影，家具之间保持清晰间距。床屏仅保留产品本身的完整背板，不保留房间整面墙。",
                "呈现真实的商业家具摄影质感。准确表现木材纹理、布艺肌理、软包形态、皮革质感、金属反射及石材纹理。采用柔和均匀的摄影棚光照，家具底部具有自然的浅灰色接触阴影，体现真实的体积感与落地感。避免过度光滑、塑料质感、结构变形及不合理的透视关系。",
                "最终生成的是一张重新设计布局的酒店家具组合产品图，而不是原始场景的背景移除版本。纯白无缝背景，无室内环境，无文字，无品牌标识，无水印。",
            ]
        elif slot_type == "twin_bed":
            lines = [
                "以参考白底家具展板为唯一产品和风格依据，生成同系列酒店双床选配方案白底产品展板。将参考图中的一张大床适配为两张独立单人床，必须恰好两张，并列摆放；保留原床的造型语言、床体结构、软包、材质、木纹、配色和细节，仅对床宽与必要结构做适配。床屏可采用一体式或分段式，风格必须与原床屏一致；双床之间与外侧按需配置与原床头柜同款的床头柜，双床不配床尾凳。",
                "下方选配套件的排版必须与参考展板明显不同：禁止照搬参考展板中的全部家具，仅从其中精选三组代表性单品交叉组合展示，例如电视柜组合、书桌配书椅组合、沙发配圆茶几组合；务必省略衣柜与床尾凳等大件，也不新增参考展板中不存在的家具。三组配件松弛错落摆放在双床下方，间距开阔、留白充足，突出模块化选配与空间灵活性。",
                "与参考展板保持一致的45°斜鸟瞰视角、写实商业家具摄影、纯白无缝背景和柔和均匀影棚光，保留轻微自然接触阴影。无墙体、窗户、人物、文字、标识和水印。",
            ]
        elif slot_type == "nightstand_closeup":
            lines = [
                "以参考白底展板中的床及床屏等家具为唯一设计依据，制作一张家具局部细节展示图。画面左右并排展示两处不同的局部特写，左右等分、大小一致，仅展示局部，不展示完整家具。",
                "选择参考图中实际存在的床屏软包、床体、床头柜或连接细节，清晰表现木饰面纹理、布艺肌理、包边缝线、板材分缝及实际存在的五金；严格保持原有造型、材质和颜色，不虚构不存在的细节。",
                "纯白背景、柔和均匀的影棚布光、轻微自然阴影，写实商业家具摄影，细节清晰；无房间环境、人物、文字、标识、水印及多余杂物。",
            ]
        else:
            lines = [
                f"任务：基于参考图生成{_SLOT_OBJECTIVES.get(slot_type, '产品展示图')}，纯白背景，移除一切环境元素。",
            ]
        if slot_type not in ("white_isolated", "twin_bed"):
            if materials:
                lines.append(f"材质（与参考图保持一致）：{'、'.join(materials)}")
            if colors:
                lines.append(f"配色（与参考图保持一致）：{'、'.join(colors)}")
        lines.append("构图要求：正方形 1:1，写实商业产品摄影质感，均匀柔和的影棚布光，轻微投影。")
    else:
        # ---- 文生图提示词（全景）----
        view = meta.get('panorama_view', structured_spec.get('panorama_view')) if slot_type == 'panorama' else None
        view_line = ('本次全景镜头要求（忽略其他与本要求冲突的机位和视角描述）：' + view['instruction'] + '；床、床屏、床头柜和配套家具尽量完整清晰。') if isinstance(view, dict) and view.get('instruction') else ''
        if slot_type == 'panorama' and structured_spec.get('panorama_prompt'):
            custom = structured_spec['panorama_prompt'].strip()
            reason = meta.get('redo_reason_category')
            if reason:
                custom += '\n本次重做需修正：' + str(reason) + ' ' + str(meta.get('detail_comment') or '')
            coverage = _panorama_coverage_line(structured_spec)
            out = custom + ('\n' + view_line if view_line else '') + '\n' + _PANORAMA_ANCHOR
            if forbidden or processes:
                out += '\n本系列禁止元素：' + '、'.join(forbidden) + '；禁止工艺：' + '、'.join(processes)
            if coverage:
                out += '\n' + coverage
            return out + '\n家具须作为统一系列完整呈现：让方案中的床、床屏、床头柜及配套家具在画面内尽可能完整、清晰可辨，避免被画面边缘裁切或大面积遮挡，以便后续提取为白底产品展板。\n构图要求：正方形 1:1，画面中不得出现任何文字、水印、LOGO 或人物。'
        lines = [f"为酒店家具定制方案「{design_code}」生成{_SLOT_OBJECTIVES.get(slot_type, ' Approved hotel furniture visual.')}",
                 "构图要求：正方形 1:1，写实商业摄影质感，柔和均匀布光，高级酒店样板间水准。方案内家具尽可能完整清晰可辨，避免裁切和遮挡，以便后续提取产品。"]

        if view_line:
            lines.insert(0, view_line)
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
        if slot_type == "panorama":
            lines.append(_PANORAMA_ANCHOR)
            coverage = _panorama_coverage_line(structured_spec)
            if coverage:
                lines.append(coverage)

    if (forbidden or processes) and not (has_reference_image and slot_type in ("white_isolated", "twin_bed")):
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
