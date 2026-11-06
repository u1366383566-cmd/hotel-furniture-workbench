/**
 * 酒店家具智能套图工作台 - 原生前端状态管理与交互控制
 * 视觉规范: premium-minimal-web-ui (Manus 风格) — 白卡片/细描边/克制蓝紫渐变/字重分层
 */

const APP_BASE = document.querySelector('meta[name="app-base"]')?.content || '';
function withBase(url) { return url.startsWith('/') && !url.startsWith('//') ? APP_BASE + url : url; }

const state = {
  currentRole: 'operator',
  currentUserId: 'simulated_operator',
  currentBatchId: null,
  batches: [],
  designs: [],
  budget: null,
  seriesRules: {},
  activeTab: 'panorama',
  activeRedoContext: null,
  unknownTasksRequestId: 0,
  designSignature: '',
};

// ---------- 基础工具 ----------

function authHeaders(includeJson = true) {
  return {
    ...(includeJson ? { 'Content-Type': 'application/json' } : {}),
    'X-User-Role': state.currentRole,
    'X-User-ID': state.currentUserId,
  };
}

async function apiFetch(url, options = {}) {
  const headers = {
    ...authHeaders(),
    ...(options.headers || {})
  };
  const res = await fetch(withBase(url), {
    ...options,
    headers,
    // 审核、预算和 unknown 保护中心必须读取最新账本状态，禁止复用旧 GET 响应缓存。
    cache: options.cache || 'no-store'
  });
  if (!res.ok) {
    let errMsg = `请求失败 (${res.status})`;
    try {
      const errData = await res.json();
      errMsg = errData.detail || errData.message || errMsg;
    } catch (e) {}
    throw new Error(errMsg);
  }
  return res.json();
}

async function downloadAuthenticatedZip(url, fallbackName) {
  const response = await fetch(withBase(url), {method: 'HEAD', headers: authHeaders(false), cache: 'no-store'});
  if (!response.ok) throw new Error(response.status === 404 ? '导出文件不存在或已清理' : `下载准备失败 (${response.status})`);
  const link = document.createElement('a');
  link.href = withBase(url);
  link.download = fallbackName;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

let exportInProgress = false;
async function runZipExport(url, options, description) {
  if (exportInProgress) { showToast('已有导出正在打包，请稍候'); return; }
  exportInProgress = true;
  const status = document.getElementById('export-progress');
  const selected = document.getElementById('export-selected');
  const batch = document.getElementById('btn-export-batch-zip');
  selected.disabled = true; batch.disabled = true;
  selected.textContent = '正在打包…';
  status.classList.remove('hidden');
  status.textContent = `正在为${description}压制水印并打包，请稍候；完成后会自动交给浏览器下载。`;
  try {
    const result = await apiFetch(url, options);
    await downloadAuthenticatedZip(result.download_url, result.file_name);
    status.textContent = `打包完成，已交给浏览器下载：${result.file_name}。请查看浏览器下载列表。`;
    await renderArchivedTab();
  } catch (error) {
    status.textContent = `导出失败：${error.message}`;
    showToast(error.message, true);
  } finally {
    exportInProgress = false;
    selected.disabled = false; batch.disabled = false;
    selected.textContent = '导出所选';
  }
}

function showToast(text, isError = false) {
  const toast = document.getElementById('toast');
  const toastText = document.getElementById('toast-text');
  toastText.innerText = text;
  toast.classList.toggle('toast-error', isError);
  toast.classList.remove('opacity-0', 'translate-y-2', 'pointer-events-none');
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => {
    toast.classList.add('opacity-0', 'translate-y-2', 'pointer-events-none');
  }, 3500);
}

// 预算防护(423)统一处理：报错文案与审批入口不再脱节——
// 负责人当场一键"批准越过预警线并重试"；操作员则明确告知找谁。
// 硬上限(严禁超支)不在此列：谁也不能批，直接报错。
async function handleBudgetGuardError(e, retryFn) {
  const msg = e.message || '';
  if (!msg.includes('预警线')) {
    showToast(msg, true);
    return;
  }
  const b = state.budget || {};
  const info = `当前已占用 ${b.image_generation_count ?? '?'} 次 | 预警线 ${b.warning_budget ?? '?'} | 硬上限 ${b.hard_limit_budget ?? '?'}（硬上限批准也无法突破）`;
  if (!['leader', 'admin'].includes(state.currentRole)) {
    showToast('已暂停：需管理员或负责人批准越过预警线。请联系管理员审批后重试。', true);
    return;
  }
  if (confirm(`${msg}\n\n${info}\n\n是否以管理员身份批准越过预警线，并立即继续本次操作？`)) {
    try {
      await apiFetch(`/api/batches/${state.currentBatchId}/approve_warning`, { method: 'POST' });
      showToast('管理员已批准越过预警线，正在继续…');
      await loadBudget();
      await retryFn();
    } catch (approveErr) {
      showToast(approveErr.message, true);
    }
  }
}

function emptyState(main, sub) {
  return `<div class="empty-state">
    <div class="empty-icon">
      <svg fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5">
        <path d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4"/>
      </svg>
    </div>
    <p class="mt-3 text-sm text-inkMuted">${main}</p>
    <p class="mt-1 text-xs text-slate-300">${sub}</p>
  </div>`;
}

// ---------- 自定义下拉组件（替代原生 select 系统弹层） ----------
// 隐藏原生 select 作数据源保留：现有 .value 读写与 change 监听零改动；
// 动态重建 option 后调用 select._syncDD() 刷新触发器与弹层。

const DD_CHEVRON = '<svg class="cust-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="m6 9 6 6 6-6"/></svg>';
const DD_CHECK = '<svg class="cust-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';

function initCustomSelect(select, opts = {}) {
  if (select.dataset.custInit) return;
  select.dataset.custInit = '1';
  const wrap = document.createElement('div');
  wrap.className = 'cust-dd';
  select.parentNode.insertBefore(wrap, select);
  wrap.appendChild(select);

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'cust-trigger';
  if (opts.triggerId) btn.id = opts.triggerId;
  btn.setAttribute('aria-haspopup', 'listbox');
  btn.setAttribute('aria-expanded', 'false');
  btn.setAttribute('aria-label', select.getAttribute('aria-label') || '选择项');
  const label = document.createElement('span');
  label.className = 'cust-label';
  btn.appendChild(label);
  btn.insertAdjacentHTML('beforeend', DD_CHEVRON);

  const list = document.createElement('div');
  list.className = 'cust-list hidden';
  let search = null;
  if (opts.searchable) {
    search = document.createElement('input');
    search.type = 'text';
    search.className = 'cust-search';
    search.placeholder = '搜索批次名称...';
    search.setAttribute('aria-label', '搜索批次名称');
    list.appendChild(search);
  }
  const itemsBox = document.createElement('div');
  list.appendChild(itemsBox);
  wrap.append(btn, list);

  function sync(filter = '') {
    const opt = select.options[select.selectedIndex];
    label.textContent = opt ? opt.text : '';
    itemsBox.innerHTML = '';
    const q = (filter || '').trim().toLowerCase();
    let shown = 0;
    [...select.options].forEach(o => {
      if (q && !o.text.toLowerCase().includes(q)) return;
      shown += 1;
      const item = document.createElement('div');
      item.className = 'cust-option' + (o.value === select.value ? ' cust-active' : '');
      item.setAttribute('data-value', o.value);
      item.setAttribute('role', 'option');
      const span = document.createElement('span');
      span.className = 'cust-option-text';
      span.textContent = o.text;
      item.appendChild(span);
      if (o.value === select.value) item.insertAdjacentHTML('beforeend', DD_CHECK);
      item.addEventListener('click', () => {
        select.value = o.value;
        select.dispatchEvent(new Event('change'));  // 既有 change 监听继续生效
        sync(search ? search.value : '');
        close();
      });
      itemsBox.appendChild(item);
    });
    if (opts.searchable && shown === 0) {
      const empty = document.createElement('div');
      empty.className = 'cust-empty';
      empty.textContent = '没有匹配的批次';
      itemsBox.appendChild(empty);
    }
  }

  function open() {
    sync(search ? search.value : '');
    list.classList.remove('hidden');
    wrap.classList.add('dd-open');
    btn.setAttribute('aria-expanded', 'true');
    if (search) { search.value = ''; setTimeout(() => search.focus(), 0); }
  }

  function close() {
    list.classList.add('hidden');
    wrap.classList.remove('dd-open');
    btn.setAttribute('aria-expanded', 'false');
  }

  btn.addEventListener('click', e => {
    e.stopPropagation();
    list.classList.contains('hidden') ? open() : close();
  });
  if (search) {
    search.addEventListener('input', () => sync(search.value));
    search.addEventListener('click', e => e.stopPropagation());
  }
  document.addEventListener('click', e => { if (!wrap.contains(e.target)) close(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });

  sync();
  select._syncDD = () => sync(search ? search.value : '');
}

function initCustomSelects(root) {
  (root || document).querySelectorAll('select').forEach(sel => {
    if (sel.id === 'batch-select') initCustomSelect(sel, { searchable: true, triggerId: 'batch-select-trigger' });
    else initCustomSelect(sel);
  });
}

// ---------- 初始化 ----------

document.addEventListener('DOMContentLoaded', async () => {
  document.getElementById('admin-budget-panel').appendChild(document.getElementById('admin-budget-metrics'));
  initCustomSelects(document);
  setupRoleSelector();
  if (APP_BASE) {
    const identity = await apiFetch('/api/session');
    const role = identity.role === 'admin' || identity.role === 'leader' ? 'admin' : 'operator';
    state.currentRole = role;
    state.currentUserId = identity.user_id;
    document.querySelector('[data-tab="system"]').classList.toggle('hidden', role !== 'admin');
    document.querySelectorAll('.role-btn').forEach(button => {
      button.remove();
    });
    const badge = document.createElement('span');
    badge.className = 'role-badge';
    badge.textContent = role === 'admin' ? '管理员' : '操作员 · 美工';
    badge.title = `当前登录身份：${badge.textContent}（${identity.user_id}）`;
    document.querySelector('.role-switch').appendChild(badge);
  }
  setupTabs();
  setupModals();
  setupAccessibleModals();
  document.getElementById('btn-start-from-plans').addEventListener('click', () => document.getElementById('btn-start-batch').click());
  document.getElementById('btn-text-save').addEventListener('click', async () => {
    try {
      await apiFetch('/api/system/text_api', {method: 'PUT', body: JSON.stringify({
        base_url: document.getElementById('cfg-text-url').value.trim(),
        model: document.getElementById('cfg-text-model').value.trim(),
        timeout_seconds: Number(document.getElementById('cfg-text-timeout').value),
        api_key: document.getElementById('cfg-text-key').value
      })});
      await loadTextApiConfig();
      showToast('文本模型配置已保存');
    } catch (e) { showToast(e.message, true); }
  });
  renderSeriesChecks();
  await loadBatches();
  await refreshWorkbenchInfo();
  document.getElementById('upstream-quota-refresh').addEventListener('click', loadUpstreamQuota);
  document.getElementById('style-new').addEventListener('click', () => {
    document.getElementById('style-editor').classList.remove('hidden');
    document.getElementById('ai-style-brief').focus();
  });
  const openNewStyle = () => {
    document.getElementById('modal-batch').classList.add('hidden');
    document.querySelector('.tab-btn[data-tab="styles"]').click();
    document.getElementById('style-new').click();
  };
  document.getElementById('new-batch-new-style').addEventListener('click', openNewStyle);
  document.getElementById('edit-batch-new-style').addEventListener('click', openNewStyle);
  let editingCounts = null;
  let editingBatchId = null;
  const closeBatchSeries = () => {
    document.getElementById('edit-batch-series-panel').classList.add('hidden');
    document.getElementById('edit-batch-series').setAttribute('aria-expanded', 'false');
  };
  document.getElementById('close-batch-series').addEventListener('click', closeBatchSeries);
  document.getElementById('edit-batch-series').addEventListener('click', async () => {
    if (!document.getElementById('edit-batch-series-panel').classList.contains('hidden')) { closeBatchSeries(); return; }
    const batch = state.batches.find(b => b.id === state.currentBatchId);
    if (!batch?.auto_workflow) { showToast('仅自动任务支持编辑组数', true); return; }
    try {
      const rules = await apiFetch('/api/system/series_rules');
      if (state.currentBatchId !== batch.id) return;
      editingBatchId = batch.id;
      editingCounts = Object.fromEntries(Object.entries(batch.series_counts).map(([id, value]) => [id, value.primary]));
      const list = document.getElementById('edit-batch-series-list'); list.replaceChildren();
      for (const [id, rule] of Object.entries(rules)) {
        const label = document.createElement('label'); label.className = 'batch-series-row';
        const name = document.createElement('span'); name.className = 'batch-series-name'; name.textContent = rule.name || id;
        const input = document.createElement('input'); input.type = 'number'; input.min = String(editingCounts[id] || 0); input.max = '30'; input.value = String(editingCounts[id] || 0); input.dataset.seriesId = id; input.className = 'wb-input'; input.setAttribute('aria-label', `${rule.name || id}总组数`);
        const field = document.createElement('span'); field.className = 'batch-series-field';
        const unit = document.createElement('span'); unit.textContent = '组';
        field.append(input, unit); label.append(name, field); list.appendChild(label);
      }
      document.getElementById('edit-batch-series-panel').classList.remove('hidden');
      document.getElementById('edit-batch-series').setAttribute('aria-expanded', 'true');
    } catch (e) { showToast(e.message, true); }
  });
  document.getElementById('save-batch-series').addEventListener('click', async event => {
    const batchId = state.currentBatchId;
    if (!editingCounts || editingBatchId !== batchId) { showToast('任务已切换，请重新打开组数编辑', true); return; }
    const inputs = [...document.querySelectorAll('#edit-batch-series-list input')];
    if (inputs.some(input => !Number.isInteger(Number(input.value)) || Number(input.value) < Number(input.min) || Number(input.value) > 30)) { showToast('请填写有效组数，已有组数不能减少', true); return; }
    const counts = Object.fromEntries(inputs.filter(input => Number(input.value) > 0).map(input => [input.dataset.seriesId, Number(input.value)]));
    event.currentTarget.disabled = true;
    try {
      const result = await apiFetch(`/api/batches/${batchId}/series-counts`, {method:'PUT', body:JSON.stringify({series_counts:counts,expected_series_counts:editingCounts})});
      closeBatchSeries();
      showToast(result.message); await loadBatches(batchId);
    } catch (e) { showToast(e.message, true); }
    finally { event.currentTarget.disabled = false; }
  });
  for (const [id, key, list] of [['style-name','name',false], ['style-description','description',false], ['style-materials','materials',true], ['style-colors','color_palette',true], ['style-headboards','headboard_types',true], ['style-beds','bed_frames',true], ['style-nightstands','nightstands',true], ['style-wardrobes','wardrobe_types',true], ['style-forbidden','forbidden_elements',true]]) {
    document.getElementById(id).addEventListener('input', e => {
      const raw = document.getElementById('series-rule-json');
      try { const rule = JSON.parse(raw.value); rule[key] = list ? e.target.value.split('\n').map(x => x.trim()).filter(Boolean) : e.target.value; raw.value = JSON.stringify(rule, null, 2); } catch (_) {}
    });
  }
  await loadUpstreamQuota();
  setInterval(loadUpstreamQuota, 60000);

  setInterval(async () => {
    if (state.currentBatchId) {
      await loadDesigns(false);
      await loadBudget();
    }
  }, 3000);
});

// 角色切换（胶囊分段）
function setupRoleSelector() {
  const btns = document.querySelectorAll('.role-btn');
  btns.forEach(btn => {
    btn.addEventListener('click', () => {
      state.currentRole = btn.dataset.role;
      state.currentUserId = `simulated_${state.currentRole}`;
      const canManage = state.currentRole === 'admin' || state.currentRole === 'leader';
      document.querySelector('[data-tab="system"]').classList.toggle('hidden', !canManage);
      if (!canManage && state.activeTab === 'system') document.querySelector('[data-tab="panorama"]').click();
      btns.forEach(b => b.classList.remove('seg-active'));
      btn.classList.add('seg-active');
      const roleLabel = state.currentRole === 'operator' ? '操作员 · 美工' : state.currentRole === 'admin' ? '管理员' : '负责人（兼容）';
      showToast(`已切换身份为: ${roleLabel}`);
      updateBudgetUI();
      renderCurrentTab();
    });
  });
}

// 标签页导航（胶囊分段）
function setupTabs() {
  const tabBtns = document.querySelectorAll('.tab-btn');
  tabBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      tabBtns.forEach(b => b.classList.remove('seg-active'));
      btn.classList.add('seg-active');
      state.activeTab = btn.dataset.tab;

      document.querySelectorAll('#tab-content > div').forEach(p => p.classList.add('hidden'));
      document.getElementById(`tab-panel-${state.activeTab}`).classList.remove('hidden');
      renderCurrentTab();
    });
  });
}

