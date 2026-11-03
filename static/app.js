'use strict';

/* Data Explorer front end. Plain JavaScript; it talks only to this app's own /api. */

const FONT = '"Segoe UI", system-ui, -apple-system, Arial, sans-serif';
// A workspace with more than one schema colours its tables by schema: --schema-1 ... --schema-6 in styles.css.
const SCHEMA_COLORS = 6;
// The kinds of relationship come from /api/meta (app.model.RANK), so a new kind needs no change here.
const KIND_TEXT = {};
const ORIGIN_TEXT = { db: 'Database', ddl: 'SQL DDL', file: 'File', query: 'SQL query', ref: 'Referenced only' };
const ORIGIN_TAG = { db: 'DB', ddl: 'DDL', file: 'FILE', query: 'QUERY', ref: 'REF' };
const SOURCE_TAG = { database: 'DB', sql_files: 'SQL', files: 'CSV' };
// One tab per feature. Later sessions switch theirs on through /api/meta "features".
const TABS = [
  { id: 'er_graph', label: 'ER graph' },
  { id: 'profile', label: 'Profile' },
  { id: 'quality', label: 'Quality' },
  { id: 'relations', label: 'Relations' },
  { id: 'sql', label: 'SQL' },
  { id: 'report', label: 'Report' },
  { id: 'scripts', label: 'Scripts' },
];
const MAX_HOPS = 6;
const LABELS_UP_TO = 45;          // above this many visible edges, labels show only on the highlighted ones
const LABEL_ENDS = 40;            // room at each end of a labelled line: the "1" or "N" there, and line enough to see its dashes

const state = {
  meta: null,
  workspaces: [],
  wsId: null,
  model: null,
  tables: new Map(),              // id -> table
  selected: null,
  focus: { on: false, root: null, hops: 1 },
  filters: { kinds: {}, minConf: 0, schema: '*', search: '' },
  layout: 'auto',
  laidOut: '',                    // signature of the node set that was last arranged
  cy: null,
  details: new Map(),             // table id -> details from the API
  detailsToken: 0,
  tab: 'er_graph',                // the feature tab that is showing
};
// profile.js and analysis.js register here: what to do when a workspace opens, a table is selected,
// a tab is shown, a relationship check moves on / ends (job, finished), or the colour theme changes.
const hooks = { workspace: [], select: [], tab: [], job: [], theme: [] };
const runHooks = (name, ...args) => hooks[name].forEach((fn) => Promise.resolve(fn(...args)).catch(fail));

// ---------------------------------------------------------------- helpers ----

const $ = (selector, root = document) => root.querySelector(selector);

/** A colour of the theme that is showing, as styles.css names it: themeColor('accent').
    What is drawn on a canvas (the graph, the charts) cannot use a CSS variable itself. */
const themeColor = (name) => getComputedStyle(document.documentElement).getPropertyValue('--' + name).trim();

