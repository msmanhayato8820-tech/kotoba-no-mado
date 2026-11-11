'use strict';
// Reads text from PDFs inside this browser page with pdf.js (Mozilla, Apache-2.0).
// The PDF never leaves the page: only the extracted text is placed in the text box.
// No OCR: pages without embedded text are reported, not guessed.
(() => {
  const BASE = '/vendor/pdfjs/';
  let library = null;
  async function load() {
    if (!library) {
      library = await import(BASE + 'build/pdf.min.mjs');
      library.GlobalWorkerOptions.workerSrc = BASE + 'build/pdf.worker.min.mjs';
    }
    return library;
  }
  const CJK = /[　-ヿ㐀-鿿豈-﫿＀-￯가-힯]/;
  const SENTENCE_END = /[。．！？!?.:：」』）)]$/;

  function isCjk(ch) { return CJK.test(ch || ''); }
  function joinPieces(left, right) {
    if (!left) return right;
    if (!right) return left;
    if (/\s$/.test(left) || /^\s/.test(right)) return left + right;
    if (isCjk(left.slice(-1)) || isCjk(right[0])) return left + right;
    if (/[A-Za-z]-$/.test(left) && /^[a-z]/.test(right)) return left.slice(0, -1) + right; // hyphenated word
    return left + ' ' + right;
  }

  // Group text items into lines (same baseline), keeping the PDF's own order.
  function toLines(items) {
    const lines = [];
    let line = null;
    for (const item of items) {
      if (typeof item.str !== 'string') continue;
      const x = item.transform[4], y = item.transform[5];
      const h = Math.abs(item.transform[3]) || item.height || 10;
      const sameLine = line && Math.abs(line.y - y) < h * 0.5 && x >= line.right - h * 0.5;
      if (!sameLine) {
        if (line && line.text.trim()) lines.push(line);
        line = {text: '', x, y, right: x, h, chars: 0};
      } else if (line.text && x - line.right > h * 0.25 && !/\s$/.test(line.text) && !/^\s/.test(item.str) && !(isCjk(line.text.slice(-1)) && isCjk(item.str[0]))) {
        line.text += ' ';
      }
      line.text += item.str;
      line.right = Math.max(line.right, x + (item.width || 0));
      if (item.hasEOL) { if (line.text.trim()) lines.push(line); line = null; }
    }
    if (line && line.text.trim()) lines.push(line);
    return lines;
  }

  // Join lines into paragraphs: a large vertical gap, or a short line ending a sentence, ends a paragraph.
  function toParagraphs(lines) {
    if (!lines.length) return '';
    const gaps = [];
    for (let i = 1; i < lines.length; i++) { const g = lines[i - 1].y - lines[i].y; if (g > 0) gaps.push(g); }
    gaps.sort((a, b) => a - b);
    const normalGap = gaps.length ? gaps[Math.floor(gaps.length / 2)] : 0;
    const widths = lines.map(l => l.right - l.x).sort((a, b) => a - b);
    const typicalWidth = widths[Math.floor(widths.length * 0.8)] || 0;
    const paragraphs = [];
    let current = '';
    lines.forEach((line, i) => {
      const text = line.text.replace(/\s+/g, ' ').trim();
      current = current ? joinPieces(current, text) : text;
      const next = lines[i + 1];
      const gap = next ? line.y - next.y : 0;
      const endsShort = (line.right - line.x) < typicalWidth * 0.75 && SENTENCE_END.test(text);
      const jump = !next || gap < 0 || (normalGap && gap > normalGap * 1.6) || next.x < line.x - line.h * 4;
      if (jump || endsShort) { paragraphs.push(current); current = ''; }
    });
    if (current) paragraphs.push(current);
    return paragraphs.join('\n\n');
  }

  // Two columns: many lines entirely left of the middle and many entirely right of it.
  function looksLikeColumns(lines, width) {
    if (lines.length < 12 || !width) return false;
    const mid = width / 2;
    let left = 0, right = 0, across = 0;
    for (const l of lines) { if (l.right < mid + 4) left++; else if (l.x > mid - 4) right++; else across++; }
    return left > lines.length * 0.25 && right > lines.length * 0.25 && across < lines.length * 0.2;
  }

  async function extract(buffer, onProgress) {
    const pdfjs = await load();
    let task;
    try {
      task = pdfjs.getDocument({data: new Uint8Array(buffer), cMapUrl: BASE + 'cmaps/', cMapPacked: true, standardFontDataUrl: BASE + 'standard_fonts/',
        isEvalSupported: false, disableFontFace: true, useSystemFonts: false, enableXfa: false,
        disableAutoFetch: true, disableStream: true, stopAtErrors: false});
      const doc = await task.promise;
      const pages = [], emptyPages = [], columnPages = [];
      for (let n = 1; n <= doc.numPages; n++) {
        const page = await doc.getPage(n);
        const content = await page.getTextContent({includeMarkedContent: false, disableNormalization: false});
        const lines = toLines(content.items);
        const text = toParagraphs(lines);
        if (text.replace(/\s/g, '').length < 5) emptyPages.push(n);
        if (looksLikeColumns(lines, page.view[2] - page.view[0])) columnPages.push(n);
        pages.push(text);
        page.cleanup();
        if (onProgress) onProgress(n, doc.numPages);
      }
      const total = doc.numPages;
      await task.destroy(); // pdf.js 6: the loading task releases the document and worker data
      return {text: pages.filter(p => p.trim()).join('\n\n'), pages: total, emptyPages, columnPages};
    } catch (error) {
      if (task) { try { await task.destroy(); } catch (_) {} }
      const name = error && error.name;
      if (name === 'PasswordException') throw new Error(I18N.t('pdfPassword'));
      if (name === 'InvalidPDFException') throw new Error(I18N.t('pdfInvalid'));
      throw new Error(I18N.t('pdfFailed'));
    }
  }
  window.PdfReader = {extract, _test: {toLines, toParagraphs, joinPieces, looksLikeColumns}};
})();
