'use strict';
// Document import, long-text translation and saving. Files are read by this page
// and sent only to this app on 127.0.0.1; the app keeps them in memory.
(() => {
  const $ = id => document.getElementById(id);
  const t = (...args) => I18N.t(...args);
  let segments=[], job=null, lastDone=-1, lastStatus='', fileBase='', fetching=false, extracting=false, handledLong='';
  // loadSeq: a newer load or a clear makes any earlier, still-running load discard its result.
  // textHash: SHA-256 of the text box, compared with the fingerprint of the translated document.
  let loadSeq=0, textHash='', hashedText=null, hashSeq=0;
  let resultJob=null, resultEpoch=0, fetchAgain=false, restoreText=true;
  const jobMark=j=>j ? `${j.id}:${j.done}/${j.total}:${j.status}` : '';
  function invalidateResult(){resultEpoch++;fetchAgain=false;segments=[];resultJob=null;lastDone=-1;lastStatus='';}
  const MAX_CHARS=200000;

  function docText(){return $('doc-text').value;}
  function joined(){return segments.map(s=>(s.translation||'')+s.after).join('').replace(/\s+$/,'');}
  function hasResult(){return segments.some(s=>s.translation);}
  async function updateHash(){
    const seq=++hashSeq, text=docText();
    textHash='';hashedText=null;render();
    let hex='';
    try{const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(text));hex=[...new Uint8Array(digest)].map(b=>b.toString(16).padStart(2,'0')).join('');}catch(_){hex='';}
    if(seq===hashSeq&&text===docText()){textHash=hex;hashedText=text;render();}
  }
  // The result on screen belongs to the text in the box only when the fingerprints match.
  function resultMatchesText(){return !!(job&&resultJob&&job.id===resultJob.id&&
    jobMark(job)===jobMark(resultJob)&&resultJob.text_sha256&&textHash&&
    hashedText===docText()&&resultJob.text_sha256===textHash);}
  function estimate(){const chunk=Number($('chunk').value);return Math.max(1,Math.ceil(docText().length/chunk));}

  function render(){
    const app=window.App, current=app.current; if(!current)return;
    const running=job&&['running','planning'].includes(job.status);
    const ready=current.phase==='ready'&&!app.pending&&!app.quitting;
    const length=docText().length;
    $('doc-count').textContent=length?t('docCount',{chars:length.toLocaleString(),parts:estimate()}):'';
    $('doc-start').disabled=!ready||running||!docText().trim()||length>MAX_CHARS||extracting;
    $('doc-cancel').disabled=!running||(job&&job.cancelling);
    for(const id of ['chunk','context','file-pick','doc-text','glossary'])$(id).disabled=running||app.quitting;
    $('drop').classList.toggle('disabled',running||app.quitting);
    $('doc-progress-wrap').hidden=!job;
    if(job){
      $('doc-progress').max=job.total;$('doc-progress').value=job.done;
      const key=running?(job.cancelling?'docCancelling':job.status==='planning'?'docPlanning':'docRunning'):job.status==='done'?'docDone':job.status==='cancelled'?'docCancelled':'docFailed';
      $('doc-progress-text').textContent=t(key,{done:job.done,total:job.total,seconds:job.seconds});
      $('doc-plan').textContent=t('docBudget',{budget:job.budget.toLocaleString(),cap:job.cap.toLocaleString(),total:job.total})+(job.planned_splits?' '+t('docPlannedSplits',{n:job.planned_splits}):'')+(job.glossary?' '+t('docGlossaryUsed',{n:job.glossary}):'');
      const notes=[];
      if(job.resplit)notes.push(t('docResplit',{n:job.resplit}));
      if(job.cut_off)notes.push(t('docCutOff',{n:job.cut_off}));
      if(job.failed)notes.push(t('docFailedParts',{n:job.failed}));
      if(job.context_reduced)notes.push(t('docContextReduced',{n:job.context_reduced}));
      $('doc-summary').hidden=!notes.length;$('doc-summary').textContent=notes.join(' ');
    } else { $('doc-summary').hidden=true; $('doc-plan').textContent=''; }
    $('doc-result').textContent=hasResult()?joined():t('docResultEmpty');
    $('doc-result').classList.toggle('has-text',hasResult());
    $('doc-pairs-wrap').hidden=!hasResult();
    const stale=hasResult()&&!running&&!resultMatchesText();
    $('doc-stale').hidden=!stale;
    $('doc-save').disabled=!hasResult()||running||stale||app.quitting;
    if(!$('save-name').value&&hasResult())$('save-name').value=defaultName();
    const translated=segments.filter(s=>s.translation).length;
    $('save-preview').textContent=hasResult()?t('savePreview',{layout:t($('save-layout').value==='pairs'?'layoutPairs':'layoutTranslation'),done:translated,total:segments.length,chars:($('save-layout').value==='pairs'?segments.reduce((n,s)=>n+s.source.length+(s.translation||'').length,0):joined().length).toLocaleString()})+(translated<segments.length?' '+t('savePartial'):''):'';
  }
  function defaultName(){
    const target=job?job.target:'';const base=((job&&job.name)||t('fileBase')).replace(/[\\/:*?"<>|]+/g,'_').slice(0,60);
    return `${base}_${target}_${App.fileStamp()}`;
  }
  function renderPairs(){
    const list=$('doc-pairs');list.replaceChildren();
    for(const s of segments){
      const item=document.createElement('li');
      const a=document.createElement('div');a.className='comparison-text';a.dir='auto';a.textContent=s.source;
      const b=document.createElement('div');b.className='comparison-text';b.dir='auto';b.textContent=s.translation===null?t('docNotYet'):s.translation||t('docPartFailed');
      item.append(a,b);
      if(s.warning){const w=document.createElement('p');w.className='small warning';w.textContent=I18N.message(s.warning);item.append(w);}
      list.append(item);
    }
  }
  async function fetchResult(){
    if(fetching){fetchAgain=true;return;}
    const expected=App.current&&App.current.job;
    if(!expected||extracting)return;
    const epoch=resultEpoch, expectedId=expected.id;
    fetching=true;fetchAgain=false;
    try{
      const data=await App.api('/api/document/result',{});
      const latest=App.current&&App.current.job;
      // A response is one snapshot. Never pair its segments with a different job.
      if(epoch!==resultEpoch||extracting||!latest||!data.job||
         latest.id!==expectedId||data.job.id!==expectedId||jobMark(data.job)!==jobMark(latest))return;
      resultJob=Object.assign({},data.job);
      segments=(data.segments||[]).map(s=>Object.assign({},s));
      lastDone=jobMark(resultJob);lastStatus=resultJob.status;
      // Rehydrate only an untouched page, never an explicitly cleared/edited input.
      if(restoreText&&!docText().trim()&&data.source_text){
        $('doc-text').value=data.source_text;fileBase=resultJob.name||'';updateHash();
      }
      restoreText=false;
      renderPairs();render();
    }
    catch(_){}finally{
      fetching=false;
      if(fetchAgain){fetchAgain=false;fetchResult();}
    }
  }
  function parseGlossary(){
    const terms=[];
    for(const line of $('glossary').value.split('\n')){
      const m=line.match(/^\s*(.+?)\s*(?:=|\t|→|=>)\s*(.+?)\s*$/); if(m)terms.push({source:m[1],target:m[2]});
    }
    return terms;
  }
  function startDocument(){
    return App.action(async()=>{
      invalidateResult();restoreText=false;renderPairs();$('save-name').value='';$('save-done').textContent='';
      await App.api('/api/document/translate',{text:docText(),name:fileBase,...App.selectedPair(),chunk:Number($('chunk').value),context:Number($('context').value),glossary:parseGlossary()});
    });
  }
  function onRefresh(){
    const current=App.current;const next=current&&current.job;
    // A short-box text was too long for one pass: move it here and split it automatically.
    const long=current&&current.long_input;
    if(long&&long.text!==handledLong&&current.phase==='ready'&&!App.pending){
      handledLong=long.text;$('doc-text').value=long.text;fileBase='';updateHash();
      $('file-info').textContent=t('autoSplitNotice');$('doc-card').scrollIntoView({behavior:'smooth',block:'start'});
      startDocument();
    }
    if(next){
      const cancelling=job&&job.cancelling&&next.status==='running';
      job=Object.assign({},next,{cancelling});
      const mark=jobMark(next);
      if(mark!==lastDone)fetchResult();
    } else if(job&&!next){job=null;invalidateResult();renderPairs();}
    render();
  }
  function clearAll(){
    loadSeq++;extracting=false;textHash='';hashedText=null;hashSeq++;restoreText=false;
    invalidateResult();job=null;fileBase='';$('glossary').value='';
    $('doc-text').value='';$('file-info').textContent='';$('file-warnings').hidden=true;$('file-warnings').replaceChildren();
    $('save-name').value='';$('save-done').textContent='';renderPairs();render();
  }
  async function readFile(file){
    if(!file||extracting||(job&&['running','planning'].includes(job.status)))return;
    if(file.size>20*1024*1024){App.showError(t('fileTooLarge'));return;}
    const seq=++loadSeq;
    invalidateResult();restoreText=false;renderPairs();
    extracting=true;$('file-info').textContent=t('fileReading',{name:file.name});render();
    try{
      const buffer=await file.arrayBuffer();
      const head=new Uint8Array(buffer.slice(0,5));
      const isPdf=String.fromCharCode(...head)==='%PDF-';
      let data;
      if(isPdf){
        // PDFs are read here in the page (pdf.js); only the extracted text is used.
        const pdf=await PdfReader.extract(buffer,(n,total)=>{$('file-info').textContent=t('pdfReading',{name:file.name,n,total});});
        const text=pdf.text.replace(/<\|plamo:/g,'<| plamo:').replace(/\n{3,}/g,'\n\n').trim();
        if(!text)throw new Error(t('pdfNoText'));
        if(text.length>200000)throw new Error(t('fileTooLong'));
        const warnings=[t('pdfOrder')];
        if(pdf.emptyPages.length)warnings.unshift(t('pdfEmptyPages',{pages:pdf.emptyPages.join(', '),n:pdf.emptyPages.length,total:pdf.pages}));
        if(pdf.columnPages.length)warnings.unshift(t('pdfColumns',{pages:pdf.columnPages.join(', ')}));
        data={text,detail:t('pdfDetail',{pages:pdf.pages}),chars:text.length,warnings};
      } else {
        const response=await fetch('/api/extract',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-File-Name':encodeURIComponent(file.name),'X-Local-Token':App.token()},body:buffer});
        data=await response.json().catch(()=>({}));
        if(!response.ok)throw new Error(data.error||t('operationFailed'));
      }
      if(seq!==loadSeq)return; // cleared or replaced while loading
      // A new document: results of the previous one must not be saved under the new name.
      if(job&&!['running','planning'].includes(job.status)){try{await App.api('/api/document/clear',{});}catch(_){}}
      if(seq!==loadSeq)return;
      invalidateResult();job=null;renderPairs();
      App.showError('');$('doc-text').value=data.text;fileBase=file.name.replace(/\.[^.]+$/,'');$('save-name').value='';$('save-done').textContent='';updateHash();
      $('file-info').textContent=t('fileLoaded',{name:file.name,kind:data.detail,chars:data.chars.toLocaleString()});
      const list=$('file-warnings');list.replaceChildren();
      for(const w of data.warnings){const li=document.createElement('li');li.textContent=I18N.message(w);list.append(li);}
      const check=document.createElement('li');check.textContent=t('fileCheck');list.append(check);
      list.hidden=false;
    }catch(error){if(seq===loadSeq){$('file-info').textContent='';App.showError(error.message);}}
    finally{if(seq===loadSeq)extracting=false;$('file').value='';render();}
  }
  $('file-pick').onclick=()=>$('file').click();
  $('file').onchange=()=>readFile($('file').files[0]);
  $('drop').onkeydown=event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();$('file').click();}};
  for(const name of ['dragenter','dragover'])$('drop').addEventListener(name,event=>{event.preventDefault();$('drop').classList.add('over');});
  for(const name of ['dragleave','drop'])$('drop').addEventListener(name,()=>$('drop').classList.remove('over'));
  $('drop').addEventListener('drop',event=>{event.preventDefault();const files=event.dataTransfer.files;if(files.length>1)App.showError(t('fileOneAtATime'));readFile(files[0]);});
  // A file dropped elsewhere on the page must not make the browser navigate away.
  window.addEventListener('dragover',event=>event.preventDefault());
  window.addEventListener('drop',event=>event.preventDefault());
  $('doc-text').oninput=()=>{restoreText=false;updateHash();render();};$('chunk').onchange=render;$('save-layout').onchange=render;
  $('doc-start').onclick=startDocument;
  $('glossary').oninput=()=>{const n=parseGlossary().length;$('glossary-note').textContent=t('glossaryHelp')+(n?' '+t('glossaryCount',{n}):'');};
  $('doc-cancel').onclick=async()=>{try{await App.api('/api/document/cancel',{});if(job)job.cancelling=true;render();}catch(error){App.showError(error.message);}};
  $('doc-save').onclick=async()=>{
    const format=$('save-format').value;let name=($('save-name').value.trim()||defaultName()).replace(/[\\/:*?"<>|]+/g,'_');
    if(!name.toLowerCase().endsWith('.'+format))name+='.'+format;
    if(!hasResult()||extracting||App.quitting||(job&&['running','planning'].includes(job.status))||!resultMatchesText()){App.showError(t('docStale'));render();return;}
    try{
      // A part without a translation is marked in the file itself, never left silently empty.
      const items=segments.map(s=>({source:s.source,translation:s.translation||t('exportMissing'),after:s.after}));
      App.saveBlob(await App.apiBlob('/api/export',{format,layout:$('save-layout').value,items,title:t('fileBase')}),name);
      $('save-done').textContent=t('saveDone',{name});
    }catch(error){App.showError(error.message);}
  };
  App.listeners.refresh.push(onRefresh);App.listeners.clear.push(clearAll);
  window.addEventListener('languagechange',()=>{renderPairs();render();});
})();
