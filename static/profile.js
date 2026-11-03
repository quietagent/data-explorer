'use strict';

/* Profile and Quality tabs. Loaded after app.js and uses its helpers (state, h, api, $, toast ...).
   Everything shown comes from this app's own /api; the charts are drawn by the bundled Chart.js. */

const SEVERITY = {
  high: { label: 'High', icon: '▲' },
  medium: { label: 'Medium', icon: '◆' },
  low: { label: 'Low', icon: '●' },
};

const prof = {
  overview: null,               // GET /workspaces/{ws}/profile
  job: null,                    // the job being watched
  timer: null,
  seenDone: -1,
  table: null,                  // profile of the open table
  tableToken: 0,
  column: null,                 // the column whose details are open
  checked: new Set(),           // tables ticked in the overview
  sort: { by: 'name', dir: 1 }, // the overview list: by table name or by rows, either way
  chart: null,
  showPersonal: false,
};
const qual = { data: null, severity: new Set(), table: '*', rule: '*' };

const wsPath = (rest = '') => `/workspaces/${enc(state.wsId)}${rest}`;
const share = (part, total) => (total ? part / total : 0);
const pct = (x) => (x === 0 ? '0%' : x < 0.001 ? '<0.1%' : x >= 0.9995 && x < 1 ? '99.9%'
  : (x * 100).toFixed(x < 0.1 ? 1 : 0).replace(/\.0$/, '') + '%');

function showNumber(v) {
  if (v == null) return '—';
  if (typeof v !== 'number') return String(v);
  if (Number.isInteger(v)) return whole(v);
  return v.toLocaleString('en-US', { maximumFractionDigits: Math.abs(v) < 10 ? 3 : 2 });
}

function when(stamp) {
  if (!stamp) return '';
  const d = new Date(stamp);
  return Number.isNaN(d.getTime()) ? stamp : d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
}

// --------------------------------------------------------------------- jobs ----

async function loadOverview() {
  if (!state.wsId) return;
  const overview = await api(wsPath('/profile'));
  prof.overview = overview;
  prof.showPersonal = overview.show_personal;
  $('#show-personal').checked = prof.showPersonal;
  $('#personal-banner').hidden = !prof.showPersonal;
  const byId = new Map(overview.tables.map((t) => [t.id, t]));
  for (const t of state.model.tables) {            // row counts for the table list
    const row = byId.get(t.id);
    if (row) { t.row_count = row.row_count; t.row_count_kind = row.row_count_kind; t.can_profile = row.can_profile; }
  }
  renderTableList();
  if (overview.job && overview.job.state === 'running' && (!prof.job || prof.job.id !== overview.job.id)) watchJob(overview.job);
  else if (!prof.job) renderJob();
}

// A workspace runs one job at a time - profiling, a relationship check or a scan of scripts - so every tab
// with jobs shows it.
const JOB_BOXES = [['job-box', 'job-progress', 'job-text', 'job-cancel'],
                   ['rel-job-box', 'rel-job-progress', 'rel-job-text', 'rel-job-cancel'],
                   ['scripts-job-box', 'scripts-job-progress', 'scripts-job-text', 'scripts-job-cancel']];
// (scripts-rescan and scripts-forget are only on the page while a scan is shown)
const JOB_BUTTONS = ['profile-all', 'profile-selected', 'profile-counts', 'profile-clear',
                     'rel-check-all', 'rel-check-selected', 'rel-suggest', 'scripts-scan', 'scripts-rescan', 'scripts-forget'];

/** What a running job is doing, in a few words. */
function jobLine(job) {
  const step = job.total ? ` ${Math.min(job.done + 1, job.total)} of ${job.total}` : '';
  let what;
  if (job.cancel_requested) what = 'Cancelling…';
  else if (job.kind === 'relations') {
    what = job.phase === 'suggesting' ? `Measuring the columns that share values${step}`
      : job.phase === 'looking' ? 'Looking for columns that share values…'
        : job.phase === 'keys' ? 'Reading the keys…' : `Checking relationships${step}`;
  } else if (job.kind === 'scripts') what = job.phase === 'saving' ? 'Saving the scan…' : `Reading scripts${step}`;
  else if (job.phase === 'row counts' && !job.counts_only) what = 'Counting rows…';
  else what = `${job.counts_only ? 'Counting' : 'Profiling'}${step}`;
  const current = job.current && !job.cancel_requested ? ` · ${job.current}` : '';
  return `${what}${current} · ${Math.round(job.elapsed_s)} s`;
}

function renderJob() {
  const job = prof.job;
  const running = !!job && job.state === 'running';
  for (const id of JOB_BUTTONS) if ($('#' + id)) $('#' + id).disabled = running;
  for (const [box, progress, text, cancel] of JOB_BOXES) {
    $('#' + box).hidden = !running;
    if (!running) continue;
    $('#' + progress).value = job.total ? Math.round(100 * job.done / job.total) : 0;
    $('#' + text).textContent = jobLine(job);
    $('#' + text).title = job.current || '';
    $('#' + cancel).disabled = job.cancel_requested;
  }
}

function watchJob(job) {
  clearTimeout(prof.timer);
  prof.job = job;
  prof.seenDone = -1;
  renderJob();
  if (job.state === 'running') prof.timer = setTimeout(pollJob, 500);
}

