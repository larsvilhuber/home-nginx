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

  // ---------- linking (cdcheck-api /video/library, /items/<code>/link-film, /items/<code>/link-shelf) ----------
  const API = '/cdcheck-api/';
  const SHELF_BASE = 'https://hibou:18443';
  async function api(path, method = 'GET', body) {
    const r = await fetch(API + path, { method, cache: 'no-store', headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
    const j = await r.json().catch(() => ({}));
    return { ok: r.ok, status: r.status, j };
  }
  (function css() {
    if (document.getElementById('mui-link-css')) return;
    const st = document.createElement('style'); st.id = 'mui-link-css';
    st.textContent = `.mui-box{margin-top:8px}.mui-box .mui-row{display:flex;gap:8px;margin:8px 0;align-items:center;flex-wrap:wrap}
.mui-box .mui-row input[type=text],.mui-box .mui-row input[type=search]{flex:1;min-width:0}
.mui-film{display:flex;gap:8px;align-items:center;padding:6px 0;border-top:1px solid #8884}.mui-film:first-child{border-top:0}
.mui-film>div{flex:1;min-width:0}.mui-note{color:var(--warn,#9a6700);font-size:.85rem}.mui-ok{color:var(--ok,#1b7f3b)}.mui-bad{color:var(--no,#b3261e)}
.mui-check{display:flex;gap:6px;align-items:flex-start;font-size:.9rem;margin:6px 0}.mui-check input{margin-top:3px}
.mui-box video{width:100%;max-height:36vh;background:#000;border-radius:10px;display:none;object-fit:cover}`;
    document.head.appendChild(st);
  })();
  const filmLabel = f => f.key + (f.version ? ' [' + f.version + ']' : '');

  // filmLink(host, { code, title, asMore, replace, onDone(result) }): search the film library (movies.tsv) and link
  // the record `code` to the film picked. Films already linked to another code are shown with a note (still allowed).
  function filmLink(host, opt) {
    const q0 = String(opt.title || '').replace(/\s*\(\d{4}\)\s*$/, '');
    host.innerHTML = `<div class="mui-box"><form class="mui-row"><input type="search" placeholder="search the film library (title, year)" value="${esc(q0)}"><button class="sec sm">Search</button></form><div class="mui-res"></div></div>`;
    const form = host.querySelector('form'), inp = host.querySelector('input'), out = host.querySelector('.mui-res');
    let films = [];
    async function go() {
      const q = inp.value.trim(); if (!q) return;
      out.innerHTML = '<div class="m">Searching…</div>';
      const r = await api('video/library?q=' + encodeURIComponent(q)).catch(e => ({ ok: false, j: { error: e.message } }));
      if (!r.ok) { out.innerHTML = `<div class="mui-bad">${esc(r.j.error || 'HTTP ' + r.status)}</div>`; return; }
      films = (r.j.films || []).slice(0, 30);
      out.innerHTML = films.length ? films.map((f, i) => {
        const others = (f.codes || []).filter(c => c !== opt.code);
        const mine = (f.codes || []).includes(opt.code);
        return `<div class="mui-film"><div><div class="t">${esc(f.title)}${f.year ? ' (' + esc(f.year) + ')' : ''}${f.version ? ' <span class="m">[' + esc(f.version) + ']</span>' : ''}</div>
          <div class="m">${esc(f.path)}${f.jellyfin_id ? '' : ' · not in Jellyfin yet'}</div>
          ${others.length ? `<div class="mui-note">already linked to ${others.map(esc).join(', ')} (another copy? linking is still allowed)</div>` : ''}
          ${mine ? '<div class="mui-ok m">this code already points at it</div>' : ''}</div>
          <button class="sm" type="button" data-i="${i}">Link</button></div>`;
      }).join('') + (r.j.count > films.length ? `<div class="m">${r.j.count - films.length} more: type more words.</div>` : '')
        : '<div class="m">Nothing in the film library matches. Is it ripped? Then import it with Tag (DVD).</div>';
    }
    form.onsubmit = e => { e.preventDefault(); go(); };
    out.addEventListener('click', async e => {
      const b = e.target.closest('button[data-i]'); if (!b) return;
      const f = films[+b.dataset.i];
      b.disabled = true;
      const body = { jellyfin_title: f.key, version: f.version };
      if (opt.asMore) body.as_more = true;
      if (opt.replace) body.replace = true;
      const r = await api('items/' + encodeURIComponent(opt.code) + '/link-film', 'POST', body).catch(e => ({ ok: false, j: { error: e.message } }));
      if (!r.ok) { b.disabled = false; out.insertAdjacentHTML('afterbegin', `<div class="mui-bad">${esc(r.j.error || 'HTTP ' + r.status)}</div>`); return; }
      out.innerHTML = `<div class="mui-ok">Linked ${esc(opt.code)} to ${esc(filmLabel(f))}.${r.j.note ? ' ' + esc(r.j.note) : ''}</div>` +
        (r.j.jellyfin_note ? `<div class="mui-note">${esc(r.j.jellyfin_note)}</div>` : '') +
        (r.j.shelf && r.j.shelf !== 'unchanged' ? `<div class="m">Shelf: ${esc(r.j.shelf)}</div>` : '');
      opt.onDone && opt.onDone(r.j);
    });
    if (q0) go(); else inp.focus();
  }
  async function unlinkFilm(code, index) {
    const r = await api('items/' + encodeURIComponent(code) + '/link-film' + (index ? '?index=' + index : ''), 'DELETE');
    if (!r.ok) throw new Error(r.j.error || 'HTTP ' + r.status);
    return r.j;
  }

  // libraryHint(host, { tmdb_id, titles:[...], year, code? }) -> Promise<getter>; getter() = {jellyfin_title, version} or
  // null. Shows "This film is already in your library: … — link this disc to it" (checked) when movies.tsv has it.
  async function libraryHint(host, opt) {
    host.innerHTML = '';
    const p = new URLSearchParams();
    if (opt.tmdb_id) p.set('tmdb_id', opt.tmdb_id);
    (opt.titles || []).filter(Boolean).forEach(t => p.append('title', t));
    if (opt.year) p.set('year', opt.year);
    if (![...p.keys()].length) return () => null;
    const r = await api('video/library?' + p.toString()).catch(() => ({ ok: false, j: {} }));
    const films = r.ok ? (r.j.films || []) : [];
    if (!films.length) return () => null;
    const others = [...new Set(films.flatMap(f => f.codes || []))];
    host.innerHTML = `<label class="mui-check"><input type="checkbox" checked><span>This film is already in your library: <b>${esc(films[0].key)}</b>${r.j.match === 'title' ? ' <span class="m">(same title and year)</span>' : ''} — link this disc to it
      ${films.length > 1 ? `<select>${films.map((f, i) => `<option value="${i}">${esc(f.version || 'no version label')} · ${esc(f.path)}</option>`).join('')}</select>` : ''}
      ${others.length ? `<span class="mui-note"> (already linked to ${others.map(esc).join(', ')})</span>` : ''}</span></label>`;
    const cb = host.querySelector('input'), sel = host.querySelector('select');
    return () => { if (!cb.checked) return null; const f = films[sel ? +sel.value : 0]; return { jellyfin_title: f.key, version: f.version }; };
  }
  async function linkFilmAfterSave(code, want) {
    if (!want) return null;
    const r = await api('items/' + encodeURIComponent(code) + '/link-film', 'POST', want).catch(e => ({ ok: false, j: { error: e.message } }));
    return r.ok ? { ok: true, ...r.j } : { ok: false, error: r.j.error || 'HTTP ' + r.status };
  }

  // shelfLine(host, code, shelf_id): "Shelf: <location> · item #N ↗" read from Shelf (GET /shelf/item).
  async function shelfLine(host, code, sid) {
    const link = id => `<a target="_blank" rel="noopener" href="${SHELF_BASE}/item/${encodeURIComponent(id)}">item #${esc(id)} ↗</a>`;
    host.innerHTML = `<span class="m">Shelf: ${sid != null ? link(sid) : '…'}</span>`;
    const r = await api('shelf/item?code=' + encodeURIComponent(code)).catch(() => ({ ok: false, j: {} }));
    if (r.ok) host.innerHTML = `<span class="m">Shelf: ${r.j.location ? '<b>' + esc(r.j.location) + '</b> · ' : ''}${link(r.j.shelf_id)}</span>`;
    else if (sid != null) host.innerHTML = `<span class="m">Shelf: ${link(sid)} <span class="mui-note">(${esc(r.j.error || 'Shelf did not answer')})</span></span>`;
  }

  // shelfLink(host, { code, candidate, onDone(result) }): link the record `code` to a Shelf item that already exists.
  // Input: Shelf copy code (CV-000123) or the item's barcode, a Shelf item address, or the item number; scanning allowed.
  function shelfLink(host, opt) {
    host.innerHTML = `<div class="mui-box"><div class="m">Link to a Shelf item that already exists: scan or type its CV- label (or its barcode), paste its Shelf address, or type the item number.</div>
      <form class="mui-row"><input type="text" autocomplete="off" autocapitalize="characters" placeholder="CV-000123, https://hibou:18443/item/123 or 123" value="${esc(opt.candidate ?? '')}">
      <button class="sec sm" type="button" data-scan>Scan</button><button class="sm">Find</button></form><video playsinline muted></video><div class="mui-out"></div></div>`;
    const form = host.querySelector('form'), inp = host.querySelector('input'), out = host.querySelector('.mui-out'), sbtn = host.querySelector('[data-scan]');
    const cam = scanner({ video: host.querySelector('video'), once: true, status: (t, c) => { out.innerHTML = `<div class="${c === 'bad' ? 'mui-bad' : 'm'}">${esc(t)}</div>`; },
      onState: on => { sbtn.textContent = on ? 'Stop' : 'Scan'; }, onCode: (raw) => { inp.value = String(raw).trim(); find(); } });
    sbtn.onclick = () => cam.running ? cam.stop() : (cam.forget(), cam.start());
    let target = null;
    function parse(v) {
      v = String(v || '').trim();
      const m = v.match(/\/(?:music\/)?item\/(\d+)/);
      if (m) return { shelf_id: +m[1] };
      if (/^\d{1,7}$/.test(v)) return { shelf_id: +v };
      if (/^https?:/.test(v)) return null;
      return v ? { shelf_code: v } : null;
    }
    async function find() {
      const t = parse(inp.value);
      if (!t) { out.innerHTML = '<div class="mui-bad">Give a CV- code, a barcode, a Shelf item address or an item number.</div>'; return; }
      out.innerHTML = '<div class="m">Asking Shelf…</div>';
      const r = await api('shelf/item?' + (t.shelf_id != null ? 'id=' + t.shelf_id : 'code=' + encodeURIComponent(t.shelf_code))).catch(e => ({ ok: false, j: { error: e.message } }));
      if (!r.ok) { out.innerHTML = `<div class="mui-bad">${r.status === 404 ? 'Shelf has no such item.' : esc(r.j.error || 'HTTP ' + r.status)}</div>`; return; }
      const it = r.j; target = { shelf_id: it.shelf_id };
      const l = await api('items?shelf_id=' + it.shelf_id).catch(() => ({ ok: false, j: [] }));
      const others = (Array.isArray(l.j) ? l.j : []).map(x => x.code).filter(c => c !== opt.code);
      out.innerHTML = `<div class="card" style="margin:6px 0"><div><b>Shelf item #${esc(it.shelf_id)}</b>: ${esc(it.title)}${it.creator ? ' — ' + esc(it.creator) : ''}
          <span class="m">(${esc(it.media_type || it.kind || '')}${it.year ? ', ' + esc(it.year) : ''})</span> <a target="_blank" rel="noopener" href="${esc(it.url)}">open ↗</a></div>
        <div class="m">${it.location ? 'at <b>' + esc(it.location) + '</b>' : 'no location'}${it.copies ? ' · ' + esc(it.copies) + ' cop' + (it.copies > 1 ? 'ies' : 'y') : ''}</div>
        ${others.length ? `<label class="mui-check"><input type="checkbox" data-shared><span>Shelf item already linked to <b>${others.map(esc).join(', ')}</b>. Link this code too (another disc of the same set).</span></label>` : ''}
        <div class="mui-row"><button class="sm" type="button" data-link>Link ${esc(opt.code)} to this item</button></div><div data-res></div></div>`;
      const lb = out.querySelector('[data-link]'), sh = out.querySelector('[data-shared]');
      if (sh) { lb.disabled = true; sh.onchange = () => { lb.disabled = !sh.checked; }; }
      lb.onclick = () => link(false, lb);
    }
    async function link(replaceUpc, btn) {
      const res = out.querySelector('[data-res]'), sh = out.querySelector('[data-shared]');
      btn.disabled = true; res.innerHTML = '<div class="m">Linking in Shelf… (a few seconds)</div>';
      const body = { ...target, place_code: 'auto' };
      if (sh && sh.checked) body.allow_shared = true;
      if (replaceUpc) body.replace_upc = true;
      const r = await api('items/' + encodeURIComponent(opt.code) + '/link-shelf', 'POST', body).catch(e => ({ ok: false, j: { error: e.message } }));
      if (!r.ok) { btn.disabled = false; res.innerHTML = `<div class="mui-bad">${esc(r.j.error || 'HTTP ' + r.status)}</div>`; return; }
      const j = r.j, pl = j.placed || {};
      res.innerHTML = `<div class="${pl.placed ? 'mui-ok' : 'mui-note'}">Linked to Shelf item #${esc(j.shelf_id)}. ${esc(pl.how || '')}${j.dropped ? ' · pending Shelf "' + esc(j.dropped) + '" cancelled' : ''}</div>
        <div class="m">Scan check in Shelf: ${j.scan && j.scan.ok ? '<span class="mui-ok">' + esc(opt.code) + ' finds this item</span>' : '<span class="mui-note">' + esc(opt.code) + ' does not find this item yet</span>'}${j.refresh && j.refresh.ok ? ' · notes line written' : j.refresh ? ' · notes: ' + esc(j.refresh.error) : ''}</div>
        ${pl.can_replace_upc ? `<div class="mui-row"><button class="sm sec" type="button" data-rep>Replace the Shelf item's UPC (${esc(pl.upc_differs)}) with ${esc(opt.code)}</button></div>` : ''}`;
      const rep = res.querySelector('[data-rep]');
      if (rep) rep.onclick = () => link(true, rep);
      opt.onDone && opt.onDone(j);
    }
    form.onsubmit = e => { e.preventDefault(); find(); };
    if (opt.candidate != null && String(opt.candidate) !== '') find();
    return { stop: () => cam.stop() };
  }

  window.MediaUI = { esc, canon, fmtSize, FILM_KINDS, scanner, tabs, api, filmLink, unlinkFilm, libraryHint, linkFilmAfterSave, shelfLine, shelfLink, SHELF_BASE };
})();
