import io
from typing import Dict, Any, Optional
from PIL import Image, ImageDraw, ImageFont
from .config import (
    RAW_IMAGES_DIR,
    PREVIEW_IMAGES_DIR,
    DEFAULT_WATERMARK,
    SEEZO_WATERMARK_PATH,
    SEEZO_WATERMARK_WIDTH_RATIO,
    SEEZO_WATERMARK_LEFT_RATIO,
    SEEZO_WATERMARK_TOP_RATIO,
)

def get_default_font(size: int = 24) -> ImageFont.ImageFont:
    """获取默认字体，跨平台 fallback"""
    try:
        # Windows 常用字体
        return ImageFont.truetype("arial.ttf", size)
    except Exception:
        try:
            return ImageFont.truetype("msyh.ttc", size)
        except Exception:
            return ImageFont.load_default()

def create_mock_furniture_image(
    slot_type: str,
    series_id: str,
    design_code: str,
    version_num: int,
    structured_spec: Dict[str, Any]
) -> Image.Image:
    """
    生成高质量 1024x1024 Mock 家具图像。
    每个槽位有独特的构图视觉特征，绝不混淆：
    - panorama: 场景透视，大床、床头柜两件、高衣柜背景、墙面背板、透视地板线
    - white_isolated: 纯白底色，家具分立重组展台，大床居中，两侧摆放拆分柜体
    - twin_bed: 标称两张独立单人床并列，贯穿式床屏，中间独立连体柜
    - nightstand_closeup: 局部特写，突出床头柜反弹抽屉、台面台灯、床屏嵌条接缝
    """
    width, height = 1024, 1024
    
    # 色调根据系列定制
    if series_id == "modern_business":
        bg_color = (235, 238, 242) if slot_type != "white_isolated" else (255, 255, 255)
        accent_color = (35, 45, 60) # 深商务灰蓝
        wood_color = (65, 45, 35)   # 深胡桃木
        metal_color = (180, 160, 120) # 钛金
        fabric_color = (190, 195, 205)
    elif series_id == "natural_wood":
        bg_color = (248, 245, 238) if slot_type != "white_isolated" else (255, 255, 255)
        accent_color = (120, 90, 60) # 暖木色
        wood_color = (205, 165, 115) # 天然橡木
        metal_color = (195, 155, 80) # 哑光黄铜
        fabric_color = (230, 225, 215) # 棉麻
    else: # soft_contemporary
        bg_color = (246, 242, 240) if slot_type != "white_isolated" else (255, 255, 255)
        accent_color = (160, 130, 140) # 柔和燕麦
        wood_color = (180, 150, 135) # 浅灰木
        metal_color = (215, 185, 160) # 香槟金
        fabric_color = (245, 238, 235) # 羊羔绒奶白

    img = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(img)
    title_font = get_default_font(30)
    sub_font = get_default_font(20)
    small_font = get_default_font(16)

    # 绘制基础装饰与特定槽位结构
    if slot_type == "panorama":
        # 地板透视
        draw.polygon([(0, 650), (1024, 650), (1024, 1024), (0, 1024)], fill=(220, 215, 205))
        for x in range(0, 1024, 90):
            draw.line([(x, 650), (int(x * 1.4) - 200, 1024)], fill=(200, 195, 185), width=2)
        # 背景墙与床屏
        draw.rectangle([160, 280, 864, 650], fill=wood_color, outline=metal_color, width=3)
        # 左侧衣柜背景
        draw.rectangle([60, 150, 180, 650], fill=accent_color, outline=(80, 80, 80), width=2)
        # 大床床体
        draw.rectangle([280, 500, 744, 750], fill=fabric_color, outline=accent_color, width=4)
        draw.rectangle([320, 520, 704, 620], fill=(255, 255, 255), outline=(200, 200, 200), width=2) # 枕头被褥
        # 左右悬挑床头柜
        draw.rectangle([180, 540, 270, 640], fill=wood_color, outline=metal_color, width=2)
        draw.rectangle([754, 540, 844, 640], fill=wood_color, outline=metal_color, width=2)
        slot_label = "SLOT 1: PANORAMA (Hotel Scenario Overview)"

    elif slot_type == "white_isolated":
        # 纯白底解构重组
        draw.rectangle([100, 820, 924, 830], fill=(225, 225, 225)) # 阴影线
        # 居中大床立体透视
        draw.rectangle([340, 420, 684, 720], fill=fabric_color, outline=accent_color, width=3)
        draw.rectangle([310, 320, 714, 420], fill=wood_color, outline=metal_color, width=3) # 床屏
        # 左侧拆分床头柜
        draw.rectangle([160, 540, 280, 680], fill=wood_color, outline=accent_color, width=2)
        # 右侧解构展示衣柜
        draw.rectangle([740, 260, 880, 700], fill=accent_color, outline=metal_color, width=3)
        slot_label = "SLOT 2: WHITE ISOLATED (Full Suite Recomposed)"

    elif slot_type == "twin_bed":
        # 同系列双床适配
        draw.polygon([(0, 700), (1024, 700), (1024, 1024), (0, 1024)], fill=(230, 228, 224))
        # 贯穿式一体背景屏
        draw.rectangle([100, 320, 924, 700], fill=wood_color, outline=metal_color, width=3)
        # 左床 (Twin Bed 1)
        draw.rectangle([160, 520, 460, 780], fill=fabric_color, outline=accent_color, width=3)
        draw.rectangle([190, 540, 430, 620], fill=(255, 255, 255), outline=(180, 180, 180), width=1)
        # 右床 (Twin Bed 2)
        draw.rectangle([564, 520, 864, 780], fill=fabric_color, outline=accent_color, width=3)
        draw.rectangle([594, 540, 834, 620], fill=(255, 255, 255), outline=(180, 180, 180), width=1)
        # 中间连体床头柜
        draw.rectangle([475, 540, 549, 660], fill=accent_color, outline=metal_color, width=2)
        slot_label = "SLOT 3: TWIN BED (Adapted Dual Configuration)"

    else: # nightstand_closeup
        # 床头柜局部特写
        draw.rectangle([100, 100, 924, 924], fill=(240, 238, 235))
        # 局部高大床屏边缘
        draw.rectangle([100, 100, 420, 924], fill=wood_color, outline=metal_color, width=4)
        # 床头柜主体（大比例突出）
        draw.rectangle([380, 440, 840, 840], fill=accent_color, outline=(255, 255, 255), width=3)
        # 抽屉接缝与金属拉手
        draw.line([(380, 620), (840, 620)], fill=metal_color, width=3)
        draw.rectangle([560, 520, 660, 535], fill=metal_color)
        draw.rectangle([560, 720, 660, 735], fill=metal_color)
        # 台灯基座
        draw.ellipse([540, 360, 680, 440], fill=metal_color, outline=(255, 255, 255), width=2)
        slot_label = "SLOT 4: NIGHTSTAND CLOSEUP (Detail Focus)"

    # 顶部和底部的清晰元数据框（确保 Mock 具有完全可信度和透明度）
    draw.rectangle([20, 20, 1004, 90], fill=(255, 255, 255, 230), outline=(200, 200, 200), width=1)
    draw.text((40, 32), slot_label, fill=(20, 20, 20), font=title_font)
    meta_str = f"Series: {series_id.upper()} | Design: {design_code} | Version: v{version_num} | Status: LOCAL MOCK"
    draw.text((40, 65), meta_str, fill=(90, 90, 90), font=sub_font)

    # 底部清晰的本地 MOCK 警示水印（原图中就固定带有，绝不假冒真实）
    draw.rectangle([20, 940, 1004, 1004], fill=(240, 240, 240, 220), outline=(210, 210, 210), width=1)
    mock_notice = "[MOCK VISUAL PROTOTYPE - OFFLINE GENERATED FOR WORKBENCH REVIEW]"
    draw.text((40, 958), mock_notice, fill=(120, 60, 40), font=sub_font)
    
    return img

