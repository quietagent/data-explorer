'use strict';

/* Scripts tab: a folder of the team's .sql and .py scripts, read by the SQL Lineage engine (never run).
   Loaded after app.js, profile.js and analysis.js and uses their helpers (state, h, api, sendJSON, $, toast,
   ask, wsPath, when, watchJob, saveFrom ...). Everything shown comes from this app's own /api, and always
   goes into the page as text: names and SQL of the scripts as they are written. */

const DIALECT_TEXT = { oracle: 'Oracle', tsql: 'SQL Server (T-SQL)', postgres: 'PostgreSQL', mysql: 'MySQL', sqlite: 'SQLite' };

// The sub-views of a scan, in the order of the segmented control.
const SCRIPT_VIEWS = [
  { id: 'joins', label: 'Joins', count: (d) => d.catalog.length },
  { id: 'conflicts', label: 'Conflicts', count: (d) => d.conflicts.length },
  { id: 'views', label: 'Suggested views', count: (d) => d.groups.length },
  { id: 'subqueries', label: 'Subquery links', count: (d) => d.subquery_links.length },
  { id: 'nokey', label: 'Joins without a key', count: (d) => (d.joins_without_key ? d.joins_without_key.length : null) },
  { id: 'impact', label: 'Impact', count: () => null },
  { id: 'unreadable', label: 'Unreadable', count: (d) => d.unreadable.length },
];

const scr = {
  data: null,                   // GET /workspaces/{ws}/scripts
  token: 0,
  view: 'joins',
  sort: { by: 'statements', dir: -1 },
  filter: '',
  open: new Set(),              // keys of the catalog rows whose uses are open
  drafts: new Map(),            // workspace id -> { folder, dialect } as typed
  filledFor: null,              // the workspace whose folder and dialect are in the tool bar
  target: '',                   // the impact picker's text
  impact: null,                 // GET /workspaces/{ws}/scripts/impact
  impactError: null,
  impactToken: 0,
};

// The catalog's columns: [label, value to sort by, a number?]
const JOIN_COLUMNS = [
  { id: 'join', label: 'Join', value: (r) => r.key },
  { id: 'types', label: 'Types', value: (r) => r.types.join(',') },
  { id: 'statements', label: 'Statements', num: true, value: (r) => r.statements },
  { id: 'scripts', label: 'Scripts', num: true, value: (r) => r.scripts.length },
  { id: 'fk', label: 'Declared FK', value: (r) => (r.declared_fk === true ? 2 : r.declared_fk === false ? 1 : 0) },
  { id: 'flags', label: 'Flags', value: (r) => joinFlags(r).join(',') },
];

async function loadScripts() {
  if (!state.wsId) return;
  const token = ++scr.token;
  const data = await api(wsPath('/scripts'));
  if (token !== scr.token) return;
  scr.data = data;
  // The folder and the dialect are filled in once per workspace (what was typed, or the last scan's); after
  // that they keep what is in them, also when a scan is forgotten.
  const fresh = scr.filledFor !== state.wsId;
  scr.filledFor = state.wsId;
  const draft = scr.drafts.get(state.wsId) || {};
  if (fresh || !$('#scripts-folder').value) $('#scripts-folder').value = draft.folder != null ? draft.folder : (data.scan ? data.scan.folder : '');
  const dialect = !fresh && $('#scripts-dialect').value ? $('#scripts-dialect').value
    : draft.dialect != null ? draft.dialect : data.default_dialect || (data.scan ? data.scan.dialect : '');
  $('#scripts-dialect').replaceChildren(h('option', { value: '', text: 'Dialect…' }),
    ...data.dialects.map((d) => h('option', { value: d, text: DIALECT_TEXT[d] || d })));
  $('#scripts-dialect').value = dialect;
  if (data.job && data.job.state === 'running' && (!prof.job || prof.job.id !== data.job.id)) watchJob(data.job);
}

const scanRunning = () => !!prof.job && prof.job.state === 'running';

/** A table name of the scan: a link that selects the table in the app when the model has it. */
function scriptTable(key) {
  const id = scr.data && scr.data.in_model[key];
  if (!id || !state.tables.has(id)) return h('span', { class: 'scr-tbl', text: key });
  return h('button', { class: 'link scr-tbl', type: 'button', text: key, title: `Select ${tableLabel(id)} in the app`,
                       onclick: () => selectTable(id, { center: true }) });
}