async function pollJob() {
  if (!prof.job) return;
  let job;
  try {
    job = await api('/jobs/' + enc(prof.job.id));
  } catch (err) {                                   // the app was restarted: the job is gone
    prof.job = null;
    renderJob();
    return;
  }
  if (job.workspace_id !== state.wsId) { prof.job = null; renderJob(); return; }
  prof.job = job;
  renderJob();
  if (job.state === 'running') {
    if (job.done !== prof.seenDone) {               // something finished: show the new counts or results
      prof.seenDone = job.done;
      if (job.kind !== 'profile') runHooks('job', job, false);
      else loadOverview().then(() => { if (state.tab === 'profile' && !state.selected) renderProfile(); }).catch(() => {});
    }
    prof.timer = setTimeout(pollJob, 700);
    return;
  }
  if (job.kind !== 'profile') runHooks('job', job, true);       // analysis.js (relations) and scripts.js report it
  else await jobFinished(job);
}

async function jobFinished(job) {
  const n = job.tables_done;
  const failed = job.errors.length;
  const what = job.counts_only ? 'counted' : 'profiled';
  if (job.state === 'cancelled') toast(`Cancelled: ${plural(n, 'table')} ${what} before it stopped. Their results are kept.`, 'info', 7000);
  else if (job.state === 'failed') toast(`The job failed: ${job.error}`, 'error', 12000);
  else toast(`${plural(n, 'table')} ${what} in ${Math.round(job.elapsed_s)} s.` + (failed ? ` ${plural(failed, 'table')} could not be read.` : ''),
    failed ? 'info' : 'ok', 6000);
  if (failed) toast(job.errors.slice(0, 6).map((e) => `${e.table}: ${e.message}`).join('\n'), 'error', 15000);
  if (job.notes.length) toast(job.notes.join('\n'), 'info', 12000);
  state.details.clear();
  await loadOverview();
  qual.data = null;
  if (state.tab === 'profile') await renderProfile();
  if (state.tab === 'quality') await renderQuality();
}

async function startJob(body) {
  let job;
  try {
    job = await sendJSON(wsPath('/profile'), body);
  } catch (err) {
    const data = err.data || {};
    if (data.needs_password && data.source) {
      const pw = await ask('Password needed', { type: 'password', ok: 'Start',
        text: `Enter the password for ${data.source.label}. It is kept in memory only until the app is closed.` });
      if (!pw) return;
      return startJob({ ...body, passwords: { ...(body.passwords || {}), [data.source.id]: pw } });
    }
    if (err.status === 409 && data.job) { watchJob(data.job); toast(err.message); return; }
    throw err;
  }
  if (body.passwords) refreshSources().catch(() => {});
  watchJob(job);
}

async function cancelJob() {
  if (!prof.job) return;
  prof.job = await api('/jobs/' + enc(prof.job.id), { method: 'DELETE' });
  renderJob();
}

function profileSelected() {
  const ids = state.selected ? [state.selected] : [...prof.checked].filter((id) => state.tables.has(id));
  if (!ids.length) { toast('Open a table, or tick tables in the list, then press "Profile selected".'); return; }
  return startJob({ tables: ids });
}

async function clearStatistics() {
  const sure = await ask('Clear saved statistics?', { type: 'none', ok: 'Clear',
    text: 'The row counts and column statistics saved for this workspace are deleted from this PC. Your marks of personal columns are kept. The database and files are not touched.' });
  if (!sure) return;
  const done = await api(wsPath('/profile'), { method: 'DELETE' });
  toast(`Saved statistics of ${plural(done.cleared_tables, 'table')} were deleted.`, 'ok', 4000);
  state.details.clear();
  qual.data = null;
  await loadOverview();
  await renderProfile();
}

async function setShowPersonal(on) {
  if (on) {
    const sure = await ask('Show personal values?', { type: 'none', ok: 'Show them',
      text: 'The real names, phone numbers, IDs, e-mail addresses and pay are read from the source again and shown on this screen until you switch this off or close the app. They are not saved.' });
    if (!sure) { $('#show-personal').checked = false; return; }
  }
  const session = await sendJSON('/session', { show_personal: !!on }, 'PUT');
  prof.showPersonal = session.show_personal;
  $('#show-personal').checked = prof.showPersonal;
  $('#personal-banner').hidden = !prof.showPersonal;
  if (state.tab === 'profile') await renderProfile();
}

// ------------------------------------------------------------- profile: list ----

async function renderProfile() {
  if (state.tab !== 'profile' || !state.model) return;
  const box = $('#profile-body');
  if (!state.selected) {
    if (!prof.overview) await loadOverview();
    destroyChart();
    box.replaceChildren(...overviewView());
    return;
  }
  const token = ++prof.tableToken;
  const id = state.selected;
  if (!prof.table || prof.table.table !== id) box.replaceChildren(h('p', { class: 'note pad', text: 'Loading…' }));
  let data;
  try {
    data = await api(wsPath(`/tables/${enc(id)}/profile?sample=false`));
  } catch (err) {
    if (token === prof.tableToken) box.replaceChildren(h('p', { class: 'note pad', text: err.message }));
    return;
  }
  if (token !== prof.tableToken) return;
  if (!prof.table || prof.table.table !== id) prof.column = prof.column && data.columns.some((c) => c.name === prof.column) ? prof.column : null;
  prof.table = data;
  destroyChart();
  box.replaceChildren(...tableProfileView(data));
  drawHistogram();
  const open = box.querySelector('tr.col-row.open');
  if (open) open.scrollIntoView({ block: 'nearest' });
  loadSample(id, token);
}

