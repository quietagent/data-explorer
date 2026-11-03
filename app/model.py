"""The common schema model.

Every source adapter (SQL files, live database, exported files) produces a
SchemaModel. A workspace merges the models of its sources into one, and every
feature reads only that merged model, so nothing downstream cares where a
table came from.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field

from . import er
from .er.inference import finalize, guess_one, names_table, pk_like

# How a relationship is known. RANK is the one place that lists the kinds: a relationship found
# several ways keeps the kind with the highest rank. Adding a kind here is all that is needed -
# stats(), the API's "kinds" filter, /api/meta and the page's checkboxes all read this table.
# KIND_INFO only holds optional texts for the page.
#   values: a link suggested by value overlap that the user accepted (app.analysis.relations).
DECLARED, JOINS, VALUES, NAMES = 'declared', 'joins', 'values', 'names'
RANK = {NAMES: 1, VALUES: 1.5, JOINS: 2, DECLARED: 3}
KIND_INFO = {
    DECLARED: {'label': 'declared', 'short': 'Declared',
               'hint': 'Foreign keys declared in the database or in DDL'},
    JOINS: {'label': 'from joins', 'short': 'Joins', 'hint': 'Inferred from JOIN conditions in SQL files'},
    VALUES: {'label': 'from values', 'short': 'Values',
             'hint': 'Suggested because the values of one column are found in a key of another table, and accepted by you'},
    NAMES: {'label': 'from names', 'short': 'Names',
            'hint': 'Inferred from column names, e.g. orders.customer_id to customers.id'},
}


def relationship_kinds():
    """Every kind in RANK, strongest evidence first, with the texts the page shows."""
    out = []
    for kind in sorted(RANK, key=lambda k: -RANK[k]):
        info = KIND_INFO.get(kind, {})
        out.append({'id': kind, 'rank': RANK[kind], 'inferred': kind != DECLARED,
                    'label': info.get('label') or f'from {kind}',
                    'short': info.get('short') or kind.capitalize(), 'hint': info.get('hint') or ''})
    return out

# Where a table or column was seen; earlier = more trusted for types and column order.
ORIGIN_PRIORITY = ('db', 'ddl', 'file', 'query', 'ref')
FULL_ORIGINS = ('db', 'ddl', 'file')            # these list every column of a table

JOIN_CONFIDENCE = {True: 0.85, False: 0.5}      # the "one" side is known / is not known
NAME_CONFIDENCE = {True: 0.8, False: 0.6}       # customer_id / billing_customer_id
GENERIC_KEYS = ('ID', 'OBJECT_ID', 'OBJID', 'OID', 'UUID', 'GUID', 'ROW_ID')   # as in pk_like()


def _priority(origins):
    return min((ORIGIN_PRIORITY.index(o) for o in origins if o in ORIGIN_PRIORITY),
               default=len(ORIGIN_PRIORITY))


# ----------------------------------------------------------------- objects ----

@dataclass
class Column:
    name: str
    type: str | None = None
    nullable: bool | None = None
    pk: bool = False                # declared primary key
    fk: bool = False                # declared foreign key
    origins: list = field(default_factory=list)
    types: dict = field(default_factory=dict)       # origin -> type as that origin reported it
    comment: str | None = None
    pk_inferred: bool = False       # looks like a key (set by finalize_model)
    fk_inferred: bool = False       # many-side of an inferred relationship
    unique: bool = False            # unique by itself: a primary key of one column, a one-column UNIQUE constraint
                                    # or unique index - as the catalog or the DDL says, never guessed from the data

    def absorb(self, other):
        mine = _priority(self.origins)
        if other.nullable is not None and (self.nullable is None or _priority(other.origins) < mine):
            self.nullable = other.nullable
        for o in other.origins:
            if o not in self.origins:
                self.origins.append(o)
        self.origins.sort(key=lambda o: _priority([o]))
        for o, t in other.types.items():
            self.types.setdefault(o, t)
        self.type = next((self.types[o] for o in ORIGIN_PRIORITY if o in self.types), self.type or other.type)
        self.pk = self.pk or other.pk
        self.fk = self.fk or other.fk
        self.unique = self.unique or other.unique
        self.comment = self.comment or other.comment

    def to_dict(self):
        return {'name': self.name, 'type': self.type, 'nullable': self.nullable, 'pk': self.pk, 'fk': self.fk,
                'origins': list(self.origins), 'types': dict(self.types), 'comment': self.comment,
                'pk_inferred': self.pk_inferred, 'fk_inferred': self.fk_inferred, 'unique': self.unique}

    @classmethod
    def from_dict(cls, d):
        return cls(name=d['name'], type=d.get('type'), nullable=d.get('nullable'), pk=bool(d.get('pk')),
                   fk=bool(d.get('fk')), origins=list(d.get('origins') or []), types=dict(d.get('types') or {}),
                   comment=d.get('comment'), unique=bool(d.get('unique')))


@dataclass
class Origin:
    """Where a table was seen. `detail` is for display; the other fields let the data be read again."""
    kind: str                       # db / ddl / file / query / ref
    source_id: str | None = None
    label: str = ''                 # the source's label
    detail: str | None = None       # file name, sheet, schema ... as shown on the page
    path: str | None = None         # file: path of the file, relative to the source's folder ('/' separated)
    sheet: str | None = None        # file: the sheet of an Excel workbook
    encoding: str | None = None     # file: text encoding when it is not UTF-8 (cp1256, utf-16 ...)
    schema: str | None = None       # db: schema, exactly as the catalog names it
    table: str | None = None        # db: table name, exactly as the catalog names it

    FIELDS = ('kind', 'source_id', 'label', 'detail', 'path', 'sheet', 'encoding', 'schema', 'table')

    def to_dict(self):
        return {k: getattr(self, k) for k in self.FIELDS}

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d.get(k) for k in cls.FIELDS if k != 'label'}, label=d.get('label') or '')


@dataclass(eq=False)
class Table:
    name: str
    schema: str | None = None
    kind: str = 'table'             # table / view
    aliases: list = field(default_factory=list)
    columns: list = field(default_factory=list)
    origins: list = field(default_factory=list)
    comment: str | None = None
    row_count: int | None = None    # hook: filled by profiling (session 2)
    _cols: dict = field(default_factory=dict, repr=False)

    @property
    def id(self):
        return f'{self.schema}.{self.name}' if self.schema else self.name

    @property
    def key(self):
        return self.id.upper()

    @property
    def complete(self):
        """True when some source listed every column (DDL, database, file)."""
        return any(o.kind in FULL_ORIGINS for o in self.origins)

    def column(self, name):
        return self._cols.get(name.upper())

    def add_column(self, name, origin, type=None, nullable=None, pk=False, fk=False, comment=None, unique=False):
        new = Column(name=name, type=type, nullable=nullable, pk=pk, fk=fk, origins=[origin],
                     types={origin: type} if type else {}, comment=comment, unique=unique)
        return self._put(new)

    def _put(self, col):
        cur = self._cols.get(col.name.upper())
        if cur is None:
            self._cols[col.name.upper()] = col
            self.columns.append(col)
            return col
        cur.absorb(col)
        return cur

    def add_origin(self, origin):
        if not any((o.kind, o.source_id, o.detail) == (origin.kind, origin.source_id, origin.detail)
                   for o in self.origins):
            self.origins.append(origin)

    def add_alias(self, alias):
        if alias.upper() != self.name.upper() and alias.upper() not in (a.upper() for a in self.aliases):
            self.aliases.append(alias)

    def absorb(self, other):
        """Merge another source's view of the same table into this one."""
        lead = other.complete and not self.complete     # the first full column list sets the order
        if any(o.kind == 'db' for o in other.origins) or not self.origins:
            self.kind = other.kind
        for o in other.origins:
            self.add_origin(o)
        for a in other.aliases:
            self.add_alias(a)
        self.comment = self.comment or other.comment
        if other.row_count is not None:
            self.row_count = other.row_count
        old = list(self.columns)
        merged = [self._put(Column.from_dict(c.to_dict())) for c in other.columns]
        if lead:
            first = {id(c) for c in merged}
            self.columns = merged + [c for c in old if id(c) not in first]

    def to_dict(self, columns=True):
        d = {'id': self.id, 'name': self.name, 'schema': self.schema, 'kind': self.kind,
             'aliases': list(self.aliases), 'origins': [o.to_dict() for o in self.origins],
             'comment': self.comment, 'row_count': self.row_count, 'column_count': len(self.columns),
             'complete': self.complete}
        if columns:
            d['columns'] = [c.to_dict() for c in self.columns]
        return d


