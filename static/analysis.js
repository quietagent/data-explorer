'use strict';

/* Relations, SQL and Report tabs. Loaded after app.js and profile.js and uses their helpers
   (state, h, api, sendJSON, $, toast, ask, wsPath, pct, when, watchJob ...).
   Everything shown comes from this app's own /api, and always goes into the page as text. */

/** Fetch a file from the app and hand it to the browser as a download. A refusal comes back as a
    message, not as a broken download. Returns the file's name. */
async function saveFrom(path, fallback) {
  let res;
  try {
    res = await fetch('/api' + path);
  } catch (e) {
    throw new Error('The app is not responding. Is it still running?');
  }
  if (!res.ok) {
    const data = (res.headers.get('content-type') || '').includes('json') ? await res.json() : null;
    throw new Error((data && data.detail) || `Request failed (${res.status})`);
  }
  const name = (/filename="([^"]+)"/.exec(res.headers.get('content-disposition') || '') || [])[1] || fallback;
  const url = URL.createObjectURL(await res.blob());
  const a = h('a', { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
  return name;
}

/** A table as it is named in lists: its name, with the schema in front only when two schemas have a table of that name. */
function tableLabel(id) {
  const table = state.tables.get(id);
  if (!table) return id;
  return state.model.tables.some((t) => t !== table && t.name.toLowerCase() === table.name.toLowerCase()) ? id : table.name;
}

// ================================================================ relations ====

const REL_STATUS = [
  ['*', 'Any result'],
  ['orphans', 'With orphan rows'],
  ['unchecked', 'Not checked'],
  ['inexact', 'Counts not exact (at least)'],
  ['sampled', 'Measured on a sample'],
  ['unproven', 'Cardinality not proven'],
];

const rel = {
  data: null,                   // GET /workspaces/{ws}/relations/check
  token: 0,
  checked: new Set(),           // keys of the relationships ticked
  open: new Set(),              // keys whose evidence is open
  sort: { by: null, dir: 1 },   // null = the model's own order
  text: '',
  kind: '*',
  status: '*',
  linksOnly: false,             // the job that was started last only looked for links by values
};

/** The measured result of a relationship, or null when it was never checked or could not be. */
const measured = (r) => (r.check && r.check.status === 'checked' ? r.check : null);

const REL_COLUMNS = [
  { id: 'name', label: 'Relationship', value: (r) => `${r.from}.${r.from_columns.join(',')}>${r.to}`.toLowerCase() },
  { id: 'kind', label: 'Kind', value: (r) => r.kind + String(r.confidence == null ? 9 : r.confidence) },
  { id: 'coverage', label: 'Coverage', title: 'The share of the child rows with a key whose key exists in the parent',
    value: (r) => (measured(r) || {}).coverage },
  { id: 'orphans', label: 'Orphans', num: true,
    title: 'Child rows whose key is not in the parent. "≥" marks a count that is a lower bound: the row says what was measured',
    value: (r) => (measured(r) || {}).orphan_rows },
  { id: 'nulls', label: 'Empty keys', num: true, title: 'Child rows whose key is empty',
    value: (r) => (measured(r) || {}).null_share },
  { id: 'cardinality', label: 'Cardinality', title: '1-1, 1-N or N-N as the data and the catalog show it',
    value: (r) => (measured(r) || {}).cardinality },
  { id: 'rows', label: 'Measured on', num: true, title: 'The rows of the child table that were really measured: all of them, or a sample',
    value: (r) => (measured(r) || {}).rows },
];

async function loadRelations() {
  if (!state.wsId) return;
  const token = ++rel.token;
  const data = await api(wsPath('/relations/check'));
  if (token !== rel.token) return;
  rel.data = data;
  const keys = new Set(data.relationships.map((r) => r.key));
  for (const set of [rel.checked, rel.open]) for (const key of [...set]) if (!keys.has(key)) set.delete(key);
  if (data.job && data.job.state === 'running' && (!prof.job || prof.job.id !== data.job.id)) watchJob(data.job);
}

function relMatches(r) {
  const c = measured(r);
  if (rel.kind !== '*' && r.kind !== rel.kind) return false;
  if (rel.status === 'orphans' && !(c && c.orphan_rows > 0)) return false;
  if (rel.status === 'unchecked' && c) return false;
  if (rel.status === 'inexact' && !(c && !c.shown.exact)) return false;
  if (rel.status === 'sampled' && !(c && c.sampled)) return false;
  if (rel.status === 'unproven' && !(c && !c.cardinality_proven)) return false;
  const needle = rel.text.trim().toLowerCase();
  if (!needle) return true;
  return [r.from, r.to, ...r.from_columns, ...r.to_columns, r.name || ''].some((x) => x.toLowerCase().includes(needle));
}

function relSorted(rows) {
  const column = REL_COLUMNS.find((c) => c.id === rel.sort.by);
  if (!column) return rows;
  const out = rows.map((r, i) => [column.value(r), i, r]);
  out.sort((a, b) => {
    if (a[0] == null || b[0] == null) return (a[0] == null) - (b[0] == null) || a[1] - b[1];    // no value: always last
    if (a[0] < b[0]) return -rel.sort.dir;
    if (a[0] > b[0]) return rel.sort.dir;
    return a[1] - b[1];
  });
  return out.map((x) => x[2]);
}

function relSide(tableId, columns, arrow) {
  return h('div', { class: 'rel-side', title: `${tableId}.${columns.join(', ')}` },
    arrow ? h('span', { class: 'rel-arrow', 'aria-hidden': 'true', text: '→' }) : null,
    h('span', { class: 'rel-tbl' }, breakable(tableLabel(tableId))),
    h('span', { class: 'rel-cols' }, columns.length === 1 ? ['.', document.createElement('wbr'), ...breakable(columns[0])]
      : [' (', ...columns.flatMap((c, i) => [i ? ', ' : '', ...breakable(c)]), ')']));
}

function kindTag(kind, confidence) {
  const info = (state.meta.relationship_kinds || []).find((k) => k.id === kind);
  const text = (info ? info.short.toLowerCase() : kind) + (kind === 'declared' || confidence == null ? '' : ` ${kindShare(kind, confidence)}`);
  return h('span', { class: 'tag ' + kind, text, title: info ? `${info.label}: ${info.hint}` : '' });
}

/** A share as a bar and a number. `text` is the server's own wording (relations.share_text): the report prints
    the same, and it is never "100%" when something is missing. */
function coverageMeter(share, title, text) {
  return h('div', { class: 'meter' + (share < 1 ? ' warn' : ' good'), title },
    h('span', { class: 'track' }, h('span', { class: 'fill', style: `width:${Math.max(share ? 2 : 0, Math.floor(share * 100))}%` })),
    h('span', { class: 'value', text }));
}

function renderRelations() {
  if (state.tab !== 'relations' || !state.model) return;
  const box = $('#relations-body');
  const d = rel.data;
  if (!d) { box.replaceChildren(h('p', { class: 'note pad', text: 'Loading…' })); return; }

  const kinds = (state.meta.relationship_kinds || []).filter((k) => d.relationships.some((r) => r.kind === k.id));
  if (rel.kind !== '*' && !kinds.some((k) => k.id === rel.kind)) rel.kind = '*';
  $('#rel-kind').replaceChildren(h('option', { value: '*', text: 'All kinds' }),
    ...kinds.map((k) => h('option', { value: k.id, text: k.short })));
  $('#rel-kind').value = rel.kind;
  $('#rel-status').replaceChildren(...REL_STATUS.map(([value, text]) => h('option', { value, text })));
  $('#rel-status').value = rel.status;
  syncRelButtons();

  const out = [];
  const s = d.summary;
  const facts = [plural(s.relationships, 'relationship'), `${s.checked} checked`];
  if (s.with_orphans) facts.push(`${s.with_orphans} with orphan rows`);
  if (s.not_exact) facts.push(`${s.not_exact} with counts that are a lower bound (≥)`);
  if (s.not_checked) facts.push(`${s.not_checked} could not be checked`);
  out.push(h('div', { class: 'view-head' }, h('div', {},
    h('div', { class: 'view-title', text: 'Relationships' }),
    h('div', { class: 'd-sub', text: s.relationships ? facts.join(' · ') + ' · click one to see the evidence'
      : 'This workspace has no relationship yet' }))));

  // links suggested by value overlap: accept or reject
  if (d.suggestions.length) {
    out.push(h('div', { class: 'sub-head' }, 'Suggested links', h('span', { class: 'n', text: d.suggestions.length })),
      h('p', { class: 'sub-note', text: 'Columns whose values are found in a key of another table. Accept one and it becomes a ' +
        'relationship (a dotted line in the graph and the exports); reject one and it is hidden and not suggested again.' }),
      h('div', { class: 'table-fit' }, h('table', { class: 'data-table rel-suggest' },
        h('colgroup', {}, h('col', {}), h('col', { class: 'w-overlap' }), h('col', { class: 'w-evidence' }), h('col', { class: 'w-actions' })),
        h('thead', {}, h('tr', {}, h('th', { class: 'pin pin-last', text: 'Column → key it matches' }), h('th', { text: 'Overlap' }),
          h('th', { text: 'Evidence' }), h('th', {}))),
        h('tbody', {}, d.suggestions.map(suggestionRow)))));
  }

  // every relationship of the model with its last check
  const rows = relSorted(d.relationships.filter(relMatches));
  if (d.relationships.length) {
    const head = REL_COLUMNS.map((c) => {
      const on = rel.sort.by === c.id;
      return h('th', { class: 'sortable' + (c.num ? ' num' : '') + (c.id === 'name' ? ' pin pin-last' : ''), title: (c.title ? c.title + '. ' : '') + 'Click to sort',
                       'aria-sort': on ? (rel.sort.dir > 0 ? 'ascending' : 'descending') : 'none',
                       // first click: the lowest coverage, or the most orphans / empty keys / rows, on top
                       onclick: () => { rel.sort = { by: c.id, dir: on ? -rel.sort.dir : (c.num ? -1 : 1) }; renderRelations(); } },
        c.label, h('span', { class: 'arrow', 'aria-hidden': 'true', text: on ? (rel.sort.dir > 0 ? '▲' : '▼') : '' }));
    });
    const able = rows.filter((r) => r.checkable);
    const all = h('input', { type: 'checkbox', 'aria-label': 'Select every relationship shown', title: 'Select every relationship shown',
      checked: able.length > 0 && able.every((r) => rel.checked.has(r.key)),
      onchange: (e) => { for (const r of able) { if (e.target.checked) rel.checked.add(r.key); else rel.checked.delete(r.key); } renderRelations(); } });
    if (d.suggestions.length || rows.length !== d.relationships.length) {
      out.push(h('div', { class: 'sub-head' }, d.suggestions.length ? 'Relationships' : null,
        rows.length !== d.relationships.length ? h('span', { class: 'n', text: `${rows.length} of ${d.relationships.length} shown` }) : null));
    }
    // Like the Profile grid: a minimum width, and in a narrow panel it scrolls sideways inside .table-fit.
    out.push(h('div', { class: 'table-fit', id: 'rel-fit' }, h('table', { class: 'data-table rel-grid' },
      h('colgroup', {}, h('col', { class: 'w-check' }), h('col', {}), h('col', { class: 'w-kind' }), h('col', { class: 'w-cov' }),
        h('col', { class: 'w-n' }), h('col', { class: 'w-nulls' }), h('col', { class: 'w-card' }), h('col', { class: 'w-rows' })),
      h('thead', {}, h('tr', {}, h('th', { class: 'c-check pin' }, all), head)),
      h('tbody', {}, rows.flatMap(relationRows)))));
    if (!rows.length) out.push(h('div', { class: 'empty-note' }, h('h3', { text: 'No relationship matches the filters' }),
      h('p', { text: 'Change the filters above to see the others.' })));
  } else {
    out.push(h('div', { class: 'empty-note' }, h('h3', { text: 'No relationships yet' }),
      h('p', { text: 'None is declared and none could be inferred from joins or names. "Find links by values" looks for ' +
        'columns whose values are found in a key of another table.' })));
  }
  if (s.relationships && !s.checked && !s.not_checked) {
    out.push(h('div', { class: 'callout' }, 'Nothing has been checked yet. ',
      h('button', { class: 'link', type: 'button', text: 'Check all', onclick: () => startRelations({ relationships: 'all' }).catch(fail) }),
      ' lets the database count each relationship over the whole child table where both tables are in the same database. ' +
      'Otherwise it reads the child\'s keys once (a sample when the table is big) and asks the parent which of them exist.'));
  }

  // what was decided about suggestions
  for (const [status, title, note] of [
    ['accepted', 'Accepted links', 'These are relationships of the kind "from values" now. Undo puts one back among the suggestions.'],
    ['rejected', 'Rejected links', 'Hidden, and not suggested again. Undo puts one back among the suggestions.']]) {
    const links = d[status];
    if (!links.length) continue;
    out.push(h('details', { class: 'decided' }, h('summary', {}, title, h('span', { class: 'n', text: links.length })),
      h('p', { class: 'sub-note', text: note }),
      h('ul', { class: 'decided-list' }, links.map((x) => h('li', {},
        h('span', { class: 'rel-pair' }, relSide(x.from, x.from_columns), relSide(x.to, x.to_columns, true)),
        h('span', { class: 'muted', text: `${x.overlap_text || pct(x.confidence || 0)} of the values found` + (x.decided_at ? ` · ${when(x.decided_at)}` : '') }),
        h('button', { class: 'btn small', type: 'button', text: 'Undo', onclick: () => decideLink(x, 'reset').catch(fail) }))))));
  }
  const top = box.scrollTop;
  const left = ($('#rel-fit') || {}).scrollLeft || 0;       // a narrow panel: the list is scrolled sideways
  box.replaceChildren(...out);
  box.scrollTop = top;
  if (left && $('#rel-fit')) $('#rel-fit').scrollLeft = left;
}

function suggestionRow(x) {
  const e = x.evidence || {};
  const lines = [e.text, e.parent_key ? `the parent column: ${e.parent_key}` : null, e.name_hint].filter(Boolean);
  return h('tr', {},
    h('td', { class: 'c-strong pin pin-last' }, relSide(x.from, x.from_columns), relSide(x.to, x.to_columns, true)),
    h('td', {}, coverageMeter(x.confidence || 0, `${whole(e.values_found)} of ${whole(e.values_checked)} distinct values exist in the parent`,
      x.overlap_text || pct(x.confidence || 0)),
      h('div', { class: 'muted small', text: `${whole(e.values_found)} of ${whole(e.values_checked)} values` })),
    h('td', { class: 'evidence' }, lines.map((t) => h('div', { text: t })),
      h('div', { class: 'muted' }, e.sampled ? h('span', { class: 'tag sampled', text: 'sample' }) : null, e.sampled ? ' ' : '', e.label || '',
        x.cardinality && x.cardinality !== 'unknown' ? ` · ${x.cardinality}` : '')),
    h('td', { class: 'c-actions' },
      h('button', { class: 'btn small primary', type: 'button', text: 'Accept', title: 'Make it a relationship of the kind "from values"',
                    onclick: () => decideLink(x, 'accept').catch(fail) }),
      h('button', { class: 'btn small', type: 'button', text: 'Reject', title: 'Hide it and do not suggest it again',
                    onclick: () => decideLink(x, 'reject').catch(fail) })));
}

function relationRows(r) {
  const c = measured(r);
  const open = rel.open.has(r.key);
  const tick = h('input', { type: 'checkbox', 'aria-label': `Select ${r.from} to ${r.to}`, disabled: !r.checkable, checked: rel.checked.has(r.key),
    onclick: (e) => e.stopPropagation(),
    onchange: (e) => { if (e.target.checked) rel.checked.add(r.key); else rel.checked.delete(r.key); syncRelButtons(); } });
  // What the list prints comes from the server (check.shown), in the words the report uses too. A count that is a
  // lower bound says so in the row itself: "≥ 40", and under the names what was measured.
  const v = c ? c.shown : null;
  const name = h('td', { class: 'pin pin-last' }, h('div', { class: 'rel-name' }, h('span', { class: 'caret', text: open ? '▾' : '▸' }),
    h('div', { class: 'rel-pair' }, relSide(r.from, r.from_columns), relSide(r.to, r.to_columns, true),
      v && v.tag ? h('div', { class: 'rel-limit', title: v.notes.join('\n') },
        h('span', { class: 'tag warn', text: 'not the whole table' }), h('span', { class: 'what', text: v.tag })) : null)));
  const cells = [h('td', { class: 'c-check pin' }, tick), name, h('td', {}, kindTag(r.kind, r.confidence))];
  if (c) {
    const orphans = c.orphan_rows ? `${v.exact ? '' : 'At least '}${whole(c.orphan_rows)} rows with ${whole(c.orphan_keys)} different keys have no parent`
      + (v.exact ? '' : ` (${v.tag})`) : (v.exact ? 'Every key exists in the parent' : `No orphan row in what was measured (${v.tag})`);
    cells.push(
      h('td', {}, c.coverage == null ? h('span', { class: 'muted', text: 'no key to check', title: v.coverage_title })
        : coverageMeter(c.coverage, v.coverage_title + (v.exact ? '' : ` (${v.tag})`), v.coverage)),
      h('td', { class: 'num' + (c.orphan_rows ? ' has-orphans' : ''), text: v.orphans, title: orphans }),
      h('td', { class: 'num', text: c.null_share == null ? '—' : v.empty_keys, title: v.empty_title }),
      h('td', { title: c.cardinality_note || '' }, h('span', { class: 'card', text: c.cardinality === 'unknown' ? 'unknown' : c.cardinality }),
        c.cardinality_proven ? null : h('span', { class: 'unproven', text: 'not proven' })),
      // two short lines: "sample of 91.8K" on one line is wider than this column
      h('td', { class: 'num', title: `${c.label}. ${v.how}.` }, c.sampled ? [h('span', { class: 'tag sampled', text: 'sample' }), h('span', { class: 'unproven', text: `of ${compact(c.rows)}` })]
        : h('span', { class: 'muted', text: `all ${compact(c.rows)}` })));
  } else {
    const why = r.check ? r.check.label : 'not checked yet';
    cells.push(h('td', { colspan: 5, class: 'muted' + (r.check && r.check.reason_code === 'timeout' ? ' timed-out' : ''), text: why, title: why }));
  }
  const rows = [h('tr', { class: 'click rel-row' + (open ? ' open' : ''), 'data-key': r.key,
                          onclick: () => { if (open) rel.open.delete(r.key); else rel.open.add(r.key); renderRelations(); } }, cells)];
  if (open) rows.push(h('tr', { class: 'col-detail' }, h('td', { colspan: 8 }, h('div', { class: 'detail-fit' }, relationDetail(r, c)))));
  return rows;
}

function relationDetail(r, c) {
  const facts = h('dl', { class: 'kv tight evidence-list' });
  const fact = (k, ...v) => { if (v.some((x) => x != null && x !== '')) facts.append(h('dt', { text: k }), h('dd', {}, v)); };
  const model = [KIND_TEXT[r.kind] || r.kind];
  if (r.name) model.push(`constraint ${r.name}`);
  if (r.kind !== 'declared' && r.confidence != null) model.push(`confidence ${kindShare(r.kind, r.confidence)}`);
  if (r.model_cardinality && r.model_cardinality !== 'unknown') model.push(`${r.model_cardinality} in the model`);
  fact('In the model', model.join(' · '));
  if (!c) {
    fact('Check', r.check ? r.check.label : 'Not checked yet.');
    if (r.check && r.check.reason_code === 'timeout') {
      fact('Why', `The check did not finish within ${rel.data.settings.check_budget_s} s and was stopped. Nothing is tried again on the whole table.`);
    }
  } else {
    const v = c.shown;
    fact('Measured', `${c.label}. ${v.how}.`);
    // a count that is a lower bound: the whole sentence, here; the row above carries the short form
    for (const note of v.notes) fact('Not exact', h('span', { class: 'warn-text', text: note }));
    if (c.method !== 'database') fact('Keys', `${whole(c.keys_checked)} of ${whole(c.keys_total)} distinct keys were looked up in the parent`);
    fact('Coverage', c.coverage == null ? 'no row has a key' : `${v.coverage_title} (${v.coverage})`);
    if (c.orphan_rows) {
      const examples = (c.orphan_examples || []).map((x) =>
        h('span', { class: 'chip' + (c.examples_masked ? ' masked' : ''), text: x.key.map((k) => (k == null ? 'null' : String(k))).join(' / '),
                    title: plural(x.rows, 'row') }, h('span', { class: 'n', text: ` ×${whole(x.rows)}` })));
      const which = c.orphan_keys > examples.length ? `The ${examples.length} keys with the most rows` + (v.exact && !c.examples_from_copy ? '. '
        : ' among the keys that were read. ') : '';
      fact('Orphans', `${v.exact ? '' : 'At least '}${whole(c.orphan_rows)} rows with ${whole(c.orphan_keys)} different keys have no parent`,
        examples.length ? h('div', { class: 'chips' }, examples) : null,
        examples.length ? h('div', { class: 'muted', text: which +
          (c.examples_masked ? 'The column is personal: its keys are saved and shown masked.' : '') }) : null);
    } else if (c.coverage != null) fact('Orphans', v.exact ? 'none' : 'none in what was measured');
    fact('Empty keys', `${whole(c.null_rows)} of ${whole(c.rows)} rows (${v.empty_keys || '0%'})`);
    fact('Parent key', c.parent_unique_how);
    fact('Cardinality', `${c.cardinality}${c.cardinality_proven ? '' : ' (not proven)'}`, c.cardinality_note ? ` — ${c.cardinality_note}` : '',
      // the database counted the rows; how often a key repeats in the child still comes from what the app read
      c.method === 'database' && c.cardinality_on_sample ? `. How often a key repeats was read from a sample of the child (${c.child_sample_method}).` : '');
    fact('Cost', `${plural(c.statements || 0, 'statement')} sent · ${c.elapsed_s} s · checked ${when(c.checked_at)}`);
  }
  const actions = h('div', { class: 'detail-actions' },
    r.checkable ? h('button', { class: 'btn small', type: 'button', text: c ? 'Check again' : 'Check this one',
                                onclick: () => startRelations({ relationships: [r.key] }).catch(fail) }) : null,
    h('button', { class: 'btn small', type: 'button', text: `Open ${tableLabel(r.from)}`, onclick: () => selectTable(r.from) }),
    r.to !== r.from ? h('button', { class: 'btn small', type: 'button', text: `Open ${tableLabel(r.to)}`, onclick: () => selectTable(r.to) }) : null);
  return h('div', { class: 'rel-detail' }, facts, actions);
}

function syncRelButtons() {
  const n = rel.checked.size;
  $('#rel-check-selected').textContent = n ? `Check selected (${n})` : 'Check selected';
}

async function startRelations(body) {
  let job;
  try {
    job = await sendJSON(wsPath('/relations/check'), body);
  } catch (err) {
    const data = err.data || {};
    if (data.needs_password && data.source) {
      const pw = await ask('Password needed', { type: 'password', ok: 'Start',
        text: `Enter the password for ${data.source.label}. It is kept in memory only until the app is closed.` });
      if (!pw) return;
      return startRelations({ ...body, passwords: { ...(body.passwords || {}), [data.source.id]: pw } });
    }
    if (err.status === 409 && data.job) { watchJob(data.job); toast(err.message); return; }
    throw err;
  }
  rel.linksOnly = Array.isArray(body.relationships) && !body.relationships.length;
  if (body.passwords) refreshSources().catch(() => {});
  watchJob(job);
}

function checkSelected() {
  const keys = [...rel.checked];
  if (!keys.length) { toast('Tick relationships in the list first, then press "Check selected".'); return; }
  return startRelations({ relationships: keys });
}

async function decideLink(link, action) {
  const done = await sendJSON(wsPath('/relations/suggestions'), { key: link.key, action });
  const where = `${tableLabel(link.from)}.${link.from_columns.join(', ')} → ${tableLabel(link.to)}.${link.to_columns.join(', ')}`;
  // An accepted link is measured at once (the server starts the check), so it is never listed as "not checked yet".
  if (action === 'accept') toast(`Accepted: ${where} is a relationship now (a dotted line in the graph). ` +
    (done.job ? 'It is being checked now.' : `It was not checked yet (${done.not_checked_now}): "Check all" measures it.`), 'ok', 7000);
  else if (action === 'reject') toast(`Rejected: ${where} is hidden and will not be suggested again.`, 'info', 5000);
  else toast(`${where} is back among the suggestions.`, 'info', 4000);
  // An accepted link is part of the model, and so of the graph and the table details.
  if (action === 'accept' || link.status === 'accepted') await openWorkspace(state.wsId, { keep: true });
  else { await loadRelations(); renderRelations(); }
  if (done.job) { rel.linksOnly = false; watchJob(done.job); }
}

/** A relationship check moved on, or ended. (A scan of scripts is scripts.js's.) */
async function relationsJob(job, finished) {
  if (job.kind !== 'relations') return;
  await loadRelations();
  renderRelations();
  if (!finished) return;
  renderJob();
  const tried = job.suggest ? (rel.data ? rel.data.suggestions.length : 0) : null;
  const failed = job.errors.length;
  const s = rel.data ? rel.data.summary : null;
  if (job.state === 'cancelled') toast(`Cancelled after ${job.done} of ${job.total} steps. What was measured before is kept.`, 'info', 7000);
  else if (job.state === 'failed') toast(`The check failed: ${job.error}`, 'error', 12000);
  else {
    const parts = [`Done in ${Math.round(job.elapsed_s)} s.`];
    if (s && s.relationships && !(job.suggest && rel.linksOnly)) {
      parts.push(`${s.checked} of ${plural(s.relationships, 'relationship')} checked` + (s.with_orphans ? `, ${s.with_orphans} with orphan rows.` : '.'));
      if (s.not_checked) parts.push(`${s.not_checked} could not be checked: the list says why.`);
    }
    if (tried != null) parts.push(tried ? `${plural(tried, 'link')} suggested by values.` : 'No link was suggested by values.');
    toast(parts.join(' '), failed ? 'info' : 'ok', 7000);
  }
  if (failed) toast(job.errors.slice(0, 6).map((e) => `${e.table}: ${e.message}`).join('\n') + (failed > 6 ? `\n… and ${failed - 6} more` : ''), 'error', 15000);
  if (job.notes.length) toast(job.notes.join('\n'), 'info', 12000);
  if (state.tab === 'report') renderReport().catch(fail);
}

// ====================================================================== SQL ====

const sqlTab = {
  info: null,                   // GET /workspaces/{ws}/sql
  source: null,                 // id of the source the query runs against
  drafts: new Map(),            // workspace id -> the text in the editor
  result: null,
  error: null,                  // { title, message, hint }
  running: false,
  note: null,
  timer: null,
  token: 0,
};

async function loadSql() {
  if (!state.wsId) return;
  const info = await api(wsPath('/sql'));
  sqlTab.info = info;
  if (!info.sources.some((s) => s.id === sqlTab.source)) sqlTab.source = (info.sources[0] || {}).id || null;
}

function renderSql() {
  if (state.tab !== 'sql' || !state.model) return;
  const info = sqlTab.info;
  if (!info) return;
  const select = $('#sql-source');
  select.replaceChildren(...info.sources.map((s) => h('option', { value: s.id,
    text: `${s.label} · ${s.engine}${s.blocked ? ' (cannot run queries yet)' : s.needs_password ? ' (password needed)' : ''}` })));
  if (sqlTab.source) select.value = sqlTab.source;
  const able = info.sources.length > 0;
  select.disabled = !able;
  $('#sql-editor').disabled = !able;
  $('#sql-run').disabled = !able || sqlTab.running || !!sqlBlocked();
  $('#sql-run').textContent = sqlTab.running ? 'Running…' : 'Run';
  $('#sql-csv').disabled = !sqlTab.result || sqlTab.running;
  $('#sql-note').textContent = `Read-only · one SELECT or WITH statement · first ${whole(info.max_rows)} rows · ${info.timeout_s} s timeout`;
  $('#sql-banner').hidden = !info.show_personal;
  renderSqlHistory();
  renderSqlResult();
}

/** Why the chosen source cannot be queried yet (an Oracle source whose list of what can be called was not read), or null. */
function sqlBlocked() {
  const source = sqlTab.info && sqlTab.info.sources.find((s) => s.id === sqlTab.source);
  return source && source.blocked ? source.blocked : null;
}

function renderSqlHistory() {
  const info = sqlTab.info;
  const labels = new Map(info.sources.map((s) => [s.id, s.label]));
  $('#sql-history-count').textContent = info.history.length ? `${info.history.length} of ${info.history_size}` : '';
  $('#sql-history-clear').hidden = !info.history.length;
  const items = info.history.map((q) => {
    const flat = q.sql.replace(/\s+/g, ' ').trim();
    const where = [when(q.ran_at), labels.get(q.source_id)].filter(Boolean).join(' · ');
    return h('li', {}, h('button', { class: 'hist', type: 'button', title: `${q.sql}\n\n${where}\nClick to put it in the editor`,
                                     onclick: () => useHistory(q) },
      h('span', { class: 'h-meta' }, h('span', { class: 'tag ' + ({ ok: 'ok', blocked: 'bad', failed: 'warn' }[q.outcome] || ''), text: q.outcome || '?' }),
        h('span', { text: when(q.ran_at) }), info.sources.length > 1 && labels.has(q.source_id) ? h('span', { class: 'h-src', text: labels.get(q.source_id) }) : null),
      h('span', { class: 'h-sql', text: flat })));
  });
  $('#sql-history').replaceChildren(...(items.length ? items : [h('li', { class: 'note h-empty',
    text: `The texts of the last ${info.history_size} queries of this workspace are kept here. Results are never saved.` })]));
}

function useHistory(q) {
  $('#sql-editor').value = q.sql;
  sqlTab.drafts.set(state.wsId, q.sql);
  if (sqlTab.info.sources.some((s) => s.id === q.source_id)) { sqlTab.source = q.source_id; $('#sql-source').value = q.source_id; }
  $('#sql-editor').focus();
  if (!sqlTab.result && !sqlTab.error) renderSqlResult();
}

function insertIntoEditor(text) {
  const editor = $('#sql-editor');
  const start = editor.selectionStart == null ? editor.value.length : editor.selectionStart;
  editor.setRangeText(text, start, editor.selectionEnd == null ? start : editor.selectionEnd, 'end');
  sqlTab.drafts.set(state.wsId, editor.value);
  editor.focus();
}

function renderSqlResult() {
  const box = $('#sql-result');
  const info = sqlTab.info;
  if (!info.sources.length) {
    box.replaceChildren(h('div', { class: 'empty-note' }, h('h3', { text: 'No source to query' }),
      h('p', { text: 'SQL files hold the structure only. Add a live database or CSV / Excel files to this workspace to run queries.' })));
    return;
  }
  if (sqlTab.running) { box.replaceChildren(h('p', { class: 'note', id: 'sql-running', text: 'Running…' })); return; }
  if (sqlBlocked() && !sqlTab.result) {
    box.replaceChildren(h('div', { class: 'callout bad', role: 'alert' }, h('strong', { text: 'No query can run on this source yet' }),
      h('div', { class: 'sql-message', text: sqlBlocked() }),
      h('div', { class: 'sql-hint', text: 'Refresh the source with the ↻ button in the source list on the left. Nothing was sent.' })));
    return;
  }
  if (sqlTab.error) {
    const e = sqlTab.error;
    box.replaceChildren(h('div', { class: 'callout bad', role: 'alert' }, h('strong', { text: e.title }),
      h('div', { class: 'sql-message', text: e.message }), e.hint ? h('div', { class: 'sql-hint', text: e.hint }) : null));
    return;
  }
  const r = sqlTab.result;
  if (!r) {
    const source = info.sources.find((s) => s.id === sqlTab.source) || info.sources[0];
    const shown = source.tables.slice(0, 60);
    box.replaceChildren(...[sqlTab.note ? h('div', { class: 'callout', text: sqlTab.note }) : null,
      h('p', { class: 'note', text: 'Write one SELECT or WITH statement and press Run (Ctrl+Enter). Nothing but a read is ever sent: ' +
        'writes, DDL, locking reads, calls of functions that are not on the allow-list and a second statement are refused before ' +
        `anything reaches the source. At most ${whole(info.max_rows)} rows come back, and personal columns are masked.` }),
      shown.length ? h('div', { class: 'section' }, h('div', { class: 'section-title' }, `Tables of ${source.label}`,
        h('span', { class: 'n', text: source.tables.length })),
        h('div', { class: 'chips' }, shown.map((t) => h('button', { class: 'chip pick', type: 'button', text: t, title: 'Click to put the name in the editor',
                                                                   onclick: () => insertIntoEditor(t) })),
          source.tables.length > shown.length ? h('span', { class: 'muted', text: `and ${source.tables.length - shown.length} more` }) : null)) : null,
    ].filter(Boolean));
    return;
  }
  const line = h('div', { class: 'result-line' },
    h('strong', { text: plural(r.row_count, 'row').replace(String(r.row_count), whole(r.row_count)) }),
    r.more ? h('span', { class: 'tag warn', text: `more rows exist — only the first ${whole(r.max_rows)} are shown`,
                         title: 'The read stopped after one row more than the limit. Narrow the query, or count with COUNT(*).' }) : null,
    h('span', { class: 'muted', text: `${r.elapsed_s} s · ${r.source.label}` }),
    r.personal.length ? h('span', { class: 'tag ' + (r.masked ? 'personal' : 'warn'),
      text: r.masked ? `${plural(r.personal.length, 'personal column')} masked` : `${plural(r.personal.length, 'personal column')} shown unmasked`,
      title: maskedColumnsTitle(r.columns) }) : null);
  const sent = h('details', { class: 'as-table' }, h('summary', { text: 'The statement that was sent' }),
    h('pre', { class: 'sql-sent', text: r.sql }));
  if (!r.columns.length) { box.replaceChildren(line, sent, h('p', { class: 'note', text: 'The statement returned no columns.' })); return; }
  // a column of numbers is set to the right, its heading with it
  const numeric = r.columns.map((_, i) => r.rows.some((row) => typeof row[i] === 'number')
    && r.rows.every((row) => row[i] == null || typeof row[i] === 'number'));
  const table = h('table', { class: 'data-table result' },
    h('thead', {}, h('tr', {}, h('th', { class: 'rownum', text: '#' }), r.columns.map((c, i) =>
      h('th', { class: (numeric[i] ? 'num ' : '') + (c.personal ? 'personal' : ''), title: c.personal ? c.reason : '' }, c.name,
        c.personal ? h('span', { class: 'tag personal', text: r.masked ? 'masked' : 'personal' }) : null)))),
    h('tbody', {}, r.rows.map((row, n) => h('tr', {}, h('td', { class: 'rownum', text: n + 1 }), row.map((v, i) =>
      h('td', { class: (v == null ? 'null ' : '') + (numeric[i] ? 'num ' : '') + (r.columns[i].personal && r.masked ? 'masked' : ''),
                title: v == null ? '' : String(v), text: v == null ? 'null' : String(v) }))))));
  box.replaceChildren(line, sent, r.rows.length ? h('div', { class: 'result-scroll', tabindex: '0' }, table)
    : h('div', { class: 'result-scroll' }, table, h('p', { class: 'note pad', text: 'The query returned no rows.' })));
}

// The tooltip of "N personal columns masked": one line per personal column, with the reason. A column that is
// shown under its own name is named once - "employee.first_name: looks like name" - and one that the statement
// renamed keeps both names: "who: employee.first_name: looks like name".
function maskedColumnsTitle(columns) {
  return columns.filter((c) => c.personal).map((c) => {
    const reason = c.reason || 'personal';
    const from = reason.includes(': ') ? reason.slice(0, reason.indexOf(': ')) : '';      // the table column, where it is known
    const named = from.toLowerCase().endsWith('.' + String(c.name).toLowerCase());
    return named || !c.name ? reason : `${c.name}: ${reason}`;
  }).join('\n');
}

function sqlFailure(err) {
  const text = err.message || String(err);
  const prefix = 'Blocked by the read-only guard: ';
  if (text.startsWith(prefix)) {
    return { title: 'Blocked by the read-only guard — nothing was sent', message: text.slice(prefix.length),
             hint: 'Only one SELECT or WITH statement is accepted. Writes, DDL, SELECT INTO, locking reads, lock hints, sequences, ' +
                   'functions that are not on the allow-list and a second statement are refused before anything reaches the source.' };
  }
  if (/timed out/i.test(text)) return { title: 'The query timed out', message: text, hint: 'Narrow it with a WHERE clause, or query fewer rows.' };
  return { title: 'The query did not run', message: text };
}

async function runSql(password) {
  if (sqlTab.running || !sqlTab.info || !sqlTab.info.sources.length) return;
  const text = $('#sql-editor').value;
  if (!text.trim()) { toast('Write a query first.'); $('#sql-editor').focus(); return; }
  const wsId = state.wsId;
  const token = ++sqlTab.token;
  sqlTab.running = true;
  sqlTab.result = null;
  sqlTab.error = null;
  sqlTab.note = null;
  renderSql();
  const started = Date.now();
  sqlTab.timer = setInterval(() => {
    const node = $('#sql-running');
    if (node) node.textContent = `Running… ${Math.round((Date.now() - started) / 1000)} s`;
  }, 1000);
  let again = null;
  try {
    const body = { source: sqlTab.source, sql: text };
    if (password) body.password = password;
    const result = await sendJSON(`/workspaces/${enc(wsId)}/sql`, body);
    if (token === sqlTab.token) sqlTab.result = result;
  } catch (err) {
    const data = err.data || {};
    if (data.needs_password && data.source) again = data.source;
    else if (token === sqlTab.token) sqlTab.error = sqlFailure(err);
  } finally {
    clearInterval(sqlTab.timer);
    sqlTab.running = false;
  }
  if (wsId !== state.wsId) return;
  if (again) {
    renderSql();
    const pw = await ask('Password needed', { type: 'password', ok: 'Run',
      text: `Enter the password for ${again.label}. It is kept in memory only until the app is closed.` });
    if (pw) return runSql(pw);
    return;
  }
  if (password) refreshSources().catch(() => {});
  await loadSql();                                  // the history has a new entry
  renderSql();
}

async function exportSqlCsv() {
  const r = sqlTab.result;
  if (!r) return;
  const name = await saveFrom(wsPath(`/sql/results/${enc(r.id)}.csv`), `query-${r.id}.csv`);
  toast(`${name}: ${whole(r.row_count)} row${r.row_count === 1 ? '' : 's'} as shown${r.masked ? ', personal values masked' : ''}.`, 'ok', 4000);
}

async function clearSqlHistory() {
  const sure = await ask('Clear the query history?', { type: 'none', ok: 'Clear',
    text: 'The saved query texts of this workspace are deleted from this PC. Results were never saved.' });
  if (!sure) return;
  await api(wsPath('/sql/history'), { method: 'DELETE' });
  await loadSql();
  renderSql();
}

/** "Mask again" on the SQL tab: the same switch as on the Profile tab. */
async function maskSqlAgain() {
  await setShowPersonal(false);
  sqlTab.result = null;                             // a result shown unmasked left the app's memory with the switch
  sqlTab.note = 'Personal values are masked again. Run the query again to see its result.';
  await loadSql();
  renderSql();
}

// =================================================================== report ====

const rep = { quality: null, busy: false };

/** The ER graph's current view in words, as the report will describe it. */
function viewInWords() {
  const f = state.filters;
  const parts = [];
  const focus = state.focus.on && state.tables.has(state.focus.root) ? state.tables.get(state.focus.root) : null;
  if (focus) parts.push(`${focus.name} and what is within ${plural(state.focus.hops, 'hop')} of it`);
  if (f.schema !== '*') parts.push(`schema ${f.schema || '(none)'}`);
  const kinds = state.meta.relationship_kinds || [];
  const on = kinds.filter((k) => f.kinds[k.id]);
  if (on.length !== kinds.length) parts.push(on.length ? `relationships: ${on.map((k) => k.label).join(', ')}` : 'no relationships');
  if (f.minConf) parts.push(`confidence from ${percent(f.minConf)}`);
  return parts;
}

async function renderReport() {
  if (state.tab !== 'report' || !state.model) return;
  const m = state.model;
  const vis = visibleSets();
  const words = viewInWords();
  $('#report-all-note').textContent = `${plural(m.tables.length, 'table')}, ${plural(m.relationships.length, 'relationship')}`;
  $('#report-view-note').textContent = (words.length ? words.join(' · ') : 'no filter or focus is set in the ER graph, so this is the whole workspace') +
    ` — ${plural(vis.tables.size, 'table')}, ${plural(vis.rels.size, 'relationship')}`;
  for (const id of ['report-html', 'report-xlsx']) $('#' + id).disabled = rep.busy;

  const list = $('#report-ready');
  if (!prof.overview || !rel.data || !rep.quality) { list.replaceChildren(h('li', { class: 'note', text: 'Loading…' })); return; }
  const item = (ok, text, action) => h('li', {}, h('span', { class: 'mark ' + (ok ? 'ok' : 'todo'), 'aria-hidden': 'true', text: ok ? '✓' : '○' }),
    h('span', { text }), action || h('span', {}));
  const go = (tab, text) => h('button', { class: 'link', type: 'button', text, onclick: () => showTab(tab) });
  const able = prof.overview.tables.filter((t) => t.can_profile);
  const profiled = able.filter((t) => t.profiled_at).length;
  const stopped = able.filter((t) => t.status === 'timed_out' || t.status === 'failed').length;
  const sampled = able.filter((t) => t.sampled).length;
  const s = rel.data.summary;
  const q = rep.quality;
  const items = [];
  if (!m.tables.length) items.push(item(false, 'The workspace is empty: add a source first.'));
  else if (!able.length) items.push(item(false, 'No table has data to profile: the report holds the structure only.'));
  else {
    items.push(item(profiled === able.length, `${profiled} of ${plural(able.length, 'table')} with data profiled` +
      (sampled ? ` · ${sampled} on a sample (listed in the report's notes)` : '') + (stopped ? ` · ${stopped} timed out or failed` : ''),
      profiled === able.length ? null : go('profile', 'Open Profile')));
  }
  if (m.relationships.length) {
    items.push(item(s.never === 0,
      `${s.checked} of ${plural(s.relationships, 'relationship')} checked on the data` + (s.with_orphans ? ` · ${s.with_orphans} with orphan rows` : '') +
      (s.not_checked ? ` · ${s.not_checked} could not be checked` : ''),
      s.never === 0 ? null : go('relations', 'Open Relations')));
  }
  items.push(item(true, `${plural(q.findings.length, 'quality finding')}` + (q.findings.length
    ? ` (${q.severities.filter((x) => q.summary[x]).map((x) => `${q.summary[x]} ${x}`).join(', ')})` : '')));
  if (rel.data.accepted.length) items.push(item(true, `${plural(rel.data.accepted.length, 'link')} accepted from value overlap`));
  list.replaceChildren(...items);
}

async function loadReportFacts() {
  rep.quality = null;
  const [, , quality] = await Promise.all([loadOverview(), loadRelations(), api(wsPath('/quality'))]);
  rep.quality = quality;
}

async function downloadReport(format, button) {
  if (rep.busy) return;
  const q = new URLSearchParams({ format, scope: $('#report-scope-view').checked ? 'view' : 'all' });
  if (q.get('scope') === 'view') viewQuery(q);
  const label = button.textContent;
  rep.busy = true;
  button.textContent = 'Building…';
  for (const id of ['report-html', 'report-xlsx']) $('#' + id).disabled = true;
  try {
    const name = await saveFrom(wsPath('/report?' + q), `report.${format}`);
    toast(`${name} was handed to the browser. Personal values are masked in it.`, 'ok', 5000);
  } finally {
    rep.busy = false;
    button.textContent = label;
    for (const id of ['report-html', 'report-xlsx']) $('#' + id).disabled = false;
  }
}

// ------------------------------------------------------------------- wiring ----

function wireAnalysis() {
  const guard = (fn) => (...args) => Promise.resolve(fn(...args)).catch(fail);

  $('#rel-check-all').addEventListener('click', guard(() => startRelations({ relationships: 'all' })));
  $('#rel-check-selected').addEventListener('click', guard(checkSelected));
  $('#rel-suggest').addEventListener('click', guard(() => startRelations({ relationships: [], suggest: true })));
  $('#rel-job-cancel').addEventListener('click', guard(cancelJob));
  $('#rel-search').addEventListener('input', (e) => { rel.text = e.target.value; renderRelations(); });
  $('#rel-kind').addEventListener('change', (e) => { rel.kind = e.target.value; renderRelations(); });
  $('#rel-status').addEventListener('change', (e) => { rel.status = e.target.value; renderRelations(); });

  const editor = $('#sql-editor');
  $('#sql-run').addEventListener('click', guard(() => runSql()));
  $('#sql-csv').addEventListener('click', guard(exportSqlCsv));
  $('#sql-mask').addEventListener('click', guard(maskSqlAgain));
  $('#sql-history-clear').addEventListener('click', guard(clearSqlHistory));
  $('#sql-source').addEventListener('change', (e) => {
    sqlTab.source = e.target.value;
    $('#sql-run').disabled = sqlTab.running || !!sqlBlocked();
    if (!sqlTab.result && !sqlTab.error) renderSqlResult();
  });
  editor.addEventListener('input', () => sqlTab.drafts.set(state.wsId, editor.value));
  editor.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); runSql().catch(fail); }
    e.stopPropagation();                            // Escape and "/" belong to the editor here
  });

  $('#report-html').addEventListener('click', guard((e) => downloadReport('html', e.currentTarget)));
  $('#report-xlsx').addEventListener('click', guard((e) => downloadReport('xlsx', e.currentTarget)));

  // the report's "current view" follows the graph's focus and the schema filter
  $('#schema-filter').addEventListener('change', guard(renderReport));
  hooks.select.push(renderReport);

  hooks.job.push(relationsJob);
  hooks.workspace.push(async ({ keep }) => {
    if (!keep) { rel.checked.clear(); rel.open.clear(); rel.text = ''; $('#rel-search').value = ''; rel.kind = '*'; rel.status = '*'; }
    rel.data = null;
    rep.quality = null;
    sqlTab.token += 1;
    sqlTab.info = null;
    sqlTab.result = null;
    sqlTab.error = null;
    sqlTab.note = null;
    editor.value = sqlTab.drafts.get(state.wsId) || '';
    if (state.tab === 'relations') { await loadRelations(); renderRelations(); }
    if (state.tab === 'sql') { await loadSql(); renderSql(); }
    if (state.tab === 'report') { await renderReport(); await loadReportFacts(); await renderReport(); }
  });
  hooks.tab.push(async (id) => {
    if (id === 'relations') { renderRelations(); await loadRelations(); renderRelations(); }
    if (id === 'sql') { await loadSql(); renderSql(); }
    if (id === 'report') { await renderReport(); await loadReportFacts(); await renderReport(); }
  });
}

document.addEventListener('DOMContentLoaded', wireAnalysis);