const statementRef = (u) => `${u.script} #${u.number}` + (u.line || u.first_line ? `, line ${u.line || u.first_line}` : '');

function usesList(uses, extra) {
  return h('ul', { class: 'scr-uses' }, uses.map((u) => h('li', {}, h('span', { class: 'scr-ref', text: statementRef(u) }),
    u.join_type ? h('span', { class: 'tag', text: u.join_type }) : null, u.via_cte ? h('span', { class: 'tag', text: 'via CTE or subquery' }) : null,
    extra ? extra(u) : null)));
}

/** The two tables of a join and its condition, as the engine wrote it. */
function joinCell(r) {
  const condition = r.pairs.map(([a, b]) => `${r.table1}.${a} = ${r.table2}.${b}`);
  if (r.condition) condition.push(r.condition);
  return h('div', { class: 'scr-join' },
    h('div', {}, scriptTable(r.table1), h('span', { class: 'muted', text: ' — ' }), scriptTable(r.table2)),
    h('div', { class: 'scr-cond', text: condition.join(' AND ') || '(no condition: a cross join)' }));
}

function joinFlags(r) {
  const flags = [];
  if (r.derived) flags.push('derived');
  if (r.non_key) flags.push('non-key');
  if (scr.data && scr.data.conflicts.some((c) => c.variants.some((v) => v.key === r.key))) flags.push('conflict');
  if (r.uses.some((u) => u.via_cte)) flags.push('via CTE');
  return flags;
}

function fkTag(value) {
  if (value === true) return h('span', { class: 'tag ok', text: 'declared FK' });
  if (value === false) return h('span', { class: 'tag warn', text: 'no declared key' });
  return h('span', { class: 'tag', text: 'not checked', title: 'Derived through a CTE or subquery, or no schema: not checked' });
}

function renderScripts() {
  if (state.tab !== 'scripts' || !state.model) return;
  const box = $('#scripts-body');
  const d = scr.data;
  $('#scripts-scan').disabled = scanRunning();
  if (!d) { box.replaceChildren(h('p', { class: 'note pad', text: 'Loading…' })); return; }
  if (!d.scan) {
    box.replaceChildren(h('div', { class: 'empty-note' }, h('h3', { text: 'No scan of scripts yet' }),
      h('p', { text: 'Enter the folder of the team\'s .sql and .py scripts above, choose their dialect and press Scan. Every script ' +
        `under the folder is read as text and never run, and nothing is written into the folder (at most ${whole(d.limits.max_files)} ` +
        `files; a file over ${d.limits.max_file_mb} MB is listed as unreadable). The tables, columns, primary keys and declared ` +
        'relationships of this workspace are the schema the joins are checked against.' })));
    return;
  }
  const s = d.summary;
  const numbers = [['files', s.files], ['statements', s.statements], ['unreadable', s.unreadable, s.unreadable > 0], ['tables', s.tables],
    ['joins', s.joins], ['subquery links', s.subquery_links], ['conflicts', s.conflicts, s.conflicts > 0], ['repeated groups', s.groups],
    ['joins without a key', s.joins_without_key == null ? '—' : s.joins_without_key], ['names not resolved', s.unknown_names]];
  const out = [h('div', { class: 'view-head' },
    h('div', {}, h('div', { class: 'view-title' }, 'Scan of ', h('span', { class: 'scr-folder', text: d.scan.folder })),
      h('div', { class: 'd-sub', text: `${when(d.scan.scanned_at)} · ${DIALECT_TEXT[d.scan.dialect] || d.scan.dialect} · ` +
        `schema: ${d.scan.schema || 'none (the workspace has no tables)'}` +
        (d.scan.default_schema ? ` · default schema ${d.scan.default_schema}` : '') })),
    h('div', { class: 'scr-actions' },
      h('button', { class: 'btn', type: 'button', id: 'scripts-rescan', text: 'Rescan', disabled: scanRunning(),
                    title: `Scan ${d.scan.folder} again (${DIALECT_TEXT[d.scan.dialect] || d.scan.dialect})`,
                    onclick: () => startScan({ folder: d.scan.folder, dialect: d.scan.dialect }).catch(fail) }),
      h('button', { class: 'btn', type: 'button', id: 'scripts-report', text: 'Download report',
                    title: 'The scan as one offline HTML file (the report of SQL Lineage), built in memory',
                    onclick: (e) => downloadScriptsReport(e.currentTarget).catch(fail) }),
      h('button', { class: 'btn danger', type: 'button', id: 'scripts-forget', text: 'Forget scan', disabled: scanRunning(),
                    title: 'Delete this scan from this PC. The scripts themselves are not touched.',
                    onclick: () => forgetScan().catch(fail) }))),
  h('div', { class: 'scr-numbers' }, numbers.map(([label, n, warn]) =>
    h('div', { class: 'scr-number' + (warn ? ' warn' : '') }, h('b', { text: typeof n === 'number' ? whole(n) : n }), h('span', { text: label })))),
  ];
  for (const warning of d.scan.schema_warnings) out.push(h('div', { class: 'callout warn', text: `Schema: ${warning}` }));
  // Names the model does not have, and a database source read without its views: some may be those views.
  if (d.views_note) out.push(h('div', { class: 'callout', id: 'scripts-views-note', text: d.views_note }));
  out.push(h('div', { class: 'seg scr-seg', role: 'tablist', 'aria-label': 'What to show of the scan' }, SCRIPT_VIEWS.map((v) => {
    const n = v.count(d);
    return h('button', { type: 'button', role: 'tab', class: v.id === scr.view ? 'active' : '', 'aria-selected': v.id === scr.view ? 'true' : 'false',
                         'data-view': v.id, onclick: () => { scr.view = v.id; renderScripts(); } },
      v.label, n == null ? null : h('span', { class: 'n', text: whole(n) }));
  })));
  const show = { joins: joinsView, conflicts: conflictsView, views: viewsView, subqueries: subqueriesView, nokey: noKeyView,
                 impact: impactView, unreadable: unreadableView }[scr.view] || joinsView;
  out.push(h('div', { class: 'scr-view', id: 'scripts-view' }, show(d)));
  const top = box.scrollTop;
  const left = ($('#scr-fit') || {}).scrollLeft || 0;
  const focused = document.activeElement && document.activeElement.id;
  box.replaceChildren(...out);
  box.scrollTop = top;
  if (left && $('#scr-fit')) $('#scr-fit').scrollLeft = left;
  if (focused && ['scr-filter', 'scr-target'].includes(focused) && $('#' + focused)) {
    const input = $('#' + focused);
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  }
}