@dataclass(eq=False)
class Relationship:
    """from = the side holding the foreign key (many), to = the referenced side (one).

    When cardinality is 'unknown' the two sides are just "matched by value" and
    their order means nothing.
    """
    from_table: Table
    from_columns: list
    to_table: Table
    to_columns: list
    source: str                         # declared / joins / names
    confidence: float | None = None     # inferred relationships only
    cardinality: str = 'unknown'        # 1-N / 1-1 / unknown
    name: str | None = None             # constraint name
    evidence: list = field(default_factory=list)
    seen: list = field(default_factory=list)    # every way this link was found

    def to_dict(self):
        return {'from': self.from_table.id, 'from_columns': list(self.from_columns),
                'to': self.to_table.id, 'to_columns': list(self.to_columns),
                'source': self.source, 'confidence': self.confidence, 'cardinality': self.cardinality,
                'name': self.name, 'evidence': list(self.evidence), 'seen': list(self.seen)}


# ------------------------------------------------------------------- model ----

class SchemaModel:
    def __init__(self):
        self.tables = []
        self.by_key = {}
        self.relationships = []
        self._rels = {}
        # A database source: what its catalog says besides tables. Oracle: {'callable': [names of what can
        # be called], 'schemas': [...], 'error': why they could not be read} - for the SQL tab's guard.
        self.catalog = None
        # A database source: the account's default schema as the catalog names it (None for SQLite, and
        # for a source saved before it was kept) - for the Scripts tab, where scripts leave it out.
        self.default_schema = None

    # -- tables
    def table(self, name, schema=None):
        """Find or create a table, merging by name case-insensitively.

        Same rules as Model.table() in the ER scripts: "ACCOUNT" and
        "RDB_OWNER.ACCOUNT" are the same table when that is unambiguous.
        """
        key = (schema.upper() + '.' if schema else '') + name.upper()
        t = self.by_key.get(key)
        if t is not None:
            return t
        short = name.upper()
        if not schema:                                # "ACCOUNT" after "RDB_OWNER.ACCOUNT"
            same = [x for x in self.tables if x.name.upper() == short]
            if len(same) == 1:
                self.by_key[key] = same[0]
                return same[0]
        else:                                         # "RDB_OWNER.ACCOUNT" after "ACCOUNT"
            t = self.by_key.get(short)
            if t is not None and t.schema is None:
                t.schema = schema
                self.by_key[key] = t
                return t
        t = Table(name=name, schema=schema)
        self.tables.append(t)
        self.by_key[key] = t
        return t

    def find(self, table_id):
        if not table_id:
            return None
        t = self.by_key.get(table_id.upper())
        if t is None:
            same = [x for x in self.tables if x.id.upper() == table_id.upper()
                    or x.name.upper() == table_id.upper()]
            t = same[0] if len(same) == 1 else None
        return t

    # -- relationships
    def relate(self, from_table, from_columns, to_table, to_columns, source, confidence=None,
               cardinality='unknown', name=None, evidence=(), seen=()):
        n = min(len(from_columns), len(to_columns))
        fcols = [self._col(from_table, c) for c in from_columns[:n]]
        tcols = [self._col(to_table, c) for c in to_columns[:n]]
        pairs = [frozenset(((id(from_table), f.upper()), (id(to_table), t.upper()))) for f, t in zip(fcols, tcols)]
        if not pairs or any(len(p) == 1 for p in pairs):      # a column related to itself
            return None
        key = frozenset(pairs)
        r = self._rels.get(key)
        if r is None:
            r = Relationship(from_table, fcols, to_table, tcols, source,
                             None if source == DECLARED else confidence, cardinality, name)
            self.relationships.append(r)
            self._rels[key] = r
        else:
            direction = cardinality != 'unknown' or source == DECLARED
            if RANK[source] > RANK[r.source]:
                r.source = source
                r.confidence = None if source == DECLARED else confidence
                adopt = direction
            else:
                if RANK[source] == RANK[r.source] and confidence is not None and r.source != DECLARED:
                    r.confidence = max(r.confidence or 0.0, confidence)
                adopt = direction and r.cardinality == 'unknown' and r.source != DECLARED
            if adopt:
                r.from_table, r.from_columns, r.to_table, r.to_columns = from_table, fcols, to_table, tcols
                r.cardinality = cardinality
            r.name = r.name or name
        for s in (source, *seen):
            if s not in r.seen:
                r.seen.append(s)
        r.seen.sort(key=lambda s: -RANK[s])
        for e in evidence:
            if e not in r.evidence:
                r.evidence.append(e)
        return r

    @staticmethod
    def _col(table, name):
        c = table.column(name)
        if c is None:                                 # a key column no source listed: keep the model consistent
            c = table.add_column(name, 'ref')
        return c.name

    # -- merging
    def merge(self, other):
        """Fold another model into this one (tables by name, relationships by their columns)."""
        tmap = {}
        for ot in other.tables:
            t = self.table(ot.name, ot.schema)
            t.absorb(ot)
            tmap[id(ot)] = t
        for r in other.relationships:
            self.relate(tmap[id(r.from_table)], r.from_columns, tmap[id(r.to_table)], r.to_columns, r.source,
                        r.confidence, r.cardinality, r.name, r.evidence, r.seen)
        return self

    # -- views
    def view(self, kinds=None, min_confidence=0.0, schemas=None, focus=None, hops=1):
        """Tables and relationships for a filtered view, optionally N hops around one table.

        The web page applies the same rules in the browser, so an exported
        "current view" matches what is on screen.
        """
        rels = [r for r in self.relationships
                if (kinds is None or r.source in kinds)
                and (r.source == DECLARED or (r.confidence or 0.0) >= min_confidence)]
        tables = self.tables
        if schemas is not None:
            wanted = {s.upper() for s in schemas}
            tables = [t for t in tables if (t.schema or '').upper() in wanted or t is focus]
        allowed = set(tables)
        rels = [r for r in rels if r.from_table in allowed and r.to_table in allowed]
        if focus is not None:
            adj = {}
            for r in rels:
                adj.setdefault(r.from_table, set()).add(r.to_table)
                adj.setdefault(r.to_table, set()).add(r.from_table)
            depth, dq = {focus: 0}, deque([focus])
            while dq:
                u = dq.popleft()
                if depth[u] >= hops:
                    continue
                for v in adj.get(u, ()):
                    if v not in depth:
                        depth[v] = depth[u] + 1
                        dq.append(v)
            tables = [t for t in tables if t in depth]
            rels = [r for r in rels if r.from_table in depth and r.to_table in depth]
        return tables, rels

    def stats(self):
        by = {kind: 0 for kind in sorted(RANK, key=lambda k: -RANK[k])}
        for r in self.relationships:
            by[r.source] = by.get(r.source, 0) + 1
        return {'tables': len(self.tables), 'columns': sum(len(t.columns) for t in self.tables),
                'relationships': len(self.relationships), **by}

    # -- persistence (structure only: names, types, keys - never data rows)
    def to_dict(self):
        out = {'tables': [t.to_dict() for t in self.tables],
               'relationships': [r.to_dict() for r in self.relationships]}
        if self.catalog is not None:
            out['catalog'] = self.catalog
        if self.default_schema:
            out['default_schema'] = self.default_schema
        return out

    @classmethod
    def from_dict(cls, d):
        m = cls()
        m.catalog = d.get('catalog')
        m.default_schema = d.get('default_schema')
        for td in d.get('tables', []):
            t = Table(name=td['name'], schema=td.get('schema'), kind=td.get('kind') or 'table',
                      aliases=list(td.get('aliases') or []), comment=td.get('comment'),
                      row_count=td.get('row_count'),
                      origins=[Origin.from_dict(o) for o in td.get('origins') or []])
            for cd in td.get('columns') or []:
                t._put(Column.from_dict(cd))
            m.tables.append(t)
            m.by_key[t.key] = t
        for rd in d.get('relationships', []):
            ft, tt = m.by_key.get(rd['from'].upper()), m.by_key.get(rd['to'].upper())
            if ft is not None and tt is not None:
                m.relate(ft, rd['from_columns'], tt, rd['to_columns'], rd['source'], rd.get('confidence'),
                         rd.get('cardinality') or 'unknown', rd.get('name'), rd.get('evidence') or (),
                         rd.get('seen') or ())
        return m


