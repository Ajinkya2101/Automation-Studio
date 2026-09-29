// Automation Studio dashboard: upload, record, run and watch live.
const $ = (sel, root = document) => root.querySelector(sel);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtTime = ts => new Date(ts * 1000).toLocaleTimeString([], { hour12: false }) + '.' + String(Math.floor((ts % 1) * 1000)).padStart(3, '0');
const fmtDate = iso => iso ? new Date(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : '';
const secs = ms => ms == null ? '' : `${(ms / 1000).toFixed(1)}s`;

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  const type = res.headers.get('content-type') || '';
  const body = type.includes('json') ? await res.json() : await res.text();
  if (!res.ok) throw new Error((body && body.detail) || body || res.statusText);
  return body;
}

const S = {
  config: { live_view_url: '' },
  cases: [],
  sel: null,        // { suite_id, case_id }
  detail: null,     // case detail from the API
  tab: 'steps',
  notice: null,     // { kind, html }
  runForm: false,
  session: null,    // active session info
  es: null,         // EventSource
  live: null,       // live / viewed run model
};

// ------------------------------------------------------------------ data
async function loadCases() {
  S.cases = await api('/api/cases');
  renderCaseList();
}

async function selectCase(suite_id, case_id, keepLive = false) {
  S.newForm = false;
  S.sel = { suite_id, case_id };
  S.detail = await api(`/api/suites/${suite_id}/cases/${encodeURIComponent(case_id)}`);
  S.runForm = false;
  if (!keepLive && !(S.session && S.session.suite_id === suite_id && S.session.case_id === case_id)) S.live = null;
  renderCaseList();
  renderMain();
}

async function refresh() {
  await loadCases();
  if (S.sel) await selectCase(S.sel.suite_id, S.sel.case_id, true);
}

// ------------------------------------------------------------------ record without Excel
function showNewForm() {
  if (S.session) return;
  S.newForm = true;
  S.sel = null;
  S.detail = null;
  S.live = null;
  renderCaseList();
  renderMain();
  $('#nf-title').focus();
}

async function createFreeform() {
  const btn = $('#nf-create');
  btn.disabled = true;
  try {
    const res = await api('/api/freeform', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: $('#nf-title').value, url: $('#nf-url').value }),
    });
    S.notice = null;
    S.tab = 'steps';
    await loadCases();
    await selectCase(res.suite_id, res.case_id);
  } catch (err) {
    $('#nf-error').textContent = err.message;
    btn.disabled = false;
  }
}

function renderNewForm(main, notice) {
  main.innerHTML = `${notice}<div class="card new-form">
    <h2>Record a test without Excel</h2>
    <p class="muted">Give the test a name and the page where it starts. Then record it: the steps, their test data and an
      Excel test script are created from what you do. You can run it automatically as often as you want.</p>
    <label for="nf-title">Test name</label>
    <input id="nf-title" placeholder="e.g. Update worker phone number" maxlength="120">
    <label for="nf-url">Start URL</label>
    <input id="nf-url" placeholder="Leave empty for the Acme Mail demo, or paste https://…">
    <div class="hint">For a site that needs a sign-in (e.g. Outlook or Fusion), you can sign in once before you record.</div>
    <div class="err-text" id="nf-error"></div>
    <div class="foot"><button class="btn" id="nf-cancel">Cancel</button><button class="btn primary" id="nf-create">Create test</button></div>
  </div>`;
  $('#nf-cancel').onclick = () => { S.newForm = false; renderMain(); };
  $('#nf-create').onclick = createFreeform;
  main.querySelectorAll('input').forEach(i => i.addEventListener('keydown', e => { if (e.key === 'Enter') createFreeform(); }));
}