function overviewView() {
  const rows = prof.overview.tables;
  const able = rows.filter((t) => t.can_profile);
  const done = able.filter((t) => t.profiled_at).length;
  const head = h('div', { class: 'view-head' },
    h('div', {}, h('div', { class: 'view-title', text: 'Profile' }),
      h('div', { class: 'd-sub', text: able.length
        ? `${done} of ${plural(able.length, 'table')} profiled · click a table to see its columns`
        : 'No table of this workspace has data to profile' })));
  if (!rows.length) return [head, h('p', { class: 'note pad', text: 'This workspace is empty. Add a source to begin.' })];
  const many = state.model.schemas.length > 1;
  const anySampled = rows.some((t) => t.sampled);            // the last column is only there when it has something to say
  const sorted = sortOverview(rows, prof.sort);
  const heading = (id, label, num) => {
    const on = prof.sort.by === id;
    return h('th', { class: 'sortable' + (num ? ' num' : ' pin pin-last'), title: 'Click to sort',
                     'aria-sort': on ? (prof.sort.dir > 0 ? 'ascending' : 'descending') : 'none',
                     // first click on "Rows": the biggest tables on top
                     onclick: () => { prof.sort = { by: id, dir: on ? -prof.sort.dir : (num ? -1 : 1) }; renderProfile().catch(fail); } },
      label, h('span', { class: 'arrow', 'aria-hidden': 'true', text: on ? (prof.sort.dir > 0 ? '▲' : '▼') : '' }));
  };
  const body = sorted.map((t) => {
    const box = h('input', { type: 'checkbox', 'aria-label': 'Select ' + t.id, disabled: !t.can_profile, checked: prof.checked.has(t.id),
      onclick: (e) => e.stopPropagation(),
      onchange: (e) => { if (e.target.checked) prof.checked.add(t.id); else prof.checked.delete(t.id); syncSelectedButton(); } });
    const table = state.tables.get(t.id) || {};
    const stopped = t.status === 'timed_out' || t.status === 'failed';
    const status = !t.can_profile ? h('span', { class: 'muted', text: 'structure only, no data to profile' })
      : stopped ? h('span', { title: t.status_message || '' },
        h('span', { class: 'tag ' + (t.status === 'failed' ? 'bad' : 'warn'), text: t.status === 'failed' ? 'failed' : 'timed out' }),
        t.profiled_at ? h('span', { class: 'muted', text: ` last profile ${when(t.profiled_at)}` }) : null)
        : t.profiled_at ? h('span', { text: when(t.profiled_at) }) : h('span', { class: 'muted', text: 'not profiled yet' });
    return h('tr', { class: 'click', onclick: () => selectTable(t.id) },
      h('td', { class: 'c-check pin' }, box),
      h('td', { class: 'c-strong pin pin-last', title: t.id }, breakable(many ? t.id : (table.name || t.id))),
      h('td', { class: 'num' + (rowsMark(t.row_count_kind) ? ' estimate' : ''), title: countTitle(t),
                text: t.row_count == null ? '—' : rowsMark(t.row_count_kind) + whole(t.row_count) }),
      h('td', { class: 'num', text: table.column_count == null ? '' : table.column_count }),
      h('td', {}, status),
      anySampled ? h('td', {}, t.sampled ? h('span', { class: 'tag sampled', text: `sample of ${compact(t.rows_profiled)}`,
        title: 'Profiled on a sample' + (t.sample_method ? `: ${t.sample_method}` : '') }) : null) : null);
  });
  return [head, h('div', { class: 'table-fit' }, h('table', { class: 'data-table overview-grid' + (anySampled ? ' with-sample' : '') },
    h('colgroup', {}, h('col', { class: 'w-check' }), h('col', {}), h('col', { class: 'w-rows' }), h('col', { class: 'w-ncols' }),
      h('col', { class: 'w-when' }), anySampled ? h('col', { class: 'w-sample' }) : null),
    h('thead', {}, h('tr', {}, h('th', { class: 'c-check pin' }), heading('name', 'Table', false), heading('rows', 'Rows', true),
      h('th', { class: 'num', text: 'Columns' }), h('th', { text: 'Profiled' }), anySampled ? h('th', {}) : null)),
    h('tbody', {}, body))),
    h('p', { class: 'note pad', text: '"~" marks an estimate taken from the database\'s own statistics; ">" a table that has ' +
      'at least that many rows and was not counted further. Database tables above ' +
      `${whole(prof.overview.settings.sample_threshold_rows)} rows are profiled on a sample of about ` +
      `${whole(prof.overview.settings.sample_target_rows)} rows. Click "Table" or "Rows" to sort the list.` })];
}

/** The tables of the Profile list in the order its headings ask for: by name or by rows, either way.
    Tables with the same rows stay in name order, and one that was not counted is always last. */