// 模态弹窗与事件
function updateBudgetHint() {
  const hint = document.getElementById('budget-hint');
  if (!hint) return;
  const seriesCount = document.querySelectorAll('.series-check:checked').length;
  if (!seriesCount) {
    hint.textContent = '请选择产品系列并填写每个系列需要的组数。';
    return;
  }
  const panoramas = Array.from(document.querySelectorAll('.series-check:checked')).reduce((n, cb) => n + (Number(cb.parentElement.querySelector('.series-primary').value) || 0), 0);
  hint.textContent = `已选择 ${seriesCount} 个系列，共 ${panoramas} 组；系统会自动规划并开始生成全景。`;
}

function setupAccessibleModals() {
  const modalIds = ['modal-batch', 'modal-redo'];
  const focusable = modal => [...modal.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')].filter(el => !el.disabled && !el.closest('.hidden'));
  const restoreFocus = modal => { if (modal?._returnFocus && document.contains(modal._returnFocus)) modal._returnFocus.focus(); };
  modalIds.forEach(id => {
    const modal = document.getElementById(id);
    if (!modal) return;
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.addEventListener('click', event => { if (event.target === modal) { modal.classList.add('hidden'); restoreFocus(modal); } });
    const observer = new MutationObserver(() => {
      if (!modal.classList.contains('hidden') && !modal._isOpen) {
        modal._isOpen = true;
        modal._returnFocus = document.activeElement;
        setTimeout(() => focusable(modal)[0]?.focus(), 0);
      } else if (modal.classList.contains('hidden') && modal._isOpen) {
        modal._isOpen = false;
        restoreFocus(modal);
      }
    });
    observer.observe(modal, { attributes: true, attributeFilter: ['class'] });
  });
  document.addEventListener('keydown', event => {
    const openModal = modalIds.map(id => document.getElementById(id)).find(modal => modal && !modal.classList.contains('hidden'));
    if (!openModal) return;
    if (event.key === 'Escape') {
      event.preventDefault(); openModal.classList.add('hidden'); restoreFocus(openModal); return;
    }
    if (event.key !== 'Tab') return;
    const items = focusable(openModal); if (!items.length) return;
    const first = items[0]; const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
}

function setupModals() {
  const btnNewBatch = document.getElementById('btn-new-batch');
  const modalBatch = document.getElementById('modal-batch');
  // 预算估算随系列勾选/预算输入实时刷新
  document.addEventListener('change', e => {
    if (e.target.classList.contains('series-check') || e.target.id === 'new-batch-warning' || e.target.id === 'new-batch-hardlimit') {
      updateBudgetHint();
    }
  });
  document.addEventListener('input', e => {
    if (e.target.id === 'new-batch-warning' || e.target.id === 'new-batch-hardlimit' || e.target.classList.contains('series-primary')) updateBudgetHint();
  });
  btnNewBatch.addEventListener('click', () => {
    modalBatch.classList.remove('hidden');
  });
  document.getElementById('btn-close-batch-modal').addEventListener('click', () => modalBatch.classList.add('hidden'));
  document.getElementById('btn-cancel-new-batch').addEventListener('click', () => modalBatch.classList.add('hidden'));

  document.getElementById('btn-submit-new-batch').addEventListener('click', async () => {
    const name = document.getElementById('new-batch-name').value.trim();
    const seriesChecks = Array.from(document.querySelectorAll('.series-check:checked')).map(c => c.value);
    const counts = Object.fromEntries(Array.from(document.querySelectorAll('.series-check:checked')).map(cb => [cb.value, {primary:Number(cb.parentElement.querySelector('.series-primary').value),standby:0}]));
    const target = Object.values(counts).reduce((sum, c) => sum + c.primary, 0);

    if (!name || seriesChecks.length === 0) {
      showToast('请填写批次名称并勾选至少一个系列', true);
      return;
    }
    if (!target || target > 50 || Object.values(counts).some(c => !Number.isInteger(c.primary) || c.primary < 1 || c.primary > 30)) {
      showToast('请填写每个系列的组数，总数不超过 50 组', true);
      return;
    }

    try {
      const payload = {
        name,
        enabled_series: seriesChecks,
        target_archived_count: target,
        designs_per_series: 2,
        standby_designs_per_series: 1,
        auto_start_panorama: false,
        smart_planning: true,
        auto_workflow: true,
        planning_brief: document.getElementById('new-batch-brief').value.trim(),
        series_counts: counts
      };
      const newBatch = await apiFetch('/api/batches', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
      modalBatch.classList.add('hidden');
      showToast(`任务已创建，正在自动规划：${newBatch.name}`);
      await loadBatches(newBatch.id);
    } catch (e) {
      showToast(e.message, true);
    }
  });

  // 重做弹窗
  const modalRedo = document.getElementById('modal-redo');
  document.getElementById('btn-close-redo').addEventListener('click', () => modalRedo.classList.add('hidden'));
  document.getElementById('btn-cancel-redo').addEventListener('click', () => modalRedo.classList.add('hidden'));

  document.getElementById('btn-submit-redo').addEventListener('click', async () => {
    const ctx = state.activeRedoContext;
    if (!ctx) return;
    const category = document.getElementById('redo-reason-category').value;
    const comment = document.getElementById('redo-comment').value.trim();
    const override = document.getElementById('redo-leader-override').checked;
    const modalRedo = document.getElementById('modal-redo');
    if (ctx.editPrompt && !document.getElementById('redo-custom-prompt').value.trim()) { showToast('请填写本次重做提示词', true); return; }

    const run = async () => {
      if (ctx.type === 'panorama') {
        await apiFetch(`/api/designs/${ctx.designId}/review_panorama`, {
          method: 'POST',
          body: JSON.stringify({
            action: 'redo',
            expected_version_id: ctx.versionId,
            reason_category: category,
            detail_comment: comment,
            leader_override: override
          })
        });
      } else {
        await apiFetch(`/api/slots/${ctx.slotId}/redo`, {
          method: 'POST',
          body: JSON.stringify({
            slot_id: ctx.slotId,
            expected_version_id: ctx.versionId,
            reason_category: category,
            detail_comment: comment,
            leader_override: override,
            custom_prompt: ctx.editPrompt ? document.getElementById('redo-custom-prompt').value : null
          })
        });
      }
      modalRedo.classList.add('hidden');
      showToast('重做任务已派发，正在重新生成...');
      await loadDesigns(true);
      await loadBudget();
    };

    try {
      await run();
    } catch (e) {
      await handleBudgetGuardError(e, run);
    }
  });

  // 批次选择下拉
  document.getElementById('batch-select').addEventListener('change', async (e) => {
    state.currentBatchId = e.target.value;
    state.budget = null;
    document.getElementById('task-quota').innerText = '—';
    document.getElementById('task-quota-status').innerText = '正在获取任务额度';
    await onBatchChanged();
  });

  // 预警审批按钮
  document.getElementById('btn-approve-warning').addEventListener('click', async () => {
    if (!state.currentBatchId) return;
    try {
      await apiFetch(`/api/batches/${state.currentBatchId}/approve_warning`, { method: 'POST' });
      showToast('管理员已明确批准跨越预警线');
      await loadBudget();
    } catch (e) {
      showToast(e.message, true);
    }
  });

  // 追加候补按钮
  document.getElementById('btn-append-standby').addEventListener('click', async () => {
    if (!state.currentBatchId) return;
    try {
      const res = await apiFetch(`/api/batches/${state.currentBatchId}/append_standby`, { method: 'POST' });
      showToast(res.message);
      await loadDesigns(true);
      await loadPlanningPanel();
      const batch = state.batches.find(b => b.id === state.currentBatchId);
      if (batch?.smart_planning) document.querySelector('[data-tab="panorama"]').click();
    } catch (e) {
      showToast(e.message, true);
    }
  });

  // 导出批次 ZIP
  document.getElementById('btn-export-batch-zip').addEventListener('click', async () => {
    if (!state.currentBatchId) return;
    await runZipExport(`/api/export/batch/${state.currentBatchId}`, {method:'POST'}, '本批次已归档图片');
  });

  document.getElementById('btn-refresh-exports').addEventListener('click', () => loadExportRecords());

  // 30 天清理保护
  document.getElementById('btn-run-cleanup').addEventListener('click', async () => {
    try {
      const res = await apiFetch('/api/system/cleanup', {
        method: 'POST',
        body: JSON.stringify({ days_threshold: 30 })
      });
      showToast(`受保护清理完成: 保护 ${res.stats.protected_version_count} 个核心版本，清理 ${res.stats.deleted_files_count} 个旧孤立文件，释放 ${res.stats.freed_mb} MB`);
    } catch (e) {
      showToast(e.message, true);
    }
  });

  // 保存水印配置
  document.getElementById('btn-save-watermark').addEventListener('click', async () => {
    if (!state.currentBatchId) return;
    const mainText = document.getElementById('cfg-main-wm').value.trim();
    const repeatText = document.getElementById('cfg-repeat-wm').value.trim();
    const opacity = parseFloat(document.getElementById('cfg-opacity-wm').value);
    const pos = document.getElementById('cfg-pos-wm').value;
    try {
      await apiFetch(`/api/system/batches/${state.currentBatchId}/watermark`, {
        method: 'POST',
        body: JSON.stringify({
          main_text: mainText,
          repeat_text: repeatText,
          opacity,
          font_size: 36,
          position: pos
        })
      });
      showToast('水印配置保存成功');
    } catch (e) {
      showToast(e.message, true);
    }
  });

  document.getElementById('btn-recover-circuit').addEventListener('click', async () => {
    if (!state.currentBatchId) return;
    try {
      const res = await apiFetch(`/api/batches/${state.currentBatchId}/circuit_break_recover`, { method: 'POST' });
      showToast(res.message);
      await loadBatches(state.currentBatchId);
    } catch (e) {
      showToast(e.message, true);
    }
  });

  document.getElementById('btn-start-batch').addEventListener('click', async () => {
    if (!state.currentBatchId) return;
    try {
      const res = await apiFetch(`/api/batches/${state.currentBatchId}/start`, { method: 'POST' });
      showToast(`${res.message}：${res.queued_designs} 款`);
      await loadBatches(state.currentBatchId);
    } catch (e) {
      showToast(e.message, true);
    }
  });

  document.getElementById('series-rule-select').addEventListener('change', async () => {
    await loadSeriesRules();
  });

  document.getElementById('btn-save-series-rule').addEventListener('click', async () => {
    const seriesId = document.getElementById('series-rule-select').value;
    let rules;
    try {
      rules = JSON.parse(document.getElementById('series-rule-json').value);
    } catch (e) {
      showToast('系列规则必须是合法 JSON', true);
      return;
    }
    delete rules.id;
    delete rules.template_version;
    try {
      const res = await apiFetch(`/api/system/series_rules/${seriesId}`, {
        method: 'PUT',
        body: JSON.stringify(rules)
      });
      showToast(`规则已保存，新模板版本 ${res.rule.template_version}`);
      await loadSeriesRules();
      await renderSeriesChecks();
    } catch (e) {
      showToast(e.message, true);
    }
  });

  // 管理员自定义新增产品系列（默认沿用内置模板，可继续编辑定制）
  document.getElementById('btn-add-series').addEventListener('click', async () => {
    const seriesId = document.getElementById('new-series-id').value.trim();
    const name = document.getElementById('new-series-name').value.trim();
    if (!seriesId || !name) {
      showToast('请填写系列英文标识和中文名称', true);
      return;
    }
    try {
      const res = await apiFetch('/api/system/series_rules', {
        method: 'POST',
        body: JSON.stringify({ series_id: seriesId, name, rule: {} })
      });
      showToast(res.message);
      document.getElementById('new-series-id').value = '';
      document.getElementById('new-series-name').value = '';
      state.seriesRules = {};
      await renderSeriesChecks();
      await loadSeriesRules();
      document.getElementById('series-rule-select').value = seriesId;
      if (document.getElementById('series-rule-select')._syncDD) {
        document.getElementById('series-rule-select')._syncDD();
      }
      await loadSeriesRules();
    } catch (e) {
      showToast(e.message, true);
    }
  });

  // 生图上游配置：仅保存 / 保存并测试连接
  document.getElementById('btn-api-save').addEventListener('click', async () => {
    try {
      const res = await saveImageApiConfig();
      showToast(res.message);
      document.getElementById('cfg-api-test-result').innerText = '';
    } catch (e) {
      showToast(e.message, true);
    }
  });

  document.getElementById('btn-api-save-test').addEventListener('click', async () => {
    const resultEl = document.getElementById('cfg-api-test-result');
    const btn = document.getElementById('btn-api-save-test');
    btn.disabled = true;
    btn.innerText = '测试中...';
    resultEl.innerText = '';
    try {
      await saveImageApiConfig();
      const result = await apiFetch('/api/system/image_api/test', { method: 'POST' });
      if (result.ok) {
        resultEl.innerHTML = `<span class="text-emerald-600 font-semibold">✓ 连接成功</span> · 可用模型: ${(result.models || []).join(', ') || '(空)'}`;
        showToast('生图上游连接测试通过');
      } else {
        resultEl.innerHTML = `<span class="text-rose-500 font-semibold">✗ ${result.error || '连接失败'}</span>`;
        showToast('生图上游连接测试失败', true);
      }
    } catch (e) {
      resultEl.innerHTML = `<span class="text-rose-500 font-semibold">✗ ${e.message}</span>`;
      showToast(e.message, true);
    } finally {
      btn.disabled = false;
      btn.innerText = '保存并测试连接';
    }
  });

  // AI 风格规划：一句话 → LLM 补全规则 → 自动创建系列
  document.getElementById('btn-ai-generate-series').addEventListener('click', async () => {
    const brief = document.getElementById('ai-style-brief').value.trim();
    const btn = document.getElementById('btn-ai-generate-series');
    if (!brief) {
      showToast('请先填写风格方向（一句话即可）', true);
      return;
    }
    btn.disabled = true;
    btn.innerText = 'AI 规划中（约 30-60 秒）...';
    try {
      const res = await apiFetch('/api/system/series_rules/generate', {
        method: 'POST',
        body: JSON.stringify({ style_brief: brief })
      });
      showToast(res.message);
      state.seriesRules = {};
      await renderSeriesChecks();
      await loadSeriesRules();
      const sel = document.getElementById('series-rule-select');
      sel.value = res.series_id;
      if (sel._syncDD) sel._syncDD();
      await loadSeriesRules();
      await loadBudget();
    } catch (e) {
      showToast(e.message, true);
    } finally {
      btn.disabled = false;
      btn.innerText = 'AI 生成新系列';
    }
  });

  // 刷新按钮
  document.getElementById('btn-refresh-designs').addEventListener('click', () => loadDesigns(true));
  document.getElementById('btn-refresh-unknown').addEventListener('click', () => loadUnknownTasks());
}

// ---------- 数据加载 ----------

// 加载批次列表
async function loadBatches(selectId = null) {
  try {
    const batches = await apiFetch('/api/batches');
    state.batches = batches;
    const select = document.getElementById('batch-select');
    select.innerHTML = '';
    if (batches.length === 0) {
      select.innerHTML = '<option value="">空库：请由管理员手动配置并批准批次</option>';
      state.currentBatchId = null;
      if (select._syncDD) select._syncDD();
      showToast('空库不会自动生产。请管理员点击“新建任务”，显式确认预算后创建。', true);
      return;
    }
    batches.forEach(b => {
      const opt = document.createElement('option');
      opt.value = b.id;
      opt.innerText = `${b.name} (${b.current_archived_count}/${b.target_archived_count})`;
      select.appendChild(opt);
    });

    state.currentBatchId = selectId || batches[0].id;
    select.value = state.currentBatchId;
    if (select._syncDD) select._syncDD();
    await onBatchChanged();
  } catch (e) {
    showToast('加载批次失败: ' + e.message, true);
  }
}

async function onBatchChanged() {
  const curBatch = state.batches.find(b => b.id === state.currentBatchId);
  document.getElementById('edit-batch-series-panel').classList.add('hidden');
  document.getElementById('edit-batch-series').setAttribute('aria-expanded', 'false');
  panoramaSimilarityCache.clear();
  const statusEl = document.getElementById('current-batch-status');
  if (curBatch) {
    document.getElementById('current-batch-name').innerText = curBatch.name;
    let tone = 'wb-badge-blue';
    let statusText = curBatch.status === 'completed' ? '已达成目标' : (curBatch.status === 'paused' ? '已暂停' : '生产中');
    if (curBatch.status === 'completed') tone = 'wb-badge-emerald';
    else if (curBatch.status === 'paused') tone = 'wb-badge-amber';
    if (curBatch.is_circuit_broken) { tone = 'wb-badge-rose'; statusText = '熔断暂停'; }
    else if (curBatch.pause_reason === 'PAUSE_UNKNOWN_PENDING') { tone = 'wb-badge-violet'; statusText = '未知结果暂停'; }
    else if (curBatch.pause_reason === 'AWAITING_LEADER_START') { tone = 'wb-badge-slate'; statusText = '待负责人启动'; }
    else if (curBatch.pause_reason === 'PLANNING_REQUIRED') { tone = 'wb-badge-amber'; statusText = '待方案规划与确认'; }
    if (curBatch.approved_rule_snapshot_status === 'legacy_unfrozen') statusText += ' · 规则快照未冻结';
    else if (curBatch.approved_rule_snapshot_status === 'explicit_legacy_migration') statusText += ' · 规则快照已显式迁移';
    else if (curBatch.approved_rule_snapshot_status === 'approved_frozen') statusText += ' · 规则快照冻结';
    statusEl.className = `wb-badge ${tone}`;
    statusEl.innerText = statusText;
    document.getElementById('current-batch-series').innerText = `启用系列: ${curBatch.enabled_series.join(' / ')}`;
    document.getElementById('metric-target').innerText = curBatch.target_archived_count;
    const startHint = document.getElementById('plan-start-hint');
    startHint.classList.toggle('hidden', !(curBatch.smart_planning && curBatch.planning_confirmed && curBatch.pause_reason === 'AWAITING_LEADER_START'));
    document.getElementById('btn-start-from-plans').disabled = !['leader', 'admin'].includes(state.currentRole);
  }
  const snapshotText = {
    approved_frozen: '本批次已冻结完整批准规则与模板版本；追加候补/唤醒均使用快照。',
    explicit_legacy_migration: '旧批次已由负责人显式迁移为当前规则；旧设计模板未被改写。',
    legacy_unfrozen: '旧批次缺少批准规则快照，系统禁止静默追加或启动。',
  }[curBatch.approved_rule_snapshot_status] || '规则快照状态未知';
  document.getElementById('rule-snapshot-status').innerText = snapshotText;
  await loadBudget();
  await loadDesigns(true);
  await loadPlanningPanel();
  await loadWatermarkConfig();
  await loadAuditLogs();
}

// 加载预算与成本
async function loadBudget() {
  if (!state.currentBatchId) return;
  try {
    const b = await apiFetch(`/api/batches/${state.currentBatchId}/budget`);
    state.budget = b;
    updateBudgetUI();
  } catch (e) {}
}

function updateBudgetUI() {
  const b = state.budget;
  if (!b) return;
  document.getElementById('task-quota').innerText = `${Math.max(0, b.hard_limit_budget - b.image_generation_count)} 次`;
  const quotaBatch = state.batches.find(x => x.id === state.currentBatchId);
  document.getElementById('task-quota-status').innerText = b.circuit_broken ? '任务熔断，暂停生成' : b.hard_limit_reached ? '已达硬上限' : b.warning_reached && !b.warning_approved ? '达到预警线，需管理员批准' : quotaBatch?.pause_reason === 'PAUSE_UNKNOWN_PENDING' ? '结果未知，任务暂停' : quotaBatch?.status === 'paused' ? '任务暂停中' : quotaBatch?.status === 'completed' ? '任务已完成' : '含在途及结果未知的占用';
  document.getElementById('metric-used').innerText = `${b.used_count}`;
  document.getElementById('metric-reserved').innerText = `${b.reserved_count} 次`;
  document.getElementById('metric-warning').innerText = `${b.warning_budget} 次`;
  document.getElementById('metric-hardlimit').innerText = `${b.hard_limit_budget} 次`;
  document.getElementById('metric-unknown').innerText = `${b.unknown_count} 笔`;
  document.getElementById('metric-failed').innerText = `${b.failed_retry_count} 次`;
  document.getElementById('metric-technical-retry').innerText = `${b.technical_retry_count} 次`;
  document.getElementById('metric-planning').innerText = `${b.planning_model_count} 次`;
  document.getElementById('metric-vision').innerText = `${b.vision_model_count} 次`;
  document.getElementById('metric-unsettled').innerText = `${b.unsettled_count} 次`;

  // 主预算水位条：填充 = 已消耗/硬上限；预警线刻度按比例定位
  const hard = Math.max(1, b.hard_limit_budget);
  const fill = document.getElementById('budget-bar-fill');
  if (fill) fill.style.width = `${Math.min(100, Math.round((b.image_generation_count / hard) * 100))}%`;
  const warnTick = document.getElementById('budget-warning-tick');
  if (warnTick) warnTick.style.left = `${Math.min(100, Math.round((b.warning_budget / hard) * 100))}%`;

  const btnApprove = document.getElementById('btn-approve-warning');
  if (b.warning_reached && !b.warning_approved) {
    btnApprove.classList.remove('hidden');
    btnApprove.innerText = ['leader', 'admin'].includes(state.currentRole) ? '批准越过预警线' : '预警暂停 (需管理员审批)';
    btnApprove.disabled = !['leader', 'admin'].includes(state.currentRole);
  } else {
    btnApprove.classList.add('hidden');
  }

  const btnRecover = document.getElementById('btn-recover-circuit');
  const currentBatch = state.batches.find(x => x.id === state.currentBatchId);
  const canRecover = !!currentBatch?.is_circuit_broken;
  btnRecover.classList.toggle('hidden', !canRecover);
  btnRecover.disabled = !['leader', 'admin'].includes(state.currentRole);
  btnRecover.title = canRecover ? `剩余试运行额度将重置为 3（当前 ${currentBatch.trial_run_remaining ?? 0}）` : '';

  const startButton = document.getElementById('btn-start-batch');
  const canStart = !(currentBatch?.smart_planning && !currentBatch?.planning_confirmed) && (currentBatch?.pause_reason === 'AWAITING_LEADER_START' || (
    currentBatch?.status === 'paused' && currentBatch?.pause_reason !== 'PAUSE_UNKNOWN_PENDING'
  ));
  startButton.classList.toggle('hidden', !canStart);
  startButton.disabled = !['leader', 'admin'].includes(state.currentRole);
  document.getElementById('btn-start-from-plans').disabled = !['leader', 'admin'].includes(state.currentRole);
}

// 加载设计列表
async function loadDesigns(forceRender = false) {
  if (!state.currentBatchId) return;
  try {
    const designs = await apiFetch(`/api/designs/batch/${state.currentBatchId}`);
    state.designs = designs;

    const archivedCount = designs.filter(d => d.status === 'archived').length;
    document.getElementById('metric-archived').innerText = archivedCount;
    const target = parseInt(document.getElementById('metric-target').innerText) || 3;
    const pct = Math.min(100, Math.round((archivedCount / target) * 100));
    document.getElementById('progress-archived').style.width = `${pct}%`;
    document.getElementById('metric-remaining').innerText = Math.max(0, target - archivedCount);
    document.getElementById('metric-pending').innerText = designs.filter(d => d.status === 'panorama_review').length;
    refreshWorkbenchInfo();

    const panoDesigns = designs.filter(d => !d.is_standby && ['draft', 'panorama_generating', 'panorama_review'].includes(d.status));
    const quadDesigns = designs.filter(d => !d.is_standby && ['panorama_approved', 'downstream_generating', 'quad_review'].includes(d.status));
    const standbyDesigns = designs.filter(d => d.is_standby);
    const archDesigns = designs.filter(d => d.status === 'archived');

    document.getElementById('badge-pano-count').innerText = panoDesigns.length;
    document.getElementById('badge-quad-count').innerText = quadDesigns.length;
    document.getElementById('badge-standby-count').innerText = standbyDesigns.length;
    document.getElementById('badge-archived-count').innerText = archDesigns.length;

    // 后台轮询只在数据真正变化时重绘，避免每 3 秒重建 DOM：
    // 既会吞掉用户点击，也会覆盖管理中心里正在填写的配置。
    const signature = designs.map(d => [
      d.id, d.status, d.planning_status, d.total_redo_count,
      d.slots.panorama?.current_version_id, d.slots.panorama?.status, d.slots.panorama?.redo_count,
      d.slots.white_isolated?.current_version_id, d.slots.white_isolated?.status,
      d.slots.twin_bed?.current_version_id, d.slots.twin_bed?.status,
      d.slots.nightstand_closeup?.current_version_id, d.slots.nightstand_closeup?.status,
    ].join('|')).join(';');
    if (forceRender || signature !== state.designSignature) {
      state.designSignature = signature;
      renderCurrentTab(true);
    }
  } catch (e) {
    console.error('加载设计组失败:', e);
  }
}

function renderCurrentTab(background = false) {
  if (state.activeTab === 'panorama') {
    renderPanoramaTab();
  } else if (state.activeTab === 'quad') {
    renderQuadTab();
  } else if (state.activeTab === 'standby') {
    renderStandbyTab();
  } else if (state.activeTab === 'archived') {
    renderArchivedTab();
  } else if (state.activeTab === 'styles' && !background) {
    loadSeriesRules();
  } else if (state.activeTab === 'system' && !background) {
    // 管理中心只在手动进入时刷新，避免轮询覆盖正在填写的 API / 规则 / 水印表单。
    loadWorkbenchSettings();
    loadImageApiConfig();
    loadTextApiConfig();
    loadUnknownTasks();
    loadAuditLogs();
  }
}

// ---------- 生图上游 API 配置 ----------

async function loadTextApiConfig() {
  try {
    const cfg = await apiFetch('/api/system/text_api');
    document.getElementById('cfg-text-url').value = cfg.base_url || '';
    document.getElementById('cfg-text-model').value = cfg.model || '';
    document.getElementById('cfg-text-timeout').value = cfg.timeout_seconds || 180;
    document.getElementById('cfg-text-key').value = '';
    document.getElementById('cfg-text-status').textContent = cfg.has_key ? `密钥已配置（${cfg.key_source}）` : '尚未配置密钥';
  } catch (e) { document.getElementById('cfg-text-status').textContent = e.message; }
}

async function loadImageApiConfig() {
  try {
    const cfg = await apiFetch('/api/system/image_api');
    document.getElementById('cfg-api-enabled').checked = cfg.enabled;
    document.getElementById('cfg-api-base-url').value = cfg.base_url || '';
    document.getElementById('cfg-api-model').value = cfg.model || '';
    document.getElementById('cfg-api-timeout').value = cfg.timeout_seconds || 300;
    document.getElementById('cfg-api-key').value = '';
    const badge = document.getElementById('cfg-api-mode-badge');
    if (cfg.enabled) {
      badge.className = 'wb-badge wb-badge-emerald';
      badge.innerText = '真实生图已启用';
    } else {
      badge.className = 'wb-badge wb-badge-slate';
      badge.innerText = 'Mock 模式';
    }
    document.getElementById('cfg-api-key-status').innerText = cfg.has_key
      ? (cfg.key_source === 'env' ? '密钥：已通过环境变量配置' : '密钥：已配置（留空保存则不变）')
      : '密钥：未配置';
  } catch (e) {
    const status = document.getElementById('cfg-api-key-status');
    if (status) status.innerText = '需要负责人/管理员权限查看生图上游配置';
  }
}

function collectImageApiConfig(includeKey = false) {
  const payload = {
    enabled: document.getElementById('cfg-api-enabled').checked,
    base_url: document.getElementById('cfg-api-base-url').value.trim(),
    model: document.getElementById('cfg-api-model').value.trim(),
    timeout_seconds: parseInt(document.getElementById('cfg-api-timeout').value) || 300,
  };
  const key = document.getElementById('cfg-api-key').value.trim();
  if (includeKey && key) payload.api_key = key;
  return payload;
}

async function saveImageApiConfig() {
  const saved = await apiFetch('/api/system/image_api', {
    method: 'PUT',
    body: JSON.stringify(collectImageApiConfig(true))
  });
  await loadImageApiConfig();
  return saved;
}

async function loadSeriesRules() {
  try {
    const rules = await apiFetch('/api/system/series_rules');
    state.seriesRules = rules;
    renderStyleCards(rules);
    const select = document.getElementById('series-rule-select');
    const previous = select.value;
    select.innerHTML = '';
    Object.entries(rules).forEach(([sid, rule]) => {
      const opt = document.createElement('option');
      opt.value = sid;
      opt.innerText = `${rule.name || sid} (${sid})`;
      select.appendChild(opt);
    });
    if ([...select.options].some(o => o.value === previous)) select.value = previous;
    if (select._syncDD) select._syncDD();
    const rule = rules[select.value];
    if (!rule) return;
    document.getElementById('series-template-version').innerText =
      `当前模板版本: ${rule.template_version}（已归档设计冻结于各自版本）`;
    document.getElementById('series-rule-json').value = JSON.stringify({ ...rule }, null, 2);
    document.getElementById('style-name').value = rule.name || '';
    document.getElementById('style-name').readOnly = ['modern_business', 'natural_wood', 'soft_contemporary'].includes(select.value);
    document.getElementById('style-description').value = rule.description || '';
    document.getElementById('style-materials').value = (rule.materials || []).join('\n');
    for (const [id, key] of [['style-headboards','headboard_types'],['style-beds','bed_frames'],['style-nightstands','nightstands'],['style-wardrobes','wardrobe_types'],['style-forbidden','forbidden_elements']]) document.getElementById(id).value = (rule[key] || []).join('\n');
    document.getElementById('style-colors').value = (rule.color_palette || []).join('\n');
  } catch (e) {
    showToast('加载系列规则失败: ' + e.message, true);
  }
}

let lastQuotaTime = null;
let quotaLoading = false;
async function loadUpstreamQuota() {
  if (quotaLoading) return;
  quotaLoading = true;
  const value = document.getElementById('upstream-quota-value');
  const time = document.getElementById('upstream-quota-time');
  try {
    const data = await apiFetch('/api/workbench/upstream-quota');
    if (data.status !== 'ok') throw new Error('unavailable');
    lastQuotaTime = data.checked_at;
    value.textContent = `${data.quota} 次`;
    time.textContent = `更新于 ${new Date(lastQuotaTime).toLocaleString('zh-CN')}`;
  } catch (_) {
    value.textContent = '暂不可获取';
    time.textContent = lastQuotaTime ? `上次成功 ${new Date(lastQuotaTime).toLocaleString('zh-CN')}` : '尚无成功查询记录';
  } finally { quotaLoading = false; }
}

function renderStyleCards(rules) {
  const container = document.getElementById('style-cards');
  container.replaceChildren();
  for (const [id, rule] of Object.entries(rules)) {
    const card = document.createElement('article'); card.className = 'style-card';
    const title = document.createElement('h3'); title.textContent = rule.name || id;
    const description = document.createElement('p'); description.textContent = rule.description || rule.hotel_mood || '暂无风格说明';
    const materials = document.createElement('p'); materials.textContent = `材质：${(rule.materials || []).join('、') || '未设置'}`;
    const colors = document.createElement('p'); colors.textContent = `配色：${(rule.color_palette || []).join('、') || '未设置'}`;
    const edit = document.createElement('button'); edit.className = 'wb-btn wb-btn-ghost'; edit.type = 'button'; edit.textContent = '编辑系列';
    edit.onclick = async () => {
      const select = document.getElementById('series-rule-select'); select.value = id;
      if (select._syncDD) select._syncDD();
      await loadSeriesRules(); document.getElementById('style-editor').classList.remove('hidden');
      document.getElementById('style-editor').scrollIntoView({behavior:'smooth', block:'start'});
    };
    card.append(title, description, materials, colors, edit); container.appendChild(card);
  }
}

// 渲染创建批次弹窗中的系列勾选项（含自定义新增系列）
async function renderSeriesChecks() {
  try {
    if (!Object.keys(state.seriesRules).length) {
      state.seriesRules = await apiFetch('/api/system/series_rules');
    }
    const container = document.getElementById('series-check-container');
    if (!container) return;
    container.innerHTML = '';
    const builtIn = ['modern_business', 'natural_wood', 'soft_contemporary'];
    Object.entries(state.seriesRules).forEach(([sid, rule]) => {
      const label = document.createElement('label');
      label.className = 'flex items-center gap-2 cursor-pointer';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.value = sid;
      cb.className = 'series-check';
      cb.setAttribute('aria-label', `启用${rule.name || sid}系列`);
      cb.checked = builtIn.includes(sid);
      const span = document.createElement('span');
      span.className = 'text-xs text-inkBody';
      span.textContent = `${rule.name || sid}（${sid}）`;
      label.appendChild(cb);
      label.appendChild(span);
      for (const [cls, title, value, min] of [['series-primary', '组数', 2, 1]]) {
        const text = document.createElement('span'); text.textContent = title; text.className = 'text-xs';
        const number = document.createElement('input'); number.type = 'number'; number.min = min; number.max = 30; number.value = value; number.className = `${cls} wb-input`; number.style.width = '4.5rem'; number.setAttribute('aria-label', `${rule.name || sid}${title}数量`);
        label.appendChild(text); label.appendChild(number);
      }
      container.appendChild(label);
    });
  } catch (e) {}
}

// ---------- 渲染 1. 全景初筛区 ----------

function escapeText(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
}

function panoStatusBadge(d, pSlot) {
  if (d.status === 'draft' && pSlot.status === 'empty') return '<span class="wb-badge wb-badge-slate">待启动</span>';
  if (d.status === 'panorama_approved') return '<span class="wb-badge wb-badge-emerald">全景已通过</span>';
  if (pSlot.status === 'generating') return '<span class="wb-badge wb-badge-blue"><span class="dot-pulse"></span>生成中</span>';
  return '<span class="wb-badge wb-badge-amber">待人工初筛</span>';
}

// 相似款提醒只是辅助信号：后台最多自动重规划两次后仍相似时，
// 继续生成并在这里提醒美工，由人判断是否重复。
const panoramaSimilarityCache = new Map();

// 方案一旦被重新规划，家具特征就变了，旧的相似结果必须失效，
// 否则卡片会继续显示规划前算出来的提醒数量。
function similarityCacheKey(design) {
  const spec = design?.structured_spec || {};
  const fingerprint = ['headboard', 'bed_frame', 'nightstand', 'wardrobe']
    .map(k => String(spec[k] || '')).join('|');
  let hash = 0;
  for (let i = 0; i < fingerprint.length; i += 1) {
    hash = (hash * 31 + fingerprint.charCodeAt(i)) | 0;
  }
  return `${design.id}::${hash}`;
}

function paintSimilarityWarning(card, candidates) {
  const slot = card.querySelector('[data-similarity-slot]');
  if (!slot) return;
  slot.replaceChildren();
  if (!candidates || !candidates.length) return;
  const details = document.createElement('details');
  details.className = 'similarity-warning';
  const summary = document.createElement('summary');
  summary.textContent = `⚠ 相似提醒 ${candidates.length} 款 · 点击查看相似部位与对照图`;
  details.appendChild(summary);
  const body = document.createElement('div');
  body.className = 'similarity-body';
  body.appendChild(candidateList(candidates));
  details.appendChild(body);
  slot.appendChild(details);
  const key = `${card.dataset.designId}::.similarity-warning`;
  if (panoramaOpenDetails.has(key)) details.open = true;
  else panoramaOpenDetails.delete(key);
}

async function refreshPanoramaSimilarities(container) {
  const cards = [...container.querySelectorAll('[data-design-id]')];
  for (const card of cards) {
    const designId = card.dataset.designId;
    const design = state.designs.find(d => d.id === designId);
    const key = similarityCacheKey(design);
    if (panoramaSimilarityCache.has(key)) {
      paintSimilarityWarning(card, panoramaSimilarityCache.get(key));
      continue;
    }
    try {
      const candidates = await apiFetch(`/api/planning/design/${designId}/similarities`);
      panoramaSimilarityCache.set(key, candidates);
      if (!card.isConnected) continue;
      paintSimilarityWarning(card, candidates);
    } catch (e) {
      // 相似提醒是辅助信息，读取失败不得阻断人工审核。
      console.warn('相似提醒读取失败', e);
    }
  }
}

// 重绘时保留美工已经展开的详情与相似提醒，避免轮询把展开状态收起来。
const panoramaOpenDetails = new Set();

function captureOpenDetails(container) {
  for (const card of container.querySelectorAll('[data-design-id]')) {
    const id = card.dataset.designId;
    for (const selector of ['.workbench-details', '.similarity-warning']) {
      const node = card.querySelector(selector);
      if (node && node.open) panoramaOpenDetails.add(`${id}::${selector}`);
      else panoramaOpenDetails.delete(`${id}::${selector}`);
    }
  }
}

function restoreOpenDetails(card) {
  const id = card.dataset.designId;
  for (const selector of ['.workbench-details', '.similarity-warning']) {
    if (!panoramaOpenDetails.has(`${id}::${selector}`)) continue;
    const node = card.querySelector(selector);
    if (node) node.open = true;
  }
}

function renderPanoramaTab() {
  const container = document.getElementById('panorama-cards-grid');
  captureOpenDetails(container);
  container.innerHTML = '';
  const batch = state.batches.find(b => b.id === state.currentBatchId);
  if (batch?.smart_planning && !batch.planning_confirmed) return;
  const panoDesigns = state.designs.filter(d => !d.is_standby && ['draft', 'panorama_generating', 'panorama_review'].includes(d.status));

  if (panoDesigns.length === 0) {
    container.innerHTML = `<div class="col-span-full wb-card">${emptyState('全景初筛暂无待处理设计', '已全部通过或已归档；可从候补池或新建批次继续生产')}</div>`;
    return;
  }

  panoDesigns.forEach(d => {
    const pSlot = d.slots.panorama;
    const curVer = pSlot.current_version;
    const card = document.createElement('div');
    card.className = 'wb-card overflow-hidden flex flex-col justify-between';

    let imgHtml = '';
    if (curVer && curVer.preview_image_path) {
      imgHtml = `
        <div class="relative w-full aspect-square bg-slate-100 overflow-hidden">
          <img src="${withBase(`/api/media/preview/${curVer.id}`)}" alt="${d.design_code}" class="w-full h-full object-contain">
          <div class="absolute top-2 right-2 px-2 py-0.5 rounded-full bg-slate-900/70 text-white text-[11px] font-mono backdrop-blur-xs">
            ${d.design_code} · v${curVer.version_num}
          </div>
          <div class="absolute bottom-2 right-2 px-2 py-0.5 rounded-full bg-emerald-600/80 text-white text-[10px] font-semibold">
            带水印预览
          </div>
        </div>
      `;
    } else if (curVer && curVer.process_status === 'failed') {
      imgHtml = `
        <div class="w-full aspect-square bg-rose-50 border-b border-rose-100 flex flex-col items-center justify-center text-rose-500 text-xs p-4 text-center">
          <svg class="w-8 h-8 mb-2" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5">
            <path d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/>
          </svg>
          生成失败
          <span class="text-[10px] text-rose-300 mt-1 break-all">${(curVer.error_message || '').slice(0, 80)}</span>
        </div>
      `;
    } else if (d.status === 'draft' && pSlot.status === 'empty') {
      imgHtml = `<div class="plan-empty-image">尚未启动全景生成</div>`;
    } else {
      imgHtml = `
        <div class="w-full aspect-square bg-slate-50 border-b border-lineLight flex flex-col items-center justify-center text-inkMuted text-xs">
          <svg class="w-8 h-8 animate-spin text-primary-500 mb-2" fill="none" viewBox="0 0 24 24">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path>
          </svg>
          全景图正在生成中（真实 API 约需 1-2 分钟）...
        </div>
      `;
    }

    const spec = d.structured_spec;
    const isReady = pSlot.status === 'ready' && curVer;
    card.dataset.designId = d.id;

    card.innerHTML = `
      ${imgHtml}
      <div class="p-4 flex-1 flex flex-col justify-between space-y-3">
        <div>
          <div class="flex items-center justify-between gap-2">
            <span class="text-xs font-bold text-inkTitle truncate">${isReady ? `<input type="checkbox" data-select="panorama" data-id="${d.id}" aria-label="选择 ${escapeText(d.design_code)}">` : ''} ${escapeText(spec.model_title || d.design_code)}</span>
            ${panoStatusBadge(d, pSlot)}
          </div>
          <div class="similarity-slot" data-similarity-slot></div>
          <details class="workbench-details"><summary>查看设计与提示词</summary><p>床屏：${escapeText(spec.headboard)}</p><p>床体：${escapeText(spec.bed_frame)}</p><p>床头柜：${escapeText(spec.nightstand)}</p><p>提示词：${escapeText(spec.panorama_prompt || '暂无')}</p></details>
        </div>

        <div class="pt-3 border-t border-slate-100 flex items-center gap-2">
          ${isReady ? `<button class="wb-btn wb-btn-ghost" data-action="pano-compare" data-id="${d.id}">对照与核实</button>` : ''}
          ${
            d.status === 'panorama_approved'
            ? `<div class="w-full text-center py-2 bg-emerald-50 border border-emerald-100 text-emerald-600 rounded-xl text-xs font-bold">✓ 已锁定基准 · 前往四图终审启动下游</div>`
            : `
              <button class="btn-approve-pano wb-btn wb-btn-grad flex-1" ${!isReady ? 'disabled' : ''} data-id="${d.id}" data-vid="${curVer ? curVer.id : ''}">
                初筛通过
              </button>
              <button class="btn-redo-pano wb-btn wb-btn-ghost" ${!isReady ? 'disabled' : ''} data-id="${d.id}" data-slot="${pSlot.id}" data-vid="${curVer ? curVer.id : ''}" data-redo="${pSlot.redo_count}" data-total="${d.total_redo_count}">
                重抽
              </button>
              <button class="btn-eliminate-pano wb-btn wb-btn-danger" data-id="${d.id}" data-vid="${curVer ? curVer.id : ''}">
                淘汰
              </button>
            `
          }
        </div>
      </div>
    `;

    container.appendChild(card);
    restoreOpenDetails(card);
  });

  refreshPanoramaSimilarities(container);

  container.querySelectorAll('.btn-approve-pano').forEach(b => {
    b.addEventListener('click', async () => {
      try {
        const result = await apiFetch(`/api/workbench/batches/${state.currentBatchId}/approve-panoramas`, {
          method: 'POST',
          body: JSON.stringify({ versions: { [b.dataset.id]: b.dataset.vid } })
        });
        if (!result.results[0]?.ok) throw new Error(result.results[0]?.error || '处理失败');
        showToast('全景已通过，正在生成白底图');
        await loadDesigns(true);
      } catch (e) {
        showToast(e.message, true);
      }
    });
  });
  syncBulkSelections();

  container.querySelectorAll('[data-action="pano-compare"]').forEach(b => {
    b.addEventListener('click', () => showComparison(b.dataset.id));
  });

  container.querySelectorAll('.btn-redo-pano').forEach(b => {
    b.addEventListener('click', () => {
      openRedoModal({
        type: 'panorama',
        designId: b.dataset.id,
        slotId: b.dataset.slot,
        versionId: b.dataset.vid,
        redoCount: parseInt(b.dataset.redo),
        totalRedo: parseInt(b.dataset.total)
      });
    });
  });

  container.querySelectorAll('.btn-eliminate-pano').forEach(b => {
    b.addEventListener('click', async () => {
      if (!confirm('确定淘汰此全景设计吗？系统将自动从候补池唤醒备选设计补齐目标缺口。')) return;
      try {
        await apiFetch(`/api/designs/${b.dataset.id}/review_panorama`, {
          method: 'POST',
          body: JSON.stringify({ action: 'eliminate', expected_version_id: b.dataset.vid, reason_category: '人工初筛淘汰' })
        });
        showToast('设计已淘汰，候补方案已自动唤醒');
        await loadDesigns(true);
        await loadBudget();
      } catch (e) {
        showToast(e.message, true);
      }
    });
  });
}

function buildQuadVersionSnapshot(design) {
  if (!design) throw new Error('设计数据已失效，请刷新后重新终审');
  const snapshot = {};
  for (const slotType of ['panorama', 'white_isolated', 'twin_bed', 'nightstand_closeup']) {
    const slot = design.slots[slotType];
    if (!slot?.current_version?.id) {
      throw new Error(`槽位 ${slotType} 尚无人工审核版本`);
    }
    snapshot[slotType] = slot.current_version.id;
  }
  return snapshot;
}

// 打开重做/重抽弹窗：展示标准化原因、重做计数，超限时暴露负责人特批开关
function openRedoModal(ctx) {
  state.activeRedoContext = ctx;
  const overLimit = (ctx.redoCount >= 2) || (ctx.totalRedo >= 4);
  const title = document.getElementById('modal-redo-title');
  if (title) {
    title.innerText = (ctx.type === 'panorama' ? '发起全景重抽' : '发起单张重做') +
      `（槽位重做 ${ctx.redoCount}/2 · 整组累计 ${ctx.totalRedo}/4）`;
  }
  document.getElementById('redo-override-box').classList.toggle('hidden', !overLimit);
  document.getElementById('redo-reason-category').value = '结构错误';
  if (document.getElementById('redo-reason-category')._syncDD) {
    document.getElementById('redo-reason-category')._syncDD();
  }
  document.getElementById('redo-comment').value = '';
  document.getElementById('redo-prompt-box').classList.toggle('hidden', !ctx.editPrompt);
  document.getElementById('redo-custom-prompt').value = ctx.promptText || '';
  document.getElementById('redo-leader-override').checked = false;
  document.getElementById('modal-redo').classList.remove('hidden');
}

document.addEventListener('click', event => {
  const button = event.target.closest('.prompt-view');
  if (!button) return;
  const design = state.designs.find(d => d.id === button.dataset.designId);
  const slot = design?.slots?.[button.dataset.slotType];
  if (!slot?.current_version) return;
  const dialog = document.createElement('dialog');
  dialog.className = 'prompt-dialog';
  const title = document.createElement('h3'); title.textContent = '图片提示词与参考版本';
  const select = document.createElement('select'); select.className = 'wb-input'; select.setAttribute('aria-label', '查看提示词的图片版本');
  const versions = slot.history_versions || [slot.current_version];
  for (const version of versions) { const option = document.createElement('option'); option.value = version.id; option.textContent = `版本 ${version.version_num}`; option.selected = version.id === slot.current_version.id; select.appendChild(option); }
  const reference = document.createElement('p');
  const promptText = document.createElement('pre'); promptText.className = 'prompt-dialog-text';
  const status = document.createElement('p');
  const actions = document.createElement('div'); actions.className = 'prompt-dialog-actions';
  const edit = document.createElement('button'); edit.type = 'button'; edit.className = 'wb-btn wb-btn-dark'; edit.textContent = '编辑并重做';
  const close = document.createElement('button'); close.type = 'button'; close.className = 'wb-btn wb-btn-ghost'; close.textContent = '关闭'; close.onclick = () => dialog.close();
  const refresh = () => {
    const version = versions.find(v => v.id === select.value); const meta = version?.prompt_metadata || {};
    const ref = meta.reference_version_id || meta.reference_panorama_version_id;
    const refSlot = button.dataset.slotType === 'white_isolated' ? design.slots.panorama : design.slots.white_isolated;
    const matched = refSlot?.history_versions?.find(v => v.id === ref);
    reference.textContent = ref ? `参考图片：${button.dataset.slotType === 'white_isolated' ? '全景' : '白底组合图'} ${matched ? 'v' + matched.version_num : ref}` : '历史参考版本未记录';
    promptText.textContent = meta.real_prompt || meta.prepared_prompt || '该版本未记录实际提示词';
    status.textContent = meta.prompt_status === 'prepared' ? '以下为准备发送的提示词，生成未成功；无法确认上游是否收到。' : '';
    edit.hidden = select.value !== slot.current_version_id || !meta.real_prompt || ['archived','eliminated'].includes(design.status) || slot.status === 'generating';
    edit.onclick = () => { dialog.close(); openRedoModal({type:'slot', designId:design.id, slotId:slot.id, versionId:slot.current_version_id, redoCount:slot.redo_count, totalRedo:design.total_redo_count, editPrompt:true, promptText:meta.real_prompt}); };
  };
  select.onchange = refresh; actions.append(edit,close); dialog.append(title,select,reference,promptText,status,actions);
  dialog.addEventListener('close', () => dialog.remove()); document.body.appendChild(dialog); refresh(); dialog.showModal();
});

// ---------- 渲染 2. 四图终审区 ----------

function renderQuadTab() {
  const container = document.getElementById('quad-designs-list');
  container.innerHTML = '';

  const quadDesigns = state.designs.filter(d => !d.is_standby && ['panorama_approved', 'downstream_generating', 'quad_review'].includes(d.status));
  if (quadDesigns.length === 0) {
    container.innerHTML = `<div class="wb-card">${emptyState('暂无进入四图阶段的设计', '需先在全景初筛中通过至少一款设计')}</div>`;
    return;
  }

  quadDesigns.forEach(d => {
    const card = document.createElement('div');
    card.className = 'wb-card p-5 space-y-4';

    const slots = d.slots;
    const pSlot = slots.panorama;
    const wSlot = slots.white_isolated;
    const tSlot = slots.twin_bed;
    const nSlot = slots.nightstand_closeup;

    const hasDownstreamStarted = d.status !== 'panorama_approved';
    const allSlotsReady = [pSlot, wSlot, tSlot, nSlot].every(s => s && (s.status === 'ready' || s.status === 'approved'));

    const statusBadge = d.status === 'quad_review'
      ? '<span class="wb-badge wb-badge-violet">四图就绪 · 待终审</span>'
      : (d.status === 'downstream_generating'
        ? '<span class="wb-badge wb-badge-blue"><span class="dot-pulse"></span>下游生成中</span>'
        : '<span class="wb-badge wb-badge-slate">已过全景 · 等待启动下游</span>');

    card.innerHTML = `
      <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-3 pb-3 border-b border-lineLight">
        <div class="min-w-0">
          <div class="flex items-center gap-2 flex-wrap">
            <h3 class="text-sm font-bold text-inkTitle truncate">${d.status === 'quad_review' && allSlotsReady ? `<input type="checkbox" data-select="quad" data-id="${d.id}" aria-label="选择 ${escapeText(d.design_code)}">` : ''} ${escapeText(d.design_code)} - ${escapeText(d.structured_spec.model_title)}</h3>
            ${statusBadge}
          </div>
          <p class="text-[11px] text-inkMuted mt-1">系列: ${d.series_name} <span class="font-mono">| 累计重做 ${d.total_redo_count}/4</span></p>
        </div>

        <div class="flex items-center gap-2 shrink-0 flex-wrap">
          ${
            !hasDownstreamStarted
            ? `<button class="btn-start-downstream wb-btn wb-btn-grad" data-id="${d.id}">启动下游三图生成 (人工确认)</button>`
            : `
              <button class="btn-archive-quad wb-btn wb-btn-grad" ${!allSlotsReady ? 'disabled' : ''} data-id="${d.id}">✓ 四图合格归档</button>
              <button class="btn-eliminate-quad wb-btn wb-btn-danger" ${d.status !== 'quad_review' ? 'disabled' : ''} data-id="${d.id}">整组淘汰并补位</button>
            `
          }
        </div>
      </div>

      <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 pt-1">
        ${renderSlotItem(slots.panorama, '1. 场景全景图 (基准)', d, true)}
        ${renderSlotItem(slots.white_isolated, '2. 整套家具白底重组', d, false)}
        ${renderSlotItem(slots.twin_bed, '3. 同系列双床配置白底', d, false)}
        ${renderSlotItem(slots.nightstand_closeup, '4. 床头区域近景白底', d, false)}
      </div>
    `;

    container.appendChild(card);
  });

  initCustomSelects(container);

  container.querySelectorAll('.btn-start-downstream').forEach(b => {
    b.addEventListener('click', async () => {
      const designId = b.dataset.id;
      const run = async () => {
        await apiFetch(`/api/designs/${designId}/start_downstream`, { method: 'POST' });
        showToast('已派发下游三图任务，系统独立调度生成');
        await loadDesigns(true);
        await loadBudget();
      };
      try {
        await run();
      } catch (e) {
        await handleBudgetGuardError(e, run);
      }
    });
  });
  syncBulkSelections();

  container.querySelectorAll('.btn-archive-quad').forEach(b => {
    b.addEventListener('click', async () => {
      const design = state.designs.find(item => item.id === b.dataset.id);
      try {
        await apiFetch(`/api/designs/${b.dataset.id}/review_quad`, {
          method: 'POST',
          body: JSON.stringify({
            action: 'archive',
            detail_comment: '四图终审一致性合格',
            expected_slot_versions: buildQuadVersionSnapshot(design)
          })
        });
        showToast('设计组已合格归档，完成套图锁定！');
        await loadDesigns(true);
        await loadBudget();
      } catch (e) {
        showToast(e.message, true);
      }
    });
  });

  container.querySelectorAll('.btn-eliminate-quad').forEach(b => {
    b.addEventListener('click', async () => {
      const design = state.designs.find(item => item.id === b.dataset.id);
      if (!confirm('确定终审淘汰整组四图吗？将作废全部槽位并自动唤醒候补。')) return;
      try {
        await apiFetch(`/api/designs/${b.dataset.id}/review_quad`, {
          method: 'POST',
          body: JSON.stringify({
            action: 'eliminate',
            detail_comment: '四图终审淘汰',
            expected_slot_versions: buildQuadVersionSnapshot(design)
          })
        });
        showToast('整组已淘汰，候补补位已就绪');
        await loadDesigns(true);
        await loadBudget();
      } catch (e) {
        showToast(e.message, true);
      }
    });
  });

  container.querySelectorAll('.btn-slot-redo').forEach(b => {
    b.addEventListener('click', () => {
      openRedoModal({
        type: 'slot',
        designId: b.dataset.designId,
        slotId: b.dataset.slotId,
        versionId: b.dataset.vid,
        redoCount: parseInt(b.dataset.redo),
        totalRedo: parseInt(b.dataset.total)
      });
    });
  });

  container.querySelectorAll('.slot-version-select').forEach(sel => {
    sel.addEventListener('change', async (e) => {
      const slotId = sel.dataset.slotId;
      const slotType = sel.dataset.slotType;
      const replaceDesignId = sel.dataset.designId;
      const versionId = e.target.value;
      if (slotType === 'panorama') {
        if (!['leader', 'admin'].includes(state.currentRole)) {
          showToast('全景替换必须由负责人或管理员确认', true);
          await loadDesigns(true);
          return;
        }
        const reason = prompt('请填写负责人全景替换原因：');
        if (!reason) {
          await loadDesigns(true);
          return;
        }
        try {
          await apiFetch(`/api/slots/designs/${replaceDesignId}/replace_panorama`, {
            method: 'POST',
            body: JSON.stringify({ new_panorama_version_id: versionId, reason })
          });
          showToast('全景已替换，旧下游版本已失效');
        } catch (err) {
          showToast(err.message, true);
        }
        await loadDesigns(true);
        return;
      }
      try {
        await apiFetch(`/api/slots/${slotId}/select_version`, {
          method: 'POST',
          body: JSON.stringify({ slot_id: slotId, version_id: versionId })
        });
        showToast('已回选版本');
        await loadDesigns(true);
      } catch (err) {
        showToast(err.message, true);
      }
    });
  });
}

function renderSlotItem(slot, label, design, isPanorama = false) {
  if (!slot) return '';
  const curVer = slot.current_version;
  const isInvalidated = curVer && curVer.is_invalidated;
  const isGenerating = slot.status === 'generating';

  let imgArea = '';
  if (curVer && curVer.preview_image_path) {
    imgArea = `
      <div class="relative w-full aspect-square bg-slate-100 rounded-xl overflow-hidden border border-lineLight">
        <img src="${withBase(`/api/media/preview/${curVer.id}`)}" alt="${design.design_code} ${label}" class="w-full h-full object-contain">
        ${
          isInvalidated
          ? `<div class="absolute inset-0 bg-rose-900/60 backdrop-blur-xs flex items-center justify-center text-white text-xs font-bold p-2 text-center">
              依赖已失效！<br>全景已变更，需重新生成
             </div>`
          : ''
        }
        <div class="absolute bottom-1.5 right-1.5 px-1.5 py-0.5 rounded-full bg-black/60 text-white text-[9px] font-mono">
          v${curVer.version_num}
        </div>
      </div>
    `;
  } else if (curVer && curVer.process_status === 'failed') {
    imgArea = `
      <div class="w-full aspect-square bg-rose-50 border border-rose-100 rounded-xl flex flex-col items-center justify-center text-rose-500 text-xs p-2 text-center">
        <svg class="w-5 h-5 mb-1" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5">
          <path d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/>
        </svg>
        生成失败
      </div>
    `;
  } else {
    imgArea = `
      <div class="w-full aspect-square bg-slate-50 border border-dashed border-slate-200 rounded-xl flex flex-col items-center justify-center text-inkMuted text-xs">
        ${
          isGenerating
          ? `<svg class="w-5 h-5 animate-spin text-primary-500 mb-1" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg>生成中...`
          : `未启动生成`
        }
      </div>
    `;
  }

  let historyOptions = '';
  if (slot.history_versions && slot.history_versions.length > 1) {
    historyOptions = `
      <div class="mt-1.5 flex items-center gap-1.5 text-[10px] text-slate-400">
        <span class="shrink-0">版本</span>
        <div class="flex-1 min-w-0">
          <select class="slot-version-select w-full" aria-label="切换${label}历史版本" data-slot-id="${slot.id}" data-slot-type="${slot.slot_type}" data-design-id="${design.id}">
          ${slot.history_versions.map(hv => `
            <option value="${hv.id}" ${hv.id === slot.current_version_id ? 'selected' : ''}>
              v${hv.version_num} ${hv.is_invalidated ? '(失效)' : ''}
            </option>
          `).join('')}
          </select>
        </div>
      </div>
    `;
  }

  return `
    <div class="bg-slate-50/60 border border-lineLight rounded-xl p-2.5 flex flex-col justify-between space-y-2">
      <div>
        <div class="text-[11px] font-bold text-inkBody flex items-center justify-between gap-1">
          <span class="truncate">${label}</span>
          <span class="text-[10px] text-slate-300 font-mono shrink-0">重做 ${slot.redo_count}/2</span>
        </div>
        <div class="mt-2">
          ${imgArea}
        </div>
        ${historyOptions}
        ${!isPanorama && curVer ? `<button type="button" class="wb-btn wb-btn-ghost prompt-view" data-design-id="${design.id}" data-slot-type="${slot.slot_type}">查看提示词</button>` : ''}
        ${!isPanorama && ['twin_bed','nightstand_closeup'].includes(slot.slot_type) && (curVer?.prompt_metadata?.reference_version_id || curVer?.prompt_metadata?.reference_panorama_version_id) && design.slots.white_isolated?.current_version_id !== (curVer.prompt_metadata.reference_version_id || curVer.prompt_metadata.reference_panorama_version_id) ? '<p class="text-xs text-amber-700">参考的白底组合图版本与当前版本不同</p>' : ''}
      </div>

      <div class="pt-1 flex items-center justify-end">
        ${
          curVer && !isPanorama
          ? `
            <button class="btn-slot-redo wb-btn wb-btn-ghost px-2 py-1 text-[11px]" data-design-id="${design.id}" data-slot-id="${slot.id}" data-vid="${curVer.id}" data-redo="${slot.redo_count}" data-total="${design.total_redo_count}">
              单张重做
            </button>
          `
          : ''
        }
      </div>
    </div>
  `;
}

// ---------- 渲染 3. 候补备选池 ----------

function renderStandbyTab() {
  const container = document.getElementById('standby-cards-grid');
  container.innerHTML = '';
  const standbyDesigns = state.designs.filter(d => d.is_standby);

  if (standbyDesigns.length === 0) {
    container.innerHTML = `<div class="col-span-full wb-card">${emptyState('当前候补池已全部唤醒或耗尽', '可点击上方"追加候补方案"补充，规划成本不计生图额度')}</div>`;
    return;
  }

  standbyDesigns.forEach(d => {
    const spec = d.structured_spec;
    const card = document.createElement('div');
    card.className = 'wb-card p-4 space-y-3';
    card.innerHTML = `
      <div class="flex items-center justify-between gap-2">
        <span class="text-xs font-bold text-inkTitle font-mono">${escapeText(d.design_code)}</span>
        <span class="wb-badge wb-badge-amber">${['pending', 'pending_append', 'ready_append'].includes(d.planning_status) || (state.batches.find(b => b.id === d.batch_id)?.smart_planning && !state.batches.find(b => b.id === d.batch_id)?.planning_confirmed) ? '待规划或确认' : '候补待命 (未耗生图额度)'}</span>
      </div>
      <div class="text-xs font-medium text-inkBody">${escapeText(spec.model_title)}</div>
      <div class="text-[11px] text-inkMuted space-y-0.5 bg-slate-50/60 border border-lineLight rounded-xl p-2.5">
        <div><span class="text-inkBody font-medium">床屏:</span> ${escapeText(spec.headboard)}</div>
        <div><span class="text-inkBody font-medium">床体:</span> ${escapeText(spec.bed_frame)}</div>
        <div><span class="text-inkBody font-medium">材质:</span> ${escapeText((spec.main_materials || []).join(', '))}</div>
        <div><span class="text-inkBody font-medium">配色:</span> ${escapeText((spec.color_scheme || []).join(', '))}</div>
      </div>
      <div class="text-[10px] text-slate-300 pt-1">
        说明: 仅当首发设计发生全景淘汰或整组淘汰时，系统自动按序将其激活为正式设计并派发生图任务。
      </div>
    `;
    container.appendChild(card);
  });
}

// ---------- 渲染 4. 已归档与导出成果 ----------

async function renderArchivedTab() {
  const container = document.getElementById('archived-designs-grid');
  container.innerHTML = '';
  const batchId = state.currentBatchId;

  // Build a Set of design IDs that appear in export records so each card can
  // show an "已导出" badge.  Also refresh the records list at the same time.
  const exportedDesignIds = new Set();
  try {
    const records = await apiFetch(`/api/export/records/${batchId}`);
    if (state.currentBatchId !== batchId) return;
    renderExportRecords(records);
    records.forEach(r => { if (r.design_id) exportedDesignIds.add(r.design_id); });
  } catch (e) {}
  if (state.currentBatchId !== batchId) return;

  const archived = state.designs.filter(d => ['archived', 'eliminated'].includes(d.status));

  if (archived.length === 0) {
    container.innerHTML = `<div class="col-span-full wb-card">${emptyState('当前批次暂无已归档的设计成果', '完成四图终审归档后，可在此导出带水印 ZIP')}</div>`;
    return;
  }

  archived.forEach(d => {
    const card = document.createElement('div');
    card.className = 'wb-card p-4 space-y-3';
    const slots = d.slots;
    const alreadyExported = exportedDesignIds.has(d.id);

    card.innerHTML = `
      <div class="flex items-center justify-between gap-2">
        <label class="text-xs font-bold text-inkTitle font-mono">${d.status === 'archived' ? `<input type="checkbox" data-select="export" data-id="${d.id}" aria-label="选择${escapeText(d.design_code)}导出">` : ''} ${escapeText(d.design_code)}</label>
        <div class="flex items-center gap-1.5">
          ${alreadyExported ? '<span class="wb-badge" style="background:#f0fdf4;color:#16a34a;border:1px solid #bbf7d0;">↓ 已导出</span>' : ''}
          <span class="wb-badge wb-badge-emerald">${d.status === 'archived' ? '✓ 已完成' : '已淘汰'}</span>
        </div>
      </div>
      <div class="text-xs font-medium text-inkBody">${escapeText(d.structured_spec.model_title)}</div>

      <div class="grid grid-cols-2 sm:grid-cols-4 gap-1.5">
        ${['panorama', 'white_isolated', 'twin_bed', 'nightstand_closeup'].map(stype => {
          const sl = slots[stype];
          const vid = sl && sl.current_version ? sl.current_version.id : null;
          return vid
            ? `<div><img src="${withBase(`/api/media/preview/${vid}`)}" alt="${d.design_code} ${stype}" class="w-full aspect-square object-cover rounded-lg border border-lineLight" title="${stype}">${stype !== 'panorama' ? `<button type="button" class="wb-btn wb-btn-ghost prompt-view" data-design-id="${d.id}" data-slot-type="${stype}">查看提示词</button>` : ''}</div>`
            : `<div class="w-full aspect-square bg-slate-100 rounded-lg"></div>`;
        }).join('')}
      </div>

      <details class="workbench-details"><summary>查看完整套图与设计说明</summary><p>系列：${escapeText(d.series_name || '')}</p><p>材质：${escapeText((d.structured_spec.main_materials || []).join('、'))}</p><p>配色：${escapeText((d.structured_spec.color_scheme || []).join('、'))}</p></details>
      <div class="pt-2 flex justify-end gap-2">
        <button class="wb-btn wb-btn-ghost" data-action="archived-compare" data-id="${d.id}">对照与核实</button>
        ${d.status === 'archived' ? `<button class="btn-export-single wb-btn wb-btn-dark" data-id="${d.id}">${alreadyExported ? '再次导出 ZIP' : '导出单款 ZIP'}</button>` : ''}
      </div>
    `;
    container.appendChild(card);
  });

  container.querySelectorAll('[data-action="archived-compare"]').forEach(b => {
    b.addEventListener('click', () => showComparison(b.dataset.id));
  });
  syncBulkSelections();

  container.querySelectorAll('.btn-export-single').forEach(b => {
    b.addEventListener('click', async () => {
      await runZipExport(`/api/export/design/${b.dataset.id}`, {method:'POST'}, '所选设计');
    });
  });
}

// 渲染导出记录列表（供 renderArchivedTab 和 loadExportRecords 复用）
function renderExportRecords(records) {
  const container = document.getElementById('export-records-container');
  if (!container) return;
  container.innerHTML = '';
  if (!records || records.length === 0) {
    container.innerHTML = '<div class="text-slate-400 py-2 text-center">暂无导出记录</div>';
    return;
  }
  records.forEach(r => {
    const item = document.createElement('div');
    item.className = 'py-1.5 border-b border-slate-100 flex items-center justify-between gap-2 last:border-b-0';
    const sizeMb = (r.file_size / (1024 * 1024)).toFixed(2);
    item.innerHTML = `
      <div class="min-w-0 flex items-center gap-2">
        <span class="font-semibold text-inkBody font-mono truncate">${escapeText(r.file_name)}</span>
        <span class="text-[10px] text-slate-300 shrink-0">${escapeText(r.export_type)}</span>
      </div>
      <div class="flex items-center gap-2 shrink-0">
        <span class="text-[10px] text-slate-300 whitespace-nowrap font-mono">${sizeMb} MB · ${escapeText(r.exported_by)} (${escapeText(r.exported_role)}) · ${escapeText(r.created_at)}</span>
        <button class="wb-btn wb-btn-ghost text-[11px] px-2 py-0.5 shrink-0" data-redownload="${escapeText(r.file_name)}">↓ 再下载</button>
      </div>
    `;
    container.appendChild(item);
  });
  // 绑定"再下载"按钮
  container.querySelectorAll('[data-redownload]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const fname = btn.dataset.redownload;
      try {
        await downloadAuthenticatedZip(`/api/export/download/${fname}`, fname);
        showToast(`已交给浏览器下载：${fname}，请查看下载列表`);
      } catch (e) {
        showToast(e.message, true);
      }
    });
  });
}

// 加载导出记录（独立刷新入口，不含已导出标记更新）
async function loadExportRecords() {
  if (!state.currentBatchId) return;
  try {
    const records = await apiFetch(`/api/export/records/${state.currentBatchId}`);
    renderExportRecords(records);
  } catch (e) {}
}

// 加载结果未知任务列表
async function loadUnknownTasks() {
  if (!state.currentBatchId) return;
  const requestId = ++state.unknownTasksRequestId;
  const requestedBatchId = state.currentBatchId;
  try {
    const tasks = await apiFetch(`/api/system/batches/${requestedBatchId}/unknown_tasks?_=${Date.now()}`);
    // 定时轮询、手动刷新和批次切换可能并发；旧响应不能覆盖最新页面状态。
    if (requestId !== state.unknownTasksRequestId || requestedBatchId !== state.currentBatchId) return;
    const container = document.getElementById('unknown-tasks-container');
    // Keep in-progress ledger evidence intact when polling returns unchanged tasks.
    const snapshot = JSON.stringify([requestedBatchId, tasks]);
    if (container.dataset.snapshot === snapshot) return;
    container.dataset.snapshot = snapshot;
    container.innerHTML = '';

    if (tasks.length === 0) {
      container.innerHTML = '<div class="text-slate-400 py-3 text-center">暂无结果未知任务，流水线运转正常</div>';
      return;
    }

    tasks.forEach(t => {
      const row = document.createElement('div');
      row.className = 'unknown-task-row p-3 bg-white border border-violet-100 rounded-xl flex flex-col sm:flex-row sm:items-center justify-between gap-2';
      row.innerHTML = `
        <div class="min-w-0">
          <div class="flex items-center gap-2">
            <span class="w-2 h-2 rounded-full bg-violet-500 dot-pulse shrink-0"></span>
            <span class="font-bold text-inkTitle text-xs">${t.design_code} · 槽位: ${t.slot_type}</span>
          </div>
          <div class="text-[11px] text-inkMuted mt-0.5 font-mono">unknown_pending_confirmation | 异常: ${t.error_message || '上游超时/断连'}</div>
        </div>
        <div class="flex items-center gap-2 flex-wrap shrink-0">
          <input aria-label="账单或流水凭据编号" class="unknown-billing-ref wb-input w-44" placeholder="账单/流水凭据编号">
          <label class="flex items-center gap-1 text-xs text-inkBody cursor-pointer"><input type="checkbox" aria-label="确认已核账且无扣费" class="unknown-attest accent-blue-600 mr-1">已核账无扣费</label>
          <button class="btn-resolve-success wb-btn wb-btn-grad px-2.5 py-1.5" data-tid="${t.task_id}">
            确认成功并恢复
          </button>
          <button class="btn-resolve-failure wb-btn wb-btn-danger px-2.5 py-1.5" data-tid="${t.task_id}">
            确认失败并释放额度
          </button>
        </div>
      `;
      container.appendChild(row);
    });

    container.querySelectorAll('.btn-resolve-success').forEach(b => {
      b.addEventListener('click', async () => {
        try {
          await apiFetch('/api/system/resolve_unknown', {
            method: 'POST',
            body: JSON.stringify({ task_id: b.dataset.tid, resolution: 'confirm_success' })
          });
          showToast('已确认成功并恢复');
          await loadUnknownTasks();
          await loadBudget();
          await loadDesigns(true);
        } catch (e) {
          showToast(e.message, true);
        }
      });
    });

    container.querySelectorAll('.btn-resolve-failure').forEach(b => {
      b.addEventListener('click', async () => {
        try {
          await apiFetch('/api/system/resolve_unknown', {
            method: 'POST',
            body: JSON.stringify({
              task_id: b.dataset.tid,
              resolution: 'confirm_failure',
              evidence: prompt('请填写人工核账依据（必填）：'),
              billing_reference: b.closest('.unknown-task-row').querySelector('.unknown-billing-ref').value.trim(),
              attests_no_charge: b.closest('.unknown-task-row').querySelector('.unknown-attest').checked
            })
          });
          showToast('已按负责人人工核账凭据释放预占预算（非真实账单机器验证）');
          await loadUnknownTasks();
          await loadBudget();
          await loadDesigns(true);
        } catch (e) {
          showToast(e.message, true);
        }
      });
    });

  } catch (e) {}
}

// 加载水印配置
async function loadWatermarkConfig() {
  if (!state.currentBatchId) return;
  try {
    const cfg = await apiFetch(`/api/system/batches/${state.currentBatchId}/watermark`);
    document.getElementById('cfg-main-wm').value = cfg.main_text;
    document.getElementById('cfg-repeat-wm').value = cfg.repeat_text;
    document.getElementById('cfg-opacity-wm').value = cfg.opacity;
    const posSel = document.getElementById('cfg-pos-wm');
    posSel.value = cfg.position;
    if (posSel._syncDD) posSel._syncDD();
  } catch (e) {}
}

// 加载审计日志
async function loadAuditLogs() {
  if (!state.currentBatchId) return;
  try {
    const logs = await apiFetch(`/api/system/batches/${state.currentBatchId}/audit_logs`);
    const container = document.getElementById('audit-log-container');
    container.innerHTML = '';
    if (logs.length === 0) {
      container.innerHTML = '<div class="text-slate-400 py-2 text-center">暂无审计日志</div>';
      return;
    }
    logs.forEach(l => {
      const item = document.createElement('div');
      item.className = 'py-1.5 border-b border-slate-100 last:border-b-0 flex items-center justify-between gap-2';
      item.innerHTML = `
        <div class="min-w-0">
          <span class="font-semibold text-inkBody font-mono">[${l.action}]</span>
          <span class="text-inkMuted ml-1.5">操作人: ${l.actor_name} (${l.actor_role})</span>
        </div>
        <div class="text-[10px] text-slate-300 whitespace-nowrap font-mono">${l.created_at}</div>
      `;
      container.appendChild(item);
    });
  } catch (e) {}
}