// ------------------------------------------------------------- the sub-views ----

function joinsView(d) {
  const needle = scr.filter.trim().toUpperCase();
  const rows = d.catalog.filter((r) => !needle || r.key.toUpperCase().includes(needle));
  const column = JOIN_COLUMNS.find((c) => c.id === scr.sort.by) || JOIN_COLUMNS[2];
  rows.sort((a, b) => {
    const x = column.value(a), y = column.value(b);
    const c = column.num ? x - y : String(x).localeCompare(String(y));
    return (scr.sort.dir * c) || a.key.localeCompare(b.key);
  });
  const head = JOIN_COLUMNS.map((c) => {
    const on = scr.sort.by === c.id;
    return h('th', { class: 'sortable' + (c.num ? ' num' : '') + (c.id === 'join' ? ' pin pin-last' : ''), title: 'Click to sort',
                     'aria-sort': on ? (scr.sort.dir > 0 ? 'ascending' : 'descending') : 'none',
                     onclick: () => { scr.sort = { by: c.id, dir: on ? -scr.sort.dir : (c.num ? -1 : 1) }; renderScripts(); } },
      c.label, h('span', { class: 'arrow', 'aria-hidden': 'true', text: on ? (scr.sort.dir > 0 ? '▲' : '▼') : '' }));
  });
  const body = rows.flatMap((r) => {
    const open = scr.open.has(r.key);
    const toggle = () => { if (open) scr.open.delete(r.key); else scr.open.add(r.key); renderScripts(); };
    const tr = [h('tr', { class: 'click' + (open ? ' open' : ''), onclick: (e) => { if (!e.target.closest('button')) toggle(); } },
      h('td', { class: 'pin pin-last' }, h('div', { class: 'scr-name' }, h('span', { class: 'caret', text: open ? '▾' : '▸' }), joinCell(r))),
      h('td', {}, r.types.map((t) => h('span', { class: 'tag', text: t }))),
      h('td', { class: 'num', text: whole(r.statements) }),
      h('td', { class: 'num', text: whole(r.scripts.length), title: r.scripts.join('\n') }),
      h('td', {}, fkTag(r.declared_fk)),
      h('td', {}, joinFlags(r).map((f) => h('span', { class: 'tag' + (f === 'conflict' || f === 'non-key' ? ' warn' : ''), text: f }))))];
    if (open) tr.push(h('tr', { class: 'col-detail' }, h('td', { colspan: 6 }, h('div', { class: 'detail-fit' }, usesList(r.uses)))));
    return tr;
  });
  return [h('div', { class: 'scr-tools' },
    h('input', { type: 'search', id: 'scr-filter', placeholder: 'Filter by table or column', autocomplete: 'off', value: scr.filter,
                 'aria-label': 'Filter the joins by table or column', oninput: (e) => { scr.filter = e.target.value; renderScripts(); } }),
    h('span', { class: 'note', text: rows.length === d.catalog.length ? `${plural(d.catalog.length, 'distinct join')} · click one for its statements`
      : `${rows.length} of ${d.catalog.length} joins shown` })),
  h('div', { class: 'table-fit', id: 'scr-fit' }, h('table', { class: 'data-table scr-grid' },
    h('colgroup', {}, h('col', {}), h('col', { class: 'w-types' }), h('col', { class: 'w-n' }), h('col', { class: 'w-n' }),
      h('col', { class: 'w-fk' }), h('col', { class: 'w-flags' })),
    h('thead', {}, h('tr', {}, head)), h('tbody', {}, body))),
  rows.length ? null : h('div', { class: 'empty-note' }, h('h3', { text: d.catalog.length ? 'No join matches the filter' : 'No joins in these scripts' }))];
}

