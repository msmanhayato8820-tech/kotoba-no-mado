'use strict';
// Voice input through the browser's speech recognition (Chrome). Chrome's
// recognition sends audio to Google's servers, which is not local processing.
// It never starts until the person agrees in this page; agreement is not saved.
(() => {
  const $ = id => document.getElementById(id);
  const t = (...args) => I18N.t(...args);
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) return; // button stays hidden in browsers without it
  let consented = false, recognition = null, listening = false;
  const LOCALES = {ja:'ja-JP', en:'en-US', 'zh-hans':'zh-CN', 'zh-hant':'zh-TW', ko:'ko-KR', fr:'fr-FR', de:'de-DE', es:'es-ES', it:'it-IT', ru:'ru-RU', vi:'vi-VN', th:'th-TH', id:'id-ID', ms:'ms-MY', tl:'fil-PH', hi:'hi-IN', ar:'ar-SA', pt:'pt-BR', nl:'nl-NL', pl:'pl-PL'};
  const button = $('voice'), status = $('voice-status'), dialog = $('voice-consent');
  button.hidden = false;

  function setStatus(key, values) { status.hidden = !key; status.textContent = key ? t(key, values) : ''; }
  function stop() { if (recognition) { try { recognition.abort(); } catch (_) {} } recognition = null; listening = false; button.textContent = t('voice'); }
  function start() {
    const picked = App.selectedPair().source;
    const lang = picked === 'auto' ? I18N.locale : picked;
    recognition = new Recognition();
    recognition.lang = LOCALES[lang] || lang;
    recognition.interimResults = false; recognition.continuous = true;
    recognition.onresult = event => {
      let text = '';
      for (let i = event.resultIndex; i < event.results.length; i++) if (event.results[i].isFinal) text += event.results[i][0].transcript;
      if (!text) return;
      const box = $('source'); box.value = (box.value + (box.value && !/\s$/.test(box.value) ? ' ' : '') + text).slice(0, 2000); box.oninput();
    };
    recognition.onerror = event => {
      const key = event.error === 'not-allowed' || event.error === 'service-not-allowed' ? 'voiceDenied' : event.error === 'network' ? 'voiceNetwork' : event.error === 'no-speech' ? 'voiceNoSpeech' : 'voiceError';
      stop(); setStatus(key);
    };
    recognition.onend = () => { if (listening) { stop(); setStatus('voiceStopped'); } };
    listening = true; button.textContent = t('voiceStop');
    setStatus('voiceListening', {language: App.languageLabel(lang)});
    recognition.start(); // Chrome asks for microphone permission here.
  }
  button.onclick = () => {
    if (listening) { stop(); setStatus('voiceStopped'); return; }
    if (consented) { start(); return; }
    $('voice-agree').checked = false; $('voice-start').disabled = true; dialog.showModal();
  };
  $('voice-agree').onchange = () => { $('voice-start').disabled = !$('voice-agree').checked; };
  $('voice-cancel').onclick = () => dialog.close();
  $('voice-start').onclick = () => { if (!$('voice-agree').checked) return; consented = true; dialog.close(); start(); };
  App.listeners.clear.push(() => { stop(); setStatus(''); });
  window.addEventListener('languagechange', () => { if (!listening) button.textContent = t('voice'); });
  window.addEventListener('pagehide', stop);
})();