def apply_watermark(
    image: Image.Image,
    main_text: str = DEFAULT_WATERMARK["main_text"],
    repeat_text: str = DEFAULT_WATERMARK["repeat_text"],
    opacity: float = DEFAULT_WATERMARK["opacity"],
    font_size: int = DEFAULT_WATERMARK["font_size"],
    position: str = DEFAULT_WATERMARK["position"],
    *,
    watermark_cfg: Optional[Dict[str, Any]] = None,
) -> Image.Image:
    """Render the single SEEZO logo watermark used by previews and exports.

    ``main_text``, ``repeat_text``, ``opacity``, ``font_size`` and ``position``
    remain accepted for old callers and request payloads. They are deliberately
    ignored: logo geometry is controlled only by the explicit SEEZO settings in
    :mod:`config`, so no text or tiled watermark can leak into an output.
    """
    del main_text, repeat_text, opacity, font_size, position
    cfg = watermark_cfg or DEFAULT_WATERMARK
    width_ratio = float(cfg.get("logo_width_ratio", SEEZO_WATERMARK_WIDTH_RATIO))
    left_ratio = float(cfg.get("logo_left_ratio", SEEZO_WATERMARK_LEFT_RATIO))
    top_ratio = float(cfg.get("logo_top_ratio", SEEZO_WATERMARK_TOP_RATIO))
    if width_ratio <= 0 or width_ratio > 1:
        raise ValueError("logo_width_ratio must be between 0 and 1")
    if left_ratio < 0 or top_ratio < 0:
        raise ValueError("logo offsets must be non-negative")

    base = image.convert("RGBA")
    with Image.open(SEEZO_WATERMARK_PATH) as source:
        logo = source.convert("RGBA")
    width = max(1, round(base.width * width_ratio))
    height = max(1, round(width * logo.height / logo.width))
    logo = logo.resize((width, height), Image.Resampling.LANCZOS)
    x = round(base.width * left_ratio)
    y = round(base.height * top_ratio)
    base.alpha_composite(logo, (x, y))
    return base.convert("RGB")