# ---------------------------------------------- bridge to the ER script code ----

def to_er(model, tables=None, relationships=None, flags=False):
    """Build the ER scripts' Model from (part of) a SchemaModel.

    Used to reuse pk_like / guess_one / finalize and the two diagram renderers.
    Returns (er_model, {Table: er Table}).
    """
    em, emap = er.Model(), {}
    for t in (model.tables if tables is None else tables):
        et = er.Table(([t.schema] if t.schema else []) + [t.name])
        et.aliases = list(t.aliases)
        et.from_ddl = t.complete
        et.ddl_pk = any(c.pk for c in t.columns)
        for i, c in enumerate(t.columns):
            ec = et.col(c.name)
            ec.type, ec.pk, ec.fk, ec.nullable = c.type, c.pk, c.fk, c.nullable
            ec.ddl_order = i if t.complete else None
            if flags:
                ec.show_pk, ec.show_fk = c.pk or c.pk_inferred, c.fk or c.fk_inferred
        em.tables.append(et)
        em.by_key[t.key] = et
        emap[t] = et
    for r in (model.relationships if relationships is None else relationships):
        if r.from_table not in emap or r.to_table not in emap:
            continue
        for fc, tc in zip(r.from_columns, r.to_columns):
            x, y = (emap[r.from_table], fc.upper()), (emap[r.to_table], tc.upper())
            emap[r.from_table].col(fc)
            emap[r.to_table].col(tc)
            em.relate(x, y, 'fk' if r.source == DECLARED else 'join')
            rel = em.rels.get(frozenset((x, y)))
            if rel is None:
                continue
            if r.source == DECLARED:
                rel.declared, rel.one = True, y
            elif r.cardinality != 'unknown' and not rel.declared:
                rel.one = y
            if r.source == VALUES and not rel.declared:
                rel.by_values = True                  # drawn dotted by the two renderers
            if r.cardinality == '1-1' and rel.one == y:
                rel.one_to_one = True                 # drawn 1 - 1 by the two renderers, as the graph shows it
    return em, emap


