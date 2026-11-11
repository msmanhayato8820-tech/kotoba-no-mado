'use strict';
const $ = id => document.getElementById(id);
const t = (...args) => I18N.t(...args);
let token='', current=null, suggestion='', actionPending=false, requestError='', quitting=false, needsReopen=false;
let setupState=null, connectedToServer=false, modelEditSerial=0;
// Include values as well as edit generation: changing away and back cancels stale requests.
function modelSelection(){return JSON.stringify([modelEditSerial,$('model-choice').value,$('model').value,$('runtime').value,$('context-tokens').value]);}
let runtimeCache={phase:'idle',path:null}, runtimeCheckPending=false;
let models=[], modelsReady=false, host={platform:'',machine:''};
let languages=[], executedLanguages=[], languageChecks={}, qualityNotes={}, languagesReady=false;
let translationRecord=null, reverseResult=null, lastClearSerial=null, pairRestored=false, pollTimer=null;
const PAIR_KEY='kotoba-window.translation-pair';
const displayControlIds=['display-language','theme','text-size'];
const listeners={refresh:[],clear:[]};

async function api(path,body) {
  const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':token},body:JSON.stringify(body)});
  const data=await response.json().catch(()=>({}));
  if (response.status===401) { needsReopen=true; render(); }
  if (!response.ok) throw new Error(data.error || t('operationFailed'));
  return data;
}
async function apiBlob(path,body) {
  const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':token},body:JSON.stringify(body)});
  if (!response.ok) { const data=await response.json().catch(()=>({})); throw new Error(data.error || t('operationFailed')); }
  return response.blob();
}
// The browser saves the file itself (its normal download). Nothing is uploaded.
function saveBlob(blob,name) {
  const url=URL.createObjectURL(blob); const link=document.createElement('a');
  link.href=url; link.download=name; link.rel='noopener'; document.body.append(link); link.click(); link.remove();
  setTimeout(()=>URL.revokeObjectURL(url),10000);
}
function showError(message) { requestError=message; render(); }
function busy() { return !!current && ['loading','translating'].includes(current.phase); }
function languageLabel(code){if(code==='auto')return t('autoDetect');const item=languages.find(x=>x.code===code);return item?languageName(item):code;}
function pairLabel(source,target){return languageLabel(source)+' → '+languageLabel(target);}
function hostSupports(model){return !!model && (model.hosts||[]).some(h=>h[0]===host.platform&&h[1]===host.machine);}
// Checked on this kind of computer (short samples etc.), as recorded in the catalog.
function confirmedHere(model){const s=model?.validation_status;if(s==='mac_windows_cpu_tested')return ['darwin','win32'].includes(host.platform);return ['short_sample_tested','mac_cpu_tested'].includes(s)&&host.platform==='darwin';}
function largeModel(model){return Number(model?.weight_bytes||0)>4e9;}
function externalTrial(model){return !!model && ['plamo-8b-gguf-q4km','plamo-31b-gguf-q4km'].includes(model.id) && model.backend==='llama.cpp';}
function experimental(model){return externalTrial(model) && host.platform==='win32' && host.machine==='x64' && (model.experimental_hosts||[]).some(h=>h[0]==='win32'&&h[1]==='x64');}
function loadable(model){return !!model && hostSupports(model) && (externalTrial(model)?experimental(model):model.load_enabled===true);}
function resetExperimental(){ $('experimental-ack').checked=false; }
function syncLoadedModel(){
  if(!modelsReady||!['loading','translating','ready'].includes(current?.phase)||!current.model?.model_id)return;
  // A different tab can load a model without this tab making a request. Never
  // carry an earlier trial check through a server-driven model/context change.
  resetExperimental();
  if($('model-choice').value!==current.model.model_id){$('model-choice').value=current.model.model_id;updateModel();}
  else updateContextNote();
}


