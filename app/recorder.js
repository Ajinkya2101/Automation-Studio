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
  const FIELDS = 'input,textarea,select,[contenteditable],[role=textbox],[role=combobox],[role=checkbox],[role=radio]';
  const IS_TOP = window.top === window;
  const lastSent = new WeakMap();
  const dirty = new WeakSet(); // rich-text editors typed into since last recorded
  let checkMode = false;
  let hoverEl = null;
  let state = null;
  let collapsed = false;

  // Icon-font glyphs (private use area) are not part of a control's real name.
  const clean = s => (s || '').replace(/[-]/g, '').replace(/\s+/g, ' ').trim();
  const cssAttr = s => s.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  // Ids containing digits (splitButton-r44, MSG_efc71ad5961) are usually regenerated on every load.
  const stableId = id => id && !/\d/.test(id);
  const fromToolbar = e => e.composedPath().some(n => n && n.id === HOST_ID);
  const cssEsc = s => (window.CSS && CSS.escape) ? CSS.escape(s) : s;
  // The element the event really happened on. Inside a web component (shadow DOM)
  // e.target is only the component itself, so the first entry of the path is used.
  const targetOf = e => {
    const t = (e.composedPath && e.composedPath()[0]) || e.target;
    return t && t.nodeType === 3 ? t.parentElement : t;
  };
  const parentOf = el => el.parentElement || (el.getRootNode && el.getRootNode().host) || null;
  // closest() that also looks outside the web component the element is in.
  function closestDeep(el, sel) {
    for (let cur = el; cur && cur.nodeType === 1;) {
      const hit = cur.closest(sel);
      if (hit) return hit;
      cur = cur.getRootNode().host;
    }
    return null;
  }
  const shown = el => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';

  // Identity of an element while the page lives, so that typing twice into the same
  // field can be told from typing into two fields that share a label.
  const elIds = new WeakMap();
  let elSeq = 0;
  const DOC = Math.random().toString(36).slice(2, 8);
  const elKey = el => {
    if (!elIds.has(el)) elIds.set(el, ++elSeq);
    return `${DOC}:${elIds.get(el)}`;
  };

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
  // Icon-only controls (Oracle Fusion toolbar buttons such as Add Row) have no text:
  // their name is the icon image's alt text or a tooltip.
  function iconNameOf(el) {
    const img = el.querySelector('img[alt]:not([alt=""]), img[title]:not([title=""])');
    if (img) return clean(img.getAttribute('alt') || img.getAttribute('title'));
    const svgTitle = el.querySelector('svg title');
    return svgTitle ? clean(svgTitle.textContent) : '';
  }
  const nameOf = el => clean(el.getAttribute('aria-label') || el.innerText || el.value) ||
    iconNameOf(el) || clean(el.getAttribute('title'));
  function labelOf(el) {
    if (el.labels && el.labels.length) return clean(el.labels[0].innerText);
    const by = el.getAttribute('aria-labelledby');
    const l = by && document.getElementById(by);
    return l ? clean(l.innerText) : clean(el.getAttribute('aria-label'));
  }
  // A CSS path that fits exactly one element. Five levels are usually enough; a path
  // that still fits other elements (the same icon in every table toolbar, the same
  // button in every row) keeps climbing, up to the top of the page if it has to.
  function cssPath(el) {
    const root = el.getRootNode();
    const count = sel => { try { return root.querySelectorAll(sel).length; } catch (e) { return 0; } };
    // An id is only trusted when it is the only one: pages repeat ids (#thumbnail on every video).
    const idOf = e => (stableId(e.id) && count('#' + cssEsc(e.id)) === 1) ? '#' + cssEsc(e.id) : null;
    const parts = [];
    for (let cur = el; cur && cur.nodeType === 1; cur = cur.parentElement) {
      const id = idOf(cur);
      if (id) { parts.unshift(id); break; }
      let sel = cur.tagName.toLowerCase();
      const name = cur.getAttribute('name');
      if (name) sel += `[name="${cssAttr(name)}"]`;
      if (cur.parentElement) {
        const same = Array.from(cur.parentElement.children).filter(x => x.tagName === cur.tagName);
        const alike = name ? same.filter(x => x.getAttribute('name') === name) : same;
        if (alike.length > 1) sel += `:nth-of-type(${same.indexOf(cur) + 1})`;
      }
      parts.unshift(sel);
      if (parts.length >= 5 && count(parts.join(' > ')) === 1) break;
    }
    const sel = parts.join(' > ');
    // Inside a web component: the path to the component, then the path within it.
    return root.host ? `${cssPath(root.host)} ${sel}` : sel;
  }

  // How many elements a name-based locator fits right now, and which of them this
  // one is. When a name is shared ("Search" on the page and in a dialog, "Edit" in
  // every row) replay uses the position instead of taking the first match.
  const ROLE_SEL = { button: 'button,[role=button],input[type=submit],input[type=button],input[type=reset]',
                     link: 'a[href],[role=link]' };
  function position(el, all, same) {
    let list;
    try { list = Array.from(el.getRootNode().querySelectorAll(all)).filter(x => shown(x) && same(x)); } catch (e) { return {}; }
    const index = list.indexOf(el);
    return index < 0 || list.length < 2 ? {} : { count: list.length, index };
  }
  function textPosition(el, text) {
    // Like the replay's text locator: the innermost elements whose whole text is `text`.
    const hits = [];
    const walker = document.createTreeWalker(el.getRootNode(), NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const piece = clean(n.data);
      if (!piece || !text.startsWith(piece)) continue;
      let cur = n.parentElement;
      while (cur && clean(cur.textContent).length < text.length) cur = cur.parentElement;
      if (cur && clean(cur.textContent) === text && !hits.includes(cur) && shown(cur)) hits.push(cur);
    }
    const index = hits.findIndex(h => h === el || el.contains(h) || h.contains(el));
    return index < 0 || hits.length < 2 ? {} : { count: hits.length, index };
  }

  // Most stable first: test id, label / accessible name, visible text, CSS path.
  function locators(el) {
    const out = [];
    const tid = el.getAttribute('data-testid');
    if (tid) out.push({ kind: 'testid', value: tid });
    const role = roleOf(el);
    if (FIELD_ROLES.includes(role)) {
      const lab = labelOf(el);
      if (lab) out.push({ kind: 'label', value: lab, ...position(el, FIELDS, x => labelOf(x) === lab) });
      const ph = clean(el.getAttribute('placeholder'));
      if (ph) out.push({ kind: 'placeholder', value: ph, ...position(el, '[placeholder]', x => clean(x.getAttribute('placeholder')) === ph) });
    } else {
      const n = nameOf(el);
      if (role && n && n.length <= 80) {
        out.push({ kind: 'role', role, value: n,
                   ...position(el, ROLE_SEL[role] || `[role="${cssAttr(role)}"]`, x => roleOf(x) === role && nameOf(x) === n) });
      }
      const tx = clean(el.innerText);
      if (tx && tx.length <= 80) out.push({ kind: 'text', value: tx, ...textPosition(el, tx) });
    }
    const aria = clean(el.getAttribute('aria-label'));
    if (aria) out.push({ kind: 'css', value: `${el.tagName.toLowerCase()}[aria-label="${cssAttr(aria)}"]` });
    const tag = el.tagName.toLowerCase();
    const title = clean(el.getAttribute('title'));
    if (title) out.push({ kind: 'css', value: `${tag}[title="${cssAttr(title)}"]` });
    const icon = el.querySelector('img[alt]:not([alt=""]), img[title]:not([title=""])');
    if (icon) {
      const attr = icon.getAttribute('alt') ? 'alt' : 'title';
      out.push({ kind: 'css', value: `${tag}:has(img[${attr}="${cssAttr(icon.getAttribute(attr))}"])` });
    }
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
  const act = (type, el, extra) => ({ type, target: describe(el), locators: locators(el), el: elKey(el), ...extra });

  function post(action) {
    if (!window.__recAction) return;
    window.__recAction(action).then(s => { if (s) { state = s; render(); } }).catch(() => {});
  }
  // Every click the user makes is recorded, with one exception: a click on a field
  // that is followed by typing into that same field only gave it focus, and the
  // typing is saved as a fill. So a click on a field is held back until the next
  // action shows which it was. If the user goes on to do anything else (pick from
  // the list or calendar the click opened, scroll it, click elsewhere) the click
  // is recorded first: without it replay never finds what it opened.
  let pendingField = null; // { el, action }
  function flushField() {
    const p = pendingField;
    pendingField = null;
    if (p) post(p.action);
  }
  function send(action, el) {
    if (action.page) { post(action); return; } // a page scroll in between decides nothing yet
    if (pendingField && pendingField.el === el && ['fill', 'press', 'select', 'upload'].includes(action.type)) pendingField = null;
    else flushField();
    post(action);
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
    send(act('fill', el, { value, masked: el.type === 'password' }), el);
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
    const f = fieldOf(targetOf(e));
    if (f) dirty.add(f);
  }
  function flushTyping() {
    if (focusedField && dirty.has(focusedField) && focusedField.isConnected) recordFill(focusedField);
  }
  window.__recFlushTyping = flushTyping;

  document.addEventListener('focusin', e => { if (!fromToolbar(e)) focusedField = fieldOf(targetOf(e)) || focusedField; }, true);
  document.addEventListener('keydown', e => {
    if (e.key.length === 1 || e.key === 'Backspace' || e.key === 'Delete') markTyped(e);
  }, true);
  ['input', 'paste', 'cut', 'drop'].forEach(t => document.addEventListener(t, markTyped, true));
  document.addEventListener('mousedown', e => { if (!fromToolbar(e)) { flushScroll(); flushTyping(); } }, true);
  window.addEventListener('pagehide', () => { flushScroll(); flushTyping(); flushField(); }, true);

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
           target, locators: page ? [] : locators(el), el: page ? undefined : elKey(el) }, el);
  }
  // Only scrolls the user makes count. Pages also scroll by themselves (an element
  // brought into view, a panel reset when it closes); those are not recorded.
  let userScrollAt = 0;
  const SCROLL_KEYS = ['PageUp', 'PageDown', 'Home', 'End', 'ArrowUp', 'ArrowDown', ' '];
  const userScrolls = () => { userScrollAt = Date.now(); };
  window.addEventListener('wheel', e => { if (!fromToolbar(e)) userScrolls(); }, { capture: true, passive: true });
  window.addEventListener('touchmove', e => { if (!fromToolbar(e)) userScrolls(); }, { capture: true, passive: true });
  document.addEventListener('keydown', e => {
    if (!fromToolbar(e) && SCROLL_KEYS.includes(e.key) && !fieldOf(targetOf(e))) userScrolls();
  }, true);
  document.addEventListener('mousedown', e => {
    // A press on a scrollbar lands outside the element's content box.
    const el = targetOf(e);
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

  const flushPending = () => { flushScroll(); flushTyping(); flushField(); };

  // The element a click is named after. Many apps (Oracle Fusion menus, tiles,
  // panels) make plain elements clickable with scripts instead of using buttons or
  // links. Such an element shows the pointer cursor, or has tabindex /
  // aria-expanded. Take the largest element that still looks like one control, so
  // its text identifies it. Returns null when nothing around the click looks like
  // a control; the click is then recorded on the exact element (see the click handler).
  function clickableOf(target) {
    const std = closestDeep(target, INTERACTIVE);
    if (std) return std;
    let el = null;
    for (let a = target, i = 0; a && a !== document.body && i < 6; a = parentOf(a), i++) {
      if (a.hasAttribute('tabindex') || a.hasAttribute('aria-expanded') || getComputedStyle(a).cursor === 'pointer') { el = a; break; }
    }
    if (!el) return null;
    while (el.parentElement && el.parentElement !== document.body &&
           getComputedStyle(el.parentElement).cursor === 'pointer' &&
           clean(el.parentElement.innerText).length <= 80) el = el.parentElement;
    return el;
  }
  const isFieldLike = el => ['textbox', 'combobox'].includes(roleOf(el)) || !!fieldOf(el);

  // Some lists (YouTube's search suggestions) act on the mouse press itself and remove
  // the item before the button is released, so the browser never fires a click on it. The
  // press is remembered and, when its element is gone at release, recorded as the click.
  let pressed = null;
  const gone = el => !el.isConnected || !el.getClientRects().length;
  document.addEventListener('mousedown', e => {
    pressed = null;
    if (fromToolbar(e) || checkMode || e.button !== 0 || !e.isTrusted) return;
    const el = clickableOf(targetOf(e));
    if (!el || isFieldLike(el)) return;
    pressed = { el, action: act('click', el) };
  }, true);
  document.addEventListener('mouseup', () => {
    const p = pressed;
    if (!p) return;
    setTimeout(() => { // a click event, if there is one, arrives before this
      if (pressed !== p) return;
      pressed = null;
      if (gone(p.el)) send(p.action, p.el);
    }, 0);
  }, true);

  // The last key pressed: a click that the keyboard caused (Enter or Space on a button) has no mouse press.
  let lastKey = null;
  let lastClick = null; // the click event before the current one
  // A click on a label is followed by one the browser makes on the label's control.
  function passedOnByLabel(target, prev) {
    if (!prev || prev.target === target || Date.now() - prev.time > 200 || !prev.target.isConnected) return false;
    const label = prev.target.closest('label');
    return !!label && label.control === target;
  }

  document.addEventListener('click', e => {
    if (fromToolbar(e)) return;
    const target = targetOf(e);
    const p = pressed;
    pressed = null;
    flushScroll();
    flushTyping();
    if (!target || target.nodeType !== 1) return;
    if (checkMode) {
      e.preventDefault(); e.stopPropagation();
      const text = clean(target.innerText || target.value).slice(0, 120);
      setCheckMode(false);
      if (text) send({ type: 'expect_text', text, target: describe(target), locators: locators(target) }, target);
      return;
    }
    // Only the user's own clicks. The page clicking for itself (element.click()) is a
    // consequence of something already recorded. So is a click the browser makes up:
    // a click on a label is passed on to its control, and Enter in a form 'clicks' its
    // submit button. The latter carries no mouse press (detail 0); a real keyboard click
    // (Enter or Space on the control itself) is told apart by the key press before it.
    if (!e.isTrusted) return;
    const prev = lastClick;
    lastClick = { target, time: Date.now() };
    if (passedOnByLabel(target, prev)) return;
    if (e.detail === 0 && !(lastKey && lastKey.target === target && Date.now() - lastKey.time < 1500)) return;
    // The pressed item was removed and the browser sent the click to what is around it.
    if (p && gone(p.el) && !p.el.contains(target)) { send(p.action, p.el); return; }
    if (target === document.documentElement || target === document.body) return;

    let el = clickableOf(target), extra = {};
    if (!el) {
      // Nothing here looks like a control (a table row, a cell, a label drawn over a
      // field), but the application may still react to the click, so it is recorded on
      // the exact element and at the exact spot.
      el = target;
      const r = el.getBoundingClientRect();
      extra = { pos: [Math.max(0, Math.round(e.clientX - r.left)), Math.max(0, Math.round(e.clientY - r.top))] };
    }
    if (el.tagName === 'INPUT' && el.type === 'file') return; // the chosen file is recorded as an upload
    const action = act('click', el, extra);
    if (isFieldLike(el)) {
      flushField();
      pendingField = { el, action }; // see send(): dropped if the user just types
      return;
    }
    send(action, el);
  }, true);

  // A double-click arrives after its two clicks; Python replaces them with this.
  document.addEventListener('dblclick', e => {
    if (fromToolbar(e) || checkMode || !e.isTrusted) return;
    const target = targetOf(e);
    if (!target || target.nodeType !== 1 || target === document.body || target === document.documentElement) return;
    const el = clickableOf(target) || target;
    if (isFieldLike(el)) return; // selects a word; the typing is what counts
    send(act('dblclick', el), el);
  }, true);

  document.addEventListener('mousedown', e => {
    if (checkMode && !fromToolbar(e)) { e.preventDefault(); e.stopPropagation(); }
  }, true);

  document.addEventListener('change', e => {
    if (fromToolbar(e)) return;
    const el = targetOf(e);
    if (el.tagName === 'SELECT') {
      send(act('select', el, { value: el.value }), el);
    } else if (el.tagName === 'INPUT' && el.type === 'file') {
      const files = Array.from(el.files || []).map(f => f.name);
      if (files.length) send(act('upload', el, { files }), el);
    } else if ((el.tagName === 'INPUT' && !['checkbox', 'radio'].includes(el.type)) || el.tagName === 'TEXTAREA') {
      recordFill(el);
    }
  }, true);

  document.addEventListener('focusout', e => {
    const f = fieldOf(targetOf(e));
    if (f && dirty.has(f) && !fromToolbar(e)) recordFill(f);
  }, true);

  // ---- keys -----------------------------------------------------------------
  // Text is saved as a fill, not key by key. Recorded as key presses are the keys
  // that act: Enter, Tab, Escape, the arrows that move through a list or menu,
  // function keys and shortcuts such as Ctrl+S.
  const EDIT_SHORTCUTS = 'acvxzy'; // select all, copy, paste, cut, undo, redo: their result is the text
  const NATIVE_ENTER = 'button,a[href],summary,select,input[type=submit],input[type=button],input[type=reset],' +
    'input[type=checkbox],input[type=radio],input[type=file],input[type=image]';
  function keyToRecord(e, target, field) {
    const k = e.key;
    if (['Control', 'Shift', 'Alt', 'Meta', 'CapsLock', 'Dead', 'Process'].includes(k) || e.isComposing) return null;
    const multiline = field && (field.tagName === 'TEXTAREA' ||
      (field.isContentEditable && (field.getAttribute('aria-multiline') === 'true' || valueOf(field).includes('\n'))));
    const onPage = !field && (target === document.body || target === document.documentElement);
    let record;
    if (k === 'Enter') {
      // In a multi-line editor Enter is a new line, captured with the text. On a
      // button or link it makes a click, which is what gets recorded.
      record = field ? !multiline : !target.matches(NATIVE_ENTER);
    } else if ((e.ctrlKey || e.metaKey || e.altKey) && k.length === 1) {
      record = !(field && !e.altKey && EDIT_SHORTCUTS.includes(k.toLowerCase()));
    } else if (k === 'Tab' || k === 'Escape' || /^F\d{1,2}$/.test(k)) {
      record = true;
    } else if (k === 'ArrowUp' || k === 'ArrowDown') {
      record = field ? !multiline : !onPage; // moves through a list; on the page itself it scrolls
    } else if (['ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown', 'Delete', 'Backspace'].includes(k)) {
      record = !field && !onPage; // inside a field these edit or move the cursor
    } else {
      record = false;
    }
    if (!record) return null;
    const mods = (e.ctrlKey ? 'Control+' : '') + (e.altKey ? 'Alt+' : '') + (e.metaKey ? 'Meta+' : '') +
      (e.shiftKey && (k.length > 1 || e.ctrlKey || e.altKey || e.metaKey) ? 'Shift+' : '');
    return mods + k; // the key as the page saw it: 's' with Ctrl, 'S' with Ctrl+Shift
  }
  document.addEventListener('keydown', e => {
    if (fromToolbar(e) || !e.isTrusted) return;
    const target = targetOf(e);
    if (!target || target.nodeType !== 1) return;
    lastKey = { target, time: Date.now() };
    const field = fieldOf(target);
    const key = keyToRecord(e, target, field);
    if (!key) return;
    if (e.repeat && key !== 'ArrowUp' && key !== 'ArrowDown') return;
    // The text typed so far belongs before the key. Enter also saves a value that was
    // not typed (filled in by the browser), as the run has to enter it too.
    if (field && (dirty.has(field) || (key === 'Enter' && valueOf(field)))) recordFill(field);
    const el = field || target;
    if (el === document.body || el === document.documentElement) {
      flushField();
      post({ type: 'press', key, global: true, target: 'the page', locators: [] });
      return;
    }
    send(act('press', el, { key }), el);
  }, true);
  document.addEventListener('keyup', e => {
    const target = targetOf(e);
    if (e.isTrusted && target && target.nodeType === 1) lastKey = { target, time: Date.now() };
  }, true);

  document.addEventListener('mouseover', e => {
    if (!checkMode || fromToolbar(e)) return;
    clearHover();
    hoverEl = targetOf(e);
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