function sortOverview(rows, sort) {
  const byName = (a, b) => a.id.localeCompare(b.id);
  return [...rows].sort((a, b) => {
    if (sort.by !== 'rows') return sort.dir * byName(a, b);
    if (a.row_count == null || b.row_count == null) return (a.row_count == null) - (b.row_count == null) || byName(a, b);
    return sort.dir * (a.row_count - b.row_count) || byName(a, b);
  });
}

/** What a row count is, in words - and for a counted one, when it was counted: a later profile keeps that count. */
function countTitle(t) {
  const text = rowsTitle(t.row_count, t.row_count_kind);
  return t.row_count != null && t.row_count_kind === 'exact' && t.counted_at ? `${text.replace(' (counted)', '')} - counted ${when(t.counted_at)}` : text;
}

function syncSelectedButton() {
  const n = state.selected ? 1 : prof.checked.size;
  $('#profile-selected').textContent = n && !state.selected ? `Profile selected (${n})` : 'Profile selected';
}

// ------------------------------------------------------------ profile: table ----

function tableProfileView(d) {
  const t = state.tables.get(d.table) || { name: d.table };
  const out = [];
  const facts = [];
  const about = d.row_count_kind === 'estimate' ? 'about ' : d.row_count_kind === 'more_than' ? 'more than ' : '';
  // An exact count is kept when a later profile only learns "more than": say when it was taken if that was not the profile.
  const counted = d.row_count_kind === 'exact' && d.counted_at && d.counted_at !== d.profiled_at ? ` (counted ${when(d.counted_at)})` : '';
  if (d.row_count != null) facts.push(`${about}${plural(d.row_count, 'row').replace(String(d.row_count), whole(d.row_count))}${counted}`);
  facts.push(plural(d.columns.length, 'column'));
  if (d.source) facts.push(d.source.label);
  if (d.profiled_at) facts.push('profiled ' + when(d.profiled_at));
  const actions = h('div', { class: 'view-actions' });
  if (d.can_profile) {
    actions.append(h('button', { class: 'btn small', type: 'button', text: d.profiled ? 'Profile again' : 'Profile this table',
                                 onclick: () => startJob({ tables: [d.table] }).catch(fail) }));
    if (d.row_count_kind !== 'exact') {
      actions.append(h('button', { class: 'btn small', type: 'button', text: 'Exact count',
        title: 'Run COUNT(*) on this table, under the statement timeout', onclick: (e) => exactCount(d.table, e.target).catch(fail) }));
    }
  }
  out.push(h('div', { class: 'view-head' },
    h('div', {},
      h('button', { class: 'link', type: 'button', text: '← All tables', onclick: () => selectTable(null) }),
      h('div', { class: 'view-title', text: t.name || d.table }),
      h('div', { class: 'd-sub', text: facts.join(' · ') })),
    actions));
  if (!d.can_profile) {
    out.push(h('div', { class: 'callout', text: 'Structure only, no data to profile. This table is known from SQL files; add a live database or CSV / Excel exports to see its data.' }));
  } else if (d.status === 'timed_out' || d.status === 'failed') {
    out.push(h('div', { class: 'callout warn', role: 'note' },
      h('strong', { text: d.status === 'failed' ? 'The last profile failed. ' : 'The last profile timed out. ' }),
      d.status_message || '', d.profiled ? ' The statistics below are from the profile before it.' : ''));
  } else if (!d.profiled) {
    out.push(h('div', { class: 'callout', text: 'Not profiled yet. Press "Profile this table" (or "Profile all") to read its statistics.' }));
  }
  if (d.sampled) {
    const of = d.row_count == null ? '' : ` of ${about}${whole(d.row_count)}`;
    out.push(h('div', { class: 'callout sampled', role: 'note' },
      h('strong', { text: 'Sampled. ' }),
      `The statistics come from ${whole(d.rows_profiled)} rows${of} — ${d.sample_method}. ` +
      'Null percentages are estimates; distinct counts, top values and duplicates are those of the sample.'));
  }
  for (const note of d.notes || []) out.push(h('div', { class: 'callout', text: note }));

  // the column grid
  const rows = [];
  for (const c of d.columns) {
    const st = c.stats;
    const open = prof.column === c.name;
    const total = st ? st.rows : 0;
    const nullShare = st && st.nulls != null ? share(st.nulls, total) : null;
    const meter = nullShare == null ? h('span', { class: 'muted', text: '—' })
      : h('div', { class: 'meter' + (nullShare > 0.5 ? ' warn' : ''), title: `${whole(st.nulls)} of ${whole(total)} rows are empty` },
        h('span', { class: 'track' }, h('span', { class: 'fill', style: `width:${Math.max(nullShare ? 2 : 0, Math.round(nullShare * 100))}%` })),
        h('span', { class: 'value', text: pct(nullShare) }));
    const distinct = !st || st.distinct == null ? '—' : whole(st.distinct);
    const value = (v) => h('td', { class: 'c-value' + (st && st.masked ? ' masked' : ''), title: v == null ? '' : String(v), text: v == null ? '—' : showNumber(v) });
    const badge = h('button', {
      class: 'badge-btn' + (c.personal ? ' on' : ''), type: 'button',
      title: (c.personal_reason ? c.personal_reason + '. ' : '') + (c.personal ? 'Click to mark it not personal.' : 'Click to mark this column personal: its values are then masked.'),
      'aria-pressed': c.personal ? 'true' : 'false',
      onclick: (e) => { e.stopPropagation(); markPersonal(d.table, c).catch(fail); },
    }, c.personal ? 'Personal' : 'Mark');
    rows.push(h('tr', { class: 'col-row click' + (open ? ' open' : ''), 'data-column': c.name,
                        onclick: () => { prof.column = open ? null : c.name; renderProfileKeep(); } },
      h('td', { class: 'c-strong pin pin-last', title: c.name }, h('div', { class: 'col-name' },
        h('span', { class: 'caret', text: open ? '▾' : '▸' }), h('span', { class: 'n' }, breakable(c.name)), keyTags(c))),
      h('td', { class: 'c-type', text: c.type ? c.type.replace(/,\s+/g, ',') : '—', title: c.type || '' }),
      h('td', {}, meter),
      h('td', { class: 'num', title: st && st.unique ? 'Every value is different' : '', text: distinct + (st && st.unique ? ' ✓' : '') }),
      value(st ? st.min : null), value(st ? st.max : null),
      h('td', { class: 'c-personal' }, badge)));
    if (open) rows.push(h('tr', { class: 'col-detail' }, h('td', { colspan: 7 }, h('div', { class: 'detail-fit' }, columnDetail(c, d)))));
  }
  // The name column takes what the fixed columns leave, and the table has a minimum width (styles.css):
  // in a narrow panel it scrolls sideways inside .table-fit instead of squeezing the names.
  out.push(h('div', { class: 'table-fit' }, h('table', { class: 'data-table cols-grid' },
    h('colgroup', {}, h('col', {}), h('col', { class: 'w-type' }), h('col', { class: 'w-null' }), h('col', { class: 'w-num' }),
      h('col', { class: 'w-val' }), h('col', { class: 'w-val' }), h('col', { class: 'w-personal' })),
    h('thead', {}, h('tr', {}, h('th', { class: 'pin pin-last', text: 'Column' }), h('th', { text: 'Type' }), h('th', { text: 'Null %' }),
      h('th', { class: 'num', text: 'Distinct' }), h('th', { text: 'Min' }), h('th', { text: 'Max' }), h('th', { text: 'Personal' }))),
    h('tbody', {}, rows))));

  out.push(h('div', { class: 'section', id: 'sample-section' },
    h('div', { class: 'section-title' }, 'Sample rows'),
    h('div', { id: 'sample-box' }, d.can_profile ? h('p', { class: 'note', text: 'Reading the first rows…' })
      : h('p', { class: 'note', text: 'No data to show.' }))));
  return out;
}

