/* 智能规划：模型输出和用户输入均通过 textContent/value 渲染。 */
const planFields = [
  ['headboard', '床屏'], ['bed_frame', '床体'], ['nightstand', '床头柜'], ['wardrobe', '柜体'],
  ['main_materials', '主要材质（每行一项）'], ['color_scheme', '家具配色（每行一项）'],
  ['companion_items', '配套家具（每行一项）'], ['soft_furnishings', '软装'],
  ['design_difference', '本款设计差异'], ['panorama_prompt', '全景提示词']
];
const planListFields = new Set(['main_materials', 'color_scheme', 'companion_items']);
let planBusy = false;
const planningDrafts = new Map();
const planningFeedback = new Map();
const openPlanningCards = new Set();
let displayedPlans = [];
function element(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (className) el.className = className;
  return el;
}
function planButton(text, action, className = 'wb-btn wb-btn-ghost') {
  const b = element('button', text, className); b.type = 'button'; b.addEventListener('click', action); return b;
}
function candidateList(candidates) {
  const wrap = element('div', undefined, 'space-y-2');
  if (!candidates.length) { wrap.appendChild(element('p', '未发现文字特征相近的候选；仍需人工判断。', 'text-xs text-inkMuted')); return wrap; }
  for (const item of candidates) {
    const card = element('div', undefined, 'p-3 rounded-xl border border-lineLight bg-slate-50');
    card.appendChild(element('strong', `${item.design_code} · ${item.feature_source} · 相似部位：${item.matched_fields.join('、')}`, 'text-xs'));
    if (item.preview_version_id) {
      const img = element('img'); img.src = `/api/media/preview/${encodeURIComponent(item.preview_version_id)}`; img.alt = `${item.design_code} 历史全景预览`; img.style.maxWidth = '180px'; img.className = 'rounded-xl mt-2'; card.appendChild(img);
    }
    for (const [key, label] of planFields.slice(0, 4)) card.appendChild(element('p', `${label}：${item.features[key] || '未记录'}`, 'text-xs text-inkMuted'));
    wrap.appendChild(card);
  }
  return wrap;
}
async function loadPlanningPanel() {
  const panel = document.getElementById('smart-planning-panel');
  const batch = state.batches.find(b => b.id === state.currentBatchId);
  if (!batch?.smart_planning) { panel.classList.add('hidden'); return; }
  const batchId = state.currentBatchId;
  try {
    const data = await apiFetch(`/api/planning/batch/${batchId}`);
    if (state.currentBatchId !== batchId) return;
    const appended = data.confirmed ? data.designs.filter(d => ['pending_append', 'ready_append'].includes(d.status)) : [];
    const readOnly = data.confirmed && !appended.length && batch.pause_reason === 'AWAITING_LEADER_START';
    const visible = !data.confirmed ? data.designs : appended.length ? appended : readOnly ? data.designs : [];
    displayedPlans = visible;
    if (!visible.length) { panel.classList.add('hidden'); return; }
    panel.classList.remove('hidden');
    document.getElementById('plan-panel-title').textContent = readOnly ? '已确认的设计方案' : '生图前设计规划';
    document.getElementById('plan-panel-description').textContent = readOnly ? '展开查看已确认的方案；核对预算后可启动全景。' : '先生成并审核正式款与候补款，核对相似提醒后由管理员确认；确认后再启动全景。';
    document.getElementById('btn-plan-all').classList.toggle('hidden', readOnly);
    document.getElementById('btn-plan-confirm').classList.toggle('hidden', readOnly);
    document.getElementById('btn-plan-confirm').textContent = data.confirmed ? '确认追加候补' : '确认全部方案';
    document.getElementById('plan-progress').textContent = `已规划 ${visible.filter(d => ['ready','ready_append'].includes(d.status)).length} / ${visible.length} 款（包含候补）`;
    const container = document.getElementById('planning-cards'); container.replaceChildren();
    for (const plan of visible) {
      const card = element('article', undefined, 'plan-card');
      card.dataset.designId = plan.id;
      const isReady = ['ready','ready_append'].includes(plan.status);
      const details = element('details');
      details.open = openPlanningCards.has(plan.id);
      details.addEventListener('toggle', () => { if (details.open) openPlanningCards.add(plan.id); else openPlanningCards.delete(plan.id); });
      const summary = element('summary', undefined, 'plan-summary');
      const heading = element('div', undefined, 'plan-heading');
      const title = element('h4', plan.code, 'plan-code');
      heading.appendChild(title);
      heading.appendChild(element('span', plan.standby ? '候补' : '正式款', 'plan-kind'));
      heading.appendChild(element('span', isReady ? '已规划' : '待规划', isReady ? 'plan-status ready' : 'plan-status pending'));
      if (isReady && plan.similarities.length) heading.appendChild(element('span', `相似提醒 ${plan.similarities.length}`, 'plan-similarity'));
      summary.appendChild(heading);
      if (isReady) {
        const excerpt = value => { const s = String(value || '未填写').replace(/\s+/g, ' ').trim(); return s.length > 58 ? `${s.slice(0, 58)}…` : s; };
        const overview = element('div', undefined, 'plan-overview');
        for (const [key, name] of [['headboard', '床屏'], ['nightstand', '床头柜']]) {
          const row = element('p'); row.appendChild(element('span', name, 'plan-overview-label')); row.appendChild(document.createTextNode(excerpt(plan.spec[key]))); overview.appendChild(row);
        }
        summary.appendChild(overview);
      } else summary.appendChild(element('p', '生成后可查看家具设计、软装和全景提示词', 'plan-overview'));
      summary.appendChild(element('span', '查看 / 编辑', 'plan-toggle'));
      details.appendChild(summary);
      const body = element('div', undefined, 'plan-body');
      const actions = element('div', undefined, 'flex gap-2 flex-wrap');
      actions.appendChild(planButton(isReady ? '重新规划' : '生成方案', async () => {
        const feedback = isReady ? (window.prompt('请输入调整意见（可留空）', planningFeedback.get(plan.id) || '') ?? null) : '';
        if (feedback === null || planBusy) return;
        planningFeedback.set(plan.id, feedback);
        planBusy = true; document.getElementById('btn-plan-confirm').disabled = true; document.getElementById('btn-plan-all').disabled = true; title.textContent += ' · 规划中…';
        try { await apiFetch(`/api/planning/design/${plan.id}/generate`, {method:'POST', body:JSON.stringify({feedback})}); planningDrafts.delete(plan.id); planningFeedback.delete(plan.id); }
        catch (e) { showToast(e.message, true); } finally { planBusy = false; await loadPlanningPanel(); }
      }));
      actions.appendChild(planButton('删除', async () => {
        if (!confirm(`删除 ${plan.code}？归档目标不会自动改变。`)) return;
        try { await apiFetch(`/api/planning/design/${plan.id}`, {method:'DELETE'}); planningDrafts.delete(plan.id); planningFeedback.delete(plan.id); openPlanningCards.delete(plan.id); await loadPlanningPanel(); } catch (e) { showToast(e.message, true); }
      }));
      if (!readOnly) body.appendChild(actions);
      if (isReady) {
        const groups = [
          {title:'家具设计', fields:planFields.slice(0, 4), open:true},
          {title:'材质、配色与软装', fields:planFields.slice(4, 8)},
          {title:'设计差异与全景提示词', fields:planFields.slice(8)}
        ];
        const inputs = {};
        for (const group of groups) {
          const section = element('details', undefined, 'plan-section'); section.open = !!group.open;
          section.appendChild(element('summary', group.title, 'plan-section-title'));
          const form = element('div', undefined, 'plan-fields');
          for (const [key, label] of group.fields) {
            const field = element('label', undefined, 'plan-field'); field.appendChild(element('span', label));
            const input = element('textarea'); input.className = 'wb-input'; input.rows = key === 'panorama_prompt' ? 9 : 4;
            input.readOnly = readOnly;
            const savedDraft = planningDrafts.get(plan.id) || {};
            input.value = savedDraft[key] ?? (planListFields.has(key) ? (plan.spec[key] || []).join('\n') : (plan.spec[key] || ''));
            input.addEventListener('input', () => { const draft = planningDrafts.get(plan.id) || {}; draft[key] = input.value; planningDrafts.set(plan.id, draft); });
            input.setAttribute('aria-label', `${plan.code} ${label}`); inputs[key] = input; field.appendChild(input); form.appendChild(field);
          }
          section.appendChild(form); body.appendChild(section);
        }
        const checkboxLabel = element('label', undefined, 'plan-confirm-check'); const checked = element('input'); checked.type='checkbox'; checkboxLabel.appendChild(checked); checkboxLabel.appendChild(document.createTextNode(' 如修改方案或提示词，我已核对提示词与方案一致')); if (!readOnly) body.appendChild(checkboxLabel);
        const saveButton = planButton('保存本款修改', async () => {
          const spec = {...plan.spec};
          for (const [key] of planFields) spec[key] = planListFields.has(key) ? inputs[key].value.split('\n').map(x=>x.trim()).filter(Boolean) : inputs[key].value.trim();
          try { await apiFetch(`/api/planning/design/${plan.id}`, {method:'PUT', body:JSON.stringify({spec, expected_spec:plan.spec, prompt_checked:checked.checked})}); planningDrafts.delete(plan.id); showToast('方案已保存'); await loadPlanningPanel(); }
          catch(e) { showToast(e.message, true); }
        }, 'wb-btn wb-btn-dark');
        if (!readOnly) body.appendChild(saveButton);
        const related = element('details', undefined, 'plan-section');
        related.appendChild(element('summary', `可能相似的设计（${plan.similarities.length}）`, 'plan-section-title'));
        related.appendChild(candidateList(plan.similarities)); body.appendChild(related);
      }
      details.appendChild(body); card.appendChild(details); container.appendChild(card);
    }
    const canManagePlanning = ['leader', 'admin'].includes(state.currentRole);
    document.getElementById('btn-plan-all').disabled = planBusy || !canManagePlanning;
    document.getElementById('btn-plan-confirm').disabled = planBusy || !canManagePlanning || visible.some(d=>!['ready','ready_append'].includes(d.status));
  } catch(e) { showToast(e.message, true); }
}
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('btn-plan-all').addEventListener('click', async () => {
    if (planBusy || !state.currentBatchId) return;
    const id = state.currentBatchId; planBusy = true;
    document.getElementById('btn-plan-confirm').disabled = true; document.getElementById('btn-plan-all').disabled = true;
    try {
      const data = await apiFetch(`/api/planning/batch/${id}`);
      const pending = data.designs.filter(d=>['pending','pending_append'].includes(d.status));
      for (let i=0; i<pending.length; i++) {
        if (id !== state.currentBatchId) break;
        document.getElementById('plan-progress').textContent = `正在规划 ${i+1} / ${pending.length}：${pending[i].code}`;
        await apiFetch(`/api/planning/design/${pending[i].id}/generate`, {method:'POST', body:'{}'});
      }
      showToast('未完成方案已规划');
    } catch(e) { showToast(`规划暂停：${e.message}；已完成方案已保留`, true); }
    finally { planBusy = false; await loadPlanningPanel(); await loadBudget(); }
  });
  document.getElementById('btn-plan-confirm').addEventListener('click', async () => {
    if (document.querySelector('#planning-cards article') && [...document.querySelectorAll('#planning-cards article')].some(card => card.dataset.designId && planningDrafts.has(card.dataset.designId))) {
      showToast('请先保存或重新规划尚未保存的修改，再确认方案', true); return;
    }
    if (!confirm('确认所有正式款与候补款已审核，并已检查相似提醒？')) return;
    try { const batch = state.batches.find(b=>b.id===state.currentBatchId); const action = batch?.planning_confirmed ? 'confirm_appended' : 'confirm'; const expected_specs = Object.fromEntries(displayedPlans.map(p=>[p.id,p.spec])); const result = await apiFetch(`/api/planning/batch/${state.currentBatchId}/${action}`, {method:'POST',body:JSON.stringify({expected_specs})}); showToast(result.message); await loadBatches(state.currentBatchId); }
    catch(e) { showToast(e.message, true); }
  });
});
async function showComparison(designId) {
  try {
    const design = state.designs.find(d=>d.id===designId);
    const candidates = await apiFetch(`/api/planning/design/${designId}/similarities`);
    const current = await apiFetch(`/api/planning/design/${designId}/features`);
    const dialog = element('dialog'); dialog.className = 'wb-card p-5'; dialog.style.cssText = 'width:min(720px,95vw);max-height:85vh;overflow:auto';
    dialog.appendChild(element('h3', '历史款对照与成图特征核实', 'text-sm font-bold'));
    dialog.appendChild(element('p', '请对照当前全景图，按需修正家具特征；未修正项保留原方案。', 'text-xs text-inkMuted'));
    if (current.version_id) {
      const img = element('img'); img.src = `/api/media/preview/${encodeURIComponent(current.version_id)}`;
      img.alt = '当前款全景预览'; img.style.maxWidth = '240px'; img.className = 'rounded-xl mt-2'; dialog.appendChild(img);
    }
    const inputs = {};
    for (const [key, label] of planFields.slice(0,4)) {
      const l = element('label', undefined, 'block text-xs'); l.appendChild(element('span', label));
      const t = element('textarea'); t.className = 'wb-input comparison-textarea'; t.rows=2; t.value = current.verified_features[key] || design.structured_spec[key] || ''; inputs[key] = t; l.appendChild(t); dialog.appendChild(l);
    }
    dialog.appendChild(element('h4', '可能相似的历史或在途款', 'text-xs font-bold'));
    dialog.appendChild(candidateList(candidates));
    dialog.appendChild(planButton('保存核实特征', async () => {
      try { await apiFetch(`/api/planning/design/${designId}/features`, {method:'PUT',body:JSON.stringify({expected_version_id:current.version_id, features:Object.fromEntries(Object.entries(inputs).map(([k,v])=>[k,v.value.trim()]))})}); showToast('已保存核实特征'); dialog.close(); }
      catch(e) { showToast(e.message,true); }
    }, 'wb-btn wb-btn-dark'));
    dialog.appendChild(planButton('关闭',()=>dialog.close()));
    dialog.addEventListener('close',()=>dialog.remove()); document.body.appendChild(dialog); dialog.showModal();
  } catch(e) { showToast(e.message,true); }
}
