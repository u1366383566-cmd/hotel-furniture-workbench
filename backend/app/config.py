import os
from pathlib import Path

# 基础目录
BASE_DIR = Path(__file__).resolve().parent.parent.parent
STORAGE_DIR = Path(os.environ.get("WORKBENCH_STORAGE_DIR", BASE_DIR / "storage")).resolve()
DB_DIR = STORAGE_DIR / "db"
RAW_IMAGES_DIR = STORAGE_DIR / "raw_images"
PREVIEW_IMAGES_DIR = STORAGE_DIR / "preview_images"
EXPORTS_DIR = STORAGE_DIR / "exports"

for d in [STORAGE_DIR, DB_DIR, RAW_IMAGES_DIR, PREVIEW_IMAGES_DIR, EXPORTS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.environ.get("WORKBENCH_DB_PATH", DB_DIR / "workbench.db")).resolve()

# 运行模式：第一阶段强约束为 Mock 本地离线模式
MOCK_MODE = True
ALLOW_REMOTE_NETWORK = False

# 每组默认预算：自动任务按目标组数计算 5 次预警、6 次硬上限。
# Legacy explicit batch budgets remain accepted for backwards compatibility.
DEFAULT_WARNING_PER_GROUP = 8
DEFAULT_HARD_LIMIT_PER_GROUP = 10
DEFAULT_WARNING_BUDGET = 20
DEFAULT_HARD_LIMIT_BUDGET = 25
DEFAULT_TARGET_ARCHIVED = 3
DEFAULT_REDO_SLOT_LIMIT = 2
DEFAULT_REDO_DESIGN_LIMIT = 4

