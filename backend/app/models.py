from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field

class BatchCreateRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=100, description="批次名称")
    enabled_series: List[str] = Field(default=["modern_business", "natural_wood", "soft_contemporary"])
    target_archived_count: int = Field(default=3, ge=1, le=50, description="批次目标归档组数")
    designs_per_series: int = Field(default=2, ge=1, le=10, description="首轮每系列规划设计数")
    standby_designs_per_series: int = Field(default=1, ge=0, le=10, description="每系列候补设计数")
    warning_budget: int = Field(default=20, ge=1)
    hard_limit_budget: int = Field(default=25, ge=1)
    auto_start_panorama: bool = Field(default=True, description="是否自动开始首发全景生成")
    smart_planning: bool = False
    planning_brief: str = Field(default="", max_length=2000)
    series_counts: Dict[str, Dict[str, int]] = Field(default_factory=dict)

class BatchResponse(BaseModel):
    id: str
    name: str
    enabled_series: List[str]
    target_archived_count: int
    current_archived_count: int
    status: str
    is_mock: bool
    warning_budget: int
    hard_limit_budget: int
    budget_warning_exceeded_approved_by: Optional[str] = None
    budget_warning_exceeded_at: Optional[str] = None
    is_circuit_broken: bool
    circuit_break_reason: Optional[str] = None
    pause_reason: Optional[str] = None
    trial_run_remaining: int = 0
    approved_rule_snapshot_status: str = "legacy_unfrozen"
    smart_planning: bool = False
    planning_confirmed: bool = False
    created_at: str
    updated_at: str

class BudgetSummary(BaseModel):
    batch_id: str
    image_generation_count: int = 0
    planning_model_count: int = 0
    vision_model_count: int = 0
    failed_retry_count: int = 0
    explicit_failure_count: int = 0
    technical_retry_count: int = 0
    unknown_count: int = 0
    reserved_count: int = 0
    used_count: int = 0
    unsettled_count: int = 0
    warning_budget: int = 20
    hard_limit_budget: int = 25
    warning_reached: bool = False
    warning_approved: bool = False
    hard_limit_reached: bool = False
    circuit_broken: bool = False

class SlotVersionDetail(BaseModel):
    id: str
    slot_id: str
    design_id: str
    version_num: int
    attempt_num: int
    raw_image_path: Optional[str] = None
    preview_image_path: Optional[str] = None
    prompt: str
    prompt_metadata: Optional[Dict[str, Any]] = None
    mock_visual_features: Optional[Dict[str, Any]] = None
    is_mock: bool
    upstream_request_id: Optional[str] = None
    process_status: str
    is_locked: bool
    is_invalidated: bool
    depends_on_panorama_version_id: Optional[str] = None
    error_message: Optional[str] = None
    is_unknown: bool
    created_at: str

class SlotDetail(BaseModel):
    id: str
    design_id: str
    slot_type: str
    current_version_id: Optional[str] = None
    status: str
    redo_count: int
    technical_retry_count: int
    updated_at: str
    current_version: Optional[SlotVersionDetail] = None
    history_versions: List[SlotVersionDetail] = []

class DesignDetail(BaseModel):
    id: str
    batch_id: str
    series_id: str
    series_name: str
    design_code: str
    is_standby: bool
    status: str
    planning_status: str = 'legacy'
    structured_spec: Dict[str, Any]
    visual_features: Optional[Dict[str, Any]] = None
    elimination_reason: Optional[str] = None
    elimination_comment: Optional[str] = None
    archived_at: Optional[str] = None
    total_redo_count: int
    slots: Dict[str, SlotDetail]
    created_at: str
    updated_at: str

class PanoramaReviewRequest(BaseModel):
    action: str = Field(..., description="'approve', 'redo', 'eliminate'")
    expected_version_id: str = Field(..., description="并发防护：当前全景版本 ID")
    reason_category: Optional[str] = Field(None, description="原因分类：结构错误/家具变形/比例异常/构图不符/风格不符/相似度过高/主观审美")
    detail_comment: Optional[str] = Field(None, description="详细说明")
    leader_override: bool = Field(default=False, description="负责人特批超出重做限制")

class QuadReviewRequest(BaseModel):
    action: str = Field(..., description="'archive', 'eliminate'")
    detail_comment: Optional[str] = None
    expected_slot_versions: Dict[str, str] = Field(..., description="并发防护：人工审核所见四个槽位当前版本 ID")

class SlotRedoRequest(BaseModel):
    slot_id: str
    expected_version_id: str
    reason_category: str
    detail_comment: Optional[str] = None
    leader_override: bool = False

class SlotVersionSelectRequest(BaseModel):
    slot_id: str
    version_id: str

class PanoramaReplaceRequest(BaseModel):
    new_panorama_version_id: str
    reason: str

class SeriesGenerateRequest(BaseModel):
    style_brief: str = Field(..., min_length=2, max_length=500,
                             description="风格方向一句话，如：新中式禅意，深胡桃木配黄铜，晨光清冷")


class SeriesCreateRequest(BaseModel):
    series_id: str = Field(..., min_length=3, max_length=32, description="系列英文标识，小写字母开头")
    name: str = Field(..., min_length=2, max_length=40, description="系列中文名")
    rule: Dict[str, Any] = Field(default_factory=dict, description="规则字段，可留空沿用内置模板后修改")


class ImageApiConfigRequest(BaseModel):
    enabled: bool = Field(default=False, description="启用真实生图上游（关闭 = 本地 Mock）")
    base_url: str = Field(..., min_length=1, max_length=300, description="生图上游地址，如 http://127.0.0.1:3000")
    model: str = Field(..., min_length=1, max_length=80, description="图片模型名，如 gpt-image-2")
    timeout_seconds: int = Field(default=300, ge=5, le=900, description="单次生图超时秒数")
    api_key: Optional[str] = Field(default=None, max_length=400, description="留空 = 保持现有密钥不变")


class TextApiConfigRequest(BaseModel):
    base_url: str = Field(..., min_length=1, max_length=300)
    model: str = Field(..., min_length=1, max_length=80)
    timeout_seconds: int = Field(default=180, ge=5, le=900)
    api_key: Optional[str] = Field(default=None, max_length=400)


class WatermarkConfigRequest(BaseModel):
    main_text: str = Field(..., min_length=2, max_length=100)
    repeat_text: str = Field(..., min_length=2, max_length=100)
    opacity: float = Field(default=0.22, ge=0.05, le=0.9)
    font_size: int = Field(default=36, ge=12, le=72)
    position: str = Field(default="bottom_right")

class UnknownTaskResolveRequest(BaseModel):
    task_id: str
    resolution: str = Field(..., description="'confirm_success' 或 'confirm_failure'")
    note: Optional[str] = None
    evidence: Optional[str] = None
    billing_reference: Optional[str] = Field(None, max_length=200, description="人工核账引用：账单编号、流水号或查询凭据")
    attests_no_charge: bool = Field(False, description="负责人确认已人工核账且无扣费")

class CleanupRunRequest(BaseModel):
    days_threshold: int = Field(default=30, ge=1)