def watermark_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Normalize legacy or logo settings to the explicit SEEZO config."""
    normalized = dict(DEFAULT_WATERMARK)
    if config:
        normalized.update({
            key: config[key]
            for key in (
                "renderer", "logo_asset", "logo_width_ratio", "logo_left_ratio", "logo_top_ratio",
                # Preserve legacy API/database fields for compatibility. The
                # renderer intentionally ignores them.
                "main_text", "repeat_text", "opacity", "font_size", "position",
            )
            if key in config and config[key] is not None
        })
    # Use the approved logo geometry even for batches saved with older defaults.
    normalized.update({
        "logo_width_ratio": SEEZO_WATERMARK_WIDTH_RATIO,
        "logo_left_ratio": SEEZO_WATERMARK_LEFT_RATIO,
        "logo_top_ratio": SEEZO_WATERMARK_TOP_RATIO,
    })
    normalized["legacy_text_ignored"] = True
    return normalized


def ensure_watermark_config_schema(conn) -> None:
    """Add explicit SEEZO settings to databases created before this renderer.

    Legacy text/tile columns remain for request and database compatibility, but
    these fields document the settings used by the only watermark renderer.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(watermark_configs)")}
    columns = {
        "renderer": "TEXT NOT NULL DEFAULT 'seezo_logo'",
        "logo_asset": "TEXT NOT NULL DEFAULT 'static/seezo-watermark.png'",
        "logo_width_ratio": "REAL NOT NULL DEFAULT 0.2",
        "logo_left_ratio": "REAL NOT NULL DEFAULT 0.0",
        "logo_top_ratio": "REAL NOT NULL DEFAULT 0.02",
    }
    for name, declaration in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE watermark_configs ADD COLUMN {name} {declaration}")


def save_real_slot_image(
    raw_bytes: bytes,
    slot_type: str,
    series_id: str,
    design_code: str,
    version_num: int,
    watermark_cfg: Optional[Dict[str, Any]] = None,
    version_id: Optional[str] = None
) -> tuple[str, str]:
    """
    保存真实上游返回的图片：
    - 统一转成 RGB PNG 原图落盘 RAW_IMAGES_DIR
    - 预览图打水印、长边缩至 800 后落盘 PREVIEW_IMAGES_DIR（真实图可能非正方形，按比例缩放）
    返回 (raw_rel_path, preview_rel_path)
    """
    import io
    img = Image.open(io.BytesIO(raw_bytes))
    img.load()
    if img.mode != "RGB":
        img = img.convert("RGB")

    v_tag = f"_{version_id[:8]}" if version_id else ""
    file_stem = f"{design_code}_{slot_type}_v{version_num}{v_tag}"
    raw_filename = f"{file_stem}_raw.png"
    preview_filename = f"{file_stem}_preview.jpg"

    img.save(RAW_IMAGES_DIR / raw_filename, format="PNG")

    wm = watermark_config(watermark_cfg)
    preview_img = apply_watermark(img, watermark_cfg=wm)
    preview_img.thumbnail((800, 800), Image.Resampling.LANCZOS)
    preview_img.save(PREVIEW_IMAGES_DIR / preview_filename, format="JPEG", quality=88)
    return raw_filename, preview_filename


def save_mock_slot_image(
    slot_type: str,
    series_id: str,
    design_code: str,
    version_num: int,
    structured_spec: Dict[str, Any],
    watermark_cfg: Optional[Dict[str, Any]] = None,
    version_id: Optional[str] = None
) -> tuple[str, str]:
    """
    生成并落盘槽位原图与预览图：
    - 原图保存至 RAW_IMAGES_DIR
    - 预览图打上轻量预览水印并保存至 PREVIEW_IMAGES_DIR
    返回 (raw_rel_path, preview_rel_path)
    """
    raw_img = create_mock_furniture_image(
        slot_type, series_id, design_code, version_num, structured_spec
    )
    v_tag = f"_{version_id[:8]}" if version_id else ""
    file_stem = f"{design_code}_{slot_type}_v{version_num}{v_tag}"
    raw_filename = f"{file_stem}_raw.png"
    preview_filename = f"{file_stem}_preview.jpg"

    raw_abs_path = RAW_IMAGES_DIR / raw_filename
    preview_abs_path = PREVIEW_IMAGES_DIR / preview_filename

    # 保存高质量原图 PNG
    raw_img.save(raw_abs_path, format="PNG")

    # 预览图打上预览水印并轻量压缩
    wm = watermark_config(watermark_cfg)
    preview_img = apply_watermark(raw_img, watermark_cfg=wm)
    # 缩小适度提高网页传输效率（如 800x800）
    preview_img_resized = preview_img.resize((800, 800), Image.Resampling.LANCZOS)
    preview_img_resized.save(preview_abs_path, format="JPEG", quality=88)

    return raw_filename, preview_filename
