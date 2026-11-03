"""SQL files source: DDL and queries, read with the parser from the ER scripts (app.er)."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

from .. import er
from ..model import DECLARED, JOINS, Origin, SchemaModel, finalize_model
from . import SourceError, read_text

MAX_FILES = 2000


def collect_sql_files(path):
    """A .sql file, or every .sql file under a folder (like the scripts do)."""
    p = Path(str(path).strip().strip('"')).expanduser()
    if p.is_dir():
        files = sorted(f for f in p.rglob('*.sql') if f.is_file())
    elif p.is_file():
        files = [p]
    else:
        raise SourceError(f'Not found: {p}')
    if not files:
        raise SourceError(f'No .sql files found in {p}')
    if len(files) > MAX_FILES:
        raise SourceError(f'{len(files)} .sql files found; the limit is {MAX_FILES}. Point to a smaller folder.')
    return files


def read_sql_files(files, label='', source_id=None):
    """Parse the files into one SchemaModel. Returns (model, warnings)."""
    files = [Path(f) for f in files]
    if not files:
        raise SourceError('No .sql files were given.')
    em, warnings, where = er.Model(), [], {}
    for f in files:
        sql = read_text(f)
        before = {t: t.from_ddl for t in em.tables}
        skipped = io.StringIO()
        with contextlib.redirect_stderr(skipped):       # the parser reports odd statements on stderr
            er.Parser(em, er.tokenize(sql)).run()
        warnings += [f'{f.name}: {line.strip(" !")}' for line in skipped.getvalue().splitlines() if line.strip()]
        for t in em.tables:
            if t not in before or (t.from_ddl and not before[t]):
                where[t] = f.name                       # first seen here, or its CREATE TABLE is here
    er.finalize(em)
    if not em.tables:
        raise SourceError('No tables found in the SQL.')
    model = from_er_model(em, label, source_id, where)
    finalize_model(model, names=False)
    return model, warnings[:50]


def from_er_model(em, label='', source_id=None, where=None):
    """Convert the scripts' Model into the common SchemaModel."""
    model, tmap = SchemaModel(), {}
    for t in em.tables:
        st = model.table(t.name, t.schema)
        origin = 'ddl' if t.from_ddl else 'query'
        st.add_origin(Origin(origin, source_id, label, (where or {}).get(t)))
        for a in t.aliases:
            st.add_alias(a)
        columns = list(t.columns.values())
        if t.from_ddl:
            columns.sort(key=lambda c: (c.ddl_order is None, c.ddl_order or 0))
        one_key = sum(1 for c in columns if c.pk) == 1       # a primary key of one column is unique by itself
        for c in columns:
            listed = t.from_ddl and c.ddl_order is not None
            st.add_column(c.name, 'ddl' if listed else 'query', type=c.type,
                          nullable=c.nullable if listed else None, pk=c.pk, fk=c.fk,
                          unique=bool(getattr(c, 'unique', False) or (c.pk and one_key)))
        tmap[t] = st

    name = lambda end: end[0].columns[end[1]].name
    declared = {}
    for tbl, cols, rt, refcols in em.declared_fks:
        r = model.relate(tmap[tbl], cols, tmap[rt], refcols, DECLARED, cardinality='1-N',
                         evidence=['FOREIGN KEY / REFERENCES in the SQL'])
        for c, rc in zip(cols, refcols):
            declared[frozenset(((tbl, c.upper()), (rt, rc.upper())))] = r

    for key, rel in em.rels.items():
        how = ' and '.join(sorted({'join': 'JOIN condition', 'in': 'IN (sub-query)'}[k]
                                  for k in rel.kinds if k in ('join', 'in')))
        if key in declared:
            r = declared[key]
            if how and r is not None:
                if JOINS not in r.seen:
                    r.seen.append(JOINS)
                r.evidence.append(f'also used in a {how} in the SQL')
            continue
        if not how:
            continue
        one = rel.one
        many = (rel.b if one == rel.a else rel.a) if one is not None else rel.a
        one = one if one is not None else rel.b
        model.relate(tmap[many[0]], [name(many)], tmap[one[0]], [name(one)], JOINS,
                     cardinality='1-N' if rel.one is not None else 'unknown',
                     evidence=[f'{how} in the SQL'])
    return model
