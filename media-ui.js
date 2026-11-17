// Shared helpers for the CD / DVD pages (check.html, tag.html): escaping, code normalisation,
// the camera barcode scanner and the CD | DVD tab switcher. Plain script, no build step; exposes window.MediaUI.
(function () {
  'use strict';
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  // cdcheck-api's canonical code: letters/digits only, upper case, a 0-led EAN-13 becomes its UPC-A
  const canon = c => { c = String(c || '').replace(/[^A-Za-z0-9]/g, '').toUpperCase(); return c.length === 13 && /^0\d+$/.test(c) ? c.slice(1) : c; };
  const fmtSize = n => n == null ? '' : n >= 1e9 ? (n / 1e9).toFixed(2) + ' GB' : (n / 1e6).toFixed(0) + ' MB';
  const FILM_KINDS = ['dvd', 'bluray', 'vhs', 'movie'];

  // ---------- camera barcode scanner ----------
  // Native BarcodeDetector (Chrome / Android) or the bundled ZXing fallback (Firefox, iOS).
  let DetPromise = null;
  function detectorClass(status) {
    if (window.BarcodeDetector) return Promise.resolve(window.BarcodeDetector);
    if (!DetPromise) {
      status('Loading barcode decoder…');
      DetPromise = new Promise((ok, bad) => {
        const s = document.createElement('script'); s.src = '/vendor/barcode-detector-3.2.3.js'; s.onload = ok; s.onerror = bad; document.head.appendChild(s);
      }).then(() => {
        BarcodeDetectionAPI.setZXingModuleOverrides({ locateFile: (f, prefix) => f.endsWith('.wasm') ? '/vendor/zxing_reader.wasm' : prefix + f });
        return BarcodeDetectionAPI.BarcodeDetector;
      }).catch(e => { DetPromise = null; throw e; });
    }
    return DetPromise;
  }
  const ALL = new Set();
  // scanner({ video, onCode(code) [async], status(text, cls), onState(running), repeatMs, once })
  //   Camera stays on and keeps reading (unless once); the same code held in view is not re-read within repeatMs;
  //   while onCode is running nothing else is read. stop() releases the camera and the screen wake lock.
  function scanner(opt) {
    const st = opt.status || (() => {});
    let stream = null, timer = null, wake = null, busy = false, last = '', lastAt = 0, starting = false;
    const self = {
      get running() { return !!stream; },
      async start() {
        if (stream || starting) return;
        if (!window.isSecureContext) { st('Camera needs HTTPS (open https://hibou/…).', 'bad'); return; }
        starting = true;
        try {
          let Det;
          try { Det = await detectorClass(st); } catch { st('Could not load barcode decoder. Type the digits instead.', 'bad'); return; }
          let fmts = ['ean_13', 'upc_a', 'ean_8', 'upc_e', 'code_39', 'code_128', 'itf', 'codabar'];
          try { const sup = await Det.getSupportedFormats(); fmts = fmts.filter(f => sup.includes(f)); } catch {}
          const det = new Det({ formats: fmts });
          ALL.forEach(s => s !== self && s.stop());          // one camera at a time on the page
          try { stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' }, audio: false }); }
          catch (e) { st('Camera error: ' + e.message, 'bad'); return; }
          const v = opt.video; v.srcObject = stream; v.style.display = 'block'; await v.play();
          if (opt.scrollIntoView) v.scrollIntoView({ block: 'nearest' });
          try { wake = await navigator.wakeLock?.request('screen'); } catch {}
          opt.onState && opt.onState(true);
          st(opt.prompt || 'Point the camera at the barcode…');
          timer = setInterval(async () => {
            if (busy || !stream) return;
            try {
              const codes = await det.detect(v);
              if (!codes.length) return;
              const raw = codes[0].rawValue, c = canon(raw), now = Date.now();
              if (c === last && now - lastAt < (opt.repeatMs || 4000)) return;
              last = c; lastAt = now;
              if (navigator.vibrate) navigator.vibrate(60);
              if (opt.once) self.stop();
              busy = true;
              try { await opt.onCode(raw, c); } finally { busy = false; lastAt = Date.now(); }
            } catch {}
          }, 250);
        } finally { starting = false; }
      },
      stop() {
        clearInterval(timer); timer = null;
        if (stream) stream.getTracks().forEach(t => t.stop());
        const was = !!stream; stream = null;
        try { wake?.release(); } catch {} wake = null;
        opt.video.style.display = 'none';
        if (was) opt.onState && opt.onState(false);
      },
      forget() { last = ''; lastAt = 0; },             // allow the same code to be read again at once
      prime(code) { last = canon(code); lastAt = Date.now(); },   // treat `code` as just read
    };
    ALL.add(self);
    return self;
  }
  document.addEventListener('visibilitychange', () => { if (document.hidden) ALL.forEach(s => s.stop()); });

  // ---------- CD | DVD tabs ----------
  // tabs({ page, tabs:['cd','dvd'], onShow(tab, prev) }) -> { show(tab), current }
  // Chosen by ?tab= or #cd / #dvd, else the last one used (localStorage), else the first.
  // Inside the hub (?embed=1) it tells the parent which tab is showing, so the hub's address can follow.
  function tabs(opt) {
    const key = opt.page + '.tab', ids = opt.tabs;
    const q = new URLSearchParams(location.search), h = location.hash.slice(1).toLowerCase();
    let saved = null; try { saved = localStorage.getItem(key); } catch {}
    let cur = null;
    const pick = [q.get('tab'), ids.includes(h) ? h : null, saved, ids[0]].map(x => String(x || '').toLowerCase()).find(x => ids.includes(x));
    function show(t, quiet) {
      t = String(t || '').toLowerCase(); if (!ids.includes(t)) return;
      const prev = cur; cur = t;
      document.querySelectorAll('[data-tab]').forEach(b => { const on = b.dataset.tab === t; b.setAttribute('aria-selected', on); b.tabIndex = on ? 0 : -1; });
      document.querySelectorAll('[data-pane]').forEach(p => { p.hidden = p.dataset.pane !== t; });
      try { localStorage.setItem(key, t); } catch {}
      try {
        const u = new URL(location.href); u.searchParams.set('tab', t);
        if (ids.includes(u.hash.slice(1).toLowerCase())) u.hash = '';
        history.replaceState(null, '', u.pathname + u.search + u.hash);
      } catch {}
      try { if (parent !== window) parent.postMessage({ type: 'media-tab', page: opt.page, tab: t }, location.origin); } catch {}
      if (prev !== t && opt.onShow) opt.onShow(t, prev, quiet);
    }
    document.querySelectorAll('[data-tab]').forEach(b => b.addEventListener('click', () => show(b.dataset.tab)));
    document.querySelector('[role=tablist]')?.addEventListener('keydown', e => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      const i = ids.indexOf(cur), n = ids[(i + (e.key === 'ArrowRight' ? 1 : ids.length - 1)) % ids.length];
      show(n); document.querySelector(`[data-tab="${n}"]`)?.focus();
    });
    addEventListener('hashchange', () => { const x = location.hash.slice(1).toLowerCase(); if (ids.includes(x)) show(x); });
    addEventListener('message', e => {           // the hub selects a tab of an already loaded page
      if (e.origin === location.origin && e.data && e.data.type === 'media-set-tab' && ids.includes(e.data.tab)) show(e.data.tab);
    });
    show(pick, true);
    return { show, get current() { return cur; } };
  }

  window.MediaUI = { esc, canon, fmtSize, FILM_KINDS, scanner, tabs };
})();