/** Render again without losing the scroll position (a column was opened or closed). */
function renderProfileKeep() {
  if (!prof.table) return;
  const box = $('#profile-body');
  const top = box.scrollTop;
  const left = (box.querySelector('.table-fit') || {}).scrollLeft || 0;      // a narrow panel: the grid is scrolled sideways
  const sample = $('#sample-box');
  destroyChart();
  box.replaceChildren(...tableProfileView(prof.table));
  if (sample) $('#sample-box').replaceWith(sample);
  drawHistogram();
  box.scrollTop = top;
  if (left) box.querySelector('.table-fit').scrollLeft = left;
}

function columnDetail(c, d) {
  const st = c.stats;
  if (!st) return h('p', { class: 'note', text: d.can_profile ? 'Not profiled yet.' : 'Structure only, no data to profile.' });
  if (st.error) return h('p', { class: 'note', text: `Statistics could not be read for this column: ${st.error}` });
  const facts = h('dl', { class: 'kv tight' });
  const fact = (k, v) => { if (v != null && v !== '') facts.append(h('dt', { text: k }), h('dd', { text: v })); };
  const filled = st.rows - (st.nulls || 0);
  fact('Rows checked', whole(st.rows) + (d.sampled ? ' (sample)' : ''));
  fact('Empty', `${whole(st.nulls)} (${pct(share(st.nulls || 0, st.rows))})`);
  if (st.distinct != null) fact('Distinct', whole(st.distinct) + (st.unique ? ' — every value is different' : ''));
  if (st.avg != null) fact('Average', showNumber(st.avg));
  if (st.min_length != null) fact('Length', st.min_length === st.max_length ? `${st.min_length}` : `${st.min_length} – ${st.max_length}`);
  if (c.personal) fact('Personal', (c.personal_reason || 'yes') + (st.live ? ' · shown unmasked (read live)' : ' · values masked'));
  if (st.stale_mask) fact('Note', 'Still masked from the last profile: profile the table again to see the values.');
  if (st.note) fact('Note', st.note);

  const left = h('div', { class: 'detail-block' }, h('div', { class: 'block-title', text: 'Top values' }));
  if (st.family === 'lob') left.append(h('p', { class: 'note', text: 'Large or binary values: only the empty ones are counted.' }));
  else if (st.top_hidden) left.append(h('p', { class: 'note', text: 'Hidden: nothing of this column\'s values is kept, not even how often each occurs.' }));
  else if (st.unique) left.append(h('p', { class: 'note', text: 'Every value appears once, so there are no top values.' }));
  else if (st.top_error) left.append(h('p', { class: 'note', text: st.top_error }));
  else if (!st.top || !st.top.length) left.append(h('p', { class: 'note', text: filled ? 'Not available.' : 'The column is empty.' }));
  else {
    const max = Math.max(...st.top.map((r) => r[1]));
    const list = h('table', { class: 'top-values' });
    for (const [v, n] of st.top) {
      list.append(h('tr', {},
        h('td', { class: 'tv-label' + (st.masked ? ' masked' : ''), title: String(v), text: String(v) }),
        h('td', { class: 'tv-bar' }, h('span', { style: `width:${Math.max(2, Math.round(100 * n / max))}%` })),
        h('td', { class: 'num', text: whole(n) }),
        h('td', { class: 'num muted', text: pct(share(n, st.rows)) })));
    }
    left.append(list);
    if (st.distinct > st.top.length) left.append(h('p', { class: 'note', text: `${whole(st.distinct - st.top.length)} more distinct values.` }));
  }

  const right = h('div', { class: 'detail-block' }, h('div', { class: 'block-title', text: 'Distribution' }));
  const hist = st.histogram;
  if (hist && hist.bins && hist.bins.length) {
    const unit = hist.kind === 'month' ? 'by month' : hist.kind === 'year' ? 'by year' : `in ${hist.bins.length} equal ranges from ${showNumber(st.min)} to ${showNumber(st.max)}`;
    right.append(h('div', { class: 'chart-box' }, h('canvas', { id: 'hist-canvas', role: 'img', 'aria-label': `Distribution of ${c.name}, ${unit}` })),
      h('p', { class: 'note', text: `Rows ${unit}${d.sampled ? ' (sample)' : ''}.` }),
      h('details', { class: 'as-table' }, h('summary', { text: 'Show as table' }),
        h('table', { class: 'top-values' }, hist.bins.map((b) => h('tr', {},
          h('td', { text: binLabel(hist, b) }), h('td', { class: 'num', text: whole(b.count) }))))));
  } else if (c.personal) right.append(h('p', { class: 'note', text: 'No distribution is kept for personal columns.' }));
  else if (st.family === 'number' || st.family === 'date') right.append(h('p', { class: 'note', text: st.histogram_error || (st.distinct <= 1 ? 'Only one value.' : 'Not available.') }));
  else right.append(h('p', { class: 'note', text: 'Distributions are drawn for numbers and dates.' }));

  return h('div', { class: 'col-detail-grid' }, h('div', { class: 'detail-block facts' }, facts), left, right);
}

