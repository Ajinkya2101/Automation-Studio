// Automation Studio dashboard: upload, record, run and read every run back.
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtTime = ts => new Date(ts * 1000).toLocaleTimeString([], { hour12: false }) + '.' + String(Math.floor((ts % 1) * 1000)).padStart(3, '0');
const fmtDate = iso => iso ? new Date(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : '';
const secs = ms => ms == null ? '' : `${(ms / 1000).toFixed(1)}s`;
const plural = (n, one, many = one + 's') => `${n} ${n === 1 ? one : many}`;
const hostOf = url => { try { return new URL(url).host; } catch { return url || 'the application'; } };

const ICON = {
  check: 'M9 16.2 4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4L9 16.2Z',
  x: 'M19 6.4 17.6 5 12 10.6 6.4 5 5 6.4 10.6 12 5 17.6 6.4 19 12 13.4 17.6 19 19 17.6 13.4 12 19 6.4Z',
  play: 'M8 5v14l11-7L8 5Z',
  stop: 'M7 7h10v10H7z',
  dash: 'M6 11h12v2H6z',
  download: 'M12 16 7 11l1.4-1.4 2.6 2.6V4h2v8.2l2.6-2.6L17 11l-5 5Zm-7 4v-2h14v2H5Z',
  warn: 'M1 21 12 2l11 19H1Zm11-3a1.2 1.2 0 1 0 0-2.4 1.2 1.2 0 0 0 0 2.4Zm-1-4h2V9h-2v5Z',
  key: 'M7 15a3 3 0 1 1 0-6 3 3 0 0 1 0 6Zm5.7-5A6 6 0 1 0 12.7 14H15v3h3v-3h3v-4h-8.3Z',
  more: 'M6 10a2 2 0 1 1 0 4 2 2 0 0 1 0-4Zm6 0a2 2 0 1 1 0 4 2 2 0 0 1 0-4Zm6 0a2 2 0 1 1 0 4 2 2 0 0 1 0-4Z',
  caret: 'm7 10 5 5 5-5H7Z',
};
const icon = (name, size = 16) => `<svg class="ico" viewBox="0 0 24 24" width="${size}" height="${size}" aria-hidden="true"><path fill="currentColor" d="${ICON[name]}"/></svg>`;

async function api(path, opts = {}) {
  let res;
  try { res = await fetch(path, opts); } catch {
    throw new Error('The studio did not answer. Check your connection and that the studio is running, then try again.');
  }
  if (res.status === 401) { location.href = '/login'; throw new Error('Signed out'); }
  const type = res.headers.get('content-type') || '';
  const body = type.includes('json') ? await res.json() : await res.text();
  if (!res.ok) throw new Error((body && body.detail) || body || res.statusText);
  return body;
}

const S = {
  config: { live_view_url: '' },
  cases: [],
  caseFilter: '',
  sel: null,        // { suite_id, case_id }
  detail: null,     // case detail from the API
  tab: 'steps',
  viewRun: null,    // run shown in the Last run tab (null: the newest)
  notice: null,     // { kind, html }
  runForm: false,
  session: null,    // active session info
  es: null,         // EventSource
  live: null,       // live session model
  user: null,
  others: [],       // sessions other users have in progress
  activity: null,
  actFilter: { q: '', action: '' },
  actLimit: 200,
  admin: null,
  adminFilter: { user: '', status: '', q: '' },
  help: false,
  showLocators: false,
  tpEdit: null,     // { si, ai } while a click's text is being made test data
  galleries: {},
};
let liveSeq = 0;

// ------------------------------------------------------------------ feedback
function announce(text) {
  const a = $('#announcer');
  a.textContent = '';
  setTimeout(() => { a.textContent = text; }, 30);
}

function showError(err) {
  S.notice = { kind: 'err', html: esc(err && err.message || err) };
  renderMain();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function confirmDialog({ title, body, ok = 'OK', danger = false, focusCancel = false }) {
  const dlg = $('#confirm');
  $('#cf-title').textContent = title;
  $('#cf-body').innerHTML = body;
  const okBtn = $('#cf-ok');
  okBtn.textContent = ok;
  okBtn.className = `btn ${danger ? 'danger-fill' : 'primary'}`;
  dlg.returnValue = '';
  dlg.showModal();
  (danger || focusCancel ? $('#cf-cancel') : okBtn).focus();
  return new Promise(resolve => dlg.addEventListener('close', () => resolve(dlg.returnValue === 'ok'), { once: true }));
}

// ------------------------------------------------------------------ data
async function loadCases() {
  S.cases = await api('/api/cases');
  renderCaseList();
}

async function selectCase(suite_id, case_id, keepLive = false) {
  const same = S.sel && S.sel.suite_id === suite_id && S.sel.case_id === case_id;
  S.newForm = false;
  S.activity = null;
  S.admin = null;
  S.help = false;
  S.sel = { suite_id, case_id };
  if (!same) { S.notice = null; S.viewRun = null; S.tpEdit = null; S.tab = null; S.descOpen = false; S.runReason = ''; }
  S.detail = await api(`/api/suites/${suite_id}/cases/${encodeURIComponent(case_id)}`);
  if (!S.tab || (S.tab === 'last' && !S.detail.runs.length)) S.tab = S.detail.runs.length ? 'last' : 'steps';
  if (!same) S.runForm = false;
  // Back on the test whose session is running: show its live panel again, with everything that happened meanwhile.
  if (S.session && S.session.suite_id === suite_id && S.session.case_id === case_id) S.live = S.sessionLive || S.live;
  else if (!keepLive) S.live = null;
  renderCaseList();
  renderMain();
}

async function refresh() {
  await loadCases();
  if (S.sel) await selectCase(S.sel.suite_id, S.sel.case_id, true);
}

const caseUrl = (suffix = '') => `/api/suites/${S.sel.suite_id}/cases/${encodeURIComponent(S.sel.case_id)}${suffix}`;

// ------------------------------------------------------------------ record without Excel
function showNewForm() {
  if (S.session) return;
  Object.assign(S, { newForm: true, activity: null, admin: null, help: false, sel: null, detail: null, live: null, notice: null });
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
    await loadCases();
    await selectCase(res.suite_id, res.case_id);
  } catch (err) {
    $('#nf-error').textContent = err.message;
    btn.disabled = false;
  }
}

function renderNewForm(main, notice) {
  main.innerHTML = `${notice}<form class="card new-form" id="nf-form">
    <h2>Record a test without Excel</h2>
    <p class="muted">Give the test a name and the page where it starts. Then record it: the steps, their test data and an
      Excel test script are created from what you do. You can run it automatically as often as you want.</p>
    <label for="nf-title">Test name</label>
    <input id="nf-title" placeholder="e.g. Update worker phone number" maxlength="120" required>
    <label for="nf-url">Start URL</label>
    <input id="nf-url" placeholder="https://…" inputmode="url" required>
    <div class="hint">For a site that needs a sign-in (e.g. Outlook or Fusion), you can sign in once before you record.</div>
    <div class="err-text" id="nf-error" role="alert"></div>
    <div class="foot"><button type="button" class="btn" data-act="nf-cancel">Cancel</button><button type="submit" class="btn primary" id="nf-create">Create test</button></div>
  </form>`;
  $('#nf-form').onsubmit = e => { e.preventDefault(); createFreeform(); };
}

// ------------------------------------------------------------------ delete
async function deleteCase() {
  const { case: c, runs } = S.detail;
  const ok = await confirmDialog({
    title: `Delete ${S.sel.case_id}?`,
    body: `<p><b>${esc(c.title)}</b></p><p>Its recording${runs.length ? ` and ${plural(runs.length, 'run')} with their screenshots and results` : ''}
      will be deleted too. This can't be undone. The Activity log keeps a record that you deleted it.</p>`,
    ok: 'Delete test case', danger: true,
  });
  if (!ok) return;
  try {
    await api(caseUrl(), { method: 'DELETE' });
  } catch (err) { showError(err); return; }
  Object.assign(S, { sel: null, detail: null, live: null, notice: null });
  await loadCases();
  announce('Test case deleted.');
  if (S.cases.length) await selectCase(S.cases[0].suite_id, S.cases[0].case_id);
  else renderMain();
}

// ------------------------------------------------------------------ upload
function setupUpload() {
  const dz = $('#dropzone'), input = $('#file-input');
  $('#btn-upload').onclick = () => input.click();
  input.addEventListener('change', () => input.files[0] && upload(input.files[0]));
  ['dragenter', 'dragover'].forEach(t => dz.addEventListener(t, e => { e.preventDefault(); dz.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(t => dz.addEventListener(t, e => { e.preventDefault(); dz.classList.remove('over'); }));
  dz.addEventListener('drop', e => e.dataTransfer.files[0] && upload(e.dataTransfer.files[0]));
}

async function upload(file) {
  const dup = S.cases.find(c => c.filename === file.name);
  if (dup && !await confirmDialog({
    title: `${file.name} is already uploaded`,
    body: `<p>It was uploaded ${esc(fmtDate(dup.uploaded_at))}${dup.created_by ? ` by ${esc(dup.created_by)}` : ''}. Uploading it again adds a
      separate copy with its own recording and runs; the earlier one stays as it is.</p>`,
    ok: 'Upload a copy', focusCancel: true,
  })) { $('#file-input').value = ''; return; }
  const dz = $('#dropzone'), hint = $('#upload-hint'), before = hint.innerHTML;
  dz.classList.add('busy');
  dz.setAttribute('aria-busy', 'true');
  $('#btn-upload').disabled = true;
  hint.textContent = `Uploading ${file.name}…`;
  const fd = new FormData();
  fd.append('file', file);
  try {
    const res = await api('/api/suites', { method: 'POST', body: fd });
    await loadCases();
    await selectCase(res.suite_id, res.cases[0]);
    if (res.warnings.length) {
      S.notice = { kind: 'warn', html: `<b>${esc(file.name)}</b> uploaded with ${plural(res.warnings.length, 'note')}:<ul>${res.warnings.map(w => `<li>${esc(w)}</li>`).join('')}</ul>` };
      renderMain();
    }
    announce(`${file.name} uploaded.`);
  } catch (err) {
    S.notice = { kind: 'err', html: `Upload of <b>${esc(file.name)}</b> failed: ${esc(err.message)}` };
    renderMain();
  } finally {
    dz.classList.remove('busy');
    dz.removeAttribute('aria-busy');
    $('#btn-upload').disabled = false;
    hint.innerHTML = before;
    $('#file-input').value = '';
  }
}

// ------------------------------------------------------------------ sessions
async function startRecording() {
  const { recording, target } = S.detail;
  if (recording) {
    const ok = await confirmDialog({
      title: 'Record this test again?',
      body: `<p>The new recording replaces the one${recording.recorded_by ? ` by <b>${esc(recording.recorded_by)}</b>` : ''} from
        ${esc(fmtDate(recording.recorded_at))}. Runs use the new recording from then on.</p>
        <p>Everything you do while recording really happens in <b>${esc(hostOf(target.url))}</b>.</p>`,
      ok: 'Re-record', focusCancel: true,
    });
    if (!ok) return;
  }
  try { attach(await api(caseUrl('/record'), { method: 'POST' })); } catch (err) { showError(err); }
}

async function startSignin() {
  try { attach(await api(caseUrl('/signin'), { method: 'POST' })); } catch (err) { showError(err); }
}

async function startRun() {
  const data = {};
  $$('#run-form [data-key]').forEach(inp => { data[inp.dataset.key] = inp.value; });
  // A used field left empty types nothing: the sign-in fails with an invalid user name, a search finds everything.
  const { defaults = {}, linked = new Set() } = S.runFormInfo || {};
  const emptied = Object.keys(data).filter(k => linked.has(k) && !data[k].trim() && String(defaults[k] ?? '').trim());
  if (emptied.length && !await confirmDialog({
    title: 'Run with empty test data?',
    body: `<p>These fields are empty but had a value when the test was recorded:</p><ul>${emptied.map(k => `<li>${esc(k)}</li>`).join('')}</ul>
      <p>The run will type nothing there (a sign-in with an empty user name fails).</p>`,
    ok: 'Run anyway',
  })) return;
  const btn = $('#rf-start');
  btn.disabled = true;
  try {
    const s = await api(caseUrl('/run'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ headed: $('#opt-headed').checked, slow_mo: Number($('#opt-speed').value), data }),
    });
    S.runForm = false;
    attach(s);
  } catch (err) { btn.disabled = false; showError(err); }
}

async function stopSession() {
  const btn = $('#live-stop');
  if (btn) { btn.disabled = true; btn.textContent = 'Stopping…'; }
  if (S.session) await api(`/api/sessions/${S.session.id}/stop`, { method: 'POST' }).catch(showError);
}

function attach(session) {
  if (S.es) S.es.close();
  S.session = session;
  // The session's own model: it keeps collecting events while the user looks at another test,
  // and is shown again when they come back.
  const live = newLive(session.kind, session.run_id);
  S.live = S.sessionLive = live;
  setStatus();
  showScreen(session);
  renderMain();
  window.scrollTo({ top: 0, behavior: 'smooth' });
  announce({ signin: 'Sign-in started.', record: 'Recording started.', run: 'Run started.' }[session.kind] || '');
  const es = new EventSource(`/api/sessions/${session.id}/events`);
  es.onmessage = e => {
    const ev = JSON.parse(e.data);
    applyEvent(live, ev);
    if (S.live === live) patchLive(live, ev);
  };
  es.addEventListener('end', async () => {
    es.close();
    S.es = null;
    S.session = null;
    S.sessionLive = null;
    setStatus();
    showScreen(null);
    const onCase = S.sel && S.sel.suite_id === session.suite_id && S.sel.case_id === session.case_id;
    if (onCase && live.kind === 'run') { S.live = null; S.tab = 'last'; S.viewRun = null; }
    if (onCase && live.kind === 'record') S.tab = 'recording';
    await refresh();
    renderMain();
    if (live.kind === 'run') {
      const run = S.detail && S.detail.runs.find(r => r.id === live.run_id);
      announce(run ? diagnose(run).title : `Run ${live.status}.`);
    } else announce(live.kind === 'record' ? 'Recording finished.' : 'Sign-in finished.');
  });
  S.es = es;
}

function setStatus() {
  const pill = $('#status-pill');
  const s = S.session;
  const cid = s && s.case_id ? ` ${esc(s.case_id)}` : '';
  const kinds = { signin: 'signing in', record: 'recording', run: 'running' };
  const others = S.others.length ? ` <span class="others">· ${S.others.length} other${S.others.length > 1 ? 's' : ''} busy</span>` : '';
  if (!s) { pill.className = 'pill idle'; pill.innerHTML = `Idle${others}`; }
  else if (s.kind === 'signin') { pill.className = 'pill signin'; pill.innerHTML = `<span class="blink"></span>Signing in${others}`; }
  else if (s.kind === 'record') { pill.className = 'pill record'; pill.innerHTML = `<span class="blink"></span>Recording${cid}${others}`; }
  else { pill.className = 'pill run'; pill.innerHTML = `<span class="blink"></span>Running${cid}${others}`; }
  pill.title = S.others.length ? 'Also in progress: ' + S.others.map(o => `${o.user || 'someone'} ${kinds[o.kind] || o.kind}`).join(', ') : '';
  $$('[data-needs-idle]').forEach(b => {
    const off = !!s || b.hasAttribute('data-blocked');
    if (b.classList.contains('stage')) b.setAttribute('aria-disabled', String(off));  // stays focusable: its text says why
    else b.disabled = off;
  });
}

// What other users are doing, every few seconds; and our own session if it was started from another tab.
async function pollStatus() {
  const st = await api('/api/status').catch(() => null);
  if (!st) return;
  const before = S.others.map(o => o.id).join();
  S.others = st.others || [];
  if (S.others.map(o => o.id).join() !== before) {
    setStatus();
    loadCases().catch(() => {});
  }
  if (st.active && !S.session) {
    await selectCase(st.active.suite_id, st.active.case_id);
    attach(st.active);
  }
}

// Live browser view: the automated browser runs on the container's virtual screen; noVNC shows it
// here, beside the steps and the log, so you can sign in, record and watch runs.
function showScreen(session) {
  const box = $('#screen'), frame = $('#screen-frame');
  const url = S.config.live_view_url;
  const shown = !!(session && url && session.live_view);
  $('#layout').classList.toggle('session', shown);
  if (!shown) {
    box.hidden = true;
    frame.removeAttribute('src');
    return;
  }
  const hints = {
    signin: 'Click inside the view to type. Save the sign-in on its toolbar.',
    record: 'Do the test here. Everything you do really happens.',
    run: 'Watch the automation. No input needed.',
  };
  box.dataset.kind = session.kind;
  $('#screen-title').textContent = { signin: 'Sign in', record: 'Recording', run: 'Automated run' }[session.kind];
  $('#screen-hint').textContent = hints[session.kind] || '';
  $('#screen-pop').href = url;
  if (frame.getAttribute('src') !== url) frame.src = url;
  box.hidden = false;
}

// ------------------------------------------------------------------ live model
function newLive(kind, run_id = null) {
  return { uid: ++liveSeq, kind, run_id, steps: [], idx: 0, logs: [], rendered: 0, done: false, status: 'running', summary: null };
}

function applyEvent(L, ev) {
  L.logs.push(ev);
  switch (ev.type) {
    case 'recording_started':
      L.steps = ev.steps.map(s => ({ ...s, actions: [] }));
      break;
    case 'recorded': {
      if (!L.steps[ev.idx]) break;
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
    case 'step_start': if (L.steps[ev.idx]) L.steps[ev.idx].status = 'running'; break;
    case 'action': if (L.steps[ev.idx]) L.steps[ev.idx].actions.push(ev); break;
    case 'step_end':
      if (L.steps[ev.idx]) Object.assign(L.steps[ev.idx], { status: ev.status, ms: ev.ms, screenshot: ev.screenshot, error: ev.error });
      break;
    case 'step_skipped': if (L.steps[ev.idx]) L.steps[ev.idx].status = 'skipped'; break;
    case 'run_end': L.summary = ev; break;
    case 'session_end': L.done = true; L.status = ev.status; break;
  }
}

// ------------------------------------------------------------------ proof
// Why a passing run of this recording would not prove the test worked (same rule as results.proof_gaps).
function proofGaps(rec) {
  if (!rec) return [];
  const steps = rec.steps || [];
  const gaps = [];
  if (!steps.some(s => (s.actions || []).some(a => a.type === 'expect_text'))) {
    gaps.push('Nothing is checked: no step has a check, so a run only shows that the clicks happened, not that the result appeared.');
  }
  const empty = steps.filter(s => !(s.actions || []).length).map(s => s.step_id);
  if (empty.length) {
    gaps.push(`${plural(empty.length, 'step')} did nothing when recorded (${empty.join(', ')}), so ${empty.length === 1 ? 'it passes' : 'they pass'} without doing anything.`);
  }
  return gaps;
}

const isUnproven = run => !!(run && run.status === 'passed' && S.detail && S.detail.recording
  && (S.detail.recording.recorded_at || '') <= (run.started_at || '') && proofGaps(S.detail.recording).length);

const viewStatus = run => isUnproven(run) ? 'unproven' : (run.display || run.status);
const shortDate = iso => iso ? new Date(iso).toLocaleDateString([], { day: 'numeric', month: 'short' }) : '';
// Engine wording the consultant does not need.
const plainError = s => String(s || '').replace(/\s*\(tried (\d+) locator\(s\)\)/g, (m, n) => ` (looked for it ${n} ways)`);

// ------------------------------------------------------------------ render: sidebar
const STATUS_LABEL = { new: 'not recorded', recorded: 'recorded, not run', passed: 'passed', unproven: 'passed, not proven',
  failed: 'failed', needs_data: 'needs new test data', stopped: 'stopped', running: 'running' };
const SHORT_LABEL = { needs_data: 'needs new data', unproven: 'not proven' };

function statusBadge(display, reason, short = false) {
  const label = (short && SHORT_LABEL[display]) || STATUS_LABEL[display] || display;
  const title = display === 'needs_data' ? `Needs new test data${reason ? `: ${reason}` : ''}` : reason;
  return `<span class="badge ${esc(display)}"${title ? ` title="${esc(title)}"` : ''}>${esc(label)}</span>`;
}

function renderCaseList() {
  const ul = $('#case-list');
  const filter = $('#case-filter');
  filter.hidden = S.cases.length <= 6;
  if (!S.cases.length) { ul.innerHTML = '<li class="none">No test cases yet. Upload an Excel or record without one.</li>'; return; }
  const q = S.caseFilter.trim().toLowerCase();
  const shown = q ? S.cases.filter(c => `${c.case_id} ${c.title} ${c.filename} ${c.created_by || ''}`.toLowerCase().includes(q)) : S.cases;
  ul.innerHTML = shown.map(c => {
    const on = !!(S.sel && S.sel.suite_id === c.suite_id && S.sel.case_id === c.case_id);
    const badge = c.last_run ? statusBadge(c.unproven ? 'unproven' : c.last_run.display,
        c.unproven ? 'The last run passed, but the recording checks nothing or has empty steps' : c.last_run.needs_data, true)
      : c.recorded ? '<span class="badge recorded">recorded</span>' : '<span class="badge new">new</span>';
    return `<li><button type="button" data-act="open-case" data-suite="${esc(c.suite_id)}" data-case="${esc(c.case_id)}" aria-current="${on}" title="${esc(c.title)}">
      <span class="cid"><span>${esc(c.case_id)}</span>${badge}</span>
      <span class="ctitle">${esc(c.title)}</span>
      <span class="cfile">${esc(c.filename)} · ${esc(shortDate(c.uploaded_at))} · ${plural(c.steps, 'step')}${c.created_by ? ` · ${esc(c.created_by)}` : ''}</span></button></li>`;
  }).join('') || `<li class="none">No test case matches “${esc(S.caseFilter)}”.</li>`;
}

// ------------------------------------------------------------------ address bar: where you are, so a reload or a shared link comes back here
function syncHash() {
  let h = '';
  if (S.admin) h = '#/admin';
  else if (S.activity) h = '#/activity';
  else if (S.sel && S.detail) {
    h = `#/case/${encodeURIComponent(S.sel.suite_id)}/${encodeURIComponent(S.sel.case_id)}/${S.tab || 'steps'}`;
    if (S.tab === 'last' && S.viewRun) h += `/${encodeURIComponent(S.viewRun)}`;
  }
  if (h && location.hash !== h) history.replaceState(null, '', h);
}

async function openFromHash() {
  const p = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean).map(decodeURIComponent);
  if (!p.length) return false;
  if (p[0] === 'admin' && S.isAdmin) { await showAdmin(); return true; }
  if (p[0] === 'activity') { await showActivity(); return true; }
  if (p[0] === 'case' && p[2]) {
    if (!S.cases.some(c => c.suite_id === p[1] && c.case_id === p[2])) {
      S.notice = { kind: 'warn', html: 'That link is to a test case you can’t see, or one that was deleted.' };
      return false;
    }
    await selectCase(p[1], p[2]);
    if (['last', 'steps', 'recording', 'runs'].includes(p[3]) && (p[3] !== 'last' || S.detail.runs.length)) {
      S.tab = p[3];
      if (p[3] === 'last' && p[4] && S.detail.runs.some(r => r.id === p[4])) S.viewRun = p[4];
      renderMain();
    }
    return true;
  }
  return false;
}

// ------------------------------------------------------------------ render: main
function renderMain() {
  renderView();
  syncHash();
}

function renderView() {
  const main = $('#main');
  const notice = S.notice ? `<div class="notice ${S.notice.kind}" role="${S.notice.kind === 'err' ? 'alert' : 'status'}"><div>${S.notice.html}</div>
    <button class="x" data-act="dismiss-notice" aria-label="Dismiss">${icon('x', 14)}</button></div>` : '';
  if (S.admin) { renderAdmin(main); return; }
  if (S.activity) { renderActivity(main); return; }
  if (S.newForm) { renderNewForm(main, notice); return; }
  if (S.help || !S.detail) { renderHelp(main, notice); return; }
  renderCase(main, notice);
}

function renderHelp(main, notice) {
  main.innerHTML = `${notice}<div class="hero">
    <h1>Record once, then automate</h1>
    <p>Do a test once while the studio watches. It replays it whenever you need, with a log line per action, a screenshot
      per step and the results written back to Excel.</p>
    <ol class="how">
      <li><b>Start from your test Excel</b><span>Steps, test data and expected results come from the template the implementation team uses. No Excel? Record without one.</span></li>
      <li><b>Record the steps</b><span>A browser opens with a toolbar showing each step. Do the step; every click and entry is captured against it.</span></li>
      <li><b>Run automatically</b><span>The studio repeats the recording with the test data, screenshots each step and shows why a step failed.</span></li>
    </ol>
    <div class="hero-actions">
      <button class="btn primary" data-act="upload">Upload test Excel</button>
      <button class="btn" data-act="new-rec" data-needs-idle><span class="dot" style="color:var(--rec)"></span>Record without Excel</button>
      <a class="btn" href="/api/sample">${icon('download')}Sample script (Outlook)</a>
      ${S.detail && S.help ? '<button class="btn" data-act="close-help">Back to the test case</button>' : ''}
    </div></div>`;
  setStatus();
}

function renderCase(main, notice) {
  const { case: c, suite, recording, runs, target } = S.detail;
  const busy = !!S.session;
  const recorded = !!recording;
  const hasTarget = !!target.url;
  const signedIn = target.has_profile;
  const freeform = !!c.freeform;
  const last = runs[0];
  const lastDisplay = last ? viewStatus(last) : '';
  const allDone = recorded && runs.length > 0;

  // Stages stay focusable while unavailable (aria-disabled), so their "why" can still be read.
  const stage = ({ n, act, done, next, title, sub, blocked, expanded }) => `<button type="button" class="stage ${done ? 'done' : ''} ${next || ''}"
      data-act="${act}" data-needs-idle ${blocked ? 'data-blocked' : ''} aria-disabled="${busy || !!blocked}"
      aria-label="Step ${n}, ${title}${done ? ', done' : ''}: ${sub}"${expanded !== undefined ? ` aria-expanded="${expanded}"` : ''}>
      <span class="n" aria-hidden="true">${done ? icon('check', 14) : n}</span><span class="tx" aria-hidden="true"><b>${title}</b><small>${sub}</small></span></button>`;
  const stages = [
    stage({ n: 1, act: 'signin', done: signedIn, blocked: !hasTarget,
      title: signedIn ? 'Signed in' : 'Sign in',
      sub: !hasTarget ? 'Needs a start URL first' : signedIn ? `Sign-in saved${target.signed_in_at ? ` ${esc(fmtDate(target.signed_in_at))}` : ''}. Click to sign in again` : 'Optional: only if the application asks for a sign-in' }),
    stage({ n: 2, act: 'record', done: recorded, blocked: !hasTarget, next: !recorded && hasTarget ? 'next rec' : '',
      title: recorded ? 'Recorded' : 'Record steps',
      sub: !hasTarget ? 'Needs a start URL first' : recorded ? `${esc(fmtDate(recording.recorded_at))}${recording.recorded_by ? ` by ${esc(recording.recorded_by)}` : ''}. Click to re-record` : 'Do the test once while the studio watches' }),
    stage({ n: 3, act: 'run', done: lastDisplay === 'passed', blocked: !recorded, next: recorded ? 'next run' : '', expanded: S.runForm,
      title: 'Run automated',
      sub: !recorded ? 'Record the steps first' : last ? `${plural(runs.length, 'run')} · last ${esc(STATUS_LABEL[lastDisplay] || lastDisplay)} ${esc(fmtDate(last.started_at))}` : 'Replay the recording with its test data' }),
  ].join('');
  const busyNote = busy ? `<p class="busy-note">${S.session.case_id === c.id ? 'This test case' : `<b>${esc(S.session.case_id)}</b>`} is
    ${{ signin: 'being signed in to', record: 'being recorded', run: 'running' }[S.session.kind] || 'busy'}. Stop it to start something else.</p>` : '';

  const tabs = [
    runs.length ? ['last', 'Last run', ''] : null,
    ['steps', freeform ? 'Steps' : 'Excel steps', c.steps.length],
    ['recording', 'Recorded automation', recorded ? countActions(recording) : ''],
    ['runs', 'Run history', runs.length],
  ].filter(Boolean);

  main.innerHTML = `${notice}
    <div class="case-head">
      <div>
        <h1>${esc(c.id)} · ${esc(c.title)}</h1>
        <div class="meta">${freeform ? `Recorded without Excel, created ${esc(fmtDate(suite.uploaded_at))}`
          : `${esc(suite.filename)}, uploaded ${esc(fmtDate(suite.uploaded_at))}`}${suite.created_by ? ` by ${esc(suite.created_by)}` : ''}${hasTarget
          ? ` · runs on <code title="${esc(target.url)}">${esc(hostOf(target.url))}</code>` : ''}</div>
        ${c.description ? `<p class="desc ${S.descOpen ? '' : 'clamp'}" id="case-desc">${esc(c.description)}</p>${c.description.length > 110
          ? `<button class="link-btn desc-more" data-act="desc-toggle" aria-controls="case-desc" aria-expanded="${!!S.descOpen}">${S.descOpen ? 'Show less' : 'Show the whole description'}</button>` : ''}` : ''}
        ${hasTarget ? '' : '<div class="target"><span class="badge failed">no start URL</span> Add a line such as <code>URL: https://…</code> to the test data of the first step in the Excel and upload it again.</div>'}
      </div>
      <details class="menu more-menu">
        <summary class="menu-btn" aria-label="More actions for this test case">More ${icon('caret')}</summary>
        <div class="menu-list" role="menu">
          <a role="menuitem" href="/api/suites/${esc(suite.id)}/workbook">Download the Excel test script<small>${freeform ? 'Created from the recording' : 'As uploaded'}</small></a>
          ${last ? `<a role="menuitem" href="/api/runs/${esc(last.id)}/issue-log">Download the last run's issue log<small>Errors, screenshots, page snapshots and trace as a ZIP</small></a>` : ''}
          <div class="sep"></div>
          <button role="menuitem" class="danger" data-act="delete" data-needs-idle ${busy ? 'disabled' : ''}>Delete test case…</button>
        </div>
      </details>
    </div>
    <div class="stepper ${allDone ? 'slim' : ''}" role="group" aria-label="Sign in, record, run">${stages}</div>
    ${busyNote}
    <div id="run-form-slot"></div>
    <div id="live"></div>
    <div class="tabs" role="tablist" aria-label="Test case views">
      ${tabs.map(([id, label, count]) => `<button role="tab" id="tab-${id}" aria-controls="tab-body" aria-selected="${S.tab === id}" tabindex="${S.tab === id ? 0 : -1}"
        data-act="tab" data-tab="${id}">${label}${count !== '' ? ` <span class="count">${count}</span>` : ''}</button>`).join('')}
    </div>
    <div id="tab-body" role="tabpanel" aria-labelledby="tab-${S.tab}" tabindex="-1"></div>`;
  renderRunForm();
  renderLive();
  renderTab();
}

const countActions = rec => rec.steps.reduce((n, s) => n + s.actions.length, 0);

function renderTab() {
  const body = $('#tab-body');
  const { case: c, recording, runs } = S.detail;
  delete S.galleries.cmp;
  delete S.galleries.film;
  if (S.tab === 'last') { renderLastRun(body); return; }
  if (S.tab === 'steps') {
    if (!c.steps.length) {
      body.innerHTML = `<div class="card empty-box">No steps yet. Use <b>Record steps</b> above and do the test once.
        The steps are created while you record: use <b>Next step</b> on the toolbar to start each new step.</div>`;
      return;
    }
    const last = runs[0] ? Object.fromEntries((runs[0].steps || []).map(s => [s.step_id, s.status])) : {};
    body.innerHTML = `<div class="script-link"><a href="/api/suites/${esc(S.detail.suite.id)}/workbook">${icon('download')} Download ${c.freeform ? 'as an Excel test script' : 'the test script (.xlsx)'}</a>
      ${c.freeform ? '<span class="faint">Typed values are test data you can change in the run form.</span>' : ''}</div>
      <div class="card table-wrap"><table class="grid">
      <thead><tr><th>Step</th><th>Process step</th><th>Description</th><th>Test data</th><th>Expected result</th><th>Last run</th></tr></thead>
      <tbody>${c.steps.map(s => `<tr>
        <td class="sid">${esc(s.step_id)}</td>
        <td>${esc(s.name)}</td>
        <td class="pre">${esc(s.description)}</td>
        <td>${kv(s.data)}</td>
        <td>${esc(s.expected)}</td>
        <td>${last[s.step_id] ? `<span class="badge ${esc(last[s.step_id])}">${esc(last[s.step_id])}</span>` : '<span class="faint">–</span>'}</td>
      </tr>`).join('')}</tbody></table></div>`;
  } else if (S.tab === 'recording') {
    if (!recording) {
      body.innerHTML = `<div class="card empty-box">Not recorded yet. Use <b>Record steps</b> above, do the test once in the browser that opens, and the automation appears here.</div>`;
      return;
    }
    const warn = (recording.warnings || []).length
      ? `<div class="notice warn" style="margin:12px 16px 0"><div><b>Check this recording before relying on it:</b><ul>${recording.warnings.map(w => `<li>${esc(w)}</li>`).join('')}</ul></div></div>`
      : '';
    body.innerHTML = `<div class="card ${S.showLocators ? '' : 'hide-locators'}" id="rec-card">${warn}
      <div class="rec-head"><span class="grow">Recorded ${esc(fmtDate(recording.recorded_at))}${recording.recorded_by ? ` by ${esc(recording.recorded_by)}` : ''} · ${plural(countActions(recording), 'action')} ·
        ${c.freeform
          ? `${plural(recording.linked_parameters, 'typed value')} saved as test data <span class="param">\${…}</span>, so you can change them in the run form.`
          : `${plural(recording.linked_parameters, 'value')} linked to Excel test data <span class="param">\${…}</span>, so changing the Excel data changes what the automation types.`}</span>
        <label class="toggle"><input type="checkbox" data-act="toggle-locators" ${S.showLocators ? 'checked' : ''}> Show locators</label></div>
      ${recording.steps.map((s, si) => `<div class="rec-step">
        <h2>${esc(s.step_id)} <span>${esc(s.name)}</span></h2>
        ${s.actions.length ? s.actions.map((a, ai) => renderAction(a, si, ai)).join('') : '<div class="faint">No actions recorded for this step.</div>'}
      </div>`).join('')}</div>`;
    const full = $('#tp-full');
    if (full) {
      full.addEventListener('mouseup', () => {
        const sel = String(window.getSelection() || '').trim();
        if (sel) { $('#tp-text').value = sel; $('#tp-key').value = suggestKey(full.textContent, sel); }
      });
      $('#tp-text').focus();
      $('#tp-form').onsubmit = e => { e.preventDefault(); saveTextParam(); };
    }
  } else {
    if (!runs.length) {
      body.innerHTML = `<div class="card empty-box">No automated runs yet.${recording ? ' Use <b>Run automated</b> above to start the first one.' : ''}</div>`;
      return;
    }
    body.innerHTML = `<div class="card table-wrap"><table class="grid">
      <thead><tr><th>Run</th><th>By</th><th>Result</th><th>Steps</th><th>Duration</th><th><span class="sr-only">Downloads</span></th></tr></thead>
      <tbody>${runs.map(r => `<tr class="${S.viewRun === r.id ? 'sel' : ''}">
        <td class="when"><button class="link-btn" data-act="view-run" data-run="${esc(r.id)}" title="${esc(r.id)}: show its steps and screenshots">${esc(fmtDate(r.started_at))}</button></td>
        <td>${esc(r.run_by || '–')}${r.by_admin ? ' <span class="chip" title="Started by an admin, not by a tester">admin run</span>' : ''}</td>
        <td>${statusBadge(viewStatus(r), r.needs_data)}${r.stopped_by ? ` <span class="by">by ${esc(r.stopped_by)}</span>` : ''}
          ${r.needs_data ? `<div class="need-why">${esc(r.needs_data)}</div>` : ''}</td>
        <td>${stepBar(r.steps)}<span class="faint">${r.passed ?? 0} passed · ${r.failed ?? 0} failed · ${r.skipped ?? 0} skipped</span></td>
        <td class="num">${secs(r.duration_ms)}</td>
        <td class="actions-cell">
          ${r.results_xlsx ? `<a class="btn small" href="${esc(r.results_xlsx)}">Results.xlsx</a>` : ''}
          <a class="btn small" href="/api/runs/${esc(r.id)}/issue-log" title="This run's full errors, screenshots, page snapshots, trace, test case and recording as a ZIP">Issue log</a>
        </td></tr>`).join('')}</tbody></table></div>`;
  }
}

const stepBar = steps => (steps || []).length
  ? `<div class="sbar" aria-hidden="true">${steps.map(s => `<i class="${esc(s.status)}"></i>`).join('')}</div>` : '';

function kv(data) {
  const e = Object.entries(data || {});
  return e.length ? `<div class="kv">${e.map(([k, v]) => `<span>${esc(k)}</span><code>${esc(v) || '<i class="faint">empty</i>'}</code>`).join('')}</div>` : '<span class="faint">–</span>';
}

// ------------------------------------------------------------------ last run: verdict + filmstrip
// Why a run ended the way it did, in the consultant's words, and the one thing to do next.
function diagnose(run) {
  const steps = run.steps || [];
  const n = steps.length;
  const status = run.display || (run.status === 'failed' && run.needs_data ? 'needs_data' : run.status);
  const idx = steps.findIndex(s => s.status === 'failed' || s.status === 'stopped');
  const step = idx >= 0 ? steps[idx] : null;
  const where = step ? `step ${idx + 1} of ${n}: ${step.name || step.step_id}` : '';
  const err = step ? (step.error || step.observed || '') : '';
  const cut = err.indexOf(': ');
  const doing = cut > 0 ? err.slice(0, cut) : '';
  const reason = plainError(cut > 0 ? err.slice(cut + 2) : err);
  const firstSentence = reason.split(/(?<=\.)\s/)[0];
  if (status === 'passed') {
    if (isUnproven(run)) {
      return { kind: 'unproven', icon: 'warn', title: `Passed, but not proven: all ${plural(n, 'step')} ran`,
        why: 'This only shows that the recorded clicks happened:', gaps: proofGaps(S.detail.recording),
        what: 'Record the test again and use <b>+ Add check</b> on text that proves each step worked, such as the email in Sent Items.',
        action: { act: 'record', label: 'Re-record with checks' } };
    }
    return { kind: 'passed', icon: 'check', title: `Passed: all ${plural(n, 'step')} worked`,
      why: `Ran in ${secs(run.duration_ms)}${run.run_by ? `, started by ${esc(run.run_by)}` : ''}.` };
  }
  if (status === 'running') return { kind: 'running', icon: 'play', title: 'Running…', why: '' };
  if (status === 'stopped') {
    return { kind: 'stopped', icon: 'stop', title: step ? `Stopped at ${esc(where)}` : 'Stopped before it finished',
      why: run.stopped_by ? `Stopped by ${esc(run.stopped_by)}.` : 'The run was stopped before every step had run.',
      what: 'Anything the steps before it did in the application stays done.', step, idx, err,
      action: { act: 'open-run-form', label: 'Run again…' } };
  }
  if (status === 'needs_data') {
    return { kind: 'needs_data', icon: 'warn', title: 'Needs new test data: the test itself is fine',
      why: esc(run.needs_data || 'A record this test uses is already used up.'),
      what: step ? `It stopped at <b>${esc(where)}</b>. The recording or an earlier run already used that record (approved, processed, shipped).` : '',
      step, idx, err, action: { act: 'open-run-form', label: 'Change test data and run', reason: run.needs_data } };
  }
  if (/sign[\s-]?in|log[\s-]?in|password|session (has )?(ended|expired)|authenticat|\bmfa\b/i.test(err)) {
    return { kind: 'failed', icon: 'key', title: `The application asked for a sign-in at ${esc(where)}`,
      why: 'The saved sign-in has probably ended (a password change, a policy, or a long time without use).',
      what: 'Sign in again, then run the test.', step, idx, err, action: { act: 'signin', label: 'Sign in again' } };
  }
  const notFound = /could not find|not found|no longer|timeout|timed out/i.test(reason);
  // The run just before this one passed: the application was probably slow or showed a message once.
  const before = S.detail.runs.find(r => r.started_at < run.started_at);
  const passedBefore = !!(before && before.status === 'passed');
  const signinTip = idx === 0 && S.detail.target.has_profile
    ? ' If this run’s screenshot shows a sign-in page, <button class="link-btn" data-act="signin">sign in again</button> instead.' : '';
  return {
    kind: 'failed', icon: 'x', title: `Failed at ${esc(where || 'an unknown step')}`,
    why: doing ? `While doing <b>${esc(doing)}</b>: ${esc(firstSentence)}` : esc(firstSentence || 'The run did not finish.'),
    what: passedBefore && notFound
      ? `The same step passed on ${esc(fmtDate(before.started_at))}, so the application was most likely slow or showed a message this time.
         Check this run’s screenshot, then run it again. If the page itself has changed, record the test again.${signinTip}`
      : notFound
        ? `The page did not show what the recording used. Compare the screenshots: if the page changed, record the test again;
           if it was still loading or showed a message, the application is the cause and the issue log is the evidence to send.${signinTip}`
        : 'Check the screenshot and the full error. If the application changed, record the test again.',
    step, idx, err,
    action: passedBefore && notFound ? { act: 'open-run-form', label: 'Run again…' } : { act: 'record', label: 'Re-record' },
    secondary: passedBefore && notFound ? { act: 'record', label: 'Re-record' } : null,
  };
}

// The same step in the passing run nearest in time, before or after this one.
function nearestPassShot(run, step) {
  if (!step) return null;
  const t = Date.parse(run.started_at);
  const best = S.detail.runs.filter(r => r.status === 'passed' && r.id !== run.id)
    .map(r => ({ r, s: (r.steps || []).find(x => x.step_id === step.step_id && x.screenshot), d: Math.abs(Date.parse(r.started_at) - t) }))
    .filter(x => x.s).sort((a, b) => a.d - b.d)[0];
  return best ? { src: best.s.screenshot, run: best.r, later: best.r.started_at > run.started_at } : null;
}

function shotButton(gal, i, src, alt) {
  return src ? `<button class="shot" data-gal="${gal}" data-i="${i}" aria-label="Enlarge: ${esc(alt)}"><img src="${esc(src)}" alt="" loading="lazy"></button>`
    : '<div class="shot none">No screenshot</div>';
}

function renderVerdict(run, { live = false } = {}) {
  const d = diagnose(run);
  const runs = S.detail.runs;
  // An older run: say how things went since, and don't push an action the latest run may not need.
  let history = '';
  if (run !== runs[0] && d.kind !== 'passed') {
    const laterPass = runs.filter(r => r.status === 'passed' && r.started_at > run.started_at).pop();
    history = `<p class="what"><b>This is an older run.</b> ${laterPass
      ? `A later run passed on ${esc(fmtDate(laterPass.started_at))}. <button class="link-btn" data-act="view-run" data-run="${esc(laterPass.id)}">Show that run</button>`
      : '<button class="link-btn" data-act="latest-run">Show the latest run</button>'}</p>`;
    d.action = null;
    d.secondary = null;
  }
  const issue = run.id && d.kind !== 'passed' && d.kind !== 'unproven'
    ? `<a href="/api/runs/${esc(run.id)}/issue-log">${icon('download')} Issue log: the evidence to send a developer</a>` : '';
  let compare = '';
  if (d.step && d.kind !== 'passed') {
    const prev = nearestPassShot(run, d.step);
    const items = [{ src: d.step.screenshot, caption: `This run · ${d.step.step_id} ${d.step.name || ''}` }];
    if (prev) items.push({ src: prev.src, caption: `${prev.later ? 'Next' : 'Last'} pass ${fmtDate(prev.run.started_at)} · ${d.step.step_id}` });
    S.galleries.cmp = items;
    compare = `<div class="compare">
      <figure><figcaption><b>This run</b>, ${esc(d.step.step_id)} when it ${d.kind === 'stopped' ? 'stopped' : 'failed'}</figcaption>${shotButton('cmp', 0, d.step.screenshot, items[0].caption)}</figure>
      ${prev ? `<figure><figcaption><b>${prev.later ? 'Next pass' : 'Last pass'}</b>, ${esc(fmtDate(prev.run.started_at))}${prev.run.run_by ? ` by ${esc(prev.run.run_by)}` : ''}</figcaption>
        ${shotButton('cmp', 1, prev.src, items[1].caption)}</figure>` : '<p class="faint no-pass">No passing run of this step to compare with yet.</p>'}</div>`;
  }
  const btn = (a, cls) => `<button class="btn ${cls}" data-act="${a.act}" data-needs-idle${a.reason ? ` data-reason="${esc(a.reason)}"` : ''} ${S.session ? 'disabled' : ''}>${a.label}</button>`;
  return `<section class="card verdict ${d.kind}" aria-label="Run result">
    <div class="verdict-head"><span class="vi" aria-hidden="true">${icon(d.icon, 18)}</span><div>
      <h2>${d.title}</h2>
      ${d.why ? `<p class="why">${d.why}</p>` : ''}
      ${d.gaps ? `<ul class="gaps">${d.gaps.map(g => `<li>${esc(g)}</li>`).join('')}</ul>` : ''}
      ${d.what ? `<p class="what">${d.what}</p>` : ''}
      ${history}
    </div></div>
    ${compare}
    ${d.err ? `<details class="full-error"><summary>Full error</summary><pre>${esc(d.err)}</pre></details>` : ''}
    ${d.action || d.secondary || issue ? `<div class="verdict-actions">
      ${d.action ? btn(d.action, 'primary') : ''}${d.secondary ? btn(d.secondary, '') : ''}
      ${live ? '' : issue}</div>` : ''}
  </section>`;
}

function renderLastRun(body) {
  const { runs } = S.detail;
  const run = (S.viewRun && runs.find(r => r.id === S.viewRun)) || runs[0];
  const isLatest = run === runs[0];
  const steps = run.steps || [];
  S.galleries.film = steps.map(s => ({ src: s.screenshot, caption: `${s.step_id} ${s.name || ''} · ${s.status}` }));
  body.innerHTML = `
    <div class="run-switch">
      <h2>${isLatest ? 'Last run' : 'Run'} <span class="mono faint">${esc(run.id)}</span> · ${esc(fmtDate(run.started_at))}${run.run_by ? ` · ${esc(run.run_by)}` : ''}</h2>
      <span>${!isLatest ? '<button class="link-btn" data-act="latest-run">Show the latest run</button> · ' : ''}
        ${run.results_xlsx ? `<a href="${esc(run.results_xlsx)}">${icon('download')} Results.xlsx</a>` : ''}</span>
    </div>
    ${renderVerdict(run)}
    <ol class="film" aria-label="Steps of this run">${steps.map((s, i) => `<li class="frame ${esc(s.status)}">
      ${s.screenshot ? shotButton('film', i, s.screenshot, `${s.step_id} ${s.name || ''}`) : `<div class="shot none">${s.status === 'skipped' ? 'Not run' : 'No screenshot'}</div>`}
      <div class="cap"><span class="top">${esc(s.step_id)}<span class="st">${statusBadge(s.status)}</span></span>
        <b>${esc(s.name || '')}</b>
        ${s.duration_ms != null ? `<span class="faint">${secs(s.duration_ms)}</span>` : ''}
        ${s.error ? `<span class="err" title="${esc(s.error)}">${esc(plainError(s.error))}</span>` : ''}</div></li>`).join('')}</ol>
    <details class="full-error run-log" data-run="${esc(run.id)}"><summary>Run log: every action, as it happened</summary>
      <div class="console" role="log" aria-label="Run log" tabindex="0" style="margin-top:8px;border-radius:8px">Loading…</div></details>`;
  const det = $('.run-log', body);
  det.addEventListener('toggle', async () => {
    if (!det.open || det.dataset.loaded) return;
    det.dataset.loaded = '1';
    const con = $('.console', det);
    try {
      const full = await api(`/api/runs/${run.id}`);
      con.innerHTML = (full.events || []).map(renderLog).join('') || 'No log for this run.';
      const firstErr = $('.ln.error', con);
      if (firstErr) { firstErr.classList.add('hl'); con.scrollTop = firstErr.offsetTop - 40; }
    } catch (err) { con.textContent = err.message; det.dataset.loaded = ''; }
  });
}

// ------------------------------------------------------------------ recorded automation
function withParams(s) {
  return esc(s).replace(/\$\{([^}]+)\}/g, '<span class="param">${$1}</span>');
}

function describeAction(a) {
  const val = a.masked && !a.param ? '••••••' : a.value;
  switch (a.type) {
    case 'goto': return `Open <span class="mono">${esc(a.url)}</span>`;
    case 'click': return `Click ${clickTarget(a)}`;
    case 'dblclick': return `Double-click ${clickTarget(a)}`;
    case 'upload': return `Attach <span class="mono">${esc((a.files || []).join(', '))}</span> to ${esc(a.target)}`;
    case 'fill': return `Type “${withParams(val)}” into ${esc(a.target)}`;
    case 'press': return `Press ${esc(a.key)}${a.global ? '' : ` in ${esc(a.target)}`}`;
    case 'select': return `Select “${withParams(val)}” in ${esc(a.target)}`;
    case 'expect_text': return `Check that “${withParams(a.text)}” is visible`;
    case 'scroll': return `Scroll ${a.page ? 'the page' : esc(a.target)} to ${a.y || 0} px${a.x ? ` (across ${a.x} px)` : ''} <span class="chip">best effort</span>`;
    default: return esc(a.type);
  }
}

// A click's target with the parts that are test data shown as ${Key}.
function clickTarget(a) {
  let t = esc(a.target);
  (a.text_params || []).forEach(tp => {
    t = t.split(esc(tp.value)).join(`<span class="param" title="&quot;${esc(tp.value)}&quot; when recorded">\${${esc(tp.key)}}</span>`);
  });
  return t;
}

// The full text a click was recorded with (its target is cut at 60 characters).
const clickText = a => ((a.locators || []).find(l => l.kind === 'role' || l.kind === 'text') || {}).value || '';

function suggestKey(full, text) {
  const before = full.slice(0, Math.max(0, full.indexOf(text))).trim().split(/\s+/).pop() || '';
  return /^[A-Za-z]{3,}$/.test(before) ? before : 'Value';
}

function renderAction(a, si, ai) {
  const loc = a.locators && a.locators[0];
  const locText = loc ? (loc.kind === 'role' ? `role=${loc.role} name="${loc.value}"` : `${loc.kind}="${loc.value}"`) : '';
  const labels = { goto: 'open', click: 'click', dblclick: 'click', upload: 'file', fill: 'type', press: 'key', select: 'select', expect_text: 'check', scroll: 'scroll' };
  const clicked = (a.type === 'click' || a.type === 'dblclick') && clickText(a).trim().length >= 4;
  const tps = clicked ? (a.text_params || []).map(tp => `<button class="btn small" data-act="tp-del" data-si="${si}" data-ai="${ai}" data-key="${esc(tp.key)}"
      title="Stop using ${esc(tp.key)} here: runs look for &quot;${esc(tp.value)}&quot; again">\${${esc(tp.key)}} ${icon('x', 12)}</button>`).join(' ') : '';
  const add = clicked ? `<button class="btn small" data-act="tp-add" data-si="${si}" data-ai="${ai}"
      title="Part of this text changes every run (an invoice or order number)? Make it test data you can change in the run form.">+ test data</button>` : '';
  const editing = S.tpEdit && S.tpEdit.si === si && S.tpEdit.ai === ai;
  let editor = '';
  if (editing) {
    const full = clickText(a);
    const guess = (full.match(/(?:^|\s)((?=[\w./-]*\d)[\w./-]{4,40})(?=\s|$)/) || ['', ''])[1];
    editor = `<form class="inline-edit" id="tp-form">
      <div class="field-label">Clicked text (select the part that changes)</div>
      <div class="full" id="tp-full">${esc(full)}</div>
      <div class="row2"><div><label class="field-label" for="tp-text">Part that changes each run</label><input id="tp-text" value="${esc(guess)}" required></div>
        <div><label class="field-label" for="tp-key">Name in the run form</label><input id="tp-key" value="${esc(suggestKey(full, guess || ' '))}" required></div></div>
      <div class="hint">Runs then click the element that shows the value you give in the run form, e.g. a new invoice number.</div>
      <div class="err-text" id="tp-err" role="alert"></div>
      <div class="foot"><button type="button" class="btn small" data-act="tp-cancel">Cancel</button><button type="submit" class="btn small primary" id="tp-save">Make it test data</button></div>
    </form>`;
  }
  return `<div class="act"><span class="type ${esc(a.type)}">${labels[a.type] || esc(a.type)}</span>
    <div><span class="desc">${describeAction(a)}</span>${a.navigates ? '<span class="chip">loads page</span>' : ''}${a.tab ? `<span class="chip">tab ${a.tab + 1}</span>` : ''}${a.frame ? '<span class="chip">in frame</span>' : ''}
    ${clicked && !editing ? `<span class="tp-tools">${tps} ${add}</span>` : ''}
    ${locText ? `<div class="loc">${esc(locText)}${a.locators.length > 1 ? ` <span title="Fallback locators used if the first one is not found">+${a.locators.length - 1} fallback</span>` : ''}</div>` : ''}
    ${editor}</div></div>`;
}

async function saveTextParam() {
  const { si, ai } = S.tpEdit;
  const text = $('#tp-text').value.trim(), key = $('#tp-key').value.trim();
  const full = $('#tp-full').textContent;
  if (!full.includes(text)) { $('#tp-err').textContent = `“${text}” is not part of the clicked text. Type it exactly as shown, or select it above.`; return; }
  $('#tp-save').disabled = true;
  try {
    await api(caseUrl('/text-params'), { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ step: si, action: ai, text, key }) });
    S.tpEdit = null;
    await selectCase(S.sel.suite_id, S.sel.case_id, true);
    announce(`${key} is now test data.`);
  } catch (err) {
    $('#tp-err').textContent = err.message;
    $('#tp-save').disabled = false;
  }
}

async function removeTextParam(si, ai, key) {
  if (!await confirmDialog({ title: `Stop using ${key} here?`, body: '<p>Runs then look for the text as it was recorded.</p>', ok: 'Stop using it' })) return;
  try {
    await api(caseUrl('/text-params'), { method: 'DELETE', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ step: si, action: ai, key }) });
    await selectCase(S.sel.suite_id, S.sel.case_id, true);
  } catch (err) { showError(err); }
}

// ------------------------------------------------------------------ run options
function renderRunForm() {
  const slot = $('#run-form-slot');
  if (!slot) return;
  if (!S.runForm || S.session || !S.detail.recording) { slot.innerHTML = ''; return; }
  const { case: c, recording, target } = S.detail;
  const linked = new Set();
  recording.steps.forEach(s => s.actions.forEach(a => {
    if (a.param) a.param.split(',').forEach(k => linked.add(k.trim()));
    (a.text_params || []).forEach(tp => linked.add(tp.key));
  }));
  const data = {};
  c.steps.forEach(s => Object.entries(s.data).forEach(([k, v]) => { if (!(k in data)) data[k] = v; }));
  S.runFormInfo = { defaults: data, linked };
  // Start from the newest passing run's values, not the recording's: the recording used those up.
  const { defaults: last = {}, used = {} } = S.detail.run_form || {};
  const start = Object.fromEntries(Object.keys(data).map(k => [k, last[k] ? last[k].value : data[k]]));
  const fromRun = Object.values(last)[0];
  const keys = Object.keys(data).filter(k => linked.has(k));
  const unused = Object.keys(data).filter(k => !linked.has(k));
  const field = k => `<label for="d-${esc(k)}">${esc(k)}</label>
      <div><input id="d-${esc(k)}" data-key="${esc(k)}" value="${esc(start[k])}" type="${/pass/i.test(k) ? 'password' : 'text'}" autocomplete="off">
        <div class="used-hint" id="u-${esc(k)}" hidden></div></div>`;
  slot.innerHTML = `<form class="card run-form" id="run-form" aria-label="Run options">
    <h2>Run ${esc(c.id)} automatically</h2>
    ${S.runReason ? `<div class="notice warn" style="margin:8px 0 10px"><div><b>The last run needed new test data:</b> ${esc(S.runReason)}
      Change that value below to one that is still open before you start.</div></div>` : ''}
    <div class="intro">${fromRun ? `Starts from the values of the last passing run (${esc(fmtDate(fromRun.at))}${fromRun.by ? `, ${esc(fromRun.by)}` : ''}).`
      : 'Starts from the values as recorded.'} Change any value to run the same steps with other data.</div>
    <div class="opts">
      <label><input type="checkbox" id="opt-headed" checked> Show the browser while it runs</label>
      <label>Speed <select id="opt-speed"><option value="0">Fast</option><option value="250" selected>Normal</option><option value="700">Slow, to demonstrate</option></select></label>
    </div>
    <div class="real">${icon('warn')}<div><b>This really happens in ${esc(hostOf(target.url))}.</b> Whatever the recording does (saving, sending, approving) is done again with these values.</div></div>
    ${keys.length ? `<div class="data-grid">${keys.map(field).join('')}</div>` : ''}
    ${unused.length ? `<details class="unused"><summary>${plural(unused.length, 'more value')} in the Excel that the recording doesn't type</summary>
      <div class="data-grid">${unused.map(field).join('')}</div></details>` : ''}
    <div class="foot"><button type="button" class="btn" data-act="rf-cancel">Cancel</button><button type="submit" class="btn primary" id="rf-start">${icon('play')} Start run</button></div>
  </form>`;
  $('#run-form').onsubmit = e => { e.preventDefault(); startRun(); };
  $$('#run-form [data-key]').forEach(inp => {
    if (!used[inp.dataset.key]) return;
    inp.addEventListener('input', () => usedHint(inp, used[inp.dataset.key]));
    usedHint(inp, used[inp.dataset.key]);
  });
}

function openRunForm() {
  if (!S.detail || !S.detail.recording || S.session) return;
  S.runForm = true;
  renderMain();
  const form = $('#run-form');
  if (!form) return;
  form.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  const first = $('.used-hint:not([hidden])', form)?.previousElementSibling || $('input[data-key]', form) || $('#rf-start');
  first.focus({ preventScroll: true });
}

// Under a run form field: this value was used before, by the recording or a passing run. For an invoice
// number or a requisition that means it is used up, so the run would fail; offer a value that is new each run.
function usedHint(inp, history) {
  const box = document.getElementById(`u-${inp.dataset.key}`);
  const v = inp.value.trim();
  const hit = /\{(now|date|time|stamp)\}/.test(v) ? null : [...history].reverse().find(h => h.value === v);
  box.hidden = !hit;
  if (!hit) return;
  const where = hit.what === 'recording' ? 'while recording' : 'by a passing run';
  const base = v.replace(/[_-]?\d+$/, '');
  const unique = /[A-Za-z]/.test(v) ? `${base || v}_{stamp}` : '';
  box.innerHTML = `Used ${where} on ${esc(fmtDate(hit.at))}${hit.by ? ` (${esc(hit.by)})` : ''}. If a run uses it up (an invoice
    or order number, a requisition), give a new one.
    ${unique ? `<button type="button" class="link-btn">Use “${esc(unique)}”, new every run</button>` : ''}`;
  const btn = box.querySelector('button');
  if (btn) btn.onclick = () => { inp.value = unique; usedHint(inp, history); inp.focus(); };
}

// ------------------------------------------------------------------ live panel
function liveTitle(L) {
  if (L.kind === 'run') return L.done ? `Run ${esc(L.run_id || '')} finished` : `Running automatically ${L.run_id ? `<span class="mono faint">${esc(L.run_id)}</span>` : ''}`;
  if (L.kind === 'signin') return L.done ? 'Sign-in finished' : 'Sign-in in progress';
  return L.done ? 'Recording finished' : 'Recording in progress';
}

function renderLive() {
  const box = $('#live');
  if (!box) return;
  const L = S.live;
  if (!L) { box.innerHTML = ''; box.dataset.uid = ''; return; }
  const isRun = L.kind === 'run';
  const isSignin = L.kind === 'signin';
  const final = L.summary && L.summary.status;
  const where = S.config.live_view_url ? 'in the live browser view' : 'in the browser window that opened';
  const freeform = !!(S.detail && S.detail.case.freeform);
  const hint = isSignin
    ? `Sign in to the application ${where}, including any MFA prompt. Then click <b>Save sign-in</b> on its toolbar. The studio never records or stores your password.`
    : `<b>Everything you do here really happens in the application.</b> Do ${freeform ? 'the test' : 'each Excel step'} ${where}. On the toolbar in the
      bottom-right corner${freeform ? ', name each step,' : ''} use <b>+ Add check</b> to mark text that proves a step worked, then <b>Next step</b>${freeform ? '. Click <b>Finish recording</b> when you are done' : ''}.`;
  box.dataset.uid = L.uid;
  L.rendered = L.logs.length;
  box.innerHTML = `<section class="card live ${L.kind}" aria-label="Live session">
    <div class="live-head">
      ${L.done ? '' : '<span class="dotlive" aria-hidden="true"></span>'}
      <div class="title" id="live-title">${liveTitle(L)}${final ? ` <span class="badge ${esc(final)}">${esc(final)}</span>` : ''}</div>
      ${L.done ? '<button class="btn small" data-act="live-close">Close</button>'
        : `<button class="btn small" id="live-stop" data-act="live-stop">${icon('stop', 14)} ${isRun ? 'Stop run' : isSignin ? 'Cancel sign-in' : 'Stop recording'}</button>`}
    </div>
    ${isRun ? `<div class="progress ${esc(final || '')}" role="progressbar" aria-label="Run progress" aria-valuemin="0" aria-valuemax="100"><div></div></div>` : ''}
    ${!isRun && !L.done ? `<div class="live-hint">${hint}</div>` : ''}
    <div class="live-body">
      <div class="steps-col" id="steps-col">${liveSteps(L)}</div>
      <div class="console" id="console" role="log" aria-label="Session log" tabindex="0">${L.logs.map(renderLog).join('')}</div>
    </div>
    ${renderSummary(L)}
  </section>`;
  updateProgress(L);
  const con = $('#console');
  con.scrollTop = con.scrollHeight;
  keepInView($('#steps-col'));
}

// One event: add its log line and refresh the step list, without rebuilding the panel. The log only
// follows new lines while you are at its bottom, and focus (e.g. on Stop) is kept.
function patchLive(L, ev) {
  const box = $('#live');
  if (!box || box.dataset.uid !== String(L.uid)) return;
  if (['session_end', 'run_end', 'recording_saved', 'recording_started', 'run_start'].includes(ev.type)) { renderLive(); return; }
  const con = $('#console');
  const atBottom = con.scrollHeight - con.scrollTop - con.clientHeight < 48;
  con.insertAdjacentHTML('beforeend', L.logs.slice(L.rendered).map(renderLog).join(''));
  L.rendered = L.logs.length;
  if (atBottom) con.scrollTop = con.scrollHeight;
  if (['step_change', 'step_named', 'recorded', 'step_start', 'step_end', 'step_skipped'].includes(ev.type)) {
    const col = $('#steps-col');
    const top = col.scrollTop;
    col.innerHTML = liveSteps(L);
    col.scrollTop = top;
    keepInView(col);
  }
  updateProgress(L);
  if (ev.type === 'step_end' && L.steps[ev.idx]) announce(`${L.steps[ev.idx].name || L.steps[ev.idx].step_id}: ${ev.status}.`);
  if (ev.type === 'step_change' && L.kind === 'record') announce(`Recording step ${ev.idx + 1}.`);
}

function updateProgress(L) {
  const bar = $('#live .progress');
  if (!bar) return;
  const done = L.steps.filter(s => ['passed', 'failed', 'skipped', 'stopped'].includes(s.status)).length;
  const pct = L.done ? 100 : L.steps.length ? Math.round(done / L.steps.length * 100) : 0;
  bar.setAttribute('aria-valuenow', pct);
  bar.firstElementChild.style.transform = `scaleX(${pct / 100})`;
}

function keepInView(col) {
  const el = col && $('.current, .running', col);
  if (!el) return;
  const r = el.getBoundingClientRect(), c = col.getBoundingClientRect();
  if (r.top < c.top || r.bottom > c.bottom) col.scrollTop += r.top - c.top - 8;
}

function liveSteps(L) {
  if (L.kind === 'signin') return `<div class="empty-box">${L.done ? 'Done.' : 'Waiting for you to sign in…'}</div>`;
  S.galleries.live = L.steps.map(s => ({ src: s.screenshot, caption: `${s.step_id} ${s.name || ''} · ${s.status || ''}` }));
  return L.steps.map((s, i) => renderLiveStep(L, s, i)).join('') || '<div class="empty-box">Starting…</div>';
}

function renderLiveStep(L, s, i) {
  if (L.kind === 'record') {
    const current = !L.done && i === L.idx;
    const cls = current ? 'current' : (s.actions.length ? 'passed' : '');
    return `<div class="lstep ${cls}"><div class="row"><span class="si">${s.actions.length && !current ? icon('check', 13) : i + 1}</span>
      <div class="nm">${esc(s.name)}<small>${esc(s.step_id)}${current ? ' · recording now' : ''}</small></div>
      <span class="ms">${plural(s.actions.length, 'action')}</span></div>
      ${s.actions.length ? `<ul>${s.actions.map(a => `<li>${esc(a.message)}</li>`).join('')}</ul>` : ''}</div>`;
  }
  const icons = { passed: icon('check', 13), failed: icon('x', 13), skipped: icon('dash', 13), stopped: icon('stop', 12) };
  return `<div class="lstep ${esc(s.status)}"><div class="row"><span class="si">${icons[s.status] || i + 1}</span>
    <div class="nm">${esc(s.name)}<small>${esc(s.step_id)} · ${esc(s.status)}</small></div>
    <span class="ms">${secs(s.ms)}</span>
    ${s.screenshot ? `<button class="thumb" data-gal="live" data-i="${i}" aria-label="Enlarge the screenshot after ${esc(s.step_id)}"><img src="${esc(s.screenshot)}" alt=""></button>` : ''}</div>
    ${s.error ? `<div class="err">${esc(s.error)}</div>` : ''}</div>`;
}

function renderLog(ev) {
  const lvl = ev.level || 'info';
  const label = { info: 'INFO', success: 'OK', warn: 'WARN', error: 'ERROR' }[lvl] || lvl.toUpperCase();
  const stepLine = ev.type === 'step_start';
  let extra = '';
  if (ev.type === 'action' && ev.status === 'pass') extra = ` <span class="loc">(${ev.ms}ms${ev.locator ? ` · ${esc(ev.locator)}` : ''}${ev.note ? ` · ${esc(ev.note)}` : ''})</span>`;
  return `<div class="ln ${esc(lvl)} ${stepLine ? 'step' : ''}"><span class="t">${fmtTime(ev.ts)}</span> <span class="lv">${label}</span>${esc(ev.message)}${extra}</div>`;
}

function renderSummary(L) {
  const s = L.summary;
  if (!s || L.kind !== 'record') return '';
  const gaps = L.done ? proofGaps(S.detail && S.detail.recording) : [];
  if (gaps.length) {
    return `<div class="summary unproven">${icon('warn')}<div><b>Recording saved, but a run of it won't prove much yet:</b>
      <ul class="gaps">${gaps.map(g => `<li>${esc(g)}</li>`).join('')}</ul></div>
      <button class="btn small primary" data-act="record" data-needs-idle ${S.session ? 'disabled' : ''}>Record again with checks</button>
      <button class="btn small" data-act="open-run-form" data-needs-idle ${S.session ? 'disabled' : ''}>Run it anyway</button></div>`;
  }
  return `<div class="summary passed">${icon('check')} ${esc(s.message)} <button class="btn small primary" data-act="open-run-form" data-needs-idle ${S.session ? 'disabled' : ''}>${icon('play', 14)} Run it automatically</button></div>`;
}

function viewRun(runId) {
  S.viewRun = runId;
  S.tab = 'last';
  renderMain();
  $('#tab-body').scrollIntoView({ behavior: 'smooth', block: 'start' });
  $('#tab-body').focus({ preventScroll: true });
}

// ------------------------------------------------------------------ lightbox
let LB = { items: [], i: 0 };
function openLightbox(items, i) {
  LB = { items: items.filter(x => x.src), i: 0 };
  const want = items[i];
  LB.i = Math.max(0, LB.items.indexOf(want));
  showLightbox();
  const dlg = $('#lightbox');
  if (!dlg.open) dlg.showModal();
  $('#lb-close').focus();
}
function showLightbox() {
  const it = LB.items[LB.i];
  if (!it) return;
  let img = $('#lightbox img');
  if (!img) { img = document.createElement('img'); $('#lightbox').appendChild(img); }
  img.src = it.src;
  img.alt = `Screenshot: ${it.caption}`;
  $('#lb-caption').textContent = `${it.caption} (${LB.i + 1} of ${LB.items.length})`;
  $('#lb-prev').disabled = LB.i === 0;
  $('#lb-next').disabled = LB.i >= LB.items.length - 1;
}
function stepLightbox(d) { LB.i = Math.min(LB.items.length - 1, Math.max(0, LB.i + d)); showLightbox(); }

// ------------------------------------------------------------------ activity (audit log)
const ACTIONS = {
  login: 'Signed in', logout: 'Signed out', login_failed: 'Wrong password', login_locked: 'Locked out',
  upload: 'Uploaded Excel', create_test: 'Created test', delete_case: 'Deleted test case',
  signin_start: 'Started sign-in', record_start: 'Started recording', run_start: 'Started run',
  stop: 'Stopped', export_issue_log: 'Exported issue log', add_test_data: 'Made text test data', remove_test_data: 'Removed test data',
};

async function showActivity() {
  try { S.activity = await api('/api/audit?limit=1000'); } catch (err) { showError(err); return; }
  Object.assign(S, { admin: null, newForm: false, help: false, sel: null, detail: null, live: null, actLimit: 200 });
  renderCaseList();
  renderMain();
}

function describeEntry(e) {
  const parts = [];
  if (e.case_id) parts.push(`<span class="mono">${esc(e.case_id)}</span>${e.title ? ` · ${esc(e.title)}` : ''}`);
  if (e.file) parts.push(esc(e.file));
  if (e.cases && e.cases.length) parts.push(`${plural(e.cases.length, 'case')}: ${e.cases.map(esc).join(', ')}`);
  if (e.run_id) parts.push(`run <span class="mono">${esc(e.run_id)}</span>`);
  if (e.url) parts.push(`<span class="mono">${esc(e.url)}</span>`);
  if (e.key) parts.push(`test data ${esc(e.key)}`);
  if (e.changed_data && e.changed_data.length) parts.push(`changed data: ${e.changed_data.map(esc).join(', ')}`);
  if (e.started_by && e.started_by !== e.user) parts.push(`started by ${esc(e.started_by)}`);
  if (e.replaces_recording_by) parts.push(`replaces the recording by ${esc(e.replaces_recording_by)}`);
  if (e.created_by && e.action === 'delete_case') parts.push(`created by ${esc(e.created_by)}`);
  if (e.scope === 'all') parts.push(e.include_passed ? 'all runs' : 'every run that did not pass');
  return parts.join(' · ');
}

function renderActivity(main) {
  const F = S.actFilter;
  const present = [...new Set(S.activity.map(e => e.action))];
  main.innerHTML = `<div class="page-head"><h1>${S.isAdmin ? 'Activity' : 'Your activity'}</h1>
      <span class="muted">${S.isAdmin ? 'Who changed what' : 'What you did'}, newest first · <button class="btn small" data-act="act-refresh">Refresh</button></span></div>
    <div class="act-filters">
      <input type="search" id="af-text" placeholder="Search user, test case or detail" value="${esc(F.q)}" aria-label="Search activity">
      <label>Action <select id="af-action"><option value="">Any</option>${present.map(a => `<option value="${esc(a)}">${esc(ACTIONS[a] || a)}</option>`).join('')}</select></label>
      <span class="faint" id="af-n"></span>
    </div>
    <div id="act-body"></div>`;
  $('#af-action').value = F.action;
  $('#af-text').oninput = e => { F.q = e.target.value; S.actLimit = 200; renderActivityRows(); };
  $('#af-action').onchange = e => { F.action = e.target.value; S.actLimit = 200; renderActivityRows(); };
  renderActivityRows();
}

function renderActivityRows() {
  const F = S.actFilter, q = F.q.trim().toLowerCase();
  const rows = S.activity.filter(e => (!F.action || e.action === F.action)
    && (!q || `${e.user} ${ACTIONS[e.action] || e.action} ${e.case_id || ''} ${e.title || ''} ${e.file || ''} ${e.run_id || ''}`.toLowerCase().includes(q)));
  $('#af-n').textContent = `${rows.length} of ${S.activity.length}`;
  const shown = rows.slice(0, S.actLimit);
  $('#act-body').innerHTML = rows.length ? `<div class="card table-wrap"><table class="grid">
      <thead><tr><th>When</th><th>User</th><th>Action</th><th>Details</th>${S.isAdmin ? '<th>From</th>' : ''}</tr></thead>
      <tbody>${shown.map(e => `<tr>
        <td class="when">${esc(fmtDate(e.ts))}</td>
        <td>${esc(e.user || '–')}</td>
        <td class="act-name ${/failed|locked/.test(e.action) ? 'bad' : ''}">${esc(ACTIONS[e.action] || e.action)}</td>
        <td>${describeEntry(e)}</td>
        ${S.isAdmin ? `<td class="mono faint">${esc(e.ip || '')}</td>` : ''}</tr>`).join('')}</tbody></table>
      ${rows.length > shown.length ? `<div class="more-rows"><button class="btn small" data-act="act-more">Show ${Math.min(200, rows.length - shown.length)} more</button></div>` : ''}</div>`
    : `<div class="card empty-box">${S.activity.length ? 'Nothing matches this filter.' : 'Nothing yet.'}</div>`;
}

// ------------------------------------------------------------------ admin overview
async function showAdmin() {
  try { S.admin = await api('/api/admin/overview'); } catch (err) { showError(err); return; }
  Object.assign(S, { activity: null, newForm: false, help: false, sel: null, detail: null, live: null });
  renderCaseList();
  renderMain();
}

function renderAdmin(main) {
  const { tests, users, active } = S.admin;
  const F = S.adminFilter;
  tests.forEach(t => { t.view = t.unproven ? 'unproven' : t.status; });
  const count = st => tests.filter(t => t.view === st).length;
  const filters = [
    ['', 'test cases', tests.length],
    ['passed', 'last run passed', count('passed')],
    ['unproven', 'passed, not proven', count('unproven')],
    ['failed', 'last run failed', count('failed')],
    ['needs_data', 'needs new test data', count('needs_data')],
    ['new', 'not recorded yet', count('new')],
  ].filter(([st, , n]) => !st || n || st === F.status || ['passed', 'failed'].includes(st));
  const creators = [...new Set(tests.map(t => t.created_by || ''))].sort();
  main.innerHTML = `<div class="page-head"><h1>Admin overview</h1>
      <span class="muted">${plural(tests.reduce((n, t) => n + t.runs, 0), 'run')} in total${active.length ? ` · now: ${active.map(a => `<b>${esc(a.kind)}</b> of <span class="mono">${esc(a.case_id)}</span> by ${esc(a.user || '–')}`).join(', ')}` : ''}
      · <button class="btn small" data-act="adm-refresh">Refresh</button></span></div>
    <div class="status-bar" role="group" aria-label="Filter test cases by status">${filters.map(([st, label, n]) =>
      `<button class="${st}" data-act="adm-status" data-status="${st}" aria-pressed="${F.status === st}"><b>${n}</b>${label}</button>`).join('')}</div>
    <h2 class="adm-h">All test cases</h2>
    <div class="adm-filters">
      <label>Created by <select id="af-user"><option value="">Everyone</option>${creators.map(c => `<option value="${esc(c)}">${c ? esc(c) : '(before sign-in)'}</option>`).join('')}</select></label>
      <label>Status <select id="af-status"><option value="">Any</option>${Object.entries(STATUS_LABEL).map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}</select></label>
      <input id="af-q" type="search" placeholder="Search ID, title or file" value="${esc(F.q)}" aria-label="Search test cases">
      <span class="faint" id="af-count"></span>
    </div>
    <div class="card table-wrap"><table class="grid adm-tests">
      <thead><tr><th>Test case</th><th>Created</th><th>Recorded</th><th>Runs</th><th>Last run</th><th>Status</th></tr></thead>
      <tbody id="af-body"></tbody></table></div>
    <h2 class="adm-h">People</h2>
    <div class="card table-wrap"><table class="grid">
      <thead><tr><th>User</th><th>Role</th><th>Test cases created</th><th>Recordings</th><th>Runs started</th><th>Last active</th></tr></thead>
      <tbody>${users.map(u => `<tr>
        <td><button class="link-btn" data-act="adm-user" data-user="${esc(u.user)}" title="Show the test cases ${esc(u.user)} created">${esc(u.user)}</button></td>
        <td>${u.role === 'admin' ? '<span class="badge recorded">admin</span>' : 'user'}</td>
        <td class="num">${u.tests}</td><td class="num">${u.recordings}</td><td class="num">${u.runs}</td>
        <td class="when">${u.last_seen ? esc(fmtDate(u.last_seen)) : '<span class="faint">never</span>'}</td></tr>`).join('')}</tbody></table></div>`;
  $('#af-user').value = F.user;
  $('#af-status').value = F.status;
  $('#af-user').onchange = e => { F.user = e.target.value; renderAdminTests(); };
  $('#af-status').onchange = e => { F.status = e.target.value; syncStatusBar(); renderAdminTests(); };
  $('#af-q').oninput = e => { F.q = e.target.value; renderAdminTests(); };
  renderAdminTests();
}

function syncStatusBar() {
  $$('.status-bar button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.status === S.adminFilter.status)));
}

function renderAdminTests() {
  const F = S.adminFilter;
  const q = F.q.trim().toLowerCase();
  const rows = S.admin.tests.filter(t => (!F.user || (t.created_by || '') === F.user) && (!F.status || t.view === F.status)
    && (!q || `${t.case_id} ${t.title} ${t.filename}`.toLowerCase().includes(q)));
  $('#af-count').textContent = `${rows.length} of ${S.admin.tests.length}`;
  $('#af-body').innerHTML = rows.length ? rows.map(t => `<tr>
      <td><button class="link-btn mono" data-act="open-case" data-suite="${esc(t.suite_id)}" data-case="${esc(t.case_id)}">${esc(t.case_id)}</button><div>${esc(t.title)}</div>
        <div class="faint">${t.source === 'recording' ? 'Recorded without Excel' : esc(t.filename)} · ${plural(t.steps, 'step')}</div></td>
      <td>${t.created_by ? esc(t.created_by) : '<span class="faint">before sign-in</span>'}<div class="faint">${esc(fmtDate(t.created_at))}</div></td>
      <td>${t.recorded_at ? `${esc(t.recorded_by || '–')}<div class="faint">${esc(fmtDate(t.recorded_at))}${t.recording_warnings ? ` · ${plural(t.recording_warnings, 'warning')}` : ''}</div>` : '<span class="faint">–</span>'}</td>
      <td>${t.runs ? `${t.runs}<div class="faint">${t.passed} passed · ${t.failed} failed${t.needs_data ? ` · ${t.needs_data} needed new data` : ''}</div>` : '<span class="faint">0</span>'}</td>
      <td>${t.last_run ? `${esc(t.last_run.run_by || '–')}${t.last_run.by_admin ? ' <span class="chip">admin</span>' : ''}<div class="faint">${esc(fmtDate(t.last_run.started_at))} · ${secs(t.last_run.duration_ms)}</div>` : '<span class="faint">–</span>'}</td>
      <td>${statusBadge(t.view, t.last_run && t.last_run.needs_data)}</td></tr>`).join('')
    : '<tr><td colspan="6" class="empty-box">No test cases match.</td></tr>';
}

async function logout() {
  $('#btn-logout').disabled = true;
  if (S.es) S.es.close();
  await fetch('/api/logout', { method: 'POST' }).catch(() => {});
  location.replace('/login');  // replace: Back does not return to the dashboard
}

// A page restored from the back/forward cache would show what it showed before signing out; load it again.
window.addEventListener('pageshow', e => { if (e.persisted) location.reload(); });

// ------------------------------------------------------------------ one place for every click
const ACTS = {
  'open-case': async el => {
    const fromList = !!el.closest('#case-list');
    await selectCase(el.dataset.suite, el.dataset.case);
    if (fromList) $('#case-list [aria-current="true"]')?.focus();
  },
  'dismiss-notice': () => { S.notice = null; renderMain(); $('#main').focus(); },
  'desc-toggle': () => { S.descOpen = !S.descOpen; renderMain(); $('.desc-more')?.focus(); },
  'upload': () => $('#file-input').click(),
  'new-rec': showNewForm,
  'close-help': () => { S.help = false; renderMain(); },
  'nf-cancel': () => { S.newForm = false; renderMain(); },
  'signin': startSignin,
  'record': startRecording,
  'run': () => {
    if (S.runForm) { S.runForm = false; S.runReason = ''; renderMain(); $('.stage[data-act="run"]')?.focus(); }
    else openRunForm();
  },
  'open-run-form': el => { S.live = S.session ? S.live : null; S.runReason = el.dataset.reason || ''; openRunForm(); },
  'rf-cancel': () => { S.runForm = false; S.runReason = ''; renderMain(); $('.stage[data-act="run"]')?.focus(); },
  'delete': deleteCase,
  'tab': el => { S.tab = el.dataset.tab; S.tpEdit = null; renderMain(); $(`#tab-${S.tab}`).focus(); },
  'view-run': el => viewRun(el.dataset.run),
  'latest-run': () => { S.viewRun = null; renderMain(); $('#tab-body').focus(); },
  'toggle-locators': el => { S.showLocators = el.checked; $('#rec-card').classList.toggle('hide-locators', !el.checked); },
  'tp-add': el => { S.tpEdit = { si: +el.dataset.si, ai: +el.dataset.ai }; renderTab(); },
  'tp-cancel': () => { S.tpEdit = null; renderTab(); },
  'tp-del': el => removeTextParam(+el.dataset.si, +el.dataset.ai, el.dataset.key),
  'live-stop': stopSession,
  'live-close': () => { S.live = null; renderLive(); },
  'act-refresh': showActivity,
  'act-more': () => { S.actLimit += 200; renderActivityRows(); },
  'adm-refresh': showAdmin,
  'adm-status': el => { S.adminFilter.status = S.adminFilter.status === el.dataset.status ? '' : el.dataset.status;
    $('#af-status').value = S.adminFilter.status; syncStatusBar(); renderAdminTests(); },
  'adm-user': el => { S.adminFilter.user = el.dataset.user; $('#af-user').value = el.dataset.user; renderAdminTests();
    $('.adm-tests').scrollIntoView({ behavior: 'smooth', block: 'start' }); },
};

document.addEventListener('click', e => {
  // Close open menus when clicking elsewhere (or on one of their items).
  $$('details.menu[open]').forEach(d => { if (!d.contains(e.target) || e.target.closest('.menu-list')) d.open = false; });
  const gal = e.target.closest('[data-gal]');
  if (gal) { openLightbox(S.galleries[gal.dataset.gal] || [], +gal.dataset.i); return; }
  const el = e.target.closest('[data-act]');
  if (!el || el.disabled || el.getAttribute('aria-disabled') === 'true') return;
  const fn = ACTS[el.dataset.act];
  if (fn) { if (el.tagName !== 'INPUT') e.preventDefault(); fn(el); }
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') $$('details.menu[open]').forEach(d => { d.open = false; $('summary', d).focus(); });
  const tab = e.target.closest && e.target.closest('[role="tab"]');
  if (tab && ['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(e.key)) {
    e.preventDefault();
    const tabs = $$('[role="tab"]');
    const i = tabs.indexOf(tab);
    const next = e.key === 'Home' ? tabs[0] : e.key === 'End' ? tabs[tabs.length - 1]
      : tabs[(i + (e.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length];
    next.click();
  }
  // Menus: arrow keys move between items, as role="menu" promises.
  const menu = e.target.closest && e.target.closest('details.menu[open]');
  if (menu && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
    e.preventDefault();
    const items = $$('[role="menuitem"]', menu).filter(x => !x.hidden && !x.disabled);
    const i = items.indexOf(document.activeElement);
    const next = i < 0 ? (e.key === 'ArrowDown' ? 0 : items.length - 1) : (i + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
    items[next]?.focus();
  }
  if ($('#lightbox').open && (e.key === 'ArrowRight' || e.key === 'ArrowLeft')) stepLightbox(e.key === 'ArrowRight' ? 1 : -1);
});

// A screenshot whose file is gone (deleted run folder, restored backup): say so instead of a broken image.
document.addEventListener('error', e => {
  const img = e.target;
  if (!(img instanceof HTMLImageElement)) return;
  const holder = img.closest('.shot, .thumb');
  if (!holder) return;
  if (holder.classList.contains('thumb')) { holder.hidden = true; return; }
  const box = document.createElement('div');
  box.className = 'shot none';
  box.textContent = 'Screenshot not available';
  holder.replaceWith(box);
}, true);

// Errors in the dashboard itself go to the studio's log, so they are in the exported issue log.
function reportError(message, stack, where) {
  fetch('/api/client-errors', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: String(message || ''), stack: String(stack || ''), where }),
  }).catch(() => {});
}
window.addEventListener('error', e => reportError(e.message, e.error && e.error.stack, `${e.filename}:${e.lineno}`));
window.addEventListener('unhandledrejection', e => reportError(e.reason && e.reason.message || e.reason, e.reason && e.reason.stack, 'promise'));

// ------------------------------------------------------------------ boot
(async function init() {
  setupUpload();
  $('#btn-new-rec').onclick = showNewForm;
  $('#btn-activity').onclick = showActivity;
  $('#btn-admin').onclick = showAdmin;
  $('#btn-logout').onclick = logout;
  $('#btn-help').onclick = () => { Object.assign(S, { help: true, activity: null, admin: null, newForm: false }); renderMain(); };
  $('#case-filter').oninput = e => { S.caseFilter = e.target.value; renderCaseList(); };
  const lb = $('#lightbox');
  $('#lb-close').onclick = () => lb.close();
  $('#lb-prev').onclick = () => stepLightbox(-1);
  $('#lb-next').onclick = () => stepLightbox(1);
  lb.addEventListener('click', e => { if (e.target === lb) lb.close(); });
  const me = await api('/api/me');
  S.user = me.user;
  S.isAdmin = me.admin;
  if (me.login) { $('#who-name').textContent = me.user; $('#btn-logout').hidden = false; }
  if (me.admin) $('#btn-admin').hidden = false;
  S.config = await api('/api/config').catch(() => S.config);
  await loadCases();
  const { active, others } = await api('/api/status');
  S.others = others || [];
  setStatus();
  setInterval(pollStatus, 4000);
  if (active) {  // our own session, e.g. after reloading the page
    await selectCase(active.suite_id, active.case_id);
    attach(active);
  } else if (await openFromHash()) {
    // back where the link or the reload pointed
  } else if (S.cases.length) {
    const linkNotice = S.notice;  // "that link is to a test case you can't see" survives opening the first case
    await selectCase(S.cases[0].suite_id, S.cases[0].case_id);
    if (linkNotice) { S.notice = linkNotice; renderMain(); }
  } else {
    renderMain();
  }
  // A link pasted into the address bar, or Back/Forward between hashes.
  window.addEventListener('hashchange', () => { if (!S.session) openFromHash(); });
})();
