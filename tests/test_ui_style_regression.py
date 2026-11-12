"""UI 风格回归测试：premium-minimal-web-ui 重构的静态守恒检查。

1. index.html 与 app.js 模板中用到的每个样式类都必须在本地 CSS 中有定义
   （离线子集 CSS 的已知痛点：新增类忘同步导致样式静默失效）。
   仅作 JS 事件钩子的标记类（无样式诉求）单独豁免，改由契约测试覆盖。
2. E2E/测试依赖的关键 id 与绑定 class 必须存在（重构不得破坏行为契约）。
3. 离线约束：前端资源不得包含任何外部 URL。
"""
import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

FRONTEND_FILES = ("index.html", "app.js", "smart-planning.js", "workbench.js")
STYLESHEET_FILES = ("styles.css", "tailwind-lite.css")

# 仅作 JS 事件绑定/数据标记、刻意不带样式的钩子类。
# bulk-failure-* 与 series-primary 由 workbench.js/app.js 动态创建，不能漏检。
JS_HOOK_CLASSES = {
    "tab-btn", "role-btn", "series-check", "series-primary", "slot-version-select",
    "btn-approve-pano", "btn-redo-pano", "btn-eliminate-pano",
    "btn-start-downstream", "btn-archive-quad", "btn-eliminate-quad",
    "btn-slot-redo", "btn-export-single", "btn-nas-upload", "btn-nas-confirm",
    "unknown-task-row", "unknown-billing-ref", "unknown-attest",
    "btn-resolve-success", "btn-resolve-failure",
    "bulk-failure-list", "bulk-failure-item",
}

# These explicit structural/role hooks are checked for presence below, but do
# not claim a visual style and therefore are exempt from CSS-definition matching.
SEEZO_MARKUP_CLASSES = {
    "section-intro", "eyebrow", "role-switch", "role-switch-label", "role-legacy",
}
NON_STYLED_CLASSES = JS_HOOK_CLASSES | SEEZO_MARKUP_CLASSES
_PSEUDO = {"hover", "focus", "active", "visited", "disabled", "before", "after", "last-child", "first-child", "checked"}


def _read_frontend(files=FRONTEND_FILES):
    """读取所有受回归检查约束的 SEEZO 前端文件。"""
    return "\n".join((STATIC_DIR / name).read_text(encoding="utf-8") for name in files)


_STRING_LITERAL = re.compile(r"['\"]([^'\"]*)['\"]")
_CLASS_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_:\-./\[\]]*$")


def _looks_like_class_value(value):
    """判断 element/planButton 的字符串参数是否像 class 列表而非显示文本。"""
    tokens = value.split()
    if not tokens or not all(_CLASS_TOKEN.fullmatch(token) for token in tokens):
        return False
    # 允许 ready/pending 这类状态词，但至少要有一个带有 class 命名特征的 token。
    return any("-" in token or token in NON_STYLED_CLASSES for token in tokens)


def _collect_raw_used_classes():
    """收集四个前端入口中的静态与动态 class token。"""
    source = _read_frontend()
    used = set()

    # HTML class 属性、JS 模板中的 class="..."，以及 className = '...'。
    for match in re.finditer(r"\bclass(?:Name)?\s*=\s*([\"'])(.*?)\1", source, re.S):
        used.update(match.group(2).split())

    # classList.add/remove/toggle/contains('x')，支持单双引号和多个参数。
    for match in re.finditer(r"classList\.(?:add|remove|toggle|contains)\(([^)]*)\)", source):
        # classList 的第一个参数是 class 名；toggle 的后续字符串参数是条件/状态值。
        literal = _STRING_LITERAL.search(match.group(1))
        if literal:
            used.update(literal.group(1).split())

    # smart-planning.js 通过 element/planButton 的 className 参数创建节点；
    # 同时覆盖 plan-status ready/pending 这样的条件 class 字符串。
    for function_name in ("element", "planButton"):
        for match in re.finditer(rf"\b{function_name}\(([^;\n]*)\)", source):
            for literal in _STRING_LITERAL.findall(match.group(1)):
                if _looks_like_class_value(literal):
                    used.update(literal.split())

    # 状态徽章动态拼接: `wb-badge ${tone}` 的 tone 取值。
    used.update({
        "wb-badge-blue", "wb-badge-emerald", "wb-badge-amber",
        "wb-badge-rose", "wb-badge-violet", "wb-badge-slate",
    })

    # 模板插值、表达式片段和刻意豁免的行为/结构钩子不参与 CSS 守恒比较。
    return {
        token for token in used
        if token and "${" not in token and "(" not in token and "!" not in token
        and token not in NON_STYLED_CLASSES
        and not re.fullmatch(r"[A-Z][A-Z0-9_]+", token)
    }


def _normalize(text):
    """归一化 CSS 转义和小数工具类，便于和模板 token 比较。"""
    text = text.replace("\\", "")
    return re.sub(r"\.(\d)", r"-dot\1", text)


def _collect_used_classes():
    """收集所有受检前端文件中实际使用的 class token（归一化后）。"""
    return {_normalize(token) for token in _collect_raw_used_classes()}