# ------------------------------------------------------------- inference ----

def type_family(type_name):
    u = (type_name or '').upper()
    if not u:
        return None
    if re.search(r'\b(U?(TINY|SMALL|MEDIUM|BIG|HUGE)?INT(EGER)?\d*|NUMBER|NUMERIC|DEC(IMAL)?|DOUBLE|FLOAT\d*'
                 r'|REAL|(SMALL)?MONEY|(BIG|SMALL)?SERIAL|LONG)\b', u):
        return 'number'
    if re.search(r'CHAR|TEXT|STRING|CLOB|UUID|UNIQUEIDENTIFIER', u):
        return 'text'
    if re.search(r'DATE|TIME', u):
        return 'date'
    return None


def resolve_directions(model):
    """Give "matched by value" relationships a one-side when a key makes it clear (guess_one)."""
    em, emap = to_er(model)
    for r in model.relationships:
        if r.source in (DECLARED, VALUES) or r.cardinality != 'unknown' or len(r.from_columns) != 1:
            continue                                  # (a value link's direction was measured: child -> parent)
        a = (emap[r.from_table], r.from_columns[0].upper())
        b = (emap[r.to_table], r.to_columns[0].upper())
        one = guess_one(a, b)
        if one is None:
            continue
        if one == a:
            r.from_table, r.from_columns, r.to_table, r.to_columns = (
                r.to_table, r.to_columns, r.from_table, r.from_columns)
        r.cardinality = '1-N'