// ------------------------------------------------------------------ upload
function setupUpload() {
  const dz = $('#dropzone'), input = $('#file-input');
  input.addEventListener('change', () => input.files[0] && upload(input.files[0]));
  ['dragenter', 'dragover'].forEach(t => dz.addEventListener(t, e => { e.preventDefault(); dz.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(t => dz.addEventListener(t, e => { e.preventDefault(); dz.classList.remove('over'); }));
  dz.addEventListener('drop', e => e.dataTransfer.files[0] && upload(e.dataTransfer.files[0]));
}

async function upload(file) {
  const dz = $('#dropzone');
  dz.classList.add('busy');
  const fd = new FormData();
  fd.append('file', file);
  try {
    const res = await api('/api/suites', { method: 'POST', body: fd });
    S.notice = res.warnings.length
      ? { kind: 'warn', html: `<b>${esc(file.name)}</b> uploaded with ${res.warnings.length} note(s):<ul>${res.warnings.map(w => `<li>${esc(w)}</li>`).join('')}</ul>` }
      : null;
    await loadCases();
    await selectCase(res.suite_id, res.cases[0]);
  } catch (err) {
    S.notice = { kind: 'err', html: `Upload failed: ${esc(err.message)}` };
    renderMain();
  } finally {
    dz.classList.remove('busy');
    $('#file-input').value = '';
  }
}

// ------------------------------------------------------------------ sessions
async function startRecording() {
  try {
    const s = await api(`/api/suites/${S.sel.suite_id}/cases/${encodeURIComponent(S.sel.case_id)}/record`, { method: 'POST' });
    attach(s);
  } catch (err) { alert(err.message); }
}

async function startSignin() {
  try {
    const s = await api(`/api/suites/${S.sel.suite_id}/cases/${encodeURIComponent(S.sel.case_id)}/signin`, { method: 'POST' });
    attach(s);
  } catch (err) { alert(err.message); }
}

async function startRun() {
  const data = {};
  document.querySelectorAll('[data-key]').forEach(inp => { data[inp.dataset.key] = inp.value; });
  const body = {
    headed: $('#opt-headed').checked,
    slow_mo: Number($('#opt-speed').value),
    reset_mailbox: !!($('#opt-reset') && $('#opt-reset').checked),
    data,
  };
  try {
    const s = await api(`/api/suites/${S.sel.suite_id}/cases/${encodeURIComponent(S.sel.case_id)}/run`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    S.runForm = false;
    attach(s);
  } catch (err) { alert(err.message); }
}

async function stopSession() {
  if (S.session) await api(`/api/sessions/${S.session.id}/stop`, { method: 'POST' }).catch(() => {});
}

function attach(session) {
  if (S.es) S.es.close();
  S.session = session;
  S.live = newLive(session.kind, session.run_id);
  setStatus();
  showScreen(session);
  renderMain();
  const es = new EventSource(`/api/sessions/${session.id}/events`);
  es.onmessage = e => { applyEvent(S.live, JSON.parse(e.data)); renderLive(); };
  es.addEventListener('end', async () => {
    es.close();
    S.es = null;
    S.session = null;
    setStatus();
    showScreen(null);
    await refresh();
    if (S.live && S.live.kind === 'record') S.tab = 'recording';
    renderMain();
  });
  S.es = es;
}

function setStatus() {
  const pill = $('#status-pill');
  const s = S.session;
  if (!s) { pill.className = 'pill idle'; pill.textContent = 'Idle'; }
  else if (s.kind === 'signin') { pill.className = 'pill signin'; pill.innerHTML = `<span class="blink"></span>Signing in`; }
  else if (s.kind === 'record') { pill.className = 'pill record'; pill.innerHTML = `<span class="blink"></span>Recording ${esc(s.case_id)}`; }
  else { pill.className = 'pill run'; pill.innerHTML = `<span class="blink"></span>Running ${esc(s.case_id)}`; }
  document.querySelectorAll('[data-needs-idle]').forEach(b => { b.disabled = !!s; });
}

// Live browser view: the automated browser runs on the container's virtual
// screen; noVNC shows it here so you can sign in, record, and watch runs.
function showScreen(session) {
  const box = $('#screen'), frame = $('#screen-frame');
  const url = S.config.live_view_url;
  if (!session || !url) {
    box.hidden = true;
    frame.removeAttribute('src');
    return;
  }
  const hints = {
    signin: 'Sign in here (click inside the view to type), then click "Save sign-in" on the toolbar.',
    record: 'Perform each Excel step here. Use the toolbar in the corner to add checks and move on.',
    run: 'Watch the automation. No input needed.',
  };
  $('#screen-title').textContent = { signin: 'Sign in', record: 'Recording', run: 'Automated run' }[session.kind];
  $('#screen-hint').textContent = hints[session.kind] || '';
  $('#screen-pop').href = url;
  if (frame.getAttribute('src') !== url) frame.src = url;
  box.hidden = false;
}

// ------------------------------------------------------------------ live model
function newLive(kind, run_id = null) {
  return { kind, run_id, steps: [], idx: 0, logs: [], done: false, status: 'running', summary: null, viewing: false };
}

function applyEvent(L, ev) {
  L.logs.push(ev);
  switch (ev.type) {
    case 'recording_started':
      L.steps = ev.steps.map(s => ({ ...s, actions: [] }));
      break;
    case 'recorded': {
      const acts = L.steps[ev.idx].actions;
      const last = acts[acts.length - 1];
      if (ev.action.type === 'fill' && last && last.type === 'fill' && last.target === ev.action.target) acts.pop();
      acts.push({ type: ev.action.type, target: ev.action.target, message: ev.message });
      break;
    }
    case 'step_change':
      // Recording without Excel creates steps as it goes.
      if (!L.steps[ev.idx]) L.steps.push({ step_id: ev.step_id, name: ev.name, actions: [] });
      L.idx = ev.idx;
      break;
    case 'step_named': if (L.steps[ev.idx]) L.steps[ev.idx].name = ev.name; break;
    case 'recording_saved': L.summary = ev; break;
    case 'run_start':
      L.run_id = ev.run_id;
      L.steps = ev.steps.map(s => ({ ...s, status: 'pending', actions: [] }));
      break;
    case 'step_start': L.steps[ev.idx].status = 'running'; break;
    case 'action': if (L.steps[ev.idx]) L.steps[ev.idx].actions.push(ev); break;
    case 'step_end':
      Object.assign(L.steps[ev.idx], { status: ev.status, ms: ev.ms, screenshot: ev.screenshot, error: ev.error });
      break;
    case 'step_skipped': L.steps[ev.idx].status = 'skipped'; break;
    case 'run_end': L.summary = ev; break;
    case 'session_end': L.done = true; L.status = ev.status; break;
  }
}

// ------------------------------------------------------------------ render: sidebar
function renderCaseList() {
  const ul = $('#case-list');
  if (!S.cases.length) { ul.innerHTML = '<li class="none">No test cases yet. Upload an Excel to begin.</li>'; return; }
  ul.innerHTML = S.cases.map(c => {
    const on = S.sel && S.sel.suite_id === c.suite_id && S.sel.case_id === c.case_id;
    const badge = c.last_run ? `<span class="badge ${c.last_run.status}">${c.last_run.status}</span>`
      : c.recorded ? '<span class="badge recorded">recorded</span>' : '<span class="badge new">new</span>';
    return `<li class="${on ? 'on' : ''}" data-suite="${esc(c.suite_id)}" data-case="${esc(c.case_id)}">
      <div class="cid"><span>${esc(c.case_id)}</span>${badge}</div>
      <div class="ctitle">${esc(c.title)}</div>
      <div class="cfile">${esc(c.filename)} · ${c.steps} steps</div></li>`;
  }).join('');
  ul.querySelectorAll('li[data-suite]').forEach(li => li.onclick = () => selectCase(li.dataset.suite, li.dataset.case));
}

// ------------------------------------------------------------------ render: main
function renderMain() {
  const main = $('#main');
  const notice = S.notice ? `<div class="notice ${S.notice.kind}">${S.notice.html}</div>` : '';
  if (S.newForm) { renderNewForm(main, notice); return; }
  if (!S.detail) {
    main.innerHTML = `${notice}<div class="hero">
      <h1>Record once, then automate</h1>
      <p>Upload a test script, perform it once while the studio watches, then replay it automatically whenever you need, with a log of every step.
        Try the <a href="/api/sample?name=acme">Acme Mail sample</a> (built-in demo app) or the <a href="/api/sample?name=outlook">Outlook sample</a> (sends a real email).</p>
      <div class="how">
        <div><span class="n">1</span><b>Upload the test Excel</b><span>Steps, test data and expected results come from the same template the implementation team uses. <a href="/api/sample">Download the sample</a>.</span></div>
        <div><span class="n">2</span><b>Record the steps</b><span>A browser opens with a toolbar showing each Excel step. Perform it; every click and entry is captured against that step.</span></div>
        <div><span class="n">3</span><b>Run automatically</b><span>The studio repeats the recording using the Excel test data, logs each action, screenshots each step and fills in the results.</span></div>
      </div></div>`;
    return;
  }
  const { case: c, suite, recording, runs, target } = S.detail;
  const busy = !!S.session;
  const recorded = !!recording;
  const needsSignin = !target.is_demo;
  const signedIn = target.has_profile;
  const stepChip = (num, done, doneText, todoText) =>
    `<span class="st ${done ? 'done' : ''}"><i>${done ? '✓' : num}</i>${done ? doneText : todoText}</span>`;
  const k = needsSignin ? 1 : 0;
  const freeform = !!c.freeform;
  const firstChip = freeform
    ? `<span class="st done"><i>✓</i>Test created${c.steps.length ? ` (${c.steps.length} steps)` : ''}</span>`
    : `<span class="st done"><i>✓</i>Excel uploaded (${c.steps.length} steps)</span>`;
  main.innerHTML = `${notice}
    <div class="case-head">
      <div>
        <div class="eyebrow">${freeform ? `Recorded without Excel · created ${esc(fmtDate(suite.uploaded_at))}`
          : `${esc(suite.filename)} · uploaded ${esc(fmtDate(suite.uploaded_at))}`}</div>
        <h1>${esc(c.id)} · ${esc(c.title)}</h1>
        ${c.description ? `<p>${esc(c.description)}</p>` : ''}
        <div class="target">Application under test: <code>${esc(target.url)}</code>
          ${needsSignin ? (signedIn ? '<span class="badge passed">sign-in saved</span>' : '<span class="badge stopped">not signed in yet</span>') : '<span class="badge new">built-in demo app</span>'}</div>
      </div>
      <div class="case-actions">
        ${needsSignin ? `<button class="btn" id="btn-signin" data-needs-idle ${busy ? 'disabled' : ''}>${signedIn ? 'Sign in again' : 'Sign in'}</button>` : ''}
        <button class="btn ${recorded ? '' : 'rec'}" id="btn-record" data-needs-idle ${busy ? 'disabled' : ''}><span class="dot"></span>${recorded ? 'Re-record' : 'Record steps'}</button>
        <button class="btn primary" id="btn-run" data-needs-idle ${busy || !recorded ? 'disabled' : ''} title="${recorded ? '' : 'Record the steps first'}">▶ Run automated</button>
      </div>
    </div>
    <div class="flow">
      ${firstChip}<span class="arrow">→</span>
      ${needsSignin ? `${stepChip(2, signedIn, 'Signed in to the application', 'Sign in once')}<span class="arrow">→</span>` : ''}
      ${stepChip(2 + k, recorded, `Recorded ${esc(fmtDate(recording && recording.recorded_at))}`, 'Record the steps once')}<span class="arrow">→</span>
      ${stepChip(3 + k, runs.length, `${runs.length} automated run(s)`, 'Run automatically')}
    </div>
    <div id="run-form-slot"></div>
    <div id="live"></div>
    <div class="tabs">
      <button data-tab="steps" class="${S.tab === 'steps' ? 'on' : ''}">${freeform ? 'Steps' : 'Excel steps'} <span class="count">${c.steps.length}</span></button>
      <button data-tab="recording" class="${S.tab === 'recording' ? 'on' : ''}">Recorded automation ${recorded ? `<span class="count">${countActions(recording)}</span>` : ''}</button>
      <button data-tab="runs" class="${S.tab === 'runs' ? 'on' : ''}">Run history <span class="count">${runs.length}</span></button>
    </div>
    <div id="tab-body"></div>`;

  $('#btn-record').onclick = () => {
    if (recorded && !confirm('Replace the existing recording with a new one?')) return;
    startRecording();
  };
  $('#btn-run').onclick = () => { S.runForm = !S.runForm; renderRunForm(); };
  const signin = $('#btn-signin');
  if (signin) signin.onclick = startSignin;
  main.querySelectorAll('[data-tab]').forEach(b => b.onclick = () => { S.tab = b.dataset.tab; renderMain(); });
  renderRunForm();
  renderLive();
  renderTab();
}

const countActions = rec => rec.steps.reduce((n, s) => n + s.actions.length, 0);

function renderTab() {
  const body = $('#tab-body');
  const { case: c, recording, runs } = S.detail;
  if (S.tab === 'steps') {
    if (!c.steps.length) {
      body.innerHTML = `<div class="card empty-box">No steps yet. Click <b>Record steps</b> and do the test once.
        The steps are created while you record: use <b>Next step</b> on the toolbar to start each new step.</div>`;
      return;
    }
    const last = runs[0] ? Object.fromEntries((runs[0].steps || []).map(s => [s.step_id, s.status])) : {};
    const script = `<div class="script-link"><a href="/api/suites/${esc(S.detail.suite.id)}/workbook">⭳ Download ${c.freeform ? 'as an Excel test script' : 'the test script (.xlsx)'}</a>
      ${c.freeform ? '<span class="faint">· typed values are test data you can change in the run form</span>' : ''}</div>`;
    body.innerHTML = `${script}<div class="card"><table class="grid">
      <thead><tr><th>Step</th><th>Process step</th><th>Description</th><th>Test data</th><th>Expected result</th><th>Last run</th></tr></thead>
      <tbody>${c.steps.map(s => `<tr>
        <td class="sid">${esc(s.step_id)}</td>
        <td>${esc(s.name)}</td>
        <td class="pre">${esc(s.description)}</td>
        <td>${kv(s.data)}</td>
        <td>${esc(s.expected)}</td>
        <td>${last[s.step_id] ? `<span class="badge ${last[s.step_id]}">${last[s.step_id]}</span>` : '<span class="faint">–</span>'}</td>
      </tr>`).join('')}</tbody></table></div>`;
  } else if (S.tab === 'recording') {
    if (!recording) {
      body.innerHTML = `<div class="card empty-box">Not recorded yet. Click <b>Record steps</b>, perform the test once in the browser that opens, and the automation appears here.</div>`;
      return;
    }
    const warn = (recording.warnings || []).length
      ? `<div class="notice warn" style="margin:12px 16px 0"><b>Check this recording before relying on it:</b><ul>${recording.warnings.map(w => `<li>${esc(w)}</li>`).join('')}</ul></div>`
      : '';
    body.innerHTML = `<div class="card">${warn}
      <div class="rec-step faint">Recorded ${esc(fmtDate(recording.recorded_at))} · ${countActions(recording)} actions ·
        ${c.freeform
          ? `${recording.linked_parameters} typed value(s) saved as test data <span class="param">\${…}</span>, so you can change them in the run form.`
          : `${recording.linked_parameters} value(s) linked to Excel test data <span class="param">\${…}</span>, so changing the Excel data changes what the automation types.`}</div>
      ${recording.steps.map(s => `<div class="rec-step">
        <h3>${esc(s.step_id)} <span class="muted" style="font-weight:500">${esc(s.name)}</span></h3>
        ${s.actions.length ? s.actions.map(renderAction).join('') : '<div class="faint">No actions recorded for this step.</div>'}
      </div>`).join('')}</div>`;
  } else {
    if (!runs.length) {
      body.innerHTML = `<div class="card empty-box">No automated runs yet.</div>`;
      return;
    }
    body.innerHTML = `<div class="card"><table class="grid">
      <thead><tr><th>Run</th><th>Started</th><th>Result</th><th>Steps</th><th>Duration</th><th></th></tr></thead>
      <tbody>${runs.map(r => `<tr>
        <td class="mono">${esc(r.id)}</td>
        <td>${esc(fmtDate(r.started_at))}</td>
        <td><span class="badge ${r.status}">${r.status}</span></td>
        <td>${r.passed ?? 0} passed · ${r.failed ?? 0} failed · ${r.skipped ?? 0} skipped</td>
        <td>${secs(r.duration_ms)}</td>
        <td style="text-align:right;white-space:nowrap">
          <button class="btn small" data-view="${esc(r.id)}">View log</button>
          ${r.results_xlsx ? `<a class="btn small" href="${esc(r.results_xlsx)}">Results.xlsx</a>` : ''}
        </td></tr>`).join('')}</tbody></table></div>`;
    body.querySelectorAll('[data-view]').forEach(b => b.onclick = () => viewRun(b.dataset.view));
  }
}

function kv(data) {
  const e = Object.entries(data || {});
  return e.length ? `<div class="kv">${e.map(([k, v]) => `<span>${esc(k)}</span><code>${esc(v) || '<i class="faint">empty</i>'}</code>`).join('')}</div>` : '<span class="faint">–</span>';
}

function withParams(s) {
  return esc(s).replace(/\$\{([^}]+)\}/g, '<span class="param">${$1}</span>');
}

function describeAction(a) {
  const val = a.masked && !a.param ? '••••••' : a.value;
  switch (a.type) {
    case 'goto': return `Open <span class="mono">${esc(a.url)}</span>`;
    case 'click': return `Click ${esc(a.target)}`;
    case 'fill': return `Type “${withParams(val)}” into ${esc(a.target)}`;
    case 'press': return `Press ${esc(a.key)} in ${esc(a.target)}`;
    case 'select': return `Select “${withParams(val)}” in ${esc(a.target)}`;
    case 'expect_text': return `Check that “${withParams(a.text)}” is visible`;
    case 'scroll': return `Scroll ${a.page ? 'the page' : esc(a.target)} to ${a.y || 0} px${a.x ? ` (across ${a.x} px)` : ''} <span class="chip">best effort</span>`;
    default: return esc(a.type);
  }
}

function renderAction(a) {
  const loc = a.locators && a.locators[0];
  const locText = loc ? (loc.kind === 'role' ? `role=${loc.role} name="${loc.value}"` : `${loc.kind}="${loc.value}"`) : '';
  const labels = { goto: 'open', click: 'click', fill: 'type', press: 'key', select: 'select', expect_text: 'check', scroll: 'scroll' };
  return `<div class="act"><span class="type ${a.type}">${labels[a.type] || a.type}</span>
    <div>${describeAction(a)}${a.navigates ? '<span class="chip">loads page</span>' : ''}
    ${locText ? `<div class="loc">${esc(locText)}${a.locators.length > 1 ? ` <span title="Fallback locators used if the first one is not found">+${a.locators.length - 1} fallback</span>` : ''}</div>` : ''}</div></div>`;
}

// ------------------------------------------------------------------ run options
function renderRunForm() {
  const slot = $('#run-form-slot');
  if (!slot) return;
  if (!S.runForm || S.session) { slot.innerHTML = ''; return; }
  const { case: c, recording } = S.detail;
  const linked = new Set();
  recording.steps.forEach(s => s.actions.forEach(a => a.param && linked.add(a.param)));
  const data = {};
  c.steps.forEach(s => Object.entries(s.data).forEach(([k, v]) => { if (!(k in data)) data[k] = v; }));
  const keys = Object.keys(data).sort((a, b) => linked.has(b) - linked.has(a));
  slot.innerHTML = `<div class="card run-form">
    <h3>Run ${esc(c.id)} automatically</h3>
    <div class="muted">Values come from the Excel. Change them here to try the same automation with different data.</div>
    <div class="opts">
      <label><input type="checkbox" id="opt-headed" checked> Show the browser while it runs</label>
      <label>Speed <select id="opt-speed"><option value="0">Fast</option><option value="250" selected>Normal</option><option value="700">Slow (demo)</option></select></label>
      ${S.detail.target.is_demo ? '<label><input type="checkbox" id="opt-reset" checked> Reset Acme Mail test data first</label>' : ''}
    </div>
    ${S.detail.target.is_demo ? '' : `<div class="notice warn" style="margin:0 0 14px">This runs against the real application at <b>${esc(S.detail.target.url)}</b>: anything the test does (such as sending an email) really happens.</div>`}
    ${keys.length ? `<div class="data-grid">${keys.map(k => `
      <label for="d-${esc(k)}">${esc(k)}${linked.has(k) ? '' : ' <span class="chip">not used</span>'}</label>
      <input id="d-${esc(k)}" data-key="${esc(k)}" value="${esc(data[k])}" type="${/pass/i.test(k) ? 'password' : 'text'}">`).join('')}</div>` : ''}
    <div class="foot"><button class="btn" id="rf-cancel">Cancel</button><button class="btn primary" id="rf-start">▶ Start run</button></div>
  </div>`;
  $('#rf-cancel').onclick = () => { S.runForm = false; renderRunForm(); };
  $('#rf-start').onclick = startRun;
}

// ------------------------------------------------------------------ live panel
function renderLive() {
  const box = $('#live');
  if (!box) return;
  const L = S.live;
  if (!L) { box.innerHTML = ''; return; }
  const isRun = L.kind === 'run';
  const doneSteps = L.steps.filter(s => ['passed', 'failed', 'skipped', 'stopped'].includes(s.status)).length;
  const pct = isRun && L.steps.length ? Math.round(doneSteps / L.steps.length * 100) : 0;
  const final = L.summary && L.summary.status;
  const isSignin = L.kind === 'signin';
  const title = L.viewing ? `Run ${esc(L.run_id)}`
    : isRun ? (L.done ? `Run ${esc(L.run_id || '')} finished` : `Running automatically ${L.run_id ? `<span class="mono faint">${esc(L.run_id)}</span>` : ''}`)
    : isSignin ? (L.done ? 'Sign-in finished' : 'Sign-in in progress')
    : (L.done ? 'Recording finished' : 'Recording in progress');
  const where = S.config.live_view_url ? 'in the live browser view above' : 'in the browser window that opened';
  const freeform = !!(S.detail && S.detail.case.freeform);
  const hint = isSignin
    ? `Sign in to the application ${where}, including any MFA prompt. Then click <b>Save sign-in</b> on its toolbar. Your password is not recorded or stored by the studio.`
    : freeform
    ? `Do the test ${where}. On the toolbar in the bottom-right corner, you can name each step. Use <b>+ Add check</b> to mark text that proves a step worked, <b>Next step</b> to start a new step, and <b>Finish recording</b> when you are done.`
    : `Do each Excel step ${where}. The toolbar in the bottom-right corner shows the current step: use <b>+ Add check</b> to mark text that proves the step worked, then <b>Next step</b>.`;

  box.innerHTML = `<div class="card live ${isSignin ? 'record' : L.kind}">
    <div class="live-head">
      ${L.done ? '' : '<span class="dotlive"></span>'}
      <div class="title">${title}${final ? ` <span class="badge ${final}">${final}</span>` : ''}</div>
      ${L.done ? '<button class="btn small" id="live-close">Close</button>'
        : `<button class="btn small" id="live-stop">${isRun ? 'Stop run' : isSignin ? 'Cancel sign-in' : 'Stop recording'}</button>`}
    </div>
    ${isRun ? `<div class="progress ${final || ''}"><div style="width:${L.done ? 100 : pct}%"></div></div>` : ''}
    ${!isRun && !L.done ? `<div class="live-hint">${hint}</div>` : ''}
    <div class="live-body">
      <div class="steps-col">${isSignin ? `<div class="empty-box">${L.done ? 'Done.' : 'Waiting for you to sign in…'}</div>`
        : L.steps.map((s, i) => renderLiveStep(L, s, i)).join('') || '<div class="empty-box">Starting…</div>'}</div>
      <div class="console" id="console">${L.logs.map(renderLog).join('')}</div>
    </div>
    ${renderSummary(L)}
  </div>`;
  const con = $('#console');
  con.scrollTop = con.scrollHeight;
  const stop = $('#live-stop');
  if (stop) stop.onclick = stopSession;
  const close = $('#live-close');
  if (close) close.onclick = () => { S.live = null; renderLive(); };
  const sumRun = $('#sum-run');
  if (sumRun) sumRun.onclick = () => { S.live = null; S.runForm = true; renderMain(); };
  box.querySelectorAll('.thumb').forEach(img => img.onclick = () => openLightbox(img.src));
}

function renderLiveStep(L, s, i) {
  if (L.kind === 'record') {
    const current = !L.done && i === L.idx;
    const cls = current ? 'current' : (s.actions.length ? 'passed' : '');
    return `<div class="lstep ${cls}"><div class="row"><span class="ico">${s.actions.length && !current ? '✓' : i + 1}</span>
      <div class="nm">${esc(s.name)}<small>${esc(s.step_id)}${current ? ' · recording now' : ''}</small></div>
      <span class="ms">${s.actions.length} action${s.actions.length === 1 ? '' : 's'}</span></div>
      ${s.actions.length ? `<ul>${s.actions.map(a => `<li>${esc(a.message)}</li>`).join('')}</ul>` : ''}</div>`;
  }
  const icons = { passed: '✓', failed: '✕', skipped: '–', stopped: '■', running: '…' };
  return `<div class="lstep ${s.status}"><div class="row"><span class="ico">${icons[s.status] || i + 1}</span>
    <div class="nm">${esc(s.name)}<small>${esc(s.step_id)} · ${s.status}</small></div>
    <span class="ms">${secs(s.ms)}</span>
    ${s.screenshot ? `<img class="thumb" src="${esc(s.screenshot)}" alt="Screenshot after ${esc(s.step_id)}">` : ''}</div>
    ${s.error ? `<div class="err">${esc(s.error)}</div>` : ''}</div>`;
}

function renderLog(ev) {
  const lvl = ev.level || 'info';
  const label = { info: 'INFO', success: 'OK', warn: 'WARN', error: 'ERROR' }[lvl] || lvl.toUpperCase();
  const stepLine = ev.type === 'step_start';
  let extra = '';
  if (ev.type === 'action' && ev.status === 'pass') extra = ` <span class="loc">(${ev.ms}ms${ev.locator ? ` · ${esc(ev.locator)}` : ''}${ev.note ? ` · ${esc(ev.note)}` : ''})</span>`;
  return `<div class="ln ${lvl} ${stepLine ? 'step' : ''}"><span class="t">${fmtTime(ev.ts)}</span> <span class="lv">${label}</span>${esc(ev.message)}${extra}</div>`;
}

function renderSummary(L) {
  const s = L.summary;
  if (!s) return '';
  if (L.kind === 'record') {
    return `<div class="summary passed">✓ ${esc(s.message)} <button class="btn small primary" id="sum-run" data-needs-idle ${S.session ? 'disabled' : ''}>▶ Run it automatically</button></div>`;
  }
  return `<div class="summary ${s.status}"><b>${s.status === 'passed' ? '✓' : '✕'} ${esc(s.message)}</b>
    ${s.results_url ? `<a class="btn small" href="${esc(s.results_url)}">Download results Excel</a>` : ''}</div>`;
}

async function viewRun(runId) {
  const run = await api(`/api/runs/${runId}`);
  const L = newLive('run', run.id);
  (run.events || []).forEach(ev => applyEvent(L, ev));
  L.done = true;
  L.viewing = true;
  if (!L.summary) L.summary = { status: run.status, message: `Run ${run.status}`, results_url: run.results_xlsx };
  S.live = L;
  renderLive();
  $('#live').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function openLightbox(src) {
  const lb = $('#lightbox');
  $('img', lb).src = src;
  lb.hidden = false;
  lb.onclick = () => { lb.hidden = true; };
}

// ------------------------------------------------------------------ boot
(async function init() {
  setupUpload();
  $('#btn-new-rec').onclick = showNewForm;
  S.config = await api('/api/config').catch(() => S.config);
  await loadCases();
  const { active } = await api('/api/status');
  if (active) {
    await selectCase(active.suite_id, active.case_id);
    attach(active);
  } else if (S.cases.length) {
    await selectCase(S.cases[0].suite_id, S.cases[0].case_id);
  } else {
    renderMain();
  }
})();