function renderRuntimeStatus(){
  const gguf=selectedModel()?.backend==='llama.cpp';
  $('runtime-cache-panel').hidden=!gguf;
  if(!gguf)return;
  const explicit=!!$('runtime').value.trim();
  const phase=runtimeCache.phase||'idle';
  const keys={idle:'runtimeCacheIdle',checking:'runtimeCacheChecking',ready:'runtimeCacheReady',missing:'runtimeCacheMissing',invalid:'runtimeCacheInvalid',unavailable:'runtimeCacheUnavailable',cancelled:'runtimeCacheUnavailable'};
  $('runtime-cache-status').textContent=t(explicit?'runtimeExplicit':keys[phase]||'runtimeCacheUnavailable');
  $('runtime-cache-path').textContent=explicit?'':runtimeCache.path||'';
  $('runtime-cache-status').classList.toggle('warning',!explicit&&['invalid','missing','unavailable'].includes(phase));
  $('runtime-check').disabled=explicit||runtimeCheckPending||phase==='checking'||actionPending||busy()||!!setupState?.active||quitting||needsReopen||!connectedToServer;
}
async function checkRuntimeCache(){
  if(runtimeCheckPending||quitting||needsReopen||!connectedToServer||$('runtime').value.trim()||selectedModel()?.backend!=='llama.cpp')return;
  runtimeCheckPending=true;runtimeCache={phase:'checking',path:null};renderRuntimeStatus();
  try{runtimeCache=await api('/api/runtime/check',{});}catch(_){runtimeCache={phase:'unavailable',path:null};}
  finally{runtimeCheckPending=false;renderRuntimeStatus();}
}

