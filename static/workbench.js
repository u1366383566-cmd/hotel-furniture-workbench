/* 选择项保存人工所见的版本，而不是刷新后静默换图。 */
const workbenchSelection = { panorama: new Map(), quad: new Map(), export: new Map() };
let selectionBatch = null;

function syncBulkSelections() {
  if (selectionBatch !== state.currentBatchId) {
    Object.values(workbenchSelection).forEach(map => map.clear());
    selectionBatch = state.currentBatchId;
  }
  for (const type of ['panorama','quad','export']) {
    const boxes = [...document.querySelectorAll(`[data-select="${type}"]`)];
    for (const box of boxes) box.checked = workbenchSelection[type].has(box.dataset.id);
    const toolbar = document.getElementById(type === 'export' ? 'export-selection' : `${type}-bulk`);
    toolbar.classList.toggle('hidden', boxes.length === 0);
    document.getElementById(`${type}-selected-count`).textContent = `已选 ${workbenchSelection[type].size} ${type === 'panorama' ? '款' : '组'}`;
    document.getElementById(`${type}-select-all`).checked = boxes.length > 0 && boxes.every(box=>box.checked);
  }
}
function selectWorkbenchItem(type,id,selected) {
  const design = state.designs.find(d=>d.id===id);
  if (!selected || !design) workbenchSelection[type].delete(id);
  else if (type==='panorama') workbenchSelection[type].set(id,design.slots.panorama.current_version_id);
  else if (type==='quad') workbenchSelection[type].set(id,buildQuadVersionSnapshot(design));
  else workbenchSelection[type].set(id,true);
}
function showBulkResult(result, retry) {
  const failed = result.results.filter(x=>!x.ok);
  const successful = result.results.length-failed.length;
  const area=document.getElementById('bulk-results'); area.replaceChildren(); area.classList.remove('hidden');
  const intro=document.createElement('strong'); intro.textContent=`已处理 ${successful} 款，${failed.length} 款未完成`; area.appendChild(intro);
  if (!failed.length) return;
  const list = document.createElement('div'); list.className = 'bulk-failure-list'; area.appendChild(list);
  for(const item of failed) {
    const row=document.createElement('div'); row.className='bulk-failure-item';
    const d=state.designs.find(x=>x.id===item.design_id);
    const message=document.createElement('span'); message.textContent=`${d?.design_code || item.design_id}：${item.error || '处理失败'}`;
    const button=document.createElement('button'); button.type='button'; button.className='wb-btn wb-btn-ghost'; button.textContent='仅重试此项';
    button.addEventListener('click', async () => { button.disabled=true; button.textContent='重试中…'; try { await retry(item); } finally { button.disabled=false; button.textContent='仅重试此项'; } });
    row.append(message, button); list.appendChild(row);
  }
}
async function refreshWorkbenchInfo() {
  try {
    const s=await apiFetch('/api/workbench/statistics');
    document.getElementById('today-tasks').textContent=s.today_tasks;
    document.getElementById('today-archived').textContent=s.today_archived;
    const batch=state.batches.find(b=>b.id===state.currentBatchId);
    const notice=document.getElementById('planning-job-notice');
    if (!batch?.auto_workflow) { notice.classList.add('hidden'); return; }
    const info=await apiFetch(`/api/workbench/batches/${batch.id}/planning`);
    if (batch.id!==state.currentBatchId) return;
    const pending=info.jobs.filter(j=>['queued','running'].includes(j.status)).length;
    const failed=info.jobs.filter(j=>j.status==='failed').length;
    notice.replaceChildren(); notice.classList.toggle('hidden',!pending && !failed);
    if (pending || failed) {
      const text=document.createElement('span'); text.textContent=pending ? `正在规划 ${pending} 款，规划完成后自动开始生成全景，无需手动启动${failed ? `；另有 ${failed} 款规划失败，请重试或联系管理员` : ''}` : `${failed} 款规划失败，未启动生图，请重试或联系管理员`; notice.appendChild(text);
      if(failed) { const button=document.createElement('button'); button.className='wb-btn wb-btn-ghost'; button.textContent='重试未完成方案'; button.onclick=async()=>{try { await apiFetch(`/api/workbench/batches/${batch.id}/retry-planning`,{method:'POST'}); await refreshWorkbenchInfo(); } catch(e) { showToast(e.message,true); }}; notice.appendChild(button); }
    }
  } catch(e) { console.error('工作台统计加载失败',e); }
}
async function loadWorkbenchSettings() {
  try {
    const settings=await apiFetch('/api/workbench/budget-settings');
    document.getElementById('budget-per-warning').value=settings.warning_per_group;
    document.getElementById('budget-per-hard').value=settings.hard_limit_per_group;
  } catch(e) { showToast(e.message,true); }
}
document.addEventListener('DOMContentLoaded',()=>{
  document.addEventListener('change',e=>{
    const type=e.target.dataset.select;
    if(type && workbenchSelection[type]) { selectWorkbenchItem(type,e.target.dataset.id,e.target.checked); syncBulkSelections(); }
  });
  for (const type of ['panorama','quad','export']) {
    document.getElementById(`${type}-select-all`).addEventListener('change',e=>{
      for (const box of document.querySelectorAll(`[data-select="${type}"]`)) selectWorkbenchItem(type,box.dataset.id,e.target.checked);
      syncBulkSelections();
    });
  }
  document.getElementById('panorama-bulk-approve').addEventListener('click',async()=>{
    const map=workbenchSelection.panorama; if(!map.size) return showToast('请选择待审核全景',true);
    const runOne = async item => { const result=await apiFetch(`/api/workbench/batches/${state.currentBatchId}/approve-panoramas`,{method:'POST',body:JSON.stringify({versions:{[item.design_id]: map.get(item.design_id)}})}); if(result.results?.[0]?.ok) map.delete(item.design_id); return result.results?.[0] || {design_id:item.design_id,ok:false,error:'无返回结果'}; };
    try { const result=await apiFetch(`/api/workbench/batches/${state.currentBatchId}/approve-panoramas`,{method:'POST',body:JSON.stringify({versions:Object.fromEntries(map)})});
      result.results.filter(x=>x.ok).forEach(x=>map.delete(x.design_id)); showBulkResult(result, async item => { const one=await runOne(item); showBulkResult({results:[one]}, async retryItem => { await runOne(retryItem); await loadDesigns(true); }); await loadDesigns(true); await loadBudget(); }); await loadDesigns(true); await loadBudget();
    } catch(e){showToast(e.message,true);}
  });
  document.getElementById('quad-bulk-archive').addEventListener('click',async()=>{
    const map=workbenchSelection.quad; if(!map.size) return showToast('请选择待验收组',true);
    const runOne = async item => { const result=await apiFetch(`/api/workbench/batches/${state.currentBatchId}/archive`,{method:'POST',body:JSON.stringify({versions:{[item.design_id]: map.get(item.design_id)}})}); if(result.results?.[0]?.ok) map.delete(item.design_id); return result.results?.[0] || {design_id:item.design_id,ok:false,error:'无返回结果'}; };
    try { const result=await apiFetch(`/api/workbench/batches/${state.currentBatchId}/archive`,{method:'POST',body:JSON.stringify({versions:Object.fromEntries(map)})});
      result.results.filter(x=>x.ok).forEach(x=>map.delete(x.design_id)); showBulkResult(result, async item => { const one=await runOne(item); showBulkResult({results:[one]}, async retryItem => { await runOne(retryItem); await loadDesigns(true); }); await loadDesigns(true); }); await loadDesigns(true);
    } catch(e){showToast(e.message,true);}
  });
  document.getElementById('export-selected').addEventListener('click',async()=>{
    const ids=[...workbenchSelection.export.keys()]; if(!ids.length) return showToast('请选择已完成组',true);
    await runZipExport('/api/export/selected',{method:'POST',body:JSON.stringify({batch_id:state.currentBatchId,design_ids:ids})}, `所选 ${ids.length} 组图片`);
  });
  document.getElementById('nas-upload-selected').addEventListener('click',async()=>{
    const ids=[...workbenchSelection.export.keys()]; if(!ids.length) return showToast('请选择已编号归档组',true);
    try { const result=await apiFetch('/api/nas-upload/enqueue',{method:'POST',body:JSON.stringify({batch_id:state.currentBatchId,design_ids:ids})});
      const ok=result.results.filter(x=>x.ok).length; showToast(`已加入或保留 ${ok} 组上传任务${ok<ids.length ? `，${ids.length-ok} 组未加入` : ''}`,ok<ids.length); await loadDesigns(true);
    } catch(e){showToast(e.message,true);}
  });
  document.getElementById('budget-settings-save').addEventListener('click',async()=>{
    try { const body={warning_per_group:Number(document.getElementById('budget-per-warning').value),hard_limit_per_group:Number(document.getElementById('budget-per-hard').value)};
      await apiFetch('/api/workbench/budget-settings',{method:'PUT',body:JSON.stringify(body)}); showToast('新任务默认额度已保存');
    } catch(e){showToast(e.message,true);}
  });
  document.addEventListener('click',e=>{
    const img=e.target.closest('img[src*="/api/media/preview/"]'); if(!img) return;
    const dialog=document.createElement('dialog'); dialog.className='image-preview-dialog';
    const close=document.createElement('button'); close.className='wb-btn wb-btn-ghost'; close.textContent='关闭'; close.onclick=()=>dialog.close();
    const full=document.createElement('img'); full.src=img.src; full.alt=img.alt || '图片预览';
    dialog.append(close,full); dialog.addEventListener('close',()=>dialog.remove()); document.body.appendChild(dialog); dialog.showModal();
  });
});