function binLabel(hist, b) {
  if (hist.kind !== 'number') return b.label;
  if (hist.whole) return b.from === b.to ? whole(b.from) : `${whole(b.from)} – ${whole(b.to)}`;
  return `${showNumber(b.from)} – ${showNumber(b.to)}`;
}

function destroyChart() {
  if (prof.chart) { prof.chart.destroy(); prof.chart = null; }
}

function drawHistogram() {
  const canvas = $('#hist-canvas');
  if (!canvas || !prof.table || typeof Chart === 'undefined') return;
  const column = prof.table.columns.find((c) => c.name === prof.column);
  const hist = column && column.stats && column.stats.histogram;
  if (!hist) return;
  const labels = hist.bins.map((b) => (hist.kind === 'number' ? (hist.whole ? (b.from === b.to ? whole(b.from) : compact(b.from)) : showNumber(Number(b.from.toPrecision(3)))) : b.label));
  Chart.defaults.font.family = FONT;
  prof.chart = new Chart(canvas, {
    type: 'bar',
    data: { labels, datasets: [{ data: hist.bins.map((b) => b.count),
                                 borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: 'bottom', maxBarThickness: 24,
                                 categoryPercentage: 0.86, barPercentage: 0.9 }] },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: {
        legend: { display: false },
        tooltip: { displayColors: false, callbacks: {
          title: (items) => binLabel(hist, hist.bins[items[0].dataIndex]),
          label: (item) => `${whole(item.parsed.y)} rows` } },
      },
      scales: {
        x: { grid: { display: false }, border: {},
             ticks: { font: { size: 10 }, maxRotation: 0, autoSkip: true, autoSkipPadding: 6 } },
        y: { beginAtZero: true, grid: {}, border: { display: false },
             ticks: { font: { size: 10 }, precision: 0, maxTicksLimit: 5, callback: (v) => compact(v) } },
      },
    },
  });
  paintChart();
}

/** Give the chart the colours of the theme that is showing: bars, axis, grid, tick labels and tooltip.
    Called when the chart is made and again when the theme changes, without drawing the data anew. */
function paintChart() {
  const chart = prof.chart;
  if (!chart) return;
  const bars = chart.data.datasets[0];
  bars.backgroundColor = themeColor('bar');
  bars.hoverBackgroundColor = themeColor('bar-hover');
  const { scales, plugins } = chart.options;
  scales.x.border.color = themeColor('line-strong');
  scales.y.grid.color = themeColor('chart-grid');
  scales.x.ticks.color = themeColor('muted');
  scales.y.ticks.color = themeColor('muted');
  plugins.tooltip.backgroundColor = themeColor('tooltip');
  plugins.tooltip.titleColor = themeColor('on-tooltip');
  plugins.tooltip.bodyColor = themeColor('on-tooltip');
  chart.update('none');
}