function conflictsView(d) {
  if (!d.conflicts.length) return emptyView('No conflicts', 'No two tables are joined on different columns in different places.');
  return [h('p', { class: 'sub-note', text: 'The same two tables joined on different columns in different places. One of the variants is often a bug.' }),
    ...d.conflicts.map((c) => h('div', { class: 'scr-box' },
      h('div', { class: 'scr-box-head' }, scriptTable(c.table1), ' and ', scriptTable(c.table2), h('span', { class: 'muted', text: `: ${c.variants.length} different joins` })),
      c.variants.map((v) => h('div', { class: 'scr-variant' }, h('div', { class: 'scr-cond', text: v.key }),
        h('div', { class: 'muted' }, fkTag(v.declared_fk), ` ${plural(v.statements, 'statement')} in ${plural(v.scripts.length, 'script')}`),
        usesList(v.uses)))))];
}

function viewsView(d) {
  const settings = d.scan.settings || {};
  if (!d.groups.length) {
    return emptyView('No suggested views', `No group of joins repeats in ${settings.MIN_GROUP_STATEMENTS || 3} or more statements from ` +
      `${settings.MIN_GROUP_SCRIPTS || 2} or more scripts.`);
  }
  return [h('p', { class: 'sub-note', text: 'Groups of joins that repeat together. Each draft view is text to check and copy: nothing was run, and no script is changed.' }),
    ...d.groups.map((g) => h('div', { class: 'scr-box' },
      h('div', { class: 'scr-box-head' }, h('span', { class: 'scr-cond', text: g.name }),
        h('span', { class: 'muted', text: ` · ${plural(g.joins.length, 'join')} in ${plural(g.statements, 'statement')} from ${plural(g.scripts.length, 'script')}` })),
      h('div', { class: 'chips' }, g.tables.map((t) => scriptTable(t))),
      g.joins.map((j) => h('div', { class: 'scr-cond', text: j })),
      h('pre', { class: 'scr-sql', text: g.sql }),
      h('button', { class: 'btn small', type: 'button', text: 'Copy SQL', onclick: (e) => copyText(g.sql, e.currentTarget) }),
      h('div', { class: 'sub-head small' }, 'Statements that could use it', h('span', { class: 'n', text: g.uses.length })),
      usesList(g.uses)))];
}

