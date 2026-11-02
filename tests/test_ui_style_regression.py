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

# 仅作 JS 事件绑定/数据标记、刻意不带样式的钩子类
JS_HOOK_CLASSES = {
    "tab-btn", "role-btn", "series-check", "slot-version-select",
    "btn-approve-pano", "btn-redo-pano", "btn-eliminate-pano",
    "btn-start-downstream", "btn-archive-quad", "btn-eliminate-quad",
    "btn-slot-redo", "btn-export-single",
    "unknown-task-row", "unknown-billing-ref", "unknown-attest",
    "btn-resolve-success", "btn-resolve-failure",
}


def _collect_used_classes():
    """收集 index.html 与 app.js 中实际使用的 class token。"""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    used = set()

    # class="..." 出现在 HTML 与 JS 模板字符串中
    for match in re.finditer(r'class="([^"]*)"', html + js):
        for token in match.group(1).split():
            used.add(token)

    # JS 动态类切换: classList.add/remove/toggle('x') 与 className = '...'
    for match in re.finditer(r"classList\.(?:add|remove|toggle)\('([^']+)'", js):
        used.add(match.group(1))
    for match in re.finditer(r"className = '([^']+)'", js):
        for token in match.group(1).split():
            used.add(token)
    # 状态徽章动态拼接: `wb-badge ${tone}` 的 tone 取值
    for tone in ("wb-badge-blue", "wb-badge-emerald", "wb-badge-amber", "wb-badge-rose", "wb-badge-violet", "wb-badge-slate"):
        used.add(tone)

    # 过滤模板插值、JS 钩子类与非法 token
    return {
        t for t in used
        if t and "${" not in t and "(" not in t and "!" not in t and t not in JS_HOOK_CLASSES
    }


# 伪类后缀：定义选择器尾部可能带 hover/focus/last-child 等，匹配时剥离
_PSEUDO = {"hover", "focus", "active", "visited", "disabled", "before", "after", "last-child", "first-child", "checked"}


def _normalize(text):
    """归一化：去除转义、把小数工具类中的 `.5` 换成可安全匹配的标记。"""
    text = text.replace("\\", "")  # .py-0\.5 -> .py-0.5 ; .hover\:bg-x:hover -> .hover:bg-x:hover
    return re.sub(r"\.(\d)", r"-dot\1", text)  # py-0.5 -> py-0-dot5（避免与小数点混淆）


def _collect_used_classes():
    """收集 index.html 与 app.js 中实际使用的 class token（归一化后）。"""
    used = _collect_raw_used_classes()
    return {_normalize(t) for t in used}


def _collect_raw_used_classes():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    used = set()

    # class="..." 出现在 HTML 与 JS 模板字符串中
    for match in re.finditer(r'class="([^"]*)"', html + js):
        for token in match.group(1).split():
            used.add(token)

    # JS 动态类切换: classList.add/remove/toggle('x') 与 className = '...'
    for match in re.finditer(r"classList\.(?:add|remove|toggle)\('([^']+)'", js):
        used.add(match.group(1))
    for match in re.finditer(r"className = '([^']+)'", js):
        for token in match.group(1).split():
            used.add(token)
    # 状态徽章动态拼接: `wb-badge ${tone}` 的 tone 取值
    for tone in ("wb-badge-blue", "wb-badge-emerald", "wb-badge-amber", "wb-badge-rose", "wb-badge-violet", "wb-badge-slate"):
        used.add(tone)

    # 过滤模板插值、JS 钩子类与非法 token
    return {
        t for t in used
        if t and "${" not in t and "(" not in t and "!" not in t and t not in JS_HOOK_CLASSES
    }


def _collect_defined_classes():
    """收集两个 CSS 文件中定义的类名（去转义、拆复合选择器、剥伪类后缀）。"""
    css = _normalize(
        "".join((STATIC_DIR / name).read_text(encoding="utf-8") for name in ("tailwind-lite.css", "styles.css"))
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
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    required_ids = [
        "batch-select", "btn-new-batch", "btn-start-batch", "btn-approve-warning", "btn-recover-circuit",
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
    for name in ("index.html", "app.js", "styles.css", "tailwind-lite.css"):
        content = (STATIC_DIR / name).read_text(encoding="utf-8")
        found = [p for p in resource_patterns if p in content]
        assert not found, f"{name} 含外部资源引用（违反离线约束）: {found}"


def test_custom_select_component_present():
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "function initCustomSelect(" in js
    assert "searchable" in js, "批次下拉必须支持弹层内搜索"
    styles = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    assert ".cust-dd select { display: none; }" in styles, "原生 select 必须隐藏"
    spinner_hidden = "inner-spin-button" in styles and "-webkit-appearance: none" in styles
    assert spinner_hidden, "number 输入框原生 spinner 必须隐藏"