function render() {
  $('reopen').hidden=!needsReopen;
  renderRuntimeStatus();
  if (!current) return;
  const isBusy=busy(), preparing=!!setupState?.active;
  const pending=current.pending;
  $('phase').textContent=t(current.phase);
  $('status').textContent=quitting ? t('quitDone') : I18N.message(current.message);
  $('light').className='dot '+(isBusy?'busy':current.phase==='ready'?'ready':'');
  $('load').disabled=preparing||actionPending||isBusy||current.phase==='ready'||quitting||!modelsReady||!loadable(selectedModel())||(experimental(selectedModel())&&!$('experimental-ack').checked);
  $('model-choice').disabled=preparing||actionPending||isBusy||current.phase==='ready'||quitting||!modelsReady;
  $('context-tokens').disabled=$('model-choice').disabled;
  for (const id of ['model','runtime','ack','experimental-ack']) $(id).disabled=preparing||actionPending||isBusy||quitting;
  $('experimental-ack').disabled=$('experimental-ack').disabled||current.phase==='ready';
  $('pick').disabled=preparing||actionPending||isBusy||quitting;
  $('runtime-pick').disabled=preparing||actionPending||isBusy||quitting;
  $('existing').disabled=preparing||actionPending||isBusy||quitting;
  $('stop').disabled=actionPending||!['loading','translating','ready'].includes(current.phase)||quitting;
  $('clear').disabled=actionPending||isBusy||quitting;
  const pair=selectedPair(), samePair=pair.source===pair.target;
  for(const id of ['source-lang','target-lang'])$(id).disabled=isBusy||actionPending||!languagesReady||quitting;
  $('swap').disabled=isBusy||actionPending||!languagesReady||quitting||pair.source==='auto';
  $('sample').disabled=isBusy||actionPending||quitting||!sampleText();
  $('translate').disabled=actionPending||current.phase!=='ready'||!languagesReady||!$('source').value.trim()||quitting||samePair;
  const error=requestError||current.error;
  $('error').hidden=!error; $('error').textContent=I18N.message(error);
  // Only the saved forward translation is shown here, with its own direction.
  const result=translationRecord?translationRecord.result:null;
  const waiting=pending&&pending.kind==='forward';
  $('result').textContent=waiting?t('translatingNow'):result?result.text:t('resultEmpty');
  $('result').classList.toggle('has-text',!!result&&!waiting);
  $('result-pair').textContent=translationRecord&&!waiting?t('resultPair',{pair:pairLabel(translationRecord.source,translationRecord.target)}):'';
  for (const id of ['copy','save-single-txt','save-single-docx']) $(id).disabled=!result||waiting||quitting;
  $('warning').hidden=!(result&&result.warning&&!waiting);
  $('warning').textContent=result?I18N.message(result.warning):'';
  $('timing').textContent=result&&!waiting?t('seconds',{seconds:result.seconds}):'';
  $('comparison').hidden=!translationRecord||waiting;
  const cannotReverse=translationRecord && (translationRecord.result.warning||translationRecord.result.text.length>2000);
  const autoSource=translationRecord && translationRecord.source==='auto';
  $('reverse').disabled=actionPending||current.phase!=='ready'||!translationRecord||!!cannotReverse||autoSource||quitting;
  $('reverse-limit').hidden=!cannotReverse;
  $('reverse-auto').hidden=!autoSource;
  $('compare-original').textContent=translationRecord?translationRecord.text:'';
  $('compare-forward').textContent=translationRecord?translationRecord.result.text:'';
  const reverseWaiting=pending&&pending.kind==='reverse';
  $('compare-reverse').textContent=reverseWaiting?t('translatingNow'):reverseResult?reverseResult.text:t('reverseEmpty');
  $('reverse-warning').hidden=!(reverseResult&&reverseResult.warning);
  $('reverse-warning').textContent=reverseResult?I18N.message(reverseResult.warning):'';
  for (const fn of listeners.refresh) fn();
}
function restorePair(record) {
  // After a reload, show the selectors that match the translation on screen.
  if (pairRestored||!languagesReady||!current) return; pairRestored=true;
  // The person's saved choice wins; the result keeps showing its own direction label.
  if (!record||savedPair()) return;
  if (record.source!=='auto'&&!languages.some(x=>x.code===record.source)) return;
  if (!languages.some(x=>x.code===record.target)) return;
  $('source-lang').value=record.source; $('target-lang').value=record.target; updateLanguage();
}
async function refresh() {
  if (quitting || actionPending) return;
  try {
    const response=await fetch('/api/status',{cache:'no-store'});
    // Not connected: stop asking. Only a fresh start of the app can connect this page.
    if (response.status===401) { needsReopen=true; connectedToServer=false; current=null; clearInterval(pollTimer); disableAll(); render(); return; }
    if (!response.ok) throw new Error(t('connectionError'));
    const data=await response.json();
    needsReopen=false; connectedToServer=true; token=data.token; current=data.state; const setupWas=setupState?.phase; setupState=data.setup||null;runtimeCache=data.runtime_cache||runtimeCache;
    translationRecord=current.forward||null; reverseResult=current.reverse||null;
    if (lastClearSerial!==null && current.clear_serial!==lastClearSerial) {
      // Another tab chose "Clear text": erase what this tab still shows.
      $('source').value=''; $('source').oninput(); for (const fn of listeners.clear) fn();
    }
    lastClearSerial=current.clear_serial;
    syncLoadedModel();
    // Preparation just finished here: the earlier "not found" result is out of date.
    if(setupWas&&setupWas!=='ready'&&setupState?.phase==='ready'&&runtimeCache.phase!=='checking')checkRuntimeCache();
    suggestion=selectedModel()?.suggestion||'';
    $('existing').hidden=!suggestion; restorePair(translationRecord); render();
  } catch (_) {
    connectedToServer=false;renderRuntimeStatus();
    $('phase').textContent=t('disconnected'); $('status').textContent=t('reopen');
    for (const id of ['load','translate','pick','stop','clear','reverse','existing','model-choice','setup-plan','setup-start','setup-cancel','setup-use']) $(id).disabled=true;
  }
}
function disableAll(){for(const element of document.querySelectorAll('main button,main input,main textarea,main select'))element.disabled=true;}
async function action(fn) {
  if (actionPending||quitting) return;
  actionPending=true; requestError=''; render();
  try { await fn(); } catch (error) { requestError=error.message; }
  finally { actionPending=false; if (quitting) render(); else await refresh(); }
}
function selectedModel(){return models.find(item=>item.id===$('model-choice').value);}
function gb(bytes){return (bytes/1e9).toFixed(2);}
function updateModel(){
  const model=selectedModel();
  if(updateModel.lastModelId!==model?.id){
    modelEditSerial++;resetExperimental();$('ack').checked=false;updateModel.lastModelId=model?.id;
  }
  suggestion=model?.suggestion||''; $('existing').hidden=!suggestion;
  executedLanguages=model?.execution_confirmed_languages||[];
  confirmedPairs=model?.execution_confirmed_pairs||[];
  qualityNotes=model?.quality_notes||{}; languageChecks={};
  $('selected-model-name').textContent=model?.name||'';
  $('model-platform').textContent=model?t(externalTrial(model)?'platformExperimental':model.backend==='llama.cpp'?'platformGguf':'platformMac')+(hostSupports(model)?'':' '+t('platformOther')):'';
  // The advanced setting asks for the runtime that this kind of model needs.
  const gguf=model&&model.backend==='llama.cpp';
  $('path-order').textContent=t(gguf?'pathOrderGguf':'pathOrderMlx');
  $('model-label').textContent=t(gguf?'modelLabelGguf':'modelLabel');
  $('model-help').textContent=t(gguf?'modelHelpGguf':'modelHelpMlx');
  $('model').placeholder=t(gguf?'modelPlaceholderGguf':'modelPlaceholder');
  $('runtime').placeholder=t(gguf?'runtimePlaceholderGguf':'runtimePlaceholder');
  $('runtime-label').textContent=t(gguf?'runtimeLabelGguf':'runtimeLabel');$('runtime-help').textContent=t(gguf?'runtimeHelpGguf':'runtimeHelp');$('runtime-pick').hidden=!gguf;
  renderRuntimeStatus();
  $('model-capacity').textContent=model?t(model.size_basis==='official_listing'?'modelFileSizeListed':'modelFileSize',{weights:gb(model.weight_bytes),total:gb(model.download_bytes)}):'';
  $('model-hosts').textContent=model?t('modelHosts'):'';
  $('model-readiness').textContent=model?(experimental(model)?t('modelExperimental'):!hostSupports(model)?t('modelWrongOs'):!model.load_enabled?(model.validation_status==='isolated_mlx_tested'?t('modelIsolatedMlxTested'):t('modelPending')):model.validation_status==='short_sample_tested'?t('modelTested'):model.validation_status==='mac_windows_cpu_tested'?t('modelMacWindowsCpuTested'):model.validation_status==='mac_cpu_tested'?t(host.platform==='win32'?'modelWindowsTrial':'modelMacCpuTested'):t('modelAvailableUntested')):'';
  if(model&&loadable(model)&&largeModel(model))$('model-readiness').textContent+=' '+t('modelLarge',{size:gb(model.weight_bytes)});
  else if(model&&loadable(model)&&!experimental(model)&&!confirmedHere(model))$('model-readiness').textContent+=' '+t('modelNotCheckedHere');
  $('model-license').href=model?((I18N.locale==='en'?model.license_url_en:model.license_url)||model.license_url):'#';
  const trial=externalTrial(model);
  $('experimental-details').hidden=!trial;
  $('experimental-check').hidden=!experimental(model);
  $('experimental-file').textContent=trial?t('experimentalFile',{name:model.external_file_name,size:gb(model.weight_bytes),revision:model.revision}):'';
  $('experimental-source').href=trial?model.external_source_url:'#';
  if(!experimental(model))resetExperimental();
  updateContextNote();
  if(languagesReady)updateLanguage();else render();
}
// Context length: 4,096 by default. Larger values are offered up to the official
// training maximum, with measured/unmeasured status shown for the selected model.
function updateContextNote(){
  const model=selectedModel(); const select=$('context-tokens'); if(!model)return;
  const loaded=current&&['loading','translating','ready'].includes(current.phase)&&current.model?.context_token_cap;
  if(loaded){
    const serverContext=String(current.model.context_token_cap);
    if(select.value!==serverContext)resetExperimental();
    select.value=serverContext;
  }
  for(const option of select.options){const value=Number(option.value);option.disabled=value>(model.max_context_tokens||4096);
    option.textContent=value.toLocaleString()+(value===4096?' '+t('contextDefault'):'')+(value===32768?' '+t('contextMax'):'')+' '+t((model.context_measured||[]).includes(value)?'contextMeasured':'contextUnmeasured');}
  const value=Number(select.value); const parts=[t('contextExplain')];
  if(value>4096)parts.push(t('contextWarning'));
  parts.push(t((model.context_measured||[]).includes(value)?'contextMeasuredNote':'contextUnmeasuredNote',{value:value.toLocaleString()}));
  $('context-note').textContent=parts.join(' ');
  $('context-note').classList.toggle('warning',value>4096);
}
function populateModels(){
  const loadedId=current&&['loading','translating','ready'].includes(current.phase)&&current.model?.model_id;
  // Beginners on every OS start with the small 2B GGUF that automatic preparation provides.
  const selected=loadedId||$('model-choice').value||'plamo-2b-gguf-q4km'; $('model-choice').replaceChildren();
  const bySize=list=>list.slice().sort((a,b)=>Number(a.weight_bytes||0)-Number(b.weight_bytes||0));
  const usable=models.filter(m=>loadable(m)&&!experimental(m));
  const groups=[['groupUsable',bySize(usable.filter(confirmedHere))],['groupUnconfirmed',bySize(usable.filter(m=>!confirmedHere(m)))],['groupExperimental',models.filter(experimental)],['groupPending',models.filter(m=>!loadable(m))]];
  for (const [label,items] of groups) {
    if (!items.length) continue;
    const group=document.createElement('optgroup'); group.label=t(label);
    for(const model of items){const option=document.createElement('option');option.value=model.id;option.textContent=model.name+(experimental(model)?' '+t('experimentalSuffix'):loadable(model)?(largeModel(model)?' '+t('largeSuffix',{size:gb(model.weight_bytes)}):''):' '+t('notUsableSuffix'));group.append(option);}
    $('model-choice').append(group);
  }
  $('model-choice').value=selected; if(!selectedModel()&&models.length)$('model-choice').value=models.find(loadable)?.id||models[0].id;
  updateModel();
}
$('model-choice').onchange=()=>{
  if(current&&['loading','translating','ready'].includes(current.phase))return;
  $('model').value='';$('runtime').value='';$('ack').checked=false;resetExperimental();requestError='';
  if(experimental(selectedModel()))$('manual-model-settings').open=true;
  updateModel();
  if(runtimeCache.phase==='idle')checkRuntimeCache();
};
$('existing').onclick=()=>{modelEditSerial++;$('model').value=suggestion;resetExperimental();render();};
$('pick').onclick=()=>action(async()=>{const data=await api('/api/pick',{locale:I18N.locale});if(!data.cancelled){modelEditSerial++;$('model').value=data.path;resetExperimental();}});
$('runtime-check').onclick=checkRuntimeCache;
$('runtime-pick').onclick=()=>action(async()=>{const data=await api('/api/pick',{locale:I18N.locale,kind:'runtime'});if(!data.cancelled){modelEditSerial++;$('runtime').value=data.path;resetExperimental();}});
$('load').onclick=()=>action(()=>{
  const confirmed=$('experimental-ack').checked;
  resetExperimental(); // Consume once, including failures or uncertain responses; never persist.
  return api('/api/load',{path:$('model').value,runtime:$('runtime').value,acknowledged:$('ack').checked,model_id:$('model-choice').value,context_tokens:Number($('context-tokens').value),experimental_acknowledged:confirmed});
});
$('experimental-ack').onchange=render;
for(const id of ['model','runtime'])$(id).oninput=()=>{modelEditSerial++;resetExperimental();render();};
$('context-tokens').onchange=()=>{modelEditSerial++;resetExperimental();updateContextNote();render();};
window.addEventListener('pageshow',()=>{resetExperimental();render();});
$('stop').onclick=()=>action(async()=>{resetExperimental();await api('/api/stop',{});});
$('source').oninput=()=>{$('count').textContent=`${$('source').value.length} / 2000`;render();};
function limitValue(){return $('limit').value==='auto'?'auto':Number($('limit').value);}
// Translation pair: any two different languages; the source may be detected automatically.
let evaluation={hubs:[],languages:[]}, confirmedPairs=[];
function selectedPair(){return {source:$('source-lang').value||'ja',target:$('target-lang').value||'en'};}
function selectedLanguage(){return languages.find(item=>item.code===$('source-lang').value);}
function languageName(item){return I18N.locale==='en'?(item.name_en||item.name):item.name;}
function sampleText(){const s=$('source-lang').value;if(s==='ja')return '今日はいい天気です。';if(s==='auto')return '';return selectedLanguage()?.sample||'';}
function savePair(){try{localStorage.setItem(PAIR_KEY,JSON.stringify(selectedPair()));}catch(_){}}
function savedPair(){
  try{
    const v=JSON.parse(localStorage.getItem(PAIR_KEY)||'null');
    if(v&&typeof v.source==='string'&&typeof v.target==='string')return v;
    // 0.5 saved {language, direction}: convert it once.
    if(v&&typeof v.language==='string'&&['from-ja','to-ja'].includes(v.direction))return v.direction==='from-ja'?{source:'ja',target:v.language}:{source:v.language,target:'ja'};
  }catch(_){}
  return null;
}
function pairStatus(source,target){
  if(source==='auto')return 'unknown';
  const hubs=evaluation.hubs||[], scope=evaluation.languages||[];
  return (hubs.includes(source)&&scope.includes(target))||(hubs.includes(target)&&scope.includes(source))?'official':'unevaluated';
}
function pairConfirmed(source,target){
  if(source==='ja')return executedLanguages.includes(target);
  if(target==='ja')return executedLanguages.includes(source);
  return confirmedPairs.some(p=>p[0]===source&&p[1]===target);
}
function updateLanguage() {
  const {source,target}=selectedPair();
  const notes=[];
  if(source===target)notes.push(t('pairSame'));
  else{
    const status=pairStatus(source,target);
    notes.push(t(status==='official'?'pairOfficial':status==='unknown'?'pairAuto':'pairUnevaluated'));
    if(source!=='auto')notes.push(t(pairConfirmed(source,target)?'pairConfirmed':'pairNotConfirmed'));
    const other=source==='ja'?target:target==='ja'?source:null;
    if(other&&languageChecks[other]==='review')notes.push(t('reviewNeeded'));
    if(other&&qualityNotes[other])notes.push(qualityNotes[other][I18N.locale]);
  }
  $('language-note').textContent=notes.join(' ');
  $('language-note').classList.toggle('warning',source===target);
  $('sample').textContent=t(sampleText()?'sample':'noSample');
  render();
}
function fillLanguageSelect(select,withAuto,selected){
  select.replaceChildren();
  if(withAuto){const option=document.createElement('option');option.value='auto';option.textContent=t('autoDetect');select.append(option);}
  for(const item of languages){const option=document.createElement('option');option.value=item.code;option.textContent=languageName(item);select.append(option);}
  select.value=selected;
}
function populateLanguages(){
  const saved=populateLanguages.done?null:savedPair();
  const source=(populateLanguages.done?$('source-lang').value:saved?.source)||'en';
  const target=(populateLanguages.done?$('target-lang').value:saved?.target)||'ja';
  const valid=code=>languages.some(x=>x.code===code);
  fillLanguageSelect($('source-lang'),true,source==='auto'||valid(source)?source:'ja');
  fillLanguageSelect($('target-lang'),false,valid(target)?target:'en');
  populateLanguages.done=true;updateLanguage();
}
$('source-lang').onchange=()=>{savePair();updateLanguage();};$('target-lang').onchange=()=>{savePair();updateLanguage();};
$('swap').onclick=()=>{const {source,target}=selectedPair();if(source==='auto')return;$('source-lang').value=target;$('target-lang').value=source;savePair();updateLanguage();};
$('sample').onclick=()=>{const text=sampleText();if(!text)return;$('source').value=text;$('source').oninput();};
$('translate').onclick=()=>action(async()=>{
  await api('/api/translate',{text:$('source').value,...selectedPair(),limit:limitValue(),kind:'forward'});
});
$('reverse').onclick=()=>action(async()=>{
  if(!translationRecord)return;
  await api('/api/translate',{text:translationRecord.result.text,source:translationRecord.target,target:translationRecord.source,limit:limitValue(),kind:'reverse'});
});
$('clear').onclick=()=>action(async()=>{await api('/api/clear',{});});
$('copy').onclick=async()=>{try{await navigator.clipboard.writeText(translationRecord.result.text);$('copy').textContent=t('copied');setTimeout(()=>$('copy').textContent=t('copy'),1500);}catch(_){showError(t('copyError'));}};
function fileStamp(){const d=new Date();const p=n=>String(n).padStart(2,'0');return `${d.getFullYear()}${p(d.getMonth()+1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;}
async function saveSingle(format){
  if(!translationRecord)return;
  try{
    const items=[{source:translationRecord.text,translation:translationRecord.result.text,after:'\n'}];
    const name=`${t('fileBase')}_${translationRecord.target}_${fileStamp()}.${format}`;
    saveBlob(await apiBlob('/api/export',{format,layout:'translation',items,title:t('fileBase')}),name);
  }catch(error){showError(error.message);}
}
$('save-single-txt').onclick=()=>saveSingle('txt');$('save-single-docx').onclick=()=>saveSingle('docx');
$('quit').onclick=()=>action(async()=>{
  await api('/api/quit',{});quitting=true;translationRecord=null;reverseResult=null;$('source').value='';$('count').textContent='0 / 2000';
  current={phase:'idle',message:'',result:null,error:null};for (const fn of listeners.clear) fn();
  for(const element of document.querySelectorAll('button,input,textarea,select')) if(!displayControlIds.includes(element.id))element.disabled=true;
});
async function loadLanguages(){try{const response=await fetch('/api/languages',{cache:'no-store'});if(!response.ok)throw new Error();const catalog=await response.json();languages=catalog.languages;evaluation=catalog.official_evaluation||evaluation;executedLanguages=selectedModel()?.execution_confirmed_languages||[];languageChecks={};qualityNotes=selectedModel()?.quality_notes||{};languagesReady=true;populateLanguages();restorePair(translationRecord);}catch(_){showError(t('languageError'));}}
async function loadModels(){try{const response=await fetch('/api/models',{cache:'no-store'});if(!response.ok)throw Error('models');const data=await response.json();models=data.models;host=data.host||host;modelsReady=true;populateModels();}catch(_){if(!needsReopen){requestError=t('modelsError');render();}}}
window.addEventListener('languagechange',()=>{if(modelsReady)populateModels();if(languagesReady)populateLanguages();render();});
// First run: ask for the app language once, before anything else.
function welcome(){
  let saved=null;try{saved=localStorage.getItem('local-translate-language');}catch(_){saved='blocked';}
  const dialog=$('welcome'); if(saved||typeof dialog.showModal!=='function')return;
  dialog.addEventListener('cancel',event=>event.preventDefault());
  for(const button of dialog.querySelectorAll('button'))button.onclick=()=>{const select=$('display-language');select.value=button.value;select.dispatchEvent(new Event('change'));dialog.close();};
  dialog.showModal();
}
async function start(){
  const match=location.hash.match(/code=([A-Za-z0-9_-]+)/);
  if(match){
    try{await fetch('/api/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:match[1]})});}catch(_){}
    history.replaceState(null,'',location.pathname); // do not keep the code in history
  }
  resetExperimental(); // Ignore any browser-restored checkbox value.
  welcome();
  await refresh(); await loadLanguages(); await loadModels();
  if(runtimeCache.phase==='idle')checkRuntimeCache();
  if(!needsReopen)pollTimer=setInterval(refresh,800);
}
function usePreparedModel(state){
  if(actionPending||quitting||needsReopen||!current||['loading','ready','translating'].includes(current.phase)||state?.phase!=='ready'||state.active)return false;
  const model=models.find(item=>item.id==='plamo-2b-gguf-q4km');
  if(!loadable(model)||!state.model_path||!state.runtime_path)return false;
  $('model-choice').value=model.id;$('model').value=state.model_path;$('runtime').value=state.runtime_path;
  $('ack').checked=false;resetExperimental();$('context-tokens').value='4096';requestError='';
  $('manual-model-settings').open=true;updateModel();$('ack').focus();return true;
}
function foundModel(){const model=models.find(item=>item.id==='plamo-2b-gguf-q4km');return loadable(model)?(model.suggestion||''):'';}
// A folder found from an earlier preparation: fill it in only when the person asks.
// The runtime field stays empty so the pinned cache is checked; nothing is loaded here.
function useFoundModel(){
  const path=foundModel();
  if(actionPending||quitting||needsReopen||!current||['loading','ready','translating'].includes(current.phase)||setupState?.active||!path)return false;
  $('model-choice').value='plamo-2b-gguf-q4km';$('model').value=path;$('runtime').value='';
  $('ack').checked=false;resetExperimental();$('context-tokens').value='4096';requestError='';
  $('manual-model-settings').open=true;updateModel();checkRuntimeCache();$('ack').focus();return true;
}
window.App={get modelSelection(){return modelSelection();},token:()=>token,api,usePreparedModel,useFoundModel,get found(){return foundModel();},get setup(){return setupState;},get connected(){return connectedToServer&&!!current&&!needsReopen&&!quitting;},apiBlob,saveBlob,selectedPair,languageLabel,pairLabel,fileStamp,action,render,listeners,get current(){return current;},get busy(){return busy();},get pending(){return actionPending;},get quitting(){return quitting;},showError};
start();
