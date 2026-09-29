// Injected into every page of the recording browser.
// Captures the user's clicks and typing as actions with several locator
// candidates each, and shows a toolbar with the current Excel step.
(() => {
  if (window.__recInstalled) return;
  window.__recInstalled = true;

  const HOST_ID = '__rec_toolbar_host';
  const FIELD_ROLES = ['textbox', 'combobox', 'checkbox', 'radio'];
  const INTERACTIVE = 'button,a[href],input,select,textarea,summary,[role=button],[role=link],' +
    '[role=tab],[role=menuitem],[role=menuitemcheckbox],[role=menuitemradio],[role=checkbox],' +
    '[role=option],[role=treeitem],[role=switch],[onclick]';
  const IS_TOP = window.top === window;
  const lastSent = new WeakMap();
  const dirty = new WeakSet(); // rich-text editors typed into since last recorded
  let checkMode = false;
  let hoverEl = null;
  let state = null;
  let collapsed = false;

  // Icon-font glyphs (private use area) are not part of a control's real name.
  const clean = s => (s || '').replace(/[\uE000-\uF8FF]/g, '').replace(/\s+/g, ' ').trim();
  const cssAttr = s => s.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  // Ids containing digits (splitButton-r44, MSG_efc71ad5961) are usually regenerated on every load.
  const stableId = id => id && !/\d/.test(id);
  const fromToolbar = e => e.composedPath().some(n => n && n.id === HOST_ID);
  const cssEsc = s => (window.CSS && CSS.escape) ? CSS.escape(s) : s;

  function roleOf(el) {
    const r = el.getAttribute('role');
    if (r) return r;
    if (el.isContentEditable) return 'textbox';
    const t = el.tagName.toLowerCase();
    if (t === 'button') return 'button';
    if (t === 'a' && el.hasAttribute('href')) return 'link';
    if (t === 'textarea') return 'textbox';
    if (t === 'select') return 'combobox';
    if (t === 'input') {
      const ty = (el.type || 'text').toLowerCase();
      if (['submit', 'button', 'reset'].includes(ty)) return 'button';
      if (ty === 'checkbox' || ty === 'radio') return ty;
      return 'textbox';
    }
    return null;
  }
  const nameOf = el => clean(el.getAttribute('aria-label') || el.innerText || el.value || el.getAttribute('title'));
  function labelOf(el) {
    if (el.labels && el.labels.length) return clean(el.labels[0].innerText);
    const by = el.getAttribute('aria-labelledby');
    const l = by && document.getElementById(by);
    return l ? clean(l.innerText) : clean(el.getAttribute('aria-label'));
  }
  function cssPath(el) {
    if (stableId(el.id)) return '#' + cssEsc(el.id);
    const parts = [];
    while (el && el.nodeType === 1 && parts.length < 5) {
      if (stableId(el.id)) { parts.unshift('#' + cssEsc(el.id)); break; }
      let sel = el.tagName.toLowerCase();
      const name = el.getAttribute('name');
      if (name) sel += `[name="${name}"]`;
      else if (el.parentElement) {
        const same = Array.from(el.parentElement.children).filter(x => x.tagName === el.tagName);
        if (same.length > 1) sel += `:nth-of-type(${same.indexOf(el) + 1})`;
      }
      parts.unshift(sel);
      el = el.parentElement;
    }
    return parts.join(' > ');
  }
  // Most stable first: test id, label / accessible name, visible text, CSS path.
  function locators(el) {
    const out = [];
    const tid = el.getAttribute('data-testid');
    if (tid) out.push({ kind: 'testid', value: tid });
    const role = roleOf(el);
    if (FIELD_ROLES.includes(role)) {
      const lab = labelOf(el);
      if (lab) out.push({ kind: 'label', value: lab });
      const ph = clean(el.getAttribute('placeholder'));
      if (ph) out.push({ kind: 'placeholder', value: ph });
    } else {
      const n = nameOf(el);
      if (role && n && n.length <= 80) out.push({ kind: 'role', role, value: n });
      const tx = clean(el.innerText);
      if (tx && tx.length <= 80) out.push({ kind: 'text', value: tx });
    }
    const aria = clean(el.getAttribute('aria-label'));
    if (aria) out.push({ kind: 'css', value: `${el.tagName.toLowerCase()}[aria-label="${cssAttr(aria)}"]` });
    out.push({ kind: 'css', value: cssPath(el) });
    return out;
  }
  function describe(el) {
    const role = roleOf(el) || el.tagName.toLowerCase();
    const n = FIELD_ROLES.includes(role)
      ? (labelOf(el) || el.getAttribute('placeholder') || el.name) : nameOf(el);
    const kind = role === 'textbox' ? 'field' : role;
    return `${kind} "${clean(n).slice(0, 60)}"`;
  }

  function send(action) {
    if (!window.__recAction) return;
    window.__recAction(action).then(s => { if (s) { state = s; render(); } }).catch(() => {});
  }
  // Rich-text editors (e.g. Outlook's To and message body) are contenteditable
  // elements, not form fields: find the editable root and read its text.
  const editableRoot = el => {
    let cur = el && el.isContentEditable ? el : null;
    while (cur && cur.parentElement && cur.parentElement.isContentEditable) cur = cur.parentElement;
    return cur;
  };
  const valueOf = el => el.isContentEditable ? el.innerText.replace(/\n+$/, '') : el.value;

  function recordFill(el) {
    const value = valueOf(el);
    dirty.delete(el);
    if (lastSent.get(el) === value) return;
    lastSent.set(el, value);
    send({ type: 'fill', value, masked: el.type === 'password',
           target: describe(el), locators: locators(el) });
  }

  // ---- pending typing -------------------------------------------------------
  // Typing is normally saved when focus leaves the field. That is not enough:
  // - buttons such as Outlook's Send can keep focus in the editor, so focus never leaves;
  // - rich editors can insert keystrokes themselves, so no 'input' event fires.
  // So typing is detected from key presses and paste/cut too, and any pending
  // typing is saved right before a click, key press, check or toolbar action.
  const isTextField = el => el && (el.tagName === 'TEXTAREA' ||
    (el.tagName === 'INPUT' && !['checkbox', 'radio', 'button', 'submit', 'reset', 'file', 'image'].includes(el.type)));
  const fieldOf = el => editableRoot(el) || (isTextField(el) ? el : null);
  let focusedField = null;

  function markTyped(e) {
    if (fromToolbar(e)) return;
    const f = fieldOf(e.target);
    if (f) dirty.add(f);
  }
  function flushTyping() {
    if (focusedField && dirty.has(focusedField) && focusedField.isConnected) recordFill(focusedField);
  }
  window.__recFlushTyping = flushTyping;

  document.addEventListener('focusin', e => { if (!fromToolbar(e)) focusedField = fieldOf(e.target) || focusedField; }, true);
  document.addEventListener('keydown', e => {
    if (e.key.length === 1 || e.key === 'Backspace' || e.key === 'Delete') markTyped(e);
  }, true);
  ['input', 'paste', 'cut', 'drop'].forEach(t => document.addEventListener(t, markTyped, true));
  document.addEventListener('mousedown', e => { if (!fromToolbar(e)) { flushScroll(); flushTyping(); } }, true);
  window.addEventListener('pagehide', () => { flushScroll(); flushTyping(); }, true);

  // ---- scrolling ------------------------------------------------------------
  // Replay scrolls to elements by itself before clicking, but some lists only
  // load more items after the user scrolls. So the final position of each
  // scroll (page or panel) is recorded once the user stops scrolling.
  let scrollTimer = null, scrollEl = null;
  const isPageScroller = el => el === document.scrollingElement || el === document.documentElement || el === document.body;
  function flushScroll() {
    if (!scrollEl) return;
    clearTimeout(scrollTimer);
    const el = scrollEl;
    scrollEl = null;
    if (!el.isConnected) return;
    const page = isPageScroller(el);
    // Name a scroll area by its label or id; its text is usually the whole list.
    const label = clean(el.getAttribute('aria-label'));
    const target = page ? 'page' : label ? `area "${label.slice(0, 60)}"`
      : `area ${stableId(el.id) ? '#' + el.id : el.tagName.toLowerCase()}`;
    send({ type: 'scroll', page, x: Math.round(el.scrollLeft), y: Math.round(el.scrollTop),
           target, locators: page ? [] : locators(el) });
  }
  // Only scrolls the user makes count. Pages also scroll by themselves (an element
  // brought into view, a panel reset when it closes); those are not recorded.
  let userScrollAt = 0;
  const SCROLL_KEYS = ['PageUp', 'PageDown', 'Home', 'End', 'ArrowUp', 'ArrowDown', ' '];
  const userScrolls = () => { userScrollAt = Date.now(); };
  window.addEventListener('wheel', e => { if (!fromToolbar(e)) userScrolls(); }, { capture: true, passive: true });
  window.addEventListener('touchmove', e => { if (!fromToolbar(e)) userScrolls(); }, { capture: true, passive: true });
  document.addEventListener('keydown', e => {
    if (!fromToolbar(e) && SCROLL_KEYS.includes(e.key) && !fieldOf(e.target)) userScrolls();
  }, true);
  document.addEventListener('mousedown', e => {
    // A press on a scrollbar lands outside the element's content box.
    const el = e.target;
    if (!el || el.nodeType !== 1 || fromToolbar(e)) return;
    const scrollable = el.scrollHeight > el.clientHeight || el.scrollWidth > el.clientWidth;
    if (scrollable && el.clientWidth > 0 && (e.offsetX > el.clientWidth || e.offsetY > el.clientHeight)) userScrolls();
  }, true);

  document.addEventListener('scroll', e => {
    if (fromToolbar(e)) return;
    const el = e.target === document ? (document.scrollingElement || document.documentElement) : e.target;
    if (!el || el.nodeType !== 1) return;
    if (el !== scrollEl && Date.now() - userScrollAt > 500) return; // not caused by the user
    if (scrollEl && scrollEl !== el) flushScroll();
    scrollEl = el;
    clearTimeout(scrollTimer);
    scrollTimer = setTimeout(flushScroll, 400);
  }, true);

  const flushPending = () => { flushScroll(); flushTyping(); };

  // Many apps (Oracle Fusion menus, tiles, panels) make plain elements clickable
  // with scripts instead of using buttons or links. Such an element shows the
  // pointer cursor, or has tabindex / aria-expanded. Take the largest element
  // that still looks like one control, so its text identifies it.
  function clickableOf(target) {
    const std = target.closest(INTERACTIVE);
    if (std) return std;
    let el = null;
    for (let a = target, i = 0; a && a !== document.body && i < 6; a = a.parentElement, i++) {
      if (a.hasAttribute('tabindex') || a.hasAttribute('aria-expanded') || getComputedStyle(a).cursor === 'pointer') { el = a; break; }
    }
    if (!el) return null;
    while (el.parentElement && el.parentElement !== document.body &&
           getComputedStyle(el.parentElement).cursor === 'pointer' &&
           clean(el.parentElement.innerText).length <= 80) el = el.parentElement;
    return el;
  }

  document.addEventListener('click', e => {
    if (fromToolbar(e)) return;
    flushScroll();
    flushTyping();
    if (checkMode) {
      e.preventDefault(); e.stopPropagation();
      const el = e.target;
      const text = clean(el.innerText || el.value).slice(0, 120);
      setCheckMode(false);
      if (text) send({ type: 'expect_text', text, target: describe(el), locators: locators(el) });
      return;
    }
    const el = clickableOf(e.target);
    if (!el) return;
    const role = roleOf(el);
    if (role === 'textbox' || role === 'combobox') return; // focusing a field; typing is captured on change
    if (fieldOf(el)) return;
    send({ type: 'click', target: describe(el), locators: locators(el) });
  }, true);

  document.addEventListener('mousedown', e => {
    if (checkMode && !fromToolbar(e)) { e.preventDefault(); e.stopPropagation(); }
  }, true);

  document.addEventListener('change', e => {
    if (fromToolbar(e)) return;
    const el = e.target;
    if (el.tagName === 'SELECT') {
      send({ type: 'select', value: el.value, target: describe(el), locators: locators(el) });
    } else if ((el.tagName === 'INPUT' && !['checkbox', 'radio'].includes(el.type)) || el.tagName === 'TEXTAREA') {
      recordFill(el);
    }
  }, true);

  document.addEventListener('focusout', e => {
    const f = fieldOf(e.target);
    if (f && dirty.has(f) && !fromToolbar(e)) recordFill(f);
  }, true);

  document.addEventListener('keydown', e => {
    if (fromToolbar(e) || e.key !== 'Enter') return;
    const root = editableRoot(e.target);
    if (root) {
      // Enter in a single-line editor (like a recipient box) confirms the entry;
      // in a multi-line body it is just a new line, captured with the text.
      if (root.getAttribute('aria-multiline') === 'true' || valueOf(root).includes('\n')) return;
      recordFill(root);
      send({ type: 'press', key: 'Enter', target: describe(root), locators: locators(root) });
      return;
    }
    if (e.target.tagName !== 'INPUT') return;
    recordFill(e.target);
    send({ type: 'press', key: 'Enter', target: describe(e.target), locators: locators(e.target) });
  }, true);

  document.addEventListener('mouseover', e => {
    if (!checkMode || fromToolbar(e)) return;
    clearHover();
    hoverEl = e.target;
    hoverEl.__recOutline = hoverEl.style.outline;
    hoverEl.style.outline = '2px solid #f59e0b';
  }, true);
  function clearHover() {
    if (hoverEl) { hoverEl.style.outline = hoverEl.__recOutline || ''; hoverEl = null; }
  }
  function setCheckMode(on) {
    checkMode = on;
    if (!on) clearHover();
    document.documentElement.style.cursor = on ? 'crosshair' : '';
    render();
  }

  // ---------------------------------------------------------------- toolbar
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const STYLE = `
    :host{all:initial}
    .bar{position:fixed;right:18px;bottom:18px;width:360px;z-index:2147483647;background:#111827;color:#f9fafb;
      font:13px/1.45 "Segoe UI",system-ui,sans-serif;border-radius:12px;box-shadow:0 12px 32px rgba(0,0,0,.35);overflow:hidden}
    .head{display:flex;align-items:center;gap:8px;padding:10px 14px;background:#1f2937;cursor:pointer}
    .dot{width:10px;height:10px;border-radius:50%;background:#ef4444;animation:p 1.2s infinite}
    @keyframes p{50%{opacity:.35}}
    .head b{flex:1;font-size:12px;letter-spacing:.6px}
    .count{font-size:12px;color:#9ca3af}
    .body{padding:12px 14px 14px}
    .sid{font-size:11px;color:#93c5fd;font-weight:600;letter-spacing:.4px}
    .name{font-size:15px;font-weight:600;margin:2px 0 6px}
    .desc{white-space:pre-line;color:#d1d5db;font-size:12px;max-height:120px;overflow:auto;margin-bottom:8px}
    .data{background:#0b1220;border-radius:8px;padding:8px 10px;font-size:12px;margin-bottom:8px}
    .data div{display:flex;gap:6px}.data span{color:#9ca3af;min-width:74px}.data code{color:#fde68a;font-family:Consolas,monospace;word-break:break-all}
    .exp{font-size:12px;color:#a7f3d0;margin-bottom:10px}
    .row{display:flex;gap:8px}
    button{flex:1;font:inherit;font-weight:600;border:0;border-radius:8px;padding:8px 10px;cursor:pointer}
    .check{background:#374151;color:#fde68a}.check.on{background:#f59e0b;color:#111827}
    .next{background:#2563eb;color:#fff}.finish{background:#16a34a;color:#fff}
    .note{font-size:12px;color:#fcd34d;margin-bottom:8px}
    .done{padding:16px 14px;color:#bbf7d0}
    .stepname{display:block;width:100%;box-sizing:border-box;font:inherit;font-size:14px;font-weight:600;margin:4px 0 8px;
      padding:6px 9px;border-radius:7px;border:1px solid #374151;background:#0b1220;color:#f9fafb}
    .stepname::placeholder{color:#6b7280;font-weight:400}
    .row + .row{margin-top:8px}`;

  let root = null;
  let stepName = ''; // free-form mode: the name typed for the current step
  let nameTimer = null;
  function render() {
    if (!root) return;
    if (!state) { root.innerHTML = `<style>${STYLE}</style>`; return; }
    if (state.mode === 'signin') {
      root.innerHTML = `<style>${STYLE}</style><div class="bar">
        <div class="head"><span class="dot"></span><b>${state.finished ? 'SIGN-IN SAVED' : 'SIGN IN ONCE'}</b></div>
        <div class="body">${state.finished ? '<div class="done">Saved. You can close this window.</div>' : `
          <div class="desc">Sign in to this site as you normally would, including any MFA prompt.
Choose "Stay signed in" if asked. When you can see the application, click Save sign-in.
Nothing you type here is recorded.</div>
          <div class="row"><button class="finish" id="save">Save sign-in ✓</button></div>`}</div></div>`;
      const save = root.getElementById('save');
      if (save) save.onclick = () => window.__recFinish().then(s => { state = s; render(); });
      return;
    }
    if (state.finished) {
      root.innerHTML = `<style>${STYLE}</style><div class="bar"><div class="head"><b>RECORDING SAVED</b></div>
        <div class="done">All steps recorded. You can close this window.</div></div>`;
      return;
    }
    if (state.freeform) { renderFreeform(); return; }
    const last = state.idx === state.total - 1;
    const data = Object.entries(state.test_data || {});
    root.innerHTML = `<style>${STYLE}</style><div class="bar">
      <div class="head" id="toggle"><span class="dot"></span><b>REC · STEP ${state.idx + 1} OF ${state.total}</b>
        <span class="count">${state.count} action${state.count === 1 ? '' : 's'}</span><span>${collapsed ? '▴' : '▾'}</span></div>
      ${collapsed ? '' : `<div class="body">
        <div class="sid">${esc(state.step_id)}</div>
        <div class="name">${esc(state.name)}</div>
        <div class="desc">${esc(state.description)}</div>
        ${data.length ? `<div class="data">${data.map(([k, v]) => `<div><span>${esc(k)}</span><code>${esc(v)}</code></div>`).join('')}</div>` : ''}
        ${state.expected ? `<div class="exp">Expected: ${esc(state.expected)}</div>` : ''}
        ${checkMode ? '<div class="note">Click the text on the page that proves this step worked.</div>' : ''}
        <div class="row">
          <button class="check ${checkMode ? 'on' : ''}" id="check">${checkMode ? 'Cancel check' : '+ Add check'}</button>
          <button class="${last ? 'finish' : 'next'}" id="next">${last ? 'Finish recording ■' : 'Next step ▶'}</button>
        </div></div>`}
    </div>`;
    root.getElementById('toggle').onclick = () => { collapsed = !collapsed; render(); };
    if (collapsed) return;
    root.getElementById('check').onclick = () => { flushPending(); setCheckMode(!checkMode); };
    root.getElementById('next').onclick = () => {
      flushPending(); // typing and scrolling in the current step belong to it
      setCheckMode(false);
      const call = last ? window.__recFinish() : window.__recNext();
      call.then(s => { state = s; render(); });
    };
  }

  // Recording without Excel: steps are created as the user goes. The user can
  // name each step; 'Next step' starts a new one, 'Finish' saves the recording.
  function renderFreeform() {
    root.innerHTML = `<style>${STYLE}</style><div class="bar">
      <div class="head" id="toggle"><span class="dot"></span><b>REC · STEP ${state.idx + 1}</b>
        <span class="count">${state.count} action${state.count === 1 ? '' : 's'}</span><span>${collapsed ? '▴' : '▾'}</span></div>
      ${collapsed ? '' : `<div class="body">
        <input class="stepname" id="stepname" placeholder="Name this step (optional)" value="${esc(stepName)}" maxlength="80">
        <div class="desc">Do this part of the test in the page. Click Next step to start a new step, or Finish recording when the test is done.</div>
        ${checkMode ? '<div class="note">Click the text on the page that proves this step worked.</div>' : ''}
        <div class="row">
          <button class="check ${checkMode ? 'on' : ''}" id="check">${checkMode ? 'Cancel check' : '+ Add check'}</button>
          <button class="next" id="next">Next step ▶</button>
        </div>
        <div class="row"><button class="finish" id="finish">Finish recording ■</button></div></div>`}
    </div>`;
    root.getElementById('toggle').onclick = () => { collapsed = !collapsed; render(); };
    if (collapsed) return;
    // Saved in Python after a short pause, so the name survives page loads (the toolbar is rebuilt on each page).
    root.getElementById('stepname').oninput = e => {
      stepName = e.target.value;
      clearTimeout(nameTimer);
      nameTimer = setTimeout(() => window.__recName && window.__recName(stepName), 500);
    };
    root.getElementById('check').onclick = () => { flushPending(); setCheckMode(!checkMode); };
    const advance = finish => () => {
      flushPending();
      setCheckMode(false);
      const name = stepName;
      (finish ? window.__recFinish(name) : window.__recNext(name)).then(s => {
        if (!finish && s.idx !== state.idx) stepName = '';
        state = s;
        render();
      });
    };
    root.getElementById('next').onclick = advance(false);
    root.getElementById('finish').onclick = advance(true);
  }

  async function refresh() {
    try { state = await window.__recState(); } catch (e) { state = null; }
    if (state && state.freeform && !stepName) stepName = state.custom_name || '';
    render();
  }
  function mount() {
    if (!IS_TOP || document.getElementById(HOST_ID)) return; // toolbar only in the main page, not iframes
    const host = document.createElement('div');
    host.id = HOST_ID;
    root = host.attachShadow({ mode: 'open' });
    document.documentElement.appendChild(host);
    refresh();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
  else mount();
})();