/** Build a DOM node. Text always goes in as text, never as HTML. */
function h(tag, props, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

async function api(path, options = {}) {
  let res;
  try {
    res = await fetch('/api' + path, options);
  } catch (e) {
    throw new Error('The app is not responding. Is it still running?');
  }
  const isJson = (res.headers.get('content-type') || '').includes('json');
  const data = isJson ? await res.json() : null;
  if (!res.ok) {
    const err = new Error((data && data.detail) || `Request failed (${res.status})`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

const sendJSON = (path, body, method = 'POST') =>
  api(path, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });

function toast(message, kind = 'info', ms = 6000) {
  const node = h('div', { class: 'toast ' + kind, text: message, title: 'Click to dismiss' });
  node.addEventListener('click', () => node.remove());
  $('#toasts').append(node);
  if (ms) setTimeout(() => node.remove(), ms);
}

const fail = (err) => toast(err.message || String(err), 'error', 9000);
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
const percent = (x) => `${Math.round(x * 100)}%`;
/** A share by the rule of fraction_text() in app/analysis/relations.py: one decimal where it says something, and
    never "0%" or "100%" unless it is exactly that (98.8%, >99.9%). For the share of a link found by values. */
function shareText(x) {
  if (x == null) return '';
  if (x <= 0) return '0%';
  if (x >= 1) return '100%';
  if (x < 0.001) return '<0.1%';
  if (x > 0.999) return '>99.9%';
  return (100 * x).toFixed(1).replace(/\.0$/, '') + '%';
}
/** How sure an inferred link is: a link found by values prints its share as measured, the others a whole percent. */
const kindShare = (kind, x) => (kind === 'values' ? shareText(x) : percent(x));
const enc = encodeURIComponent;
const whole = (n) => (n == null ? '—' : Number(n).toLocaleString('en-US'));

/** 1,438 · 12.9K · 2.0M - a row count short enough for a list. */
function compact(n) {
  if (n == null) return '—';
  if (n < 10000) return whole(n);
  if (n < 1e6) return (n / 1e3).toFixed(n < 1e5 ? 1 : 0) + 'K';
  if (n < 1e9) return (n / 1e6).toFixed(n < 1e8 ? 1 : 0) + 'M';
  return (n / 1e9).toFixed(1) + 'B';
}

/** A row count as shown in lists: "~" in front of an estimate, ">" when the table has at least that many rows. */
const rowsMark = (kind) => (kind === 'estimate' ? '~' : kind === 'more_than' ? '>' : '');
const rowsText = (count, kind) => (count == null ? '' : rowsMark(kind) + compact(count));
const rowsTitle = (count, kind) => (count == null ? 'Rows not counted yet'
  : kind === 'estimate' ? `About ${whole(count)} rows (an estimate from the database's own statistics)`
    : kind === 'more_than' ? `More than ${whole(count)} rows: the read stopped there and the table was not counted further`
      : `${whole(count)} rows (counted)`);

/** Ask for one value in a small dialog. Resolves to the text, or null if cancelled. */
function ask(title, { text = '', value = '', type = 'text', ok = 'OK' } = {}) {
  const dialog = $('#ask-dialog');
  const input = $('#ask-input');
  $('#ask-title').textContent = title;
  $('#ask-text').textContent = text;
  $('#ask-ok').textContent = ok;
  input.type = type === 'none' ? 'text' : type;
  input.value = value;
  input.hidden = type === 'none';
  $('#ask-cancel').onclick = () => dialog.close('cancel');
  return new Promise((resolve) => {
    dialog.addEventListener('close', () => {
      resolve(dialog.returnValue === 'ok' ? (type === 'none' ? true : input.value) : null);
    }, { once: true });
    dialog.returnValue = '';
    dialog.showModal();
    if (!input.hidden) input.select();
  });
}

// -------------------------------------------------------------- workspaces ----

async function init() {
  wireEvents();
  try {
    state.meta = await api('/meta');
    renderKindToggles();
    renderTabs();
    fillEngines();
    await refreshWorkspaces();
    const wanted = new URLSearchParams(location.search).get('ws') || safeStorage('get');
    let id = state.workspaces.some((w) => w.id === wanted) ? wanted : (state.workspaces[0] || {}).id;
    if (!id) {
      id = (await sendJSON('/workspaces', { name: 'My workspace' })).id;
      await refreshWorkspaces();
    }
    await openWorkspace(id);
  } catch (err) {
    fail(err);
  }
}

function safeStorage(op, value) {
  try {
    if (op === 'get') return localStorage.getItem('data-explorer.workspace');
    localStorage.setItem('data-explorer.workspace', value);
  } catch (e) { /* storage may be blocked; not needed */ }
  return null;
}

async function refreshWorkspaces() {
  state.workspaces = await api('/workspaces');
  const select = $('#ws-select');
  select.replaceChildren(...state.workspaces.map((w) => h('option', { value: w.id, text: w.name })));
  if (state.wsId) select.value = state.wsId;
}

async function openWorkspace(id, { keep = false } = {}) {
  const model = await api(`/workspaces/${enc(id)}/model`);
  state.wsId = id;
  state.model = model;
  state.tables = new Map(model.tables.map((t) => [t.id, t]));
  state.details.clear();
  if (!keep) {
    state.selected = null;
    state.focus = { on: false, root: null, hops: state.focus.hops };
    state.filters.schema = '*';
  } else {
    if (!state.tables.has(state.selected)) state.selected = null;
    if (!state.tables.has(state.focus.root)) state.focus = { on: false, root: null, hops: state.focus.hops };
    if (state.filters.schema !== '*' && !model.schemas.includes(state.filters.schema)) state.filters.schema = '*';
  }
  $('#ws-select').value = id;
  safeStorage('set', id);
  history.replaceState(null, '', '?ws=' + enc(id));
  document.title = `${model.workspace.name} - Data Explorer`;
  renderSources();
  renderSchemaFilter();
  renderTableList();
  buildGraph();
  syncToolbar();
  renderDetails();
  runHooks('workspace', { keep });
}

async function newWorkspace() {
  const name = await ask('New workspace', { text: 'A workspace is a set of sources that are explored together.', value: '', ok: 'Create' });
  if (name === null) return;
  const ws = await sendJSON('/workspaces', { name });
  await refreshWorkspaces();
  await openWorkspace(ws.id);
}

async function renameWorkspace() {
  const name = await ask('Rename workspace', { value: state.model.workspace.name, ok: 'Rename' });
  if (!name) return;
  await sendJSON(`/workspaces/${enc(state.wsId)}`, { name }, 'PATCH');
  await refreshWorkspaces();
  state.model.workspace.name = name;
  document.title = `${name} - Data Explorer`;
}

async function deleteWorkspace() {
  const name = state.model.workspace.name;
  const sure = await ask(`Delete "${name}"?`, {
    type: 'none', ok: 'Delete',
    text: 'The workspace, its saved structure and any files uploaded to it are removed from this PC. Your databases and original files are not touched.',
  });
  if (!sure) return;
  await api(`/workspaces/${enc(state.wsId)}`, { method: 'DELETE' });
  state.wsId = null;
  await refreshWorkspaces();
  let next = (state.workspaces[0] || {}).id;
  if (!next) {
    next = (await sendJSON('/workspaces', { name: 'My workspace' })).id;
    await refreshWorkspaces();
  }
  await openWorkspace(next);
}

// ----------------------------------------------------------------- sources ----

function renderSources() {
  const items = state.model.workspace.sources.map((s) => {
    const sub = h('div', { class: 'sub' }, `${plural(s.tables, 'table')} · ${plural(s.relationships, 'link')}`);
    if (s.warnings.length) {
      sub.append(' · ', h('span', { class: 'warn', text: `${s.warnings.length} note${s.warnings.length === 1 ? '' : 's'}`,
                                    title: s.warnings.join('\n'), onclick: () => toast(s.warnings.join('\n'), 'info', 15000) }));
    }
    if (s.needs_password) sub.append(' · password needed to refresh');
    return h('li', { class: 'source', title: `${s.label}\n${sourceTitle(s)}` },
      h('span', { class: 'tag ' + s.kind, text: SOURCE_TAG[s.kind] || '?' }),
      h('div', {}, h('div', { class: 'name', text: s.label }), sub),
      h('div', { class: 'actions' },
        s.has_password ? h('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Forget password',
                                       title: 'Forget the password of this database. It is only held in memory; reading the database again will ask for it.',
                                       onclick: () => forgetPassword(s).catch(fail) }, '⚿') : null,
        h('button', { class: 'icon-btn', type: 'button', title: 'Read this source again', 'aria-label': 'Refresh source',
                      onclick: () => refreshSource(s).catch(fail) }, '↻'),
        h('button', { class: 'icon-btn', type: 'button', title: 'Remove this source from the workspace', 'aria-label': 'Remove source',
                      onclick: () => removeSource(s).catch(fail) }, '×')));
  });
  $('#source-list').replaceChildren(...items);
}

function sourceTitle(s) {
  const st = s.settings || {};
  if (s.kind === 'database') {
    const where = st.engine === 'sqlite' ? st.database : `${st.host}${st.port ? ':' + st.port : ''} / ${st.database || ''}`;
    return `${st.engine}: ${where}${st.username ? '\nuser: ' + st.username : ''}${st.schema_filter ? '\nschemas: ' + st.schema_filter : ''}\nread ${s.refreshed_at}`;
  }
  return `${st.mode === 'upload' ? 'uploaded: ' + (st.files || []).join(', ') : st.path}\nread ${s.refreshed_at}`;
}

async function refreshSource(s, password) {
  try {
    await sendJSON(`/workspaces/${enc(state.wsId)}/sources/${enc(s.id)}/refresh`, password ? { password } : {});
  } catch (err) {
    if (err.data && err.data.needs_password) {
      const pw = await ask('Password needed', { type: 'password', ok: 'Refresh',
        text: `Enter the password for ${s.label}. It is kept in memory only until the app is closed.` });
      if (pw) return refreshSource(s, pw);
      return;
    }
    throw err;
  }
  toast(`${s.label} was read again.`, 'ok', 3000);
  await openWorkspace(state.wsId, { keep: true });
}

/** A password was given in some prompt (or forgotten): show what the sources say now. */
async function refreshSources() {
  const id = state.wsId;
  const workspace = await api(`/workspaces/${enc(id)}`);
  if (id !== state.wsId) return;
  state.model.workspace = workspace;
  renderSources();
}

async function forgetPassword(s) {
  await api(`/workspaces/${enc(state.wsId)}/sources/${enc(s.id)}/password`, { method: 'DELETE' });
  toast(`The password for ${s.label} was removed from memory.`, 'ok', 4000);
  await openWorkspace(state.wsId, { keep: true });
}

async function removeSource(s) {
  const sure = await ask(`Remove "${s.label}"?`, { type: 'none', ok: 'Remove',
    text: 'Only the workspace forgets this source. The database or files themselves are not touched.' });
  if (!sure) return;
  await api(`/workspaces/${enc(state.wsId)}/sources/${enc(s.id)}`, { method: 'DELETE' });
  await openWorkspace(state.wsId, { keep: true });
}

// ---------------------------------------------------------- source dialog ----

function fillEngines() {
  const select = $('#pane-db [name=engine]');
  select.replaceChildren(...state.meta.engines.map((e) =>
    h('option', { value: e.id, text: e.label + (e.installed ? '' : ' (driver not installed)') })));
  onEngineChange();
}

function currentEngine() {
  const id = $('#pane-db [name=engine]').value;
  return state.meta.engines.find((e) => e.id === id) || state.meta.engines[0];
}

function onEngineChange() {
  const e = currentEngine();
  const form = $('#pane-db');
  const isFile = e.id === 'sqlite';
  form.querySelectorAll('.db-net').forEach((n) => { n.hidden = isFile; });
  $('#db-database-label').textContent = e.database_label;
  form.elements.port.placeholder = e.port || '';
  if (!form.elements.port.value || state.meta.engines.some((x) => String(x.port) === form.elements.port.value)) {
    form.elements.port.value = e.port || '';
  }
  $('#db-hint').textContent = e.hint || '';
  $('#db-test-result').hidden = true;
}

function dbBody() {
  const f = $('#pane-db').elements;
  return {
    engine: f.engine.value, host: f.host.value.trim(), port: f.port.value, database: f.database.value.trim(),
    username: f.username.value.trim(), password: f.password.value, schema_filter: f.schema_filter.value.trim(),
    include_views: f.include_views.checked, save_profile: f.save_profile.checked,
  };
}

async function openSourceDialog(pane) {
  const dialog = $('#source-dialog');
  showPane(pane || 'sql');
  $('#db-test-result').hidden = true;
  try {
    const profiles = await api('/profiles');
    const select = $('#pane-db [name=profile]');
    select.replaceChildren(h('option', { value: '', text: '(none)' }),
      ...profiles.map((p) => h('option', { value: p.id, text: p.name })));
    select.onchange = () => {
      const p = profiles.find((x) => x.id === select.value);
      if (!p) return;
      const f = $('#pane-db').elements;
      f.engine.value = p.engine;
      onEngineChange();
      f.host.value = p.host || '';
      f.port.value = p.port || '';
      f.database.value = p.database || '';
      f.username.value = p.username || '';
      f.schema_filter.value = p.schema_filter || '';
      f.include_views.checked = !!p.include_views;
      f.password.value = '';
    };
  } catch (err) { fail(err); }
  dialog.showModal();
}

function showPane(name) {
  for (const b of document.querySelectorAll('#source-dialog .seg button')) b.classList.toggle('active', b.dataset.pane === name);
  for (const p of document.querySelectorAll('#source-dialog .pane')) p.hidden = p.id !== 'pane-' + name;
}

async function submitFiles(form, route) {
  const data = new FormData(form);
  if (![...data.getAll('files')].some((f) => f && f.name) && !String(data.get('path') || '').trim()) {
    throw new Error('Choose files to upload, or enter a file or folder path.');
  }
  return api(`/workspaces/${enc(state.wsId)}/sources/${route}`, { method: 'POST', body: data });
}

async function addSource(form, work) {
  const button = form.querySelector('button[type=submit]');
  const label = button.textContent;
  button.disabled = true;
  button.textContent = 'Reading…';
  try {
    const source = await work();
    $('#source-dialog').close();
    form.reset();
    if (form.id === 'pane-db') onEngineChange();
    toast(`${source.label}: ${plural(source.tables, 'table')} read.` +
          (source.warnings.length ? ` ${plural(source.warnings.length, 'note')} - see the source.` : ''), 'ok', 5000);
    await openWorkspace(state.wsId, { keep: true });
  } catch (err) {
    fail(err);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

async function testConnection() {
  const box = $('#db-test-result');
  const button = $('#db-test');
  button.disabled = true;
  box.hidden = false;
  box.className = 'test-result';
  box.textContent = 'Connecting…';
  try {
    const r = await sendJSON('/db/test', dbBody());
    box.className = 'test-result ok';
    const lines = [`Connected${r.server_version ? ' · server ' + r.server_version : ''} · ${plural(r.tables, 'table')} ` +
                   `${dbBody().schema_filter ? 'in the schema filter' : 'in the default schema' + (r.default_schema ? ' (' + r.default_schema + ')' : '')} · ${r.elapsed_ms} ms`];
    if (r.protections.length) lines.push('Protections: ' + r.protections.join(', ') + '.');
    box.replaceChildren(...lines.map((t) => h('div', { text: t })));
    if (r.schemas.length) {
      const input = $('#pane-db [name=schema_filter]');
      box.append(h('div', { text: 'Schemas (click to add to the filter):' }),
        h('div', { class: 'schemas' }, r.schemas.map((s) => h('button', { type: 'button', text: s, onclick: () => {
          const now = input.value.split(',').map((x) => x.trim()).filter(Boolean);
          if (!now.includes(s)) now.push(s);
          input.value = now.join(', ');
        } }))));
      $('#schema-options').replaceChildren(...r.schemas.map((s) => h('option', { value: s })));
    }
  } catch (err) {
    box.className = 'test-result bad';
    box.textContent = err.message;
  } finally {
    button.disabled = false;
  }
}

// --------------------------------------------------------------- table list ----

function renderSchemaFilter() {
  const select = $('#schema-filter');
  const schemas = state.model.schemas;
  select.hidden = schemas.length <= 1;
  select.replaceChildren(h('option', { value: '*', text: `All schemas (${schemas.length})` }),
    ...schemas.map((s) => h('option', { value: s, text: s || '(no schema)' })));
  select.value = state.filters.schema;
}

function tableMatches(t, needle) {
  if (!needle) return { ok: true };
  if (t.id.toLowerCase().includes(needle)) return { ok: true };
  const col = t.column_names.find((c) => c.toLowerCase().includes(needle));
  return col ? { ok: true, column: col } : { ok: false };
}

function renderTableList() {
  const needle = state.filters.search.trim().toLowerCase();
  const schema = state.filters.schema;
  const many = state.model.schemas.length > 1;
  const visible = visibleSets();
  const rows = [];
  const tables = [...state.model.tables].sort((a, b) => a.name.localeCompare(b.name));
  for (const t of tables) {
    if (schema !== '*' && (t.schema || '') !== schema) continue;
    const match = tableMatches(t, needle);
    if (!match.ok) continue;
    const meta = h('div', { class: 'meta' });
    if (match.column) meta.append('column ', h('mark', { text: match.column }));
    else {
      const bits = [];
      if (many) bits.push(t.schema || '(no schema)');
      if (t.kind === 'view') bits.push('view');
      bits.push([...new Set(t.origins.map((o) => ORIGIN_TAG[o.kind] || o.kind))].join(' · '));
      meta.textContent = bits.join(' · ');
    }
    rows.push(h('li', {
      class: 'table-item' + (t.id === state.selected ? ' selected' : '') + (visible.tables.has(t.id) ? '' : ' dim'),
      'data-id': t.id, title: t.id + (t.comment ? '\n' + t.comment : ''),
      onclick: () => selectTable(t.id, { center: true }),
      ondblclick: () => focusOn(t.id),
    }, h('span', { class: 'name', text: t.name }),
       h('span', { class: 'rows' + (rowsMark(t.row_count_kind) ? ' estimate' : ''),
                   text: rowsText(t.row_count, t.row_count_kind), title: rowsTitle(t.row_count, t.row_count_kind) }),
       h('span', { class: 'cols', text: t.column_count, title: plural(t.column_count, 'column') }), meta));
  }
  $('#table-list').replaceChildren(...rows);
  const total = state.model.tables.length;
  $('#table-count').textContent = rows.length === total ? String(total) : `${rows.length} of ${total}`;
}

// -------------------------------------------------------------------- graph ----

let measureCtx = null;
function nodeWidth(label) {
  if (!measureCtx) {
    measureCtx = document.createElement('canvas').getContext('2d');
    measureCtx.font = `600 12px ${FONT}`;
  }
  return Math.max(84, Math.ceil(measureCtx.measureText(label).width) + 28);
}

const labelRooms = new Map();
/** The length a line needs to carry its label between its two ends without covering the tables. */
function labelRoom(label) {
  if (!label) return 0;
  if (!labelRooms.has(label)) {
    const ctx = document.createElement('canvas').getContext('2d');
    ctx.font = `10px ${FONT}`;
    labelRooms.set(label, Math.ceil(ctx.measureText(label).width) + 2 * LABEL_ENDS);
  }
  return labelRooms.get(label);
}

/* A table that points at itself. Its loop (the style below) spans, from the middle of the box, 48 px to the left to 4 px
   to the right and 16 to 52 px up, whatever the box's width, and its label is placed from the loop's middle, 26 px left
   of the box's middle and 46 px up. Moved by these, the label starts 16 px right of the loop (past the "1" at its end),
   level with its middle. */
const LOOP_LABEL_RIGHT = 46;
const LOOP_LABEL_DOWN = 12;
function loopLabel(label) {
  return label ? LOOP_LABEL_RIGHT + (labelRoom(label) - 2 * LABEL_ENDS) / 2 + 2 : 0;    // (+2: the text's background)
}

function edgeLabel(r) {
  const pairs = r.from_columns.map((c, i) => {
    const other = r.to_columns[i];
    if (c.toLowerCase() === (other || '').toLowerCase()) return c;
    return r.cardinality === 'unknown' ? `${c} = ${other}` : `${c} → ${other}`;
  });
  return pairs.join(', ');
}

/** The graph's style sheet in the colours of the theme that is showing. */
function graphStyle() {
  const c = themeColor;
  return [
    { selector: 'node', style: {
      shape: 'round-rectangle', width: 'data(w)', height: 34, 'background-color': 'data(bg)',
      'border-width': 1.5, 'border-color': 'data(bd)', label: 'data(label)', color: c('graph-node-ink'),
      'font-family': FONT, 'font-size': 12, 'font-weight': 600, 'text-valign': 'center', 'text-halign': 'center',
      'min-zoomed-font-size': 5, 'overlay-padding': 4 } },
    { selector: 'node[?partial]', style: { 'border-style': 'dashed', 'background-color': c('graph-partial') } },
    { selector: 'node[?view]', style: { 'font-style': 'italic' } },
    { selector: 'edge', style: {
      'curve-style': 'bezier', width: 1.4, 'line-color': c('graph-edge'), label: 'data(label)', color: c('graph-edge-ink'),
      'font-family': FONT, 'font-size': 10, 'text-rotation': 'autorotate', 'text-background-color': c('surface-2'),
      'text-background-opacity': 0.92, 'text-background-padding': 2, 'min-zoomed-font-size': 7,
      'source-label': 'data(sl)', 'target-label': 'data(tl)', 'source-text-offset': 13, 'target-text-offset': 13,
      // A table that points at itself: the loop stands on the table's top edge. Both of its control points are
      // above the box whatever its width (1.4 x 46 px from the middle, 5 degrees right and 65 left of straight up).
      // At -45deg one of them was INSIDE a box wider than 127 px: no loop was drawn and Cytoscape warned
      // "Edge has invalid endpoints" (HR_DEPARTMENT in the demo database).
      'loop-direction': '-30deg', 'loop-sweep': '-70deg', 'control-point-step-size': 46 } },
    // Its label stands level, beside the loop on its right (lx: loopLabel()), not written across it.
    { selector: 'edge:loop', style: { 'text-rotation': 'none', 'text-margin-x': 'data(lx)', 'text-margin-y': LOOP_LABEL_DOWN } },
    { selector: 'edge[kind != "declared"]', style: { 'line-style': 'dashed', 'line-dash-pattern': [6, 4] } },
    { selector: 'edge[kind = "names"]', style: { 'line-color': c('graph-edge-light') } },
    { selector: 'edge[kind = "values"]', style: { 'line-style': 'dotted', width: 2 } },      // accepted from value overlap
    { selector: 'edge.nolabel', style: { label: '', 'source-label': '', 'target-label': '' } },
    { selector: '.faded', style: { opacity: 0.2 } },
    { selector: 'node.neighbor', style: { 'border-color': c('graph-neighbor'), 'border-width': 2 } },
    { selector: 'node.root', style: { 'border-color': c('graph-root'), 'border-width': 3 } },
    { selector: 'node.selected', style: { 'border-color': c('graph-selected-line'), 'border-width': 3, 'background-color': c('graph-selected') } },
    { selector: 'edge.hl', style: { 'line-color': c('graph-hl'), width: 2.2, color: c('graph-hl-ink'), 'z-index': 20 } },
    { selector: '.hidden', style: { display: 'none' } },
  ];
}

/** Fill and line of a table in the graph: by schema (node data `si`), or plain when there is one schema. */
function nodeColors(si) {
  if (si < 0) return [themeColor('graph-node'), themeColor('graph-node-line')];
  const n = (si % SCHEMA_COLORS) + 1;
  return [themeColor(`schema-${n}`), themeColor(`schema-${n}-line`)];
}

/** The theme changed. The graph is given its colours again and nothing else: the tables stay where
    they are, and so do the zoom and what is selected. */
function applyTheme() {
  const theme = window.dataExplorerTheme;
  if (theme) for (const input of document.querySelectorAll('#theme-switch input')) input.checked = input.value === theme.choice();
  const cy = state.cy;
  if (cy) {
    cy.batch(() => cy.nodes().forEach((n) => {
      const [bg, bd] = nodeColors(n.data('si'));
      n.data({ bg, bd });
    }));
    cy.style(graphStyle());
  }
  runHooks('theme');
}

function buildGraph() {
  if (state.cy) { state.cy.destroy(); state.cy = null; }
  state.laidOut = '';
  const m = state.model;
  $('#graph-empty').hidden = m.tables.length > 0;
  if (!m.tables.length) { updateStatus(); return; }
  const nodes = m.tables.map((t) => {
    const si = m.schemas.length > 1 ? m.schemas.indexOf(t.schema || '') : -1;
    const [bg, bd] = nodeColors(si);
    return { group: 'nodes', data: { id: t.id, label: t.name, w: nodeWidth(t.name), bg, bd, si,
                                     partial: !t.complete, view: t.kind === 'view' } };
  });
  // Edges run from the "one" side to the "many" side, so the hierarchy layout puts parents first.
  const edges = m.relationships.map((r) => {
    const known = r.cardinality !== 'unknown';
    const label = edgeLabel(r);
    return { group: 'edges', data: { id: r.id, source: r.to, target: r.from, label, kind: r.source,
                                     sl: known ? '1' : '', tl: known ? (r.cardinality === '1-1' ? '1' : 'N') : '',
                                     lx: r.to === r.from ? loopLabel(label) : 0 } };
  });
  const big = nodes.length > 250;
  const cy = cytoscape({
    container: $('#graph'), elements: nodes.concat(edges), style: graphStyle(), layout: { name: 'preset' },
    minZoom: 0.05, maxZoom: 3, boxSelectionEnabled: false, selectionType: 'single',
    textureOnViewport: big, hideEdgesOnViewport: big,
  });
  cy.on('tap', 'node', (evt) => selectTable(evt.target.id()));
  cy.on('dbltap', 'node', (evt) => focusOn(evt.target.id()));
  cy.on('tap', (evt) => { if (evt.target === cy) selectTable(null); });
  cy.on('tap', 'edge', (evt) => {
    const e = evt.target;
    const other = e.source().id() === state.selected ? e.target().id() : e.source().id();
    selectTable(other);
  });
  state.cy = cy;
  applyView({ fit: true });
}

/** Which tables and relationships are visible. The server applies the same rules for "current view" exports. */
function visibleSets() {
  const m = state.model;
  const f = state.filters;
  const focus = state.focus.on && state.tables.has(state.focus.root) ? state.focus.root : null;
  const tables = new Set();
  for (const t of m.tables) if (f.schema === '*' || (t.schema || '') === f.schema || t.id === focus) tables.add(t.id);
  let rels = m.relationships.filter((r) => f.kinds[r.source]
    && (r.source === 'declared' || (r.confidence || 0) >= f.minConf) && tables.has(r.from) && tables.has(r.to));
  if (focus) {
    const adj = new Map();
    const link = (a, b) => { if (!adj.has(a)) adj.set(a, new Set()); adj.get(a).add(b); };
    for (const r of rels) { link(r.from, r.to); link(r.to, r.from); }
    const depth = new Map([[focus, 0]]);
    const queue = [focus];
    while (queue.length) {
      const u = queue.shift();
      if (depth.get(u) >= state.focus.hops) continue;
      for (const v of adj.get(u) || []) if (!depth.has(v)) { depth.set(v, depth.get(u) + 1); queue.push(v); }
    }
    for (const id of [...tables]) if (!depth.has(id)) tables.delete(id);
    rels = rels.filter((r) => depth.has(r.from) && depth.has(r.to));
  }
  return { tables, rels: new Set(rels.map((r) => r.id)), focus };
}

function applyView({ fit = false, relayout = false } = {}) {
  const cy = state.cy;
  if (!cy) { updateStatus(); return; }
  const vis = visibleSets();
  cy.batch(() => {
    cy.nodes().forEach((n) => n.toggleClass('hidden', !vis.tables.has(n.id())));
    cy.edges().forEach((e) => e.toggleClass('hidden', !vis.rels.has(e.id())));
    cy.nodes().removeClass('root');
    if (vis.focus) cy.getElementById(vis.focus).addClass('root');
  });
  const signature = [...vis.tables].sort().join('|');
  if (relayout || signature !== state.laidOut) {
    state.laidOut = signature;
    runLayout();
  } else if (fit) {
    fitView();
  }
  applyHighlight();
  updateStatus(vis);
  document.querySelectorAll('#table-list .table-item').forEach((li) =>
    li.classList.toggle('dim', !vis.tables.has(li.dataset.id)));
}

/** Fit the visible tables in the window, without blowing a small view up beyond 125%. */
function fitView() {
  const cy = state.cy;
  if (!cy) return;
  const visible = cy.elements().not('.hidden');
  if (visible.empty()) return;
  cy.fit(visible, 40);
  if (cy.zoom() > 1.25) { cy.zoom(1.25); cy.center(visible); }
}

function layoutOptions(name, count, direction = 'LR') {
  if (name === 'dagre') {
    const across = direction === 'LR';
    return { name: 'dagre', rankDir: direction, nodeSep: across ? 26 : 44, edgeSep: 12, rankSep: across ? 130 : 96,
             ranker: 'network-simplex', fit: false, animate: false };
  }
  if (name === 'fcose') {
    return { name: 'fcose', quality: count > 400 ? 'draft' : 'default', randomize: true, animate: false, fit: false,
             nodeDimensionsIncludeLabels: true, packComponents: false, nodeSeparation: 90,
             idealEdgeLength: () => 150, nodeRepulsion: () => 14000, edgeElasticity: () => 0.4, gravity: 0.3,
             numIter: 2500 };
  }
  return { name: 'grid', fit: false, animate: false, avoidOverlapPadding: 24, condense: true };
}

/** After the hierarchy layout: a line is at least as long as its label needs (labelRoom).
    The layout puts the tables in ranks - columns from left to right, or rows from top to bottom - a fixed
    gap apart, and a label is written along its line: "MSISDN_ACTIVATED → MSISDN" on a line of 96 px covered
    both tables. The ranks are moved apart, each gap by what the lines across it still lack and no more; a
    table never leaves its rank, so nothing comes to lie on anything else. */
function makeRoomForLabels(nodes, edges, direction) {
  const axis = direction === 'LR' ? 'x' : 'y';
  const half = (n) => (axis === 'x' ? n.width() : n.height()) / 2;
  const at = (n) => Math.round(n.position(axis));
  const ranks = [...new Set(nodes.map(at))].sort((a, b) => a - b);
  const rank = new Map(ranks.map((position, i) => [position, i]));
  const lines = edges.map((e) => {
    const [a, b] = at(e.source()) <= at(e.target()) ? [e.source(), e.target()] : [e.target(), e.source()];
    return { from: rank.get(at(a)), to: rank.get(at(b)), room: labelRoom(e.data('label')),
             length: (b.position(axis) - half(b)) - (a.position(axis) + half(a)) };
  }).filter((line) => line.to > line.from).sort((x, y) => (x.to - x.from) - (y.to - y.from));
  const more = new Array(ranks.length).fill(0);            // more[i]: how much the gap in front of rank i grows
  for (const line of lines) {
    let gained = 0;
    for (let i = line.from + 1; i <= line.to; i += 1) gained += more[i];
    const lacking = line.room - line.length - gained;
    if (lacking > 0) for (let i = line.from + 1; i <= line.to; i += 1) more[i] += lacking / (line.to - line.from);
  }
  const shift = [];
  more.reduce((sum, extra, i) => (shift[i] = sum + extra), 0);
  if (!shift.some((by) => by > 0)) return;
  nodes.forEach((n) => { n.position(axis, n.position(axis) + shift[rank.get(at(n))]); });
}

const LINE_MARGIN = 8;          // the free space kept between a line and a table it passes

/** The part of a straight line inside a box: [t0, t1] along the line (0 at a, 1 at b), or null when it misses. */
function lineInBox(a, b, box) {
  let t0 = 0;
  let t1 = 1;
  for (const [d, low, high, from] of [[b.x - a.x, box.x1, box.x2, a.x], [b.y - a.y, box.y1, box.y2, a.y]]) {
    if (d === 0) {
      if (from <= low || from >= high) return null;
      continue;
    }
    let [u, v] = [(low - from) / d, (high - from) / d];
    if (u > v) [u, v] = [v, u];
    t0 = Math.max(t0, u);
    t1 = Math.min(t1, v);
    if (t1 - t0 <= 1e-9) return null;
  }
  return [t0, t1];
}

/** After the hierarchy layout: no line runs through the box of a table that is not one of its ends.
    A line is drawn straight from one table to the other, but the layout leaves a line that skips a rank a place of
    its own in the ranks between: the straight line can run behind a table there, and the account seemed to point at
    the payment method. The table in the way is moved aside in its rank, away from the line, together with the tables
    of its rank beyond it, so that nothing comes to lie on anything else (as makeRoomForLabels() moves ranks); then
    every line is checked again - a table that moved takes its own lines with it. */
function clearLines(nodes, edges, direction) {
  const axis = direction === 'LR' ? 'x' : 'y';            // across the ranks
  const across = axis === 'x' ? 'y' : 'x';                // along a rank
  const at = (n) => Math.round(n.position(axis));
  const box = (n) => {
    const p = n.position();
    const w = n.width() / 2 + LINE_MARGIN;
    const hgt = n.height() / 2 + LINE_MARGIN;
    return { x1: p.x - w, x2: p.x + w, y1: p.y - hgt, y2: p.y + hgt };
  };
  const crossings = (first) => {
    const found = [];
    for (const e of edges.toArray()) {
      const a = e.source().position();
      const b = e.target().position();
      for (const n of nodes.toArray()) {
        if (n.same(e.source()) || n.same(e.target()) || !lineInBox(a, b, box(n))) continue;
        found.push({ e, a, b, n });
        if (first) return found;
      }
    }
    return found;
  };
  const before = nodes.map((n) => [n, { ...n.position() }]);
  const went = new Map();                                 // table id -> the way it was moved: 1 or -1
  for (let round = 0; round < 20 * nodes.length; round += 1) {
    const [hit] = crossings(true);
    if (!hit) return true;
    const { e, a, b, n } = hit;
    const room = box(n);
    // Where the line is along the rank, over the width of the table's box across the ranks.
    const [low, high] = axis === 'x' ? [room.x1, room.x2] : [room.y1, room.y2];
    const d = b[axis] - a[axis];
    let [t0, t1] = d === 0 ? [0, 1] : [(low - a[axis]) / d, (high - a[axis]) / d];
    if (t0 > t1) [t0, t1] = [t1, t0];
    t0 = Math.max(0, t0);
    t1 = Math.min(1, t1);
    const ends = [a[across] + (b[across] - a[across]) * t0, a[across] + (b[across] - a[across]) * t1];
    const line = [Math.min(...ends), Math.max(...ends)];
    const middle = n.position(across);
    const half = (across === 'y' ? n.height() : n.width()) / 2 + LINE_MARGIN;
    // The side of the line the table is on decides which way it goes, the first time; after that it keeps going that
    // way (over the line, if need be), so that two lines never push it to and fro. The tables beyond it in its rank go
    // too - unless one of them is an end of the line, which would only take the line along: then the other way.
    const rank = at(n);
    const group = (way) => nodes.filter((m) => at(m) === rank && (way > 0 ? m.position(across) >= middle : m.position(across) <= middle));
    const carries = (way) => group(way).some((m) => m.same(e.source()) || m.same(e.target()));
    let away = went.get(n.id()) || (middle >= (line[0] + line[1]) / 2 ? 1 : -1);
    if (carries(away) && !carries(-away)) away = -away;
    went.set(n.id(), away);
    const by = away > 0 ? line[1] - (middle - half) + 1 : -((middle + half) - line[0] + 1);
    group(away).forEach((m) => {
      m.position(across, m.position(across) + by);
      if (!went.has(m.id())) went.set(m.id(), away);
    });
  }
  // Lines that could not all be cleared (seen only in dense views): keep the tables where they cross fewer.
  const left = crossings(false).length;
  const after = nodes.map((n) => [n, { ...n.position() }]);
  before.forEach(([n, p]) => n.position(p));
  if (left < crossings(false).length) after.forEach(([n, p]) => n.position(p));
  return false;
}

function runLayout() {
  const cy = state.cy;
  const visible = cy.elements().not('.hidden');
  const nodes = visible.nodes();
  if (nodes.empty()) return;
  const edges = visible.edges().filter((e) => !e.isLoop());
  const connected = edges.connectedNodes();
  const lonely = nodes.not(connected);
  let name = state.layout;
  if (name === 'auto') name = connected.length <= 80 ? 'dagre' : 'fcose';
  const finish = () => {
    placeLonely(lonely, connected);
    fitView();
  };
  if (connected.empty()) { finish(); return; }
  const part = connected.union(edges);
  // Every line shows its label only up to LABELS_UP_TO lines (applyHighlight): only then is room made for them.
  const labelled = visible.edges().length <= LABELS_UP_TO;
  if (name === 'dagre') {
    // Try left-to-right and top-to-bottom, and keep whichever shows the tables larger in this window.
    const fitZoom = () => {
      const box = part.boundingBox();
      return Math.min(cy.width() / (box.w + 80), cy.height() / (box.h + 80));
    };
    const arrange = (direction) => {
      part.layout(layoutOptions('dagre', connected.length, direction)).run();
      if (labelled) makeRoomForLabels(connected, edges, direction);
      clearLines(connected, edges, direction);
    };
    arrange('TB');
    const down = fitZoom();
    arrange('LR');
    if (down > fitZoom() * 1.1) arrange('TB');
    finish();
    return;
  }
  const layout = part.layout(layoutOptions(name, connected.length));
  layout.one('layoutstop', finish);
  layout.run();
}

/** Tables with no visible relationship go in rows under the rest, in name order. */
function placeLonely(lonely, connected) {
  if (lonely.empty()) return;
  const box = connected.empty() ? { x1: 0, y2: -60, w: 0 } : connected.boundingBox();
  const maxWidth = Math.max(box.w, 900);
  let x = box.x1;
  let y = box.y2 + 70;
  lonely.sort((a, b) => a.data('label').localeCompare(b.data('label'))).forEach((n) => {
    const w = n.data('w');
    if (x > box.x1 && x + w > box.x1 + maxWidth) { x = box.x1; y += 52; }
    n.position({ x: x + w / 2, y });
    x += w + 18;
  });
}

function applyHighlight() {
  const cy = state.cy;
  if (!cy) return;
  cy.batch(() => {
    cy.elements().removeClass('selected neighbor faded hl');
    const visible = cy.elements().not('.hidden');
    const node = state.selected ? cy.getElementById(state.selected) : cy.collection();
    if (node.nonempty() && !node.hasClass('hidden')) {
      const edges = node.connectedEdges().not('.hidden');
      const hood = edges.connectedNodes().union(node);
      visible.addClass('faded');
      hood.removeClass('faded');
      edges.removeClass('faded').addClass('hl');
      hood.not(node).addClass('neighbor');
      node.addClass('selected');
    }
    const edges = visible.edges();
    const crowded = edges.length > LABELS_UP_TO;
    cy.edges().forEach((e) => e.toggleClass('nolabel', crowded && !e.hasClass('hl')));
  });
}

function updateStatus(vis) {
  const box = $('#graph-status');
  if (!state.model || !state.model.tables.length) { box.textContent = ''; return; }
  vis = vis || visibleSets();
  const s = state.model.stats;
  const parts = [];
  if (vis.tables.size === s.tables) parts.push(plural(s.tables, 'table'));
  else parts.push(`${vis.tables.size} of ${plural(s.tables, 'table')}`);
  parts.push(`${vis.rels.size === s.relationships ? '' : vis.rels.size + ' of '}${plural(s.relationships, 'relationship')}` +
             ` (${s.declared || 0} declared, ${s.relationships - (s.declared || 0)} inferred)`);
  if (vis.focus) parts.push(`focus: ${state.tables.get(vis.focus).name}, ${plural(state.focus.hops, 'hop')}`);
  box.textContent = parts.join(' · ');
}

// -------------------------------------------------------- selection and focus ----

function selectTable(id, { center = false } = {}) {
  state.selected = id && state.tables.has(id) ? id : null;
  if (state.selected) {
    const vis = visibleSets();
    if (!vis.tables.has(state.selected)) {
      // The table is filtered out: move the focus to it, or drop the schema filter.
      if (state.focus.on) state.focus.root = state.selected;
      else { state.filters.schema = '*'; $('#schema-filter').value = '*'; renderTableList(); }
      applyView();
      center = false;
    }
  }
  document.querySelectorAll('#table-list .table-item').forEach((li) => {
    const on = li.dataset.id === state.selected;
    li.classList.toggle('selected', on);
    if (on) li.scrollIntoView({ block: 'nearest' });
  });
  applyHighlight();
  syncToolbar();
  if (center && state.selected && state.cy) {
    const node = state.cy.getElementById(state.selected);
    if (node.nonempty()) state.cy.animate({ center: { eles: node } }, { duration: 200 });
  }
  renderDetails();
  runHooks('select', state.selected);
}

function focusOn(id) {
  if (!state.tables.has(id)) return;
  state.selected = id;
  state.focus.on = true;
  state.focus.root = id;
  applyView({ fit: true });
  selectTable(id);
}

function toggleFocus() {
  if (state.focus.on) {
    state.focus.on = false;
    applyView({ fit: true });
  } else {
    if (!state.selected) { toast('Select a table first, then turn Focus on. Double-clicking a table also focuses on it.'); return; }
    state.focus.on = true;
    state.focus.root = state.selected;
    applyView({ fit: true });
  }
  syncToolbar();
}

function setHops(n) {
  state.focus.hops = Math.max(1, Math.min(MAX_HOPS, n));
  syncToolbar();
  if (state.focus.on) applyView({ fit: true });
}

function syncToolbar() {
  const on = state.focus.on;
  const button = $('#focus-toggle');
  button.setAttribute('aria-pressed', on ? 'true' : 'false');
  const root = on && state.tables.get(state.focus.root);
  button.textContent = root ? `Focus: ${root.name}` : 'Focus';
  $('#hops-value').textContent = plural(state.focus.hops, 'hop');
  $('#export-view-note').textContent = root ? `(${root.name}, ${plural(state.focus.hops, 'hop')})` : '(as filtered)';
}

// ------------------------------------------------------------------ details ----

async function renderDetails() {
  const box = $('#details');
  const token = ++state.detailsToken;
  if (!state.selected) { box.replaceChildren(...summaryView()); return; }
  let d = state.details.get(state.selected);
  if (!d) {
    box.replaceChildren(h('p', { class: 'note', text: 'Loading…' }));
    try {
      d = await api(`/workspaces/${enc(state.wsId)}/tables/${enc(state.selected)}`);
    } catch (err) {
      if (token === state.detailsToken) box.replaceChildren(h('p', { class: 'note', text: err.message }));
      return;
    }
    state.details.set(d.id, d);
  }
  if (token !== state.detailsToken) return;
  box.replaceChildren(...tableView(d));
  $('#right-panel').scrollTop = 0;
}

function summaryView() {
  const m = state.model;
  const s = m.stats;
  if (!s.tables) {
    return [h('div', { class: 'd-title', text: m.workspace.name }),
            h('p', { class: 'note', text: 'This workspace is empty. Add a source to begin.' })];
  }
  return [
    h('div', { class: 'd-title', text: m.workspace.name }),
    h('div', { class: 'd-sub', text: `${plural(m.workspace.sources.length, 'source')} merged into one model` }),
    h('div', { class: 'section' },
      h('div', { class: 'section-title' }, 'Summary'),
      h('dl', { class: 'kv' },
        h('dt', { text: 'Tables' }), h('dd', { text: s.tables }),
        h('dt', { text: 'Columns' }), h('dd', { text: s.columns }),
        (state.meta.relationship_kinds || []).flatMap((k) => [
          h('dt', { text: k.inferred ? 'Inferred ' + k.label : 'Declared keys' }), h('dd', { text: s[k.id] || 0 })]))),
    h('div', { class: 'callout' },
      'Click a table to see its columns and relationships. Double-click it to focus the graph on its neighbourhood.'),
  ];
}

/** A name that may wrap after an underscore instead of in the middle of a word. */
function breakable(name) {
  const parts = name.split('_');
  return parts.flatMap((p, i) => (i < parts.length - 1 ? [p + '_', document.createElement('wbr')] : [p]));
}

function keyTags(c) {
  const tags = [];
  if (c.pk) tags.push(h('span', { class: 'tag pk', text: 'PK', title: 'Primary key (declared)' }));
  else if (c.pk_inferred) tags.push(h('span', { class: 'tag pk guess', text: 'PK?', title: 'Looks like the key (inferred from the name)' }));
  // A primary key is unique already; any other column a constraint or a unique index makes unique says so.
  if (c.unique && !c.pk) tags.push(h('span', { class: 'tag uq', text: 'unique', title: 'Unique (a UNIQUE constraint or a unique index)' }));
  if (c.fk) tags.push(h('span', { class: 'tag fk', text: 'FK', title: 'Foreign key (declared)' }));
  else if (c.fk_inferred) tags.push(h('span', { class: 'tag fk guess', text: 'FK?', title: 'Points at another table (inferred)' }));
  return tags;
}

function tableView(d) {
  const many = state.model.schemas.length > 1;
  const out = [];
  out.push(h('div', { class: 'd-title', text: d.name }));
  const sub = [];
  if (d.schema) sub.push('schema ' + d.schema);
  sub.push(d.kind);
  sub.push(plural(d.columns.length, 'column'));
  if (d.aliases.length) sub.push('alias ' + d.aliases.join(', '));
  out.push(h('div', { class: 'd-sub', text: sub.join(' · ') }));
  if (d.comment) out.push(h('div', { class: 'd-sub', text: d.comment }));
  out.push(h('div', { class: 'd-tags' }, [...new Set(d.origins.map((o) => o.kind))].map((k) =>
    h('span', { class: 'tag ' + k, text: ORIGIN_TEXT[k] || k }))));
  out.push(h('div', { class: 'd-actions' },
    h('button', { class: 'btn small', type: 'button', text: 'Focus here', title: 'Show only this table and its neighbours',
                  onclick: () => focusOn(d.id) }),
    h('button', { class: 'btn small', type: 'button', text: 'Centre in graph', onclick: () => selectTable(d.id, { center: true }) })));
  if (!d.complete) {
    out.push(h('div', { class: 'callout', text: 'Only the columns that the SQL or a foreign key mentions are known for this table.' }));
  }

  // columns
  const rows = d.columns.map((c) => {
    const typeTitle = Object.entries(c.types || {}).map(([o, t]) => `${ORIGIN_TEXT[o] || o}: ${t}`).join('\n');
    const name = h('td', { class: 'c-name' + (c.pk ? ' is-pk' : ''), title: `${c.name}\nSeen in: ` + c.origins.map((o) => ORIGIN_TEXT[o] || o).join(', ') }, breakable(c.name));
    if (c.comment) name.append(h('div', { class: 'c-comment', text: c.comment }));
    return h('tr', {}, name,
      h('td', { class: 'c-type', title: typeTitle, text: c.type ? c.type.replace(/,\s+/g, ',') : '—' }),
      h('td', { class: 'c-keys' }, keyTags(c)),
      h('td', { class: 'c-null' + (c.nullable === false ? ' no' : ''),
                title: c.nullable === null ? 'Not known from this source' : (c.nullable ? 'NULL allowed' : 'NOT NULL'),
                text: c.nullable === null ? '—' : (c.nullable ? 'yes' : 'no') }));
  });
  out.push(h('div', { class: 'section' },
    h('div', { class: 'section-title' }, 'Columns', h('span', { class: 'n', text: d.columns.length })),
    h('table', { class: 'cols-table' },
      h('colgroup', {}, h('col', {}), h('col', { class: 'w-type' }), h('col', { class: 'w-keys' }), h('col', { class: 'w-null' })),
      h('thead', {}, h('tr', {}, h('th', { text: 'Name' }), h('th', { text: 'Type' }), h('th', { text: 'Key' }), h('th', { text: 'Null', title: 'NULL allowed' }))),
      h('tbody', {}, rows))));

  // relationships
  const groups = [
    ['references', 'Points to (many → one)'],
    ['referenced_by', 'Pointed at by (one ← many)'],
    ['self', 'Within this table'],
    ['match', 'Matched by value (no clear key)'],
  ];
  const section = h('div', { class: 'section' },
    h('div', { class: 'section-title' }, 'Relationships', h('span', { class: 'n', text: d.relationships.length })));
  if (!d.relationships.length) section.append(h('p', { class: 'note', text: 'None found for this table.' }));
  for (const [role, label] of groups) {
    const links = d.relationships.filter((r) => r.role === role);
    if (!links.length) continue;
    const group = h('div', { class: 'rel-group' }, h('div', { class: 'label', text: label }));
    for (const r of links) {
      const other = state.tables.get(r.other);
      const arrow = role === 'referenced_by' ? '←' : role === 'match' ? '=' : '→';
      const pair = r.columns.map((c, i) => `${c} ${arrow} ${other ? other.name : r.other}.${r.other_columns[i]}`).join(', ');
      const kind = r.source === 'declared' ? 'declared' : `${KIND_TEXT[r.source]} ${kindShare(r.source, r.confidence || 0)}`;
      const also = r.seen.filter((s) => s !== r.source).map((s) => KIND_TEXT[s]);
      const why = [r.cardinality !== 'unknown' ? r.cardinality : null, also.length ? 'also ' + also.join(', ') : null].filter(Boolean).join(' · ');
      group.append(h('button', { class: 'rel', type: 'button', title: r.evidence.join('\n') + '\nClick to open ' + r.other,
                                 onclick: () => selectTable(r.other, { center: true }) },
        h('span', { class: 'other', text: (many && other && other.schema ? other.schema + '.' : '') + (other ? other.name : r.other) }),
        h('span', { class: 'tag ' + r.source, text: kind }),
        h('span', { class: 'pair', text: pair }),
        why ? h('span', { class: 'why', text: why }) : null));
    }
    section.append(group);
  }
  out.push(section);

  // provenance
  out.push(h('div', { class: 'section' },
    h('div', { class: 'section-title' }, 'Where this came from'),
    h('ul', { class: 'origin-list' }, d.origins.map((o) => h('li', {},
      h('span', { class: 'tag ' + o.kind, text: ORIGIN_TAG[o.kind] || o.kind }),
      h('span', { class: 'what', text: `${ORIGIN_TEXT[o.kind] || o.kind} — ${o.label || 'source'}` }),
      o.detail ? h('span', { class: 'detail', text: o.detail }) : null))),
    d.columns.some((c) => c.pk_inferred || c.fk_inferred)
      ? h('p', { class: 'note', text: 'PK? / FK? are inferred from names or joins, not declared.' }) : null));
  return out;
}

// ------------------------------------------------------------------- export ----

/** The current view of the graph as query parameters. The export and the report apply the same rules. */
function viewQuery(q) {
  const f = state.filters;
  q.set('kinds', Object.keys(f.kinds).filter((k) => f.kinds[k]).join(','));
  if (f.minConf) q.set('min_confidence', String(f.minConf));
  if (f.schema !== '*') q.set('schema', f.schema);
  if (state.focus.on && state.tables.has(state.focus.root)) {
    q.set('focus', state.focus.root);
    q.set('hops', String(state.focus.hops));
  }
  return q;
}

function exportUrl(scope, format) {
  const q = new URLSearchParams({ format });
  if ($('#export-all-columns').checked && format === 'png') q.set('all_columns', 'true');
  if (scope === 'view') viewQuery(q);
  return `/api/workspaces/${enc(state.wsId)}/export?${q}`;
}

function download(url) {
  const a = h('a', { href: url, download: '' });
  document.body.append(a);
  a.click();
  a.remove();
}

// ------------------------------------------------------------------- wiring ----

/** One checkbox per kind of relationship, as listed by the server. */
function renderKindToggles() {
  const kinds = state.meta.relationship_kinds || [];
  $('#kind-toggles').replaceChildren(...kinds.map((k) => {
    KIND_TEXT[k.id] = k.label;
    state.filters.kinds[k.id] = true;
    const line = 'line ' + (k.id === 'values' ? 'dotted' : k.inferred ? 'dashed' : 'solid') + (k.id === 'names' ? ' light' : '');
    return h('label', { class: 'chip-toggle', title: k.hint },
      h('input', { type: 'checkbox', id: 'kind-' + k.id, checked: true,
                   onchange: (e) => { state.filters.kinds[k.id] = e.target.checked; applyView(); } }),
      h('span', { class: line }), k.short);
  }));
}

function renderTabs() {
  const features = state.meta.features || {};
  $('#tabs').replaceChildren(...TABS.map((t) => {
    const on = !!features[t.id];
    return h('button', { class: 'tab' + (t.id === state.tab ? ' active' : ''), type: 'button', disabled: !on,
                         'data-tab': t.id, title: on ? '' : 'Planned - not built yet',
                         onclick: () => showTab(t.id) },
      t.label, on ? null : h('span', { class: 'soon', text: 'soon' }));
  }));
}

/** Show one feature tab; each has its own view in the centre panel. */
function showTab(id) {
  if (!(state.meta.features || {})[id] || !$('#view-' + id)) return;
  state.tab = id;
  for (const b of document.querySelectorAll('#tabs .tab')) b.classList.toggle('active', b.dataset.tab === id);
  for (const v of document.querySelectorAll('.center > .view')) v.hidden = v.id !== 'view-' + id;
  if (id === 'er_graph' && state.cy) { state.cy.resize(); fitView(); }
  runHooks('tab', id);
}

function wireEvents() {
  const guard = (fn) => (...args) => Promise.resolve(fn(...args)).catch(fail);

  const theme = window.dataExplorerTheme;          // static/theme.js, loaded before the page was drawn
  if (theme) {
    for (const input of document.querySelectorAll('#theme-switch input')) {
      input.addEventListener('change', () => { if (input.checked) theme.set(input.value); });
    }
    theme.onChange(applyTheme);
    applyTheme();
  }

  $('#ws-select').addEventListener('change', guard((e) => openWorkspace(e.target.value)));
  $('#ws-new').addEventListener('click', guard(newWorkspace));
  $('#ws-rename').addEventListener('click', guard(renameWorkspace));
  $('#ws-delete').addEventListener('click', guard(deleteWorkspace));

  $('#add-source').addEventListener('click', () => openSourceDialog());
  $('#empty-add').addEventListener('click', () => openSourceDialog());
  for (const b of document.querySelectorAll('#source-dialog .seg button')) b.addEventListener('click', () => showPane(b.dataset.pane));
  $('#pane-sql').addEventListener('submit', (e) => { e.preventDefault(); addSource(e.target, () => submitFiles(e.target, 'sql')); });
  $('#pane-files').addEventListener('submit', (e) => { e.preventDefault(); addSource(e.target, () => submitFiles(e.target, 'files')); });
  $('#pane-db').addEventListener('submit', (e) => {
    e.preventDefault();
    addSource(e.target, () => sendJSON(`/workspaces/${enc(state.wsId)}/sources/db`, dbBody()));
  });
  $('#pane-db [name=engine]').addEventListener('change', onEngineChange);
  $('#db-test').addEventListener('click', testConnection);

  $('#table-search').addEventListener('input', (e) => { state.filters.search = e.target.value; renderTableList(); });
  $('#schema-filter').addEventListener('change', (e) => {
    state.filters.schema = e.target.value;
    const hidden = state.selected && !visibleSets().tables.has(state.selected);
    renderTableList();
    applyView({ fit: true });
    if (hidden) selectTable(null);             // the selected table is no longer in view
  });

  $('#focus-toggle').addEventListener('click', toggleFocus);
  $('#hops-minus').addEventListener('click', () => setHops(state.focus.hops - 1));
  $('#hops-plus').addEventListener('click', () => setHops(state.focus.hops + 1));
  $('#min-conf').addEventListener('change', (e) => { state.filters.minConf = Number(e.target.value); applyView(); });
  $('#layout-select').addEventListener('change', (e) => { state.layout = e.target.value; applyView({ relayout: true }); });
  $('#relayout').addEventListener('click', () => applyView({ relayout: true }));
  const zoomBy = (factor) => {
    const cy = state.cy;
    if (cy) cy.zoom({ level: cy.zoom() * factor, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
  };
  $('#zoom-in').addEventListener('click', () => zoomBy(1.25));
  $('#zoom-out').addEventListener('click', () => zoomBy(0.8));
  $('#zoom-fit').addEventListener('click', fitView);

  const menu = $('#export-menu');
  const exportButton = $('#export-btn');
  const closeMenu = () => { menu.hidden = true; exportButton.setAttribute('aria-expanded', 'false'); };
  exportButton.addEventListener('click', (e) => {
    e.stopPropagation();
    menu.hidden = !menu.hidden;
    exportButton.setAttribute('aria-expanded', menu.hidden ? 'false' : 'true');
  });
  menu.addEventListener('click', (e) => {
    e.stopPropagation();
    const b = e.target.closest('button[data-format]');
    if (!b) return;
    if (!state.model || !state.model.tables.length) { toast('There is nothing to export yet.'); return; }
    download(exportUrl(b.dataset.scope, b.dataset.format));
    closeMenu();
  });
  document.addEventListener('click', closeMenu);

  const toggle = (cls) => () => {
    $('#layout').classList.toggle(cls);
    if (state.cy) { state.cy.resize(); fitView(); }
  };
  $('#toggle-left').addEventListener('click', toggle('no-left'));
  $('#toggle-right').addEventListener('click', toggle('no-right'));
  // The graph's box also changes size when the tab bar or the line under the graph wraps.
  new ResizeObserver(() => { if (state.cy) state.cy.resize(); }).observe($('#graph'));
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !document.querySelector('dialog[open]')) { closeMenu(); selectTable(null); }
    if (e.key === '/' && !/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName) && !document.querySelector('dialog[open]')) {
      e.preventDefault();
      $('#table-search').focus();
    }
  });
}

document.addEventListener('DOMContentLoaded', init);