async function loadSample(id, token) {
  const d = prof.table;
  if (!d || !d.can_profile) return;
  let sample;
  try {
    sample = await api(wsPath(`/tables/${enc(id)}/sample`));
  } catch (err) {
    sample = { error: err.message };
  }
  if (token !== prof.tableToken || state.selected !== id) return;
  const box = $('#sample-box');
  if (!box) return;
  if (!sample || sample.error) {
    const retry = sample && sample.needs_password
      ? h('button', { class: 'btn small', type: 'button', text: 'Enter password', onclick: () => samplePassword(sample.source).catch(fail) }) : null;
    box.replaceChildren(h('p', { class: 'note', text: (sample && sample.error) || 'Not available.' }), retry || '');
    return;
  }
  if (!sample.rows.length) { box.replaceChildren(h('p', { class: 'note', text: 'The table has no rows.' })); return; }
  const personal = new Set(sample.personal);
  const note = `First ${sample.rows.length} rows, read live and never saved.` +
    (sample.personal.length ? (sample.masked ? ' Personal values are masked.' : ' Personal values are shown unmasked.') : '') +
    (sample.left_out.length ? ` Not shown (large or binary): ${sample.left_out.join(', ')}.` : '');
  box.replaceChildren(
    h('p', { class: 'note', text: note }),
    h('div', { class: 'scroll-x' }, h('table', { class: 'data-table sample' },
      h('thead', {}, h('tr', {}, sample.columns.map((c) => h('th', { class: personal.has(c) ? 'personal' : '', title: personal.has(c) ? 'Personal column' : '' },
        c, personal.has(c) ? h('span', { class: 'tag personal', text: sample.masked ? 'masked' : 'personal' }) : null)))),
      h('tbody', {}, sample.rows.map((row) => h('tr', {}, row.map((v, i) =>
        h('td', { class: (v == null ? 'null ' : '') + (personal.has(sample.columns[i]) && sample.masked ? 'masked' : ''),
                  title: v == null ? '' : String(v), text: v == null ? 'null' : String(v) }))))))));
}

async function samplePassword(source) {
  const pw = await ask('Password needed', { type: 'password', ok: 'Read',
    text: `Enter the password for ${source.label}. It is kept in memory only until the app is closed.` });
  if (!pw) return;
  await sendJSON(wsPath(`/sources/${enc(source.id)}/refresh`), { password: pw });
  await openWorkspace(state.wsId, { keep: true });
}

async function exactCount(tableId, button) {
  button.disabled = true;
  button.textContent = 'Counting…';
  try {
    const r = await api(wsPath(`/tables/${enc(tableId)}/count`), { method: 'POST' });
    toast(`${tableId}: ${whole(r.row_count)} rows (counted in ${r.elapsed_s} s).`, 'ok', 5000);
  } finally {
    button.disabled = false;
    button.textContent = 'Exact count';
  }
  await loadOverview();
  await renderProfile();
}

async function markPersonal(tableId, c) {
  const r = await sendJSON(wsPath('/personal'), { table: tableId, column: c.name, personal: !c.personal });
  if (r.reprofile) toast(`${c.name} is no longer treated as personal. Its saved values stay masked until the table is profiled again.`, 'info', 8000);
  else toast(r.personal ? `${c.name} is now treated as personal: its values are masked.` : `${c.name} is no longer treated as personal.`, 'ok', 4000);
  qual.data = null;
  await renderProfile();
}

// ------------------------------------------------------------------ quality ----