function subqueriesView(d) {
  if (!d.subquery_links.length) return emptyView('No subquery links', 'No column is tied to another through IN, EXISTS or a correlated subquery.');
  return [h('p', { class: 'sub-note', text: 'Columns tied through IN, NOT IN, EXISTS or a correlated subquery. They are not joins: the join list, ' +
      'the conflicts, the suggested views and the joins without a key leave them out. They count for impact.' }),
  h('div', { class: 'table-fit' }, h('table', { class: 'data-table scr-grid' },
    h('colgroup', {}, h('col', {}), h('col', { class: 'w-types' }), h('col', { class: 'w-n' })),
    h('thead', {}, h('tr', {}, h('th', { class: 'pin pin-last', text: 'Link' }), h('th', { text: 'Kinds' }), h('th', { class: 'num', text: 'Statements' }))),
    h('tbody', {}, d.subquery_links.map((r) => h('tr', {},
      // every use here is of the type "subquery": its kind (in, exists ...) is what tells them apart
      h('td', { class: 'pin pin-last' }, joinCell(r), usesList(r.uses.map((u) => ({ ...u, join_type: null })),
        (u) => (u.kind ? h('span', { class: 'tag', text: u.kind }) : null))),
      h('td', {}, r.kinds.map((k) => h('span', { class: 'tag', text: k }))),
      h('td', { class: 'num', text: whole(r.statements) }))))))];
}

function noKeyView(d) {
  if (d.joins_without_key == null) {
    return emptyView('Keys not checked', 'The workspace had no tables when it was scanned, so there was no schema to check the joins against. ' +
      'Add a source with the tables, then rescan.');
  }
  if (!d.joins_without_key.length) return emptyView('Every join has a declared key', 'Each join matches a foreign key declared in the workspace model.');
  return [h('p', { class: 'sub-note', text: 'Joins the workspace model does not declare as a foreign key, in either direction. Links the app ' +
      'inferred or accepted from values are not declared keys. "non-key" marks a condition that is not plain column equality.' }),
  h('div', { class: 'table-fit' }, h('table', { class: 'data-table scr-grid' },
    h('colgroup', {}, h('col', {}), h('col', { class: 'w-types' }), h('col', { class: 'w-n' }), h('col', { class: 'w-n' })),
    h('thead', {}, h('tr', {}, h('th', { class: 'pin pin-last', text: 'Join' }), h('th', { text: 'Types' }), h('th', { class: 'num', text: 'Statements' }),
      h('th', { class: 'num', text: 'Scripts' }))),
    h('tbody', {}, d.joins_without_key.map((r) => h('tr', {},
      h('td', { class: 'pin pin-last' }, joinCell(r), r.non_key ? h('span', { class: 'tag warn', text: 'non-key' }) : null, usesList(r.uses)),
      h('td', {}, r.types.map((t) => h('span', { class: 'tag', text: t }))),
      h('td', { class: 'num', text: whole(r.statements) }),
      h('td', { class: 'num', text: whole(r.scripts.length) }))))))];
}