# 产品系列规范库（三大系列）
SERIES_RULES = {
    "modern_business": {
        "id": "modern_business",
        "name": "现代商务",
        "name_en": "Modern Business",
        "description": "极简硬朗、沉稳干练，专为高端商务差旅酒店定制",
        "headboard_types": ["全包覆硬朗皮质分块床屏", "深色木饰面嵌入拉丝金属线条背板"],
        "bed_frames": ["一体式嵌入型悬浮大床", "落地封闭式防滑踢脚床体"],
        "nightstands": ["悬挑式连体床头柜配隐形反弹抽屉", "金属几何支架落地小边几"],
        "wardrobe_types": ["深灰铝框透光灰玻衣柜", "深胡桃木色通顶收纳柜体"],
        "materials": ["深胡桃木饰面", "哑光拉丝钛金", "科技皮革", "深灰色钢化玻璃"],
        "color_palette": ["冷灰", "深胡桃木色", "钛金黑", "暖白局部跳色"],
        "forbidden_elements": ["欧式雕花", "过度复古做旧", "多余圆弧波浪装饰", "高饱和度跳色"],
        "forbidden_processes": ["复杂手工雕花", "高光钢琴漆", "异形热弯玻璃"],
        "soft_upholstery_usage": "仅床屏局部、坐面软垫和床头靠背使用科技皮革/织物",
        "metal_usage": "仅拉丝钛金线条、踢脚收口和有限五金拉手",
        "allowed_structural_changes": ["床宽适配", "床屏分段调整", "床头柜组合拆分", "柜体朝向调整"],
        "hotel_mood": "高端商务差旅、克制高效、安静整洁",
        "companion_items": ["双人位布艺转角沙发配大理石圆茶几", "单人皮质扶手椅配圆形边几", "书桌配皮质座椅", "深胡桃木电视柜配壁挂电视", "金属木质行李架", "皮质软包床尾凳", "深胡桃木落地灯"],
        "twin_adapter_rule": "保持床屏横向对称分段与金属收口模数，分化为双单人床并居中配置独立连体柜",
        "white_bg_rule": "解构全景中深胡桃木柜体、大床及床头柜，在纯白影棚背景下以立体透视平铺组合",
    },
    "natural_wood": {
        "id": "natural_wood",
        "name": "自然木质",
        "name_en": "Natural Wood",
        "description": "质朴温暖、原木温润，专为自然度假与精品美学酒店定制",
        "headboard_types": ["天然白橡木直纹格栅全景背板", "原木边框嵌入米白麻织软包"],
        "bed_frames": ["实木厚边圆角悬浮床架", "浅色原木低矮日式地台床"],
        "nightstands": ["实木双层开放式转角置物柜", "圆柱形原木雕刻一体床头小墩"],
        "wardrobe_types": ["无把手白橡木移门衣柜", "开放式挂衣与收纳隔板组合"],
        "materials": ["天然白橡木", "原色白蜡木", "质朴粗织棉麻", "哑光黄铜配件"],
        "color_palette": ["原木暖色", "米白", "亚麻浅灰", "苔藓绿点缀"],
        "forbidden_elements": ["亮光烤漆", "塑料质感贴皮", "复杂欧式构件", "暗沉重工业风"],
        "forbidden_processes": ["高光烤漆", "强腐蚀做旧", "复杂雕刻贴金"],
        "soft_upholstery_usage": "仅床屏局部和坐垫使用棉麻织物，禁止大面积软包",
        "metal_usage": "仅哑光黄铜小五金和有限包边，禁止工业金属框架",
        "allowed_structural_changes": ["双床等距排布", "背板分段延伸", "床头柜模块化组合", "地台高度适配"],
        "hotel_mood": "自然度假、温暖质朴、安静亲肤",
        "companion_items": ["双人位棉麻布艺沙发配原木茶几", "原木扶手椅配圆形边几", "原木书桌配编织座椅", "白橡木电视柜", "藤木行李架", "原木床尾凳", "纸艺落地灯"],
        "twin_adapter_rule": "格栅背板贯穿墙面，两张原木单人床等距排布，中间设有一体格栅置物台",
        "white_bg_rule": "原木单品解构为轻盈透气组合，白底背景凸显天然木纹真实肌理与圆润边角",
    },
    "soft_contemporary": {
        "id": "soft_contemporary",
        "name": "柔和当代",
        "name_en": "Soft Contemporary",
        "description": "流线弧度、温和优雅，专为当代轻奢与女性偏好高端度假酒店定制",
        "headboard_types": ["流线型弧度包边奶白软包大背板", "曲面微弧双色分层床屏"],
        "bed_frames": ["圆弧倒角软包边框防磕碰床体", "内缩式超轻体感悬浮床架"],
        "nightstands": ["圆角半开放式烤漆床头柜", "水磨石台面椭圆双层床头柜"],
        "wardrobe_types": ["奶油风圆弧收边通顶隐形门衣柜", "暖光嵌入式无边框衣帽区"],
        "materials": ["亲肤羊羔绒/微水泥感科技布", "珍珠白细腻烤漆", "香槟金微细金属件", "温润人造石"],
        "color_palette": ["奶油米白", "柔沙色", "香槟金", "浅燕麦灰"],
        "forbidden_elements": ["锐利尖角", "大面积深黑工业铁艺", "厚重压抑硬质线条"],
        "forbidden_processes": ["锐利金属切割外露", "冷硬拉丝黑钢", "高对比硬拼色"],
        "soft_upholstery_usage": "床屏、床边和床头凳使用羊羔绒/科技布，以圆弧包边收口",
        "metal_usage": "仅香槟金细线、小型拉手和有限支脚，禁止结构外露铁艺",
        "allowed_structural_changes": ["床宽适配", "弧度半径调整", "背板贯通或分段", "床头柜形态适配"],
        "hotel_mood": "当代轻奢、柔和放松、精致温馨",
        "companion_items": ["弧形米白布艺沙发配香槟金圆茶几", "羊羔绒扶手椅配椭圆小边几", "奶油色梳妆书桌配软包座椅", "圆弧收边电视柜", "香槟金行李架", "软包长条床尾凳", "弧形落地灯"],
        "twin_adapter_rule": "采用波浪流线造型贯通双床背板，两张圆角双床优雅并列，配圆柱床头柜",
        "white_bg_rule": "纯白干净底色下突出家具的柔美弧度、羊羔绒触感与温润奶白光影",
    }
}

# SEEZO 水印设置
#
# The renderer deliberately has one source of truth: the transparent SEEZO logo
# shipped with the application. Legacy text fields remain in this mapping so old
# batch rows and request payloads can still be read, but they are compatibility
# fields only and are never used when an image is rendered or exported.
SEEZO_WATERMARK_PATH = BASE_DIR / "static" / "seezo-watermark.png"
SEEZO_WATERMARK_ASSET = "static/seezo-watermark.png"
SEEZO_WATERMARK_WIDTH_RATIO = 418 / 1024
SEEZO_WATERMARK_LEFT_RATIO = 0.0
SEEZO_WATERMARK_TOP_RATIO = 83 / 1024

DEFAULT_WATERMARK = {
    "renderer": "seezo_logo",
    "logo_asset": SEEZO_WATERMARK_ASSET,
    "logo_width_ratio": SEEZO_WATERMARK_WIDTH_RATIO,
    "logo_left_ratio": SEEZO_WATERMARK_LEFT_RATIO,
    "logo_top_ratio": SEEZO_WATERMARK_TOP_RATIO,
    # Backward-compatible request/database fields. They are intentionally not
    # consulted by watermark_service.apply_watermark().
    "main_text": "HOTEL WORKBENCH - CONFIDENTIAL",
    "repeat_text": "SAMPLE PREVIEW NOT FOR COMMERCIAL",
    "opacity": 0.22,
    "font_size": 36,
    "position": "bottom_right",
}