async function renderQuality() {
  if (state.tab !== 'quality' || !state.model) return;
  const box = $('#quality-body');
  if (!qual.data) {
    box.replaceChildren(h('p', { class: 'note pad', text: 'Loading…' }));
    try {
      qual.data = await api(wsPath('/quality'));
    } catch (err) {
      box.replaceChildren(h('p', { class: 'note pad', text: err.message }));
      return;
    }
  }
  const q = qual.data;
  const tables = [...new Set(q.findings.map((f) => f.table))].sort((a, b) => a.localeCompare(b));
  const rules = [...new Set(q.findings.map((f) => f.rule))];
  if (qual.table !== '*' && !tables.includes(qual.table)) qual.table = '*';
  if (qual.rule !== '*' && !rules.includes(qual.rule)) qual.rule = '*';
  $('#quality-table').replaceChildren(h('option', { value: '*', text: `All tables (${tables.length})` }),
    ...tables.map((t) => h('option', { value: t, text: t })));
  $('#quality-table').value = qual.table;
  $('#quality-rule').replaceChildren(h('option', { value: '*', text: `All rules (${rules.length})` }),
    ...rules.map((r) => h('option', { value: r, text: q.rules[r] || r })));
  $('#quality-rule').value = qual.rule;
  const visible = q.findings.filter((f) => (qual.table === '*' || f.table === qual.table) && (qual.rule === '*' || f.rule === qual.rule));
  $('#quality-severities').replaceChildren(...q.severities.map((s) => {
    const n = visible.filter((f) => f.severity === s).length;
    return h('label', { class: 'chip-toggle sev ' + s, title: `Show or hide ${SEVERITY[s].label.toLowerCase()} findings` },
      h('input', { type: 'checkbox', checked: !qual.severity.has(s),
                   onchange: (e) => { if (e.target.checked) qual.severity.delete(s); else qual.severity.add(s); renderQuality(); } }),
      h('span', { class: 'sev-icon', 'aria-hidden': 'true', text: SEVERITY[s].icon }), `${SEVERITY[s].label} ${n}`);
  }));
  $('#quality-note').textContent = q.tables_with_data
    ? `${q.tables_profiled} of ${plural(q.tables_with_data, 'table')} with data profiled`
    : 'No table has data: only the structure was checked';

  const out = [];
  if (q.tables_profiled < q.tables_with_data) {
    out.push(h('div', { class: 'callout' },
      `${q.tables_with_data - q.tables_profiled} of ${plural(q.tables_with_data, 'table')} with data ${q.tables_with_data - q.tables_profiled === 1 ? 'has' : 'have'} not been profiled yet, so only the checks that need no data ran for them. `,
      h('button', { class: 'link', type: 'button', text: 'Profile all', onclick: () => { showTab('profile'); startJob({ tables: 'all' }).catch(fail); } })));
  }
  const shown = visible.filter((f) => !qual.severity.has(f.severity));
  if (!shown.length) {
    out.push(h('div', { class: 'empty-note' }, h('h3', { text: q.findings.length ? 'No findings match the filters' : 'No findings' }),
      h('p', { text: q.findings.length ? 'Change the filters above to see the others.'
        : !q.tables_with_data ? 'Only the structure could be checked: no table of this workspace has data to profile.'
          : (q.tables_profiled ? 'The checks found nothing to report in the profiled tables.' : 'Profile the tables to check their data.') })));
  }
  for (const s of q.severities) {
    const group = shown.filter((f) => f.severity === s);
    if (!group.length) continue;
    out.push(h('div', { class: 'sev-head ' + s }, h('span', { class: 'sev-icon', 'aria-hidden': 'true', text: SEVERITY[s].icon }),
      `${SEVERITY[s].label}`, h('span', { class: 'n', text: group.length })));
    out.push(h('ul', { class: 'findings' }, group.map((f) => h('li', {},
      h('button', { class: 'finding ' + s, type: 'button', title: 'Open this table' + (f.column ? ' and column' : '') + ' in the Profile tab',
                    onclick: () => jumpTo(f.table, f.column) },
        h('span', { class: 'f-where', title: f.table + (f.column ? '.' + f.column : '') },
          h('span', { class: 'f-table', text: f.table }), f.column ? h('span', { class: 'f-column', text: '.' + f.column }) : null),
        h('span', { class: 'tag', text: q.rules[f.rule] || f.rule }),
        f.sampled ? h('span', { class: 'tag sampled', text: 'sample',
          title: 'Found on a sample of the table' + (f.sample_method ? `: ${f.sample_method}` : '') }) : null,
        h('span', { class: 'f-message', text: f.message }))))));
  }
  box.replaceChildren(...out);
}

/** From a finding to its table and column in the Profile tab. */
function jumpTo(tableId, column) {
  prof.column = column || null;
  prof.table = null;
  showTab('profile');
  if (state.selected === tableId) renderProfile(); else selectTable(tableId);
}

// ------------------------------------------------------------------- wiring ----

function wireProfile() {
  const guard = (fn) => (...args) => Promise.resolve(fn(...args)).catch(fail);
  $('#profile-all').addEventListener('click', guard(() => startJob({ tables: 'all' })));
  $('#profile-selected').addEventListener('click', guard(profileSelected));
  $('#profile-counts').addEventListener('click', guard(() => startJob({ tables: 'all', counts_only: true })));
  $('#job-cancel').addEventListener('click', guard(cancelJob));
  $('#profile-clear').addEventListener('click', guard(clearStatistics));
  $('#show-personal').addEventListener('change', guard((e) => setShowPersonal(e.target.checked)));
  $('#personal-off').addEventListener('click', guard(() => setShowPersonal(false)));
  $('#quality-table').addEventListener('change', (e) => { qual.table = e.target.value; renderQuality(); });
  $('#quality-rule').addEventListener('change', (e) => { qual.rule = e.target.value; renderQuality(); });

  hooks.workspace.push(async ({ keep }) => {
    clearTimeout(prof.timer);
    if (!keep) { prof.checked.clear(); prof.column = null; }
    prof.job = null;
    prof.table = null;
    prof.overview = null;
    qual.data = null;
    await loadOverview();
    syncSelectedButton();
    if (state.tab === 'profile') await renderProfile();
    if (state.tab === 'quality') await renderQuality();
  });
  hooks.select.push(async () => {
    syncSelectedButton();
    if (state.tab === 'profile') await renderProfile();
  });
  hooks.tab.push(async (id) => {
    if (id === 'profile') { await loadOverview(); await renderProfile(); }
    if (id === 'quality') { qual.data = null; await renderQuality(); }
  });
  hooks.theme.push(paintChart);
}

document.addEventListener('DOMContentLoaded', wireProfile);