function impactView(d) {
  const picker = h('div', { class: 'scr-tools' },
    h('input', { type: 'text', id: 'scr-target', list: 'scr-targets', placeholder: 'A table or a column: TABLE.COLUMN', autocomplete: 'off',
                 value: scr.target, 'aria-label': 'Table or column to see the impact of', spellcheck: 'false',
                 oninput: (e) => { scr.target = e.target.value; pickImpact(); } }),
    h('datalist', { id: 'scr-targets' }, d.targets.map((t) => h('option', { value: t }))),
    h('span', { class: 'note', text: 'Every statement that uses it, then level by level the scripts downstream: those that read what an affected script writes.' }));
  const out = [picker];
  if (scr.impactError) out.push(h('div', { class: 'callout warn', text: scr.impactError }));
  const r = scr.impact;
  if (!r) return out;
  const table = r.kind === 'table' ? r.target : r.target.slice(0, r.target.lastIndexOf('.'));
  out.push(h('div', { class: 'view-title scr-impact-title', text: `Impact of ${r.kind} ${r.target}` }),
    h('h4', { class: 'scr-step', text: `Direct (${plural(r.direct.length, 'use')})` }),
    h('div', { class: 'scr-line' }, 'Table: ', scriptTable(table)),
    r.direct.length ? h('ul', { class: 'scr-uses' }, r.direct.map((u) => h('li', {}, h('span', { class: 'scr-ref', text: statementRef(u) }),
      h('span', { class: 'tag', text: u.role }), h('span', { class: 'scr-cond', text: u.item })))) : h('p', { class: 'note', text: 'none' }));
  if (!r.levels.length) out.push(h('p', { class: 'note', text: 'Downstream: none. The scripts above write no other table.' }));
  for (const level of r.levels) {
    out.push(h('h4', { class: 'scr-step', text: `Level ${level.level} (${plural(level.tables.length, 'table')}, ${plural(level.statements.length, 'statement')})` }),
      h('div', { class: 'scr-line muted', text: 'Tables:' }),
      h('ul', { class: 'scr-uses' }, level.tables.map((t) => h('li', {}, scriptTable(t.table),
        h('span', { class: 'muted', text: ` written by ${t.written_by.map((w) => `${w.script} #${w.number}`).join(', ')}` })))),
      h('div', { class: 'scr-line muted', text: 'Statements that read them:' }),
      h('ul', { class: 'scr-uses' }, level.statements.map((s) => h('li', {}, h('span', { class: 'scr-ref', text: statementRef(s) }),
        h('span', { class: 'muted', text: ' reads ' }), s.reads.flatMap((t, i) => [i ? ', ' : '', scriptTable(t)])))));
  }
  if (r.stopped) out.push(h('p', { class: 'note', text: `Stopped after ${(d.scan.settings || {}).IMPACT_MAX_DEPTH || ''} levels (IMPACT_MAX_DEPTH).` }));
  if (r.unreadable.length || r.unreadable_elsewhere) {
    out.push(h('h4', { class: 'scr-step', text: 'Not read, so not in the lists above' }),
      h('ul', { class: 'scr-uses' }, r.unreadable.map((u) => h('li', {}, h('span', { class: 'scr-ref', text: statementRef(u) }), h('span', { class: 'muted', text: ` ${u.reason}` }))),
        r.unreadable_elsewhere ? h('li', { class: 'muted', text: `and ${plural(r.unreadable_elsewhere, 'more statement')} in other scripts (see Unreadable)` }) : null));
  }
  return out;
}

function unreadableView(d) {
  const out = [];
  if (!d.unreadable.length) out.push(emptyView('Everything was read', 'Every file and every statement could be read.'));
  else {
    out.push(h('p', { class: 'sub-note', text: 'Files and statements that could not be read for sure. They are listed, never guessed: what they read and write is not in the other lists.' }),
      h('div', { class: 'table-fit' }, h('table', { class: 'data-table scr-grid scr-unreadable' },
        h('colgroup', {}, h('col', {}), h('col', { class: 'w-lines' }), h('col', { class: 'w-type' }), h('col', {})),
        h('thead', {}, h('tr', {}, h('th', { class: 'pin pin-last', text: 'File' }), h('th', { text: 'Line' }), h('th', { text: 'Type' }), h('th', { text: 'Reason' }))),
        h('tbody', {}, d.unreadable.map((u) => h('tr', {},
          h('td', { class: 'pin pin-last' }, h('span', { class: 'scr-ref', text: u.script + (u.number ? ` #${u.number}` : '') })),
          h('td', { class: 'num', text: u.first_line ? (u.last_line && u.last_line !== u.first_line ? `${u.first_line}–${u.last_line}` : String(u.first_line)) : '—' }),
          h('td', { text: u.type }),
          h('td', {}, h('div', { text: u.reason }), u.text ? h('details', { class: 'as-table' }, h('summary', { text: 'The text as written' }),
            h('pre', { class: 'scr-sql', text: u.text })) : null)))))));
  }
  if (d.unknowns.length) {
    out.push(h('div', { class: 'sub-head' }, 'Names whose table is not certain', h('span', { class: 'n', text: d.unknowns.length })),
      h('ul', { class: 'scr-uses' }, d.unknowns.map((u) => h('li', {}, h('span', { class: 'scr-ref', text: `${u.script} #${u.number}, line ${u.line}` }),
        h('span', { class: 'scr-cond', text: u.name }), h('span', { class: 'muted', text: ` ${u.reason}` })))));
  }
  return out;
}

function emptyView(title, text) {
  return h('div', { class: 'empty-note' }, h('h3', { text: title }), h('p', { text }));
}

// --------------------------------------------------------------------- actions ----

/** The impact picker: ask the server once the text is one of the scan's tables or columns. */
async function pickImpact() {
  const d = scr.data;
  const typed = scr.target.trim();
  const key = d.targets.find((t) => t === typed) || d.targets.find((t) => t.toUpperCase() === typed.toUpperCase());
  if (!key) {
    if (scr.impact || scr.impactError) { scr.impact = null; scr.impactError = null; renderScripts(); }
    return;
  }
  const token = ++scr.impactToken;
  try {
    const result = await api(wsPath('/scripts/impact?name=' + enc(key)));
    if (token !== scr.impactToken) return;
    scr.impact = result;
    scr.impactError = null;
  } catch (err) {
    if (token !== scr.impactToken) return;
    scr.impact = null;
    scr.impactError = err.message;
  }
  renderScripts();
}

function copyText(text, button) {
  const done = () => { button.textContent = 'Copied'; setTimeout(() => { button.textContent = 'Copy SQL'; }, 1500); };
  const fallback = () => {
    const area = h('textarea', { class: 'scr-copy', 'aria-hidden': 'true' });
    area.value = text;
    document.body.append(area);
    area.select();
    try { document.execCommand('copy'); done(); } catch (e) { toast('Copy did not work here: select the text and press Ctrl+C.'); } finally { area.remove(); }
  };
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, fallback);
  else fallback();
}

async function startScan(body) {
  if (!body.folder || !body.folder.trim()) { toast('Enter the folder of the scripts first.'); $('#scripts-folder').focus(); return; }
  if (!body.dialect) { toast('Choose the dialect of the scripts first.'); $('#scripts-dialect').focus(); return; }
  let job;
  try {
    job = await sendJSON(wsPath('/scripts/scan'), body);
  } catch (err) {
    const data = err.data || {};
    if (err.status === 409 && data.job) { watchJob(data.job); toast(err.message); return; }
    throw err;
  }
  watchJob(job);
  renderScripts();
}

function scanFromToolbar() {
  return startScan({ folder: $('#scripts-folder').value, dialect: $('#scripts-dialect').value });
}

async function forgetScan() {
  const sure = await ask('Forget this scan?', { type: 'none', ok: 'Forget',
    text: 'The saved scan of this workspace is deleted from this PC. The scripts themselves are not touched.' });
  if (!sure) return;
  await api(wsPath('/scripts'), { method: 'DELETE' });
  scr.impact = null;
  scr.target = '';
  await loadScripts();
  renderScripts();
  toast('The scan was forgotten.', 'info', 4000);
}

async function downloadScriptsReport(button) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = 'Building…';
  try {
    const name = await saveFrom(wsPath('/scripts/report'), 'scripts-report.html');
    toast(`${name} was handed to the browser: one offline file, the same report as SQL Lineage's.`, 'ok', 5000);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

/** A scan moved on, or ended (profile.js watches every job of the workspace). */
async function scriptsJob(job, finished) {
  if (job.kind !== 'scripts') {                     // another job of the workspace ended: Rescan and Forget are free again
    if (finished && state.tab === 'scripts') renderScripts();
    return;
  }
  if (!finished) return;
  if (job.state === 'cancelled') toast('The scan was cancelled. The last scan, if there was one, is kept.', 'info', 6000);
  else if (job.state === 'failed') toast(`The scan failed: ${job.error}`, 'error', 12000);
  else toast(`${plural(job.total, 'script')} read in ${Math.round(job.elapsed_s)} s.`, 'ok', 6000);
  scr.impact = null;
  scr.impactError = null;
  await loadScripts();
  renderScripts();
  if (scr.target) await pickImpact();
}

// --------------------------------------------------------------------- wiring ----

function wireScripts() {
  const guard = (fn) => (...args) => Promise.resolve(fn(...args)).catch(fail);
  $('#scripts-scan').addEventListener('click', guard(scanFromToolbar));
  $('#scripts-job-cancel').addEventListener('click', guard(cancelJob));
  const remember = () => scr.drafts.set(state.wsId, { folder: $('#scripts-folder').value, dialect: $('#scripts-dialect').value });
  $('#scripts-folder').addEventListener('input', remember);
  $('#scripts-dialect').addEventListener('change', remember);
  $('#scripts-folder').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); scanFromToolbar().catch(fail); }
    e.stopPropagation();                            // "/" and Escape belong to the field here
  });

  hooks.job.push(scriptsJob);
  hooks.workspace.push(async () => {
    scr.data = null;
    scr.filledFor = null;
    scr.open.clear();
    scr.filter = '';
    scr.target = '';
    scr.impact = null;
    scr.impactError = null;
    if (state.tab === 'scripts') { renderScripts(); await loadScripts(); renderScripts(); }
  });
  hooks.tab.push(async (id) => {
    if (id === 'scripts') { renderScripts(); await loadScripts(); renderScripts(); }
  });
}

document.addEventListener('DOMContentLoaded', wireScripts);