def is_key(table, etable, name):
    """True when a column is a key BY ITSELF - what another column may point at.

    A column that a constraint or a unique index makes unique; the primary key when it is one
    column. One column of a key of several columns is no key: two rows may share it. Only where
    nothing is declared (a file, a table known from queries) does the name decide (pk_like).
    """
    column = table.column(name)
    if column is not None and column.unique:
        return True
    declared = [c for c in table.columns if c.pk]
    if declared:
        return len(declared) == 1 and column is declared[0]
    return pk_like(etable, name.upper())


def one_side(a, b, emap):
    """Which of two columns ((table, COLUMN) each) is the "one" side of a link between them - `a`, `b` or None.
    guess_one() of the ER scripts with is_key() for its question "is this a key?": a link between two
    columns that are both not unique is never made, and neither is one whose direction nothing tells."""
    pa, pb = is_key(a[0], emap[a[0]], a[1]), is_key(b[0], emap[b[0]], b[1])
    if pa != pb:
        return a if pa else b
    if pa and a[0] is not b[0]:
        na, nb = a[0].name.upper(), b[0].name.upper()
        if nb.startswith(na) and nb != na:            # R_OE_BPI  <-  R_OE_BPI_SUSPEND_REASON
            return a
        if na.startswith(nb) and na != nb:
            return b
    return None


