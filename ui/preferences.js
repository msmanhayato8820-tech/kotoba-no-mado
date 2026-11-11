'use strict';

// Display preferences only. Translation text and model settings never enter storage.
(() => {
  const THEME_KEY = 'kotoba-window.theme';
  const SIZE_KEY = 'kotoba-window.text-size';
  const THEMES = ['system', 'light', 'dark'];
  const SCALES = {standard: 1, large: 1.2, xlarge: 1.4};

  function readChoice(key, choices, fallback) {
    try {
      const stored = window.localStorage.getItem(key);
      return choices.includes(stored) ? stored : fallback;
    } catch (_) {
      return fallback; // Private browsing or blocked storage must not break the app.
    }
  }

  function saveChoice(key, value) {
    try { window.localStorage.setItem(key, value); }
    catch (_) { /* The selection still works for the current page. */ }
  }

  function initialize() {
    const root = document.documentElement;
    const themeSelect = document.getElementById('theme');
    const sizeSelect = document.getElementById('text-size');
    let theme = readChoice(THEME_KEY, THEMES, 'system');
    let size = readChoice(SIZE_KEY, Object.keys(SCALES), 'standard');
    let systemTheme = null;
    try {
      if (typeof window.matchMedia === 'function') {
        systemTheme = window.matchMedia('(prefers-color-scheme: dark)');
      }
    } catch (_) { /* Use light when a system preference is unavailable. */ }

    function applyTheme() {
      root.dataset.theme = theme === 'system'
        ? (systemTheme && systemTheme.matches ? 'dark' : 'light')
        : theme;
      if (themeSelect) themeSelect.value = theme;
    }

    function applySize() {
      root.dataset.textSize = size;
      root.style.setProperty('--ui-scale', String(SCALES[size]));
      if (sizeSelect) sizeSelect.value = size;
    }

    applyTheme();
    applySize();

    if (themeSelect) {
      themeSelect.addEventListener('change', () => {
        if (!THEMES.includes(themeSelect.value)) {
          applyTheme();
          return;
        }
        theme = themeSelect.value;
        applyTheme();
        saveChoice(THEME_KEY, theme);
      });
    }
    if (sizeSelect) {
      sizeSelect.addEventListener('change', () => {
        if (!Object.prototype.hasOwnProperty.call(SCALES, sizeSelect.value)) {
          applySize();
          return;
        }
        size = sizeSelect.value;
        applySize();
        saveChoice(SIZE_KEY, size);
      });
    }

    if (systemTheme) {
      const followSystem = () => { if (theme === 'system') applyTheme(); };
      if (typeof systemTheme.addEventListener === 'function') {
        systemTheme.addEventListener('change', followSystem);
      } else if (typeof systemTheme.addListener === 'function') {
        systemTheme.addListener(followSystem); // Older WebKit.
      }
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initialize, {once: true});
  } else {
    initialize();
  }
})();
