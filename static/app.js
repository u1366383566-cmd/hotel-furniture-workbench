/**
 * 酒店家具智能套图工作台 - 原生前端状态管理与交互控制
 * 视觉规范: premium-minimal-web-ui (Manus 风格) — 白卡片/细描边/克制蓝紫渐变/字重分层
 */

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
  const res = await fetch(url, {
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
  const res = await fetch(url, { headers: authHeaders(false) });
  if (!res.ok) {
    let message = `ZIP 下载失败 (${res.status})`;
    try {
      const data = await res.json();
      message = data.detail || message;
    } catch (e) {}
    throw new Error(message);
  }
  const blob = await res.blob();
  const objectUrl = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = objectUrl;
  link.download = fallbackName;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(objectUrl);
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
  if (state.currentRole !== 'leader') {
    showToast('已暂停：需负责人批准越过预警线。请右上角切换为「负责人」后重试。', true);
    return;
  }
  if (confirm(`${msg}\n\n${info}\n\n是否以负责人身份批准越过预警线，并立即继续本次操作？`)) {
    try {
      await apiFetch(`/api/batches/${state.currentBatchId}/approve_warning`, { method: 'POST' });
      showToast('负责人已批准越过预警线，正在继续…');
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
  initCustomSelects(document);
  setupRoleSelector();
  setupTabs();
  setupModals();
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
      btns.forEach(b => b.classList.remove('seg-active'));
      btn.classList.add('seg-active');
      showToast(`已切换身份为: ${state.currentRole.toUpperCase()}`);
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
    hint.textContent = '请至少勾选一个产品系列，这里会显示生图次数估算。';
    return;
  }
  const panoramas = Array.from(document.querySelectorAll('.series-check:checked')).reduce((n, cb) => n + (Number(cb.parentElement.querySelector('.series-primary').value) || 0), 0);
  const downstream = 3;                        // 走完 1 组四图闭环需要 3 张下游
  const minNeed = panoramas + downstream;
  const suggest = minNeed + 1;                 // +1 次重做余量
  const warningVal = parseInt(document.getElementById('new-batch-warning').value);
  const hardVal = parseInt(document.getElementById('new-batch-hardlimit').value);
  let tone = 'text-inkMuted';
  let extra = '';
  if (hardVal && hardVal < minNeed) {
    tone = 'text-rose-500';
    extra = ` ⚠ 硬上限 ${hardVal} 低于最小需求 ${minNeed}，流程中途会被硬阻断！`;
  } else if (warningVal && warningVal < minNeed) {
    tone = 'text-amber-600';
    extra = ` ⚠ 预警线 ${warningVal} 低于首发需求，中途会暂停等你批准（可接受，但需人工盯）`;
  } else if (hardVal && warningVal) {
    extra = ' ✓ 预算充足';
  }
  hint.className = `p-2.5 rounded-xl border border-lineLight bg-slate-50/60 text-[11px] leading-relaxed ${tone}`;
  hint.innerHTML =
    `当前勾选 <b>${seriesCount}</b> 个系列 → 确认后首发 <b>${panoramas}</b> 张全景；` +
    `走完 1 组四图还需 <b>${downstream}</b> 张下游 → 最少 <b>${minNeed}</b> 次，` +
    `建议预警线 ≥ ${minNeed}、硬上限 ≥ ${suggest}。${extra}`;
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
    if (state.currentRole === 'operator') {
      showToast('只有负责人 (Leader) 或管理员 (Admin) 允许创建生产批次', true);
      return;
    }
    modalBatch.classList.remove('hidden');
  });
  document.getElementById('btn-close-batch-modal').addEventListener('click', () => modalBatch.classList.add('hidden'));
  document.getElementById('btn-cancel-new-batch').addEventListener('click', () => modalBatch.classList.add('hidden'));

  document.getElementById('btn-submit-new-batch').addEventListener('click', async () => {
    const name = document.getElementById('new-batch-name').value.trim();
    const target = parseInt(document.getElementById('new-batch-target').value);
    const warning = parseInt(document.getElementById('new-batch-warning').value);
    const hardlimit = parseInt(document.getElementById('new-batch-hardlimit').value);
    const seriesChecks = Array.from(document.querySelectorAll('.series-check:checked')).map(c => c.value);

    if (!name || seriesChecks.length === 0) {
      showToast('请填写批次名称并勾选至少一个系列', true);
      return;
    }
    if (!target || !warning || !hardlimit) {
      showToast('目标数、预警线和硬上限都必须由负责人显式填写', true);
      return;
    }
    if (hardlimit <= warning) {
      showToast('硬上限必须大于预警线', true);
      return;
    }
    if (target >= 10 && hardlimit <= 25) {
      showToast('日常生产不能套用小样 25 次硬上限，请显式评估更大预算', true);
      return;
    }

    try {
      const payload = {
        name,
        enabled_series: seriesChecks,
        target_archived_count: target,
        designs_per_series: 2,
        standby_designs_per_series: 1,
        warning_budget: warning,
        hard_limit_budget: hardlimit,
        auto_start_panorama: false,
        smart_planning: true,
        planning_brief: document.getElementById('new-batch-brief').value.trim(),
        series_counts: Object.fromEntries(Array.from(document.querySelectorAll('.series-check:checked')).map(cb => [cb.value, {
          primary: Number(cb.parentElement.querySelector('.series-primary').value),
          standby: Number(cb.parentElement.querySelector('.series-standby').value)
        }]))
      };
      const newBatch = await apiFetch('/api/batches', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
      modalBatch.classList.add('hidden');
      showToast(`成功创建批次: ${newBatch.name}`);
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
            leader_override: override
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
    await onBatchChanged();
  });

  // 预警审批按钮
  document.getElementById('btn-approve-warning').addEventListener('click', async () => {
    if (!state.currentBatchId) return;
    try {
      await apiFetch(`/api/batches/${state.currentBatchId}/approve_warning`, { method: 'POST' });
      showToast('负责人已明确特批跨越预警线');
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
    const btn = document.getElementById('btn-export-batch-zip');
    const originalText = btn.innerText;
    btn.disabled = true;
    btn.innerText = '正在压制最终水印并打包，请稍候...';
    try {
      const res = await apiFetch(`/api/export/batch/${state.currentBatchId}`, { method: 'POST' });
      showToast(res.message);
      await loadExportRecords();
      await downloadAuthenticatedZip(res.download_url, res.file_name);
    } catch (e) {
      showToast(e.message, true);
    } finally {
      btn.disabled = false;
      btn.innerText = originalText;
    }
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
    } catch (e) {
      showToast(e.message, true);
    }
  });

  // 负责人自定义新增产品系列（默认沿用内置模板，可继续编辑定制）
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
    if (state.currentRole === 'operator') {
      showToast('AI 系列规划需要负责人或管理员权限', true);
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
      select.innerHTML = '<option value="">空库：请由负责人手动配置并批准批次</option>';
      state.currentBatchId = null;
      if (select._syncDD) select._syncDD();
      showToast('空库不会自动生产。请负责人点击"新建批次"，显式确认预算后创建。', true);
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
    document.getElementById('btn-start-from-plans').disabled = state.currentRole === 'operator';
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
    btnApprove.innerText = state.currentRole === 'leader' ? '负责人特批越过预警线' : '预警暂停 (需负责人审批)';
    btnApprove.disabled = state.currentRole !== 'leader';
  } else {
    btnApprove.classList.add('hidden');
  }

  const btnRecover = document.getElementById('btn-recover-circuit');
  const currentBatch = state.batches.find(x => x.id === state.currentBatchId);
  const canRecover = !!currentBatch?.is_circuit_broken;
  btnRecover.classList.toggle('hidden', !canRecover);
  btnRecover.disabled = state.currentRole === 'operator';
  btnRecover.title = canRecover ? `剩余试运行额度将重置为 3（当前 ${currentBatch.trial_run_remaining ?? 0}）` : '';

  const startButton = document.getElementById('btn-start-batch');
  const canStart = !(currentBatch?.smart_planning && !currentBatch?.planning_confirmed) && (currentBatch?.pause_reason === 'AWAITING_LEADER_START' || (
    currentBatch?.status === 'paused' && currentBatch?.pause_reason !== 'PAUSE_UNKNOWN_PENDING'
  ));
  startButton.classList.toggle('hidden', !canStart);
  startButton.disabled = state.currentRole === 'operator';
  document.getElementById('btn-start-from-plans').disabled = state.currentRole === 'operator';
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

    const panoDesigns = designs.filter(d => !d.is_standby && ['draft', 'panorama_generating', 'panorama_review'].includes(d.status));
    const quadDesigns = designs.filter(d => !d.is_standby && ['panorama_approved', 'downstream_generating', 'quad_review'].includes(d.status));
    const standbyDesigns = designs.filter(d => d.is_standby);
    const archDesigns = designs.filter(d => d.status === 'archived');

    document.getElementById('badge-pano-count').innerText = panoDesigns.length;
    document.getElementById('badge-quad-count').innerText = quadDesigns.length;
    document.getElementById('badge-standby-count').innerText = standbyDesigns.length;
    document.getElementById('badge-archived-count').innerText = archDesigns.length;

    renderCurrentTab();
  } catch (e) {
    console.error('加载设计组失败:', e);
  }
}

function renderCurrentTab() {
  if (state.activeTab === 'panorama') {
    renderPanoramaTab();
  } else if (state.activeTab === 'quad') {
    renderQuadTab();
  } else if (state.activeTab === 'standby') {
    renderStandbyTab();
  } else if (state.activeTab === 'archived') {
    renderArchivedTab();
  } else if (state.activeTab === 'system') {
    loadSeriesRules();
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
  await apiFetch('/api/system/image_api', {
    method: 'PUT',
    body: JSON.stringify(collectImageApiConfig(true))
  });
  await loadImageApiConfig();
}

async function loadSeriesRules() {
  try {
    const rules = await apiFetch('/api/system/series_rules');
    state.seriesRules = rules;
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
  } catch (e) {
    showToast('加载系列规则失败: ' + e.message, true);
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
      for (const [cls, title, value, min] of [['series-primary', '正式款', 2, 1], ['series-standby', '候补款', 1, 0]]) {
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

function renderPanoramaTab() {
  const container = document.getElementById('panorama-cards-grid');
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
          <img src="/api/media/preview/${curVer.id}" alt="${d.design_code}" class="w-full h-full object-cover">
          <div class="absolute top-2 left-2 px-2 py-0.5 rounded-full bg-slate-900/70 text-white text-[11px] font-mono backdrop-blur-xs">
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

    card.innerHTML = `
      ${imgHtml}
      <div class="p-4 flex-1 flex flex-col justify-between space-y-3">
        <div>
          <div class="flex items-center justify-between gap-2">
            <span class="text-xs font-bold text-inkTitle truncate">${escapeText(spec.model_title || d.design_code)}</span>
            ${panoStatusBadge(d, pSlot)}
          </div>
          <div class="mt-2 text-[11px] text-inkMuted space-y-0.5 bg-slate-50/60 border border-lineLight rounded-xl p-2.5">
            <div><span class="text-inkBody font-semibold">床屏:</span> ${escapeText(spec.headboard)}</div>
            <div><span class="text-inkBody font-semibold">床体:</span> ${escapeText(spec.bed_frame)}</div>
            <div><span class="text-inkBody font-semibold">柜体:</span> ${escapeText(spec.nightstand)}</div>
            <div><span class="text-inkBody font-semibold">材质:</span> ${escapeText((spec.main_materials || []).join(', '))}</div>
          </div>
          <div class="mt-2 text-[10px] text-slate-300 font-mono flex items-center justify-between">
            <span>槽位重做 ${pSlot.redo_count}/2</span>
            <span>整组累计 ${d.total_redo_count}/4</span>
          </div>
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
  });

  container.querySelectorAll('.btn-approve-pano').forEach(b => {
    b.addEventListener('click', async () => {
      try {
        await apiFetch(`/api/designs/${b.dataset.id}/review_panorama`, {
          method: 'POST',
          body: JSON.stringify({ action: 'approve', expected_version_id: b.dataset.vid })
        });
        showToast('全景已初筛通过并锁定，可在"四图终审区"启动下游生成');
        await loadDesigns(true);
      } catch (e) {
        showToast(e.message, true);
      }
    });
  });

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
  document.getElementById('redo-leader-override').checked = false;
  document.getElementById('modal-redo').classList.remove('hidden');
}

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
            <h3 class="text-sm font-bold text-inkTitle truncate">${escapeText(d.design_code)} - ${escapeText(d.structured_spec.model_title)}</h3>
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
        if (state.currentRole === 'operator') {
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
        <img src="/api/media/preview/${curVer.id}" alt="${design.design_code} ${label}" class="w-full h-full object-cover">
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
        <span class="text-xs font-bold text-inkTitle font-mono">${d.design_code}</span>
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

function renderArchivedTab() {
  const container = document.getElementById('archived-designs-grid');
  container.innerHTML = '';
  loadExportRecords();
  const archived = state.designs.filter(d => d.status === 'archived');

  if (archived.length === 0) {
    container.innerHTML = `<div class="col-span-full wb-card">${emptyState('当前批次暂无已归档的设计成果', '完成四图终审归档后，可在此导出带水印 ZIP')}</div>`;
    return;
  }

  archived.forEach(d => {
    const card = document.createElement('div');
    card.className = 'wb-card p-4 space-y-3';
    const slots = d.slots;

    card.innerHTML = `
      <div class="flex items-center justify-between gap-2">
        <span class="text-xs font-bold text-inkTitle font-mono">${d.design_code}</span>
        <span class="wb-badge wb-badge-emerald">✓ 已终审归档</span>
      </div>
      <div class="text-xs font-medium text-inkBody">${escapeText(d.structured_spec.model_title)}</div>

      <div class="grid grid-cols-4 gap-1.5">
        ${['panorama', 'white_isolated', 'twin_bed', 'nightstand_closeup'].map(stype => {
          const sl = slots[stype];
          const vid = sl && sl.current_version ? sl.current_version.id : null;
          return vid
            ? `<img src="/api/media/preview/${vid}" alt="${d.design_code} ${stype}" class="w-full aspect-square object-cover rounded-lg border border-lineLight" title="${stype}">`
            : `<div class="w-full aspect-square bg-slate-100 rounded-lg"></div>`;
        }).join('')}
      </div>

      <div class="pt-2 flex justify-end">
        <button class="wb-btn wb-btn-ghost" data-action="archived-compare" data-id="${d.id}">对照与核实</button>
        <button class="btn-export-single wb-btn wb-btn-dark" data-id="${d.id}">
          导出单款带水印 ZIP
        </button>
      </div>
    `;
    container.appendChild(card);
  });

  container.querySelectorAll('[data-action="archived-compare"]').forEach(b => {
    b.addEventListener('click', () => showComparison(b.dataset.id));
  });

  container.querySelectorAll('.btn-export-single').forEach(b => {
    b.addEventListener('click', async () => {
      const originalText = b.innerText;
      b.disabled = true;
      b.innerText = '导出中...';
      try {
        const res = await apiFetch(`/api/export/design/${b.dataset.id}`, { method: 'POST' });
        showToast('带水印 ZIP 导出成功');
        await loadExportRecords();
        await downloadAuthenticatedZip(res.download_url, res.file_name);
      } catch (e) {
        showToast(e.message, true);
      } finally {
        b.disabled = false;
        b.innerText = originalText;
      }
    });
  });
}

// 加载导出记录
async function loadExportRecords() {
  if (!state.currentBatchId) return;
  try {
    const records = await apiFetch(`/api/export/records/${state.currentBatchId}`);
    const container = document.getElementById('export-records-container');
    container.innerHTML = '';
    if (records.length === 0) {
      container.innerHTML = '<div class="text-slate-400 py-2 text-center">暂无导出记录</div>';
      return;
    }
    records.forEach(r => {
      const item = document.createElement('div');
      item.className = 'py-1.5 border-b border-slate-100 flex items-center justify-between gap-2 last:border-b-0';
      const sizeMb = (r.file_size / (1024 * 1024)).toFixed(2);
      item.innerHTML = `
        <div class="min-w-0">
          <span class="font-semibold text-inkBody font-mono">${r.file_name}</span>
          <span class="text-[10px] text-slate-300 ml-1.5">${r.export_type}</span>
        </div>
        <div class="text-[10px] text-slate-300 whitespace-nowrap font-mono">${sizeMb} MB · ${r.exported_by} (${r.exported_role}) · ${r.created_at}</div>
      `;
      container.appendChild(item);
    });
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