def infer_by_names(model, evidence='column name matches the key of this table'):
    """Add relationships suggested by column names, e.g. orders.customer_id -> customers.id.

    A column points at a table when its name - or the tail of it - is the name of that table's
    key: its one-column primary key, a column a UNIQUE constraint makes unique (DEALER_CODE in
    the table of subscriptions and in the table of dealers), or <table>_id. The target is unique
    by itself (is_key), so a link between two columns that are both not unique is never made: two
    tables that each have an EFFECTIVE_FROM date are not related by it. A column that already
    points somewhere (declared key, or a join with a clear direction) is left alone. Returns the
    relationships it added.
    """
    em, emap = to_er(model)
    owners = _key_owners(model, emap)
    taken = {(r.from_table, c.upper()) for r in model.relationships
             if r.source == DECLARED or r.cardinality != 'unknown' for c in r.from_columns}
    added = []
    for t in model.tables:
        for col in list(t.columns):
            key = col.name.upper()
            if key in GENERIC_KEYS or (t, key) in taken:
                continue
            parts = key.split('_')
            # CUSTOMER_ID, then the tail of BILLING_CUSTOMER_ID; a one-word name (CUSTOMERID) is tried whole
            for i in range(max(1, len(parts) - 1)):
                tail = '_'.join(parts[i:])
                found = _name_target(owners, emap, t, col, tail)
                if found is None:
                    continue
                target, target_col = found
                if target is None:                    # ambiguous: do not guess
                    break
                confidence = NAME_CONFIDENCE[i == 0]
                pointed = target.column(target_col)
                fa, fb = type_family(col.type), type_family(pointed.type)
                if fa and fb:
                    confidence += 0.05 if fa == fb else -0.25
                why = evidence if pointed.pk or not pointed.unique else 'column name matches a unique column of this table'
                r = model.relate(t, [col.name], target, [target_col], NAMES, round(confidence, 2), '1-N',
                                 evidence=[why])
                if r is not None and r.source == NAMES:
                    added.append(r)
                break
    return added


def _key_owners(model, emap):
    """Index for speed: key-like name -> tables that might own it. pk_like() still decides.

    Without it every column would be tried against every table, which is too
    slow for a schema with thousands of tables.
    """
    by_stem, by_key = {}, {}
    for t in model.tables:
        for c in t.columns:                           # unique by itself: a key, whatever it is called
            if c.unique and not c.pk:
                by_key.setdefault(c.name.upper(), []).append(t)
        if emap[t].ddl_pk:                            # a declared key: only that column is the key
            key = [c for c in t.columns if c.pk]
            if len(key) != 1:                         # a multi-column key cannot be pointed at by one column
                continue
            by_key.setdefault(key[0].name.upper(), []).append(t)
            # a single declared key also answers to <table>_id, whatever the key itself is called
        name = t.name.upper()
        stems = set()
        for s in (name, name.rsplit('_', 1)[-1]):     # every stem pk_like could build, and a few more
            stems |= {s, s[:-1], s[:-2], s[:-3] + 'Y'}
        for stem in stems:
            by_stem.setdefault(stem, []).append(t)

    def owners(tail):
        found = list(by_key.get(tail, ()))
        for ending in ('_ID', 'ID', '_KEY'):
            if tail.endswith(ending):
                found += by_stem.get(tail[:-len(ending)], ())
        return list(dict.fromkeys(found))
    return owners