def _collect_defined_classes():
    """收集两个 CSS 文件中定义的类名（去转义、拆复合选择器、剥伪类后缀）。"""
    css = _normalize(
        "".join((STATIC_DIR / name).read_text(encoding="utf-8") for name in STYLESHEET_FILES)
    )
    defined = set()
    token_re = re.compile(r"\.([a-zA-Z_][a-zA-Z0-9_\-]*(?:\[[^\]]*\])?(?:\/\d+)?(?::[a-zA-Z][a-zA-Z0-9_\-]*)*)")
    for raw in token_re.findall(css):
        segs = raw.split(":")
        while len(segs) > 1 and segs[-1] in _PSEUDO:
            segs.pop()
        if segs and segs[0]:
            defined.add(":".join(segs))
    return defined


def test_all_used_classes_are_defined_locally():
    used = _collect_used_classes()
    defined = _collect_defined_classes()
    missing = sorted(t for t in used if t not in defined)
    assert not missing, f"以下类在本地 CSS 中无定义（会静默丢样式）: {missing}"


def test_behavior_contract_ids_and_classes_exist():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = _read_frontend()

    required_ids = [
        "batch-select", "btn-new-batch", "btn-start-batch", "btn-approve-warning", "btn-recover-circuit",
        "today-tasks", "today-archived", "metric-remaining", "metric-pending", "bulk-results",
        "panorama-bulk", "panorama-select-all", "panorama-selected-count", "panorama-bulk-approve",
        "quad-bulk", "quad-select-all", "quad-selected-count", "quad-bulk-archive",
        "export-selection", "export-select-all", "export-selected-count", "export-selected",
        "planning-job-notice", "smart-planning-panel", "plan-start-hint", "btn-start-from-plans",
        "btn-plan-all", "btn-plan-confirm", "plan-progress", "planning-cards", "admin-budget-panel",
        "budget-per-warning", "budget-per-hard", "budget-settings-save",
        "current-batch-name", "current-batch-status", "current-batch-series", "metric-archived", "metric-target",
        "progress-archived", "metric-used", "metric-reserved", "metric-warning", "metric-hardlimit",
        "metric-unknown", "metric-failed", "metric-technical-retry", "metric-planning", "metric-vision",
        "metric-unsettled", "badge-pano-count", "badge-quad-count", "badge-standby-count", "badge-archived-count",
        "tab-panel-panorama", "tab-panel-quad", "tab-panel-standby", "tab-panel-archived", "tab-panel-system",
        "panorama-cards-grid", "quad-designs-list", "standby-cards-grid", "archived-designs-grid",
        "export-records-container", "btn-refresh-exports", "unknown-tasks-container", "btn-refresh-unknown",
        "rule-snapshot-status", "series-rule-select", "series-rule-json", "series-template-version",
        "btn-save-series-rule", "btn-add-series", "new-series-id", "new-series-name", "series-check-container",
        "cfg-main-wm", "cfg-repeat-wm", "cfg-opacity-wm", "cfg-pos-wm", "btn-save-watermark", "btn-run-cleanup",
        "audit-log-container", "modal-redo", "modal-redo-title", "btn-close-redo", "btn-cancel-redo",
        "btn-submit-redo", "redo-reason-category", "redo-comment", "redo-override-box", "redo-leader-override",
        "modal-batch", "btn-close-batch-modal", "btn-cancel-new-batch", "btn-submit-new-batch",
        "new-batch-name", "new-batch-target", "new-batch-warning", "new-batch-hardlimit",
        "toast", "toast-text", "budget-bar-fill", "budget-warning-tick", "batch-dd-holder",
    ]
    for eid in required_ids:
        assert f'id="{eid}"' in html, f"index.html 缺少契约 id: {eid}"

    # SEEZO role/section hooks are intentional markup contracts even though they
    # are not standalone CSS selectors.
    required_markup_classes = [
        "section-intro", "eyebrow", "role-switch", "role-switch-label", "role-legacy",
    ]
    for cls in required_markup_classes:
        assert re.search(rf'class="[^"]*\b{re.escape(cls)}\b[^"]*"', html), (
            f"index.html 缺少 SEEZO 结构/角色 class: {cls}"
        )

    # JS 事件绑定 class（E2E 与功能依赖）
    for cls in sorted(JS_HOOK_CLASSES):
        assert f"'{cls}'" in js or f'"{cls}"' in js or f".{cls}" in js, f"app.js 缺少事件绑定 class: {cls}"
    # E2E 依赖的自绘下拉触发器与选项标记
    assert "batch-select-trigger" in js
    assert "'cust-option'" in js and "cust-trigger" in js


def test_no_external_resources_in_frontend():
    # 只拦截真正的资源加载模式（脚本/样式/字体/图片外链）；
    # 文本中的示例 URL（如输入框 placeholder）合法。
    resource_patterns = ('src="http', 'href="http', "src='http", "href='http", "@import", "url(http")
    for name in (*FRONTEND_FILES, *STYLESHEET_FILES):
        content = (STATIC_DIR / name).read_text(encoding="utf-8")
        found = [p for p in resource_patterns if p in content]
        assert not found, f"{name} 含外部资源引用（违反离线约束）: {found}"


def test_custom_select_component_present():
    js = _read_frontend()
    assert "function initCustomSelect(" in js
    assert "searchable" in js, "批次下拉必须支持弹层内搜索"
    styles = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    assert ".cust-dd select { display: none; }" in styles, "原生 select 必须隐藏"
    spinner_hidden = "inner-spin-button" in styles and "-webkit-appearance: none" in styles
    assert spinner_hidden, "number 输入框原生 spinner 必须隐藏"
