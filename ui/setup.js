'use strict';
// No background download or automatic model load. Every start follows a fresh
// visible plan and an explicit, never-persisted consent checkbox.
(() => {
  const $ = id => document.getElementById(id), t = (...args) => I18N.t(...args), A = window.App;
  let plan=null, pending=false, error='', used=false, attempted=false, foundUsed=false;
  const bytes = value => (Number(value||0)/1e9).toFixed(2)+' GB';
  const text = value => typeof value==='object'&&value ? (value[I18N.locale]||value.en||value.ja||'') : (value||'');
  const engineActive = () => !!A.current&&['loading','ready','translating'].includes(A.current.phase);
  const state = () => A.setup||{phase:'idle',active:false};
  function link(id, value) {
    const url=text(value); $(id).hidden=!url;
    // All links come from this app's fixed plan, never from a form field.
    if(url&&url.startsWith('https://')) $(id).href=url; else $(id).removeAttribute('href');
  }
  function renderPlan() {
    if(!plan)return;
    $('setup-size').textContent=t('prepareSize',{size:bytes(plan.download_bytes)});
    $('setup-space').textContent=t('prepareSpace',{required:bytes(plan.required_free_bytes),free:bytes(plan.free_bytes)});
    $('setup-hosts').textContent=(plan.download_hosts||[...new Set((plan.files||[]).map(f=>f.download_host).filter(Boolean))]).join(', ');
    $('setup-save-dir').textContent=plan.save_dir||'';
    $('setup-files').replaceChildren();
    for(const file of plan.files||[]){const item=document.createElement('li');item.textContent=file.name+' ('+bytes(file.bytes)+')';$('setup-files').append(item);}
    const model=(plan.files||[]).find(f=>f.kind==='model'), runtime=(plan.files||[]).find(f=>f.kind==='runtime');
    link('setup-license',plan.model_license_url||model?.license_url);
    link('setup-runtime-license',runtime?.license_url||'https://github.com/ggml-org/llama.cpp/blob/b11938/LICENSE');
    link('setup-registration',plan.commercial_registration_url||'https://forms.gle/mTL8tBLrMYXKNZD56');
    $('setup-commercial').textContent=t('prepareCommercial');
    const warnings=plan.warnings_i18n?.[I18N.locale]||plan.warnings||[];
    $('setup-plan-warnings').textContent=warnings.map(value=>I18N.message(text(value))).join(' ');
  }
  function render() {
    const s=state(), active=!!s.active, ready=s.phase==='ready';
    const blocked=pending||A.pending||A.quitting||!A.connected;
    $('setup-plan').disabled=blocked||active||engineActive();
    $('setup-plan').hidden=active||ready;
    $('setup-plan').textContent=t(['error','cancelled'].includes(s.phase)||attempted?'prepareRetry':'preparePlan');
    $('setup-plan-details').hidden=!plan||active||ready||attempted;
    $('setup-consent').disabled=blocked||active||engineActive()||attempted;
    $('setup-start').disabled=blocked||active||engineActive()||!plan||!$('setup-consent').checked||attempted;
    $('setup-cancel').hidden=!active;
    $('setup-cancel').disabled=blocked||s.phase==='cancelling';
    $('setup-use').hidden=!ready;
    $('setup-use').disabled=blocked||engineActive()||active;
    $('setup-use-note').hidden=!used||used.selection!==A.modelSelection;
    // After a restart the earlier download is offered again; it is never loaded automatically.
    const found=$('setup-found'), offer=!!found&&!!A.found&&!active&&!ready&&!plan&&!attempted&&(s.phase||'idle')==='idle';
    if(found){found.hidden=!offer;found.disabled=blocked||engineActive();$('setup-found-note').hidden=!foundUsed||foundUsed.selection!==A.modelSelection;}
    $('setup-progress-wrap').hidden=!active;
    const total=Number(s.total_bytes||0), done=Number(s.progress_bytes||0);
    $('setup-progress').max=Math.max(1,total);$('setup-progress').value=Math.min(done,Math.max(1,total));
    $('setup-progress-text').textContent=active?t('prepareProgress',{done:bytes(done),total:bytes(total),file:s.current_file||''}):'';
    $('setup-status').textContent=engineActive()&&!active?t('prepareStopFirst'):offer?t('prepareFound'):t('preparePhase_'+(s.phase||'idle'));
    const failure=error||(s.error_i18n?text(s.error_i18n):s.error);
    $('setup-error').hidden=!failure;$('setup-error').textContent=I18N.message(failure||'');
    renderPlan();
  }
  async function get(path) {
    const response=await fetch(path,{cache:'no-store'}), data=await response.json();
    if(!response.ok)throw new Error(data.error||t('prepareFailed'));
    return data;
  }
  $('setup-plan').onclick=async()=>{
    if(pending||A.pending||!A.connected||state().active||engineActive()||A.quitting)return;
    pending=true;error='';plan=null;used=false;attempted=false;$('setup-consent').checked=false;render();
    try{plan=await get('/api/setup/plan');}catch(e){error=e.message;}finally{pending=false;render();}
  };
  $('setup-consent').onchange=render;
  $('setup-start').onclick=async()=>{
    if(pending||A.pending||!A.connected||!plan||!$('setup-consent').checked||state().active||engineActive()||attempted||A.quitting)return;
    // Consume this UI confirmation even when the result is uncertain. Retrying
    // requires showing the plan again; polling never resends this request.
    pending=true;attempted=true;error='';$('setup-consent').checked=false;render();
    try{await A.action(()=>A.api('/api/setup/start',{acknowledged:true,plan_id:plan.plan_id}));}
    catch(e){error=e.message;}
    finally{pending=false;render();}
  };
  $('setup-cancel').onclick=async()=>{
    if(pending||A.pending||!A.connected||!state().active||A.quitting)return;
    pending=true;error='';plan=null;$('setup-consent').checked=false;render();
    try{await A.action(()=>A.api('/api/setup/cancel',{}));}catch(e){error=e.message;}finally{pending=false;render();}
  };
  $('setup-use').onclick=async()=>{
    if(pending||A.pending||!A.connected||engineActive()||state().phase!=='ready'||A.quitting)return;
    pending=true;error='';const selection=A.modelSelection;render();
    try{const prepared=await get('/api/setup/status');
      // A newer edit wins, including a change away and back while waiting.
      if(selection===A.modelSelection){used=A.usePreparedModel(prepared)?{selection:A.modelSelection}:false;if(!used)error=t('prepareUseFailed');}
    }
    catch(e){error=e.message;}finally{pending=false;render();}
  };
  if($('setup-found'))$('setup-found').onclick=()=>{
    if(pending||A.pending||!A.connected||engineActive()||state().active||A.quitting)return;
    error='';foundUsed=A.useFoundModel()?{selection:A.modelSelection}:false;if(!foundUsed)error=t('prepareUseFailed');render();
  };
  A.listeners.refresh.push(render);
  window.addEventListener('languagechange',render);
  render();
})();