def _personal(column, table, other_column, other):
    """True when one of two columns holds personal values by its name (an e-mail address, a phone or service
    number, a person's name ...). Such a column is something a person HAS: a table of customers and a table
    of employees both have an EMAIL, and a unique constraint on one of them does not make the other point
    at it. A column of that kind is only linked when a key is declared - or by its values, when asked."""
    from .profiling.personal import personal_by_name      # (imported here: the profiling package imports this module)
    return bool(personal_by_name(column, table.name) or personal_by_name(other_column, other.name))


def _name_target(owners, emap, table, col, tail):
    """The table (and its key column) that column `col` of `table` points at, judging by `tail`.

    None = no table matches; (None, None) = more than one matches equally well.
    """
    key = col.name.upper()
    if tail in GENERIC_KEYS:
        return None
    # One column of a multi-column key is not a key by itself (is_key), so it can point elsewhere.
    if tail == key and is_key(table, emap[table], key):
        return None                                   # this is the table's own key, not a pointer
    options = []
    for other in owners(tail):
        if other is table and tail == key:
            continue
        if is_key(other, emap[other], tail):
            if other.column(tail) is not None:
                if not other.column(tail).pk and _personal(col.name, table, tail, other):
                    continue                          # two tables with an EMAIL, one of them unique: not a link
                target_col, exact = other.column(tail).name, 0
            else:
                generic = next((other.column(g).name for g in GENERIC_KEYS if other.column(g) is not None), None)
                if generic is None:
                    continue
                target_col, exact = generic, 1
        else:
            # CUSTOMER_ID -> customers, whose declared single-column key has another name (id, cust_no ...).
            # Not when customers has its own non-key CUSTOMER_ID column: then it is unclear which is meant.
            declared = [c for c in other.columns if c.pk]
            if len(declared) != 1 or other.column(tail) is not None or not names_table(emap[other], tail):
                continue
            target_col, exact = declared[0].name, 1
        a, b = (table, key), (other, target_col.upper())
        if a == b:
            continue
        if one_side(a, b, emap) != b:                 # the other column is not the key of the two, or both are
            continue
        # ACCOUNT_ID fits ACCOUNTS by its whole name, and CUSTOMER_ACCOUNTS only by its last word.
        name = other.name.upper()
        whole_name = 0 if tail.startswith(name[:max(3, len(name) - 3)]) else 1
        # A key that is itself a declared foreign key belongs to an extension of another table
        # (PROFILES.PARENT_ID -> PARENTS.PARENT_ID): the table it points at is the better target.
        borrowed = 1 if other.column(target_col).fk else 0
        options.append((exact, borrowed, whole_name, other, target_col))
    if not options:
        return None
    options.sort(key=lambda o: o[:3])
    if len(options) > 1 and options[0][:3] == options[1][:3]:
        return None, None                             # two tables fit equally well
    return options[0][3], options[0][4]


def set_flags(model):
    """Cardinality of declared keys, confidence of joins, and the inferred PK/FK badges."""
    for r in model.relationships:
        if r.source == DECLARED:
            pk = {c.name.upper() for c in r.from_table.columns if c.pk}
            r.cardinality = '1-1' if pk and pk == {c.upper() for c in r.from_columns} else '1-N'
            r.confidence = None
        elif r.source == JOINS:
            r.confidence = JOIN_CONFIDENCE[r.cardinality != 'unknown']
    em, emap = to_er(model)
    finalize(em)                                      # sets show_pk / show_fk exactly like the scripts
    for t, et in emap.items():
        for c in t.columns:
            ec = et.columns[c.name.upper()]
            c.pk_inferred = bool(ec.show_pk and not c.pk)
            c.fk_inferred = bool(ec.show_fk and not c.fk)


def finalize_model(model, names=True):
    """Run the inference steps on a model (an adapter's own, or a merged workspace)."""
    resolve_directions(model)
    if names:
        infer_by_names(model)
    set_flags(model)
    return model
