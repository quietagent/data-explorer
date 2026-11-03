"""Parse-time model: tables, columns and relationships found in SQL.

Copied from tools/sql_to_drawio.py and tools/sql_to_erd.py (identical in both).
App additions: Column.nullable, Column.unique, Rel.by_values, Rel.one_to_one and Model.declared_fks.
"""

from collections import OrderedDict


class Column:
    def __init__(self, name):
        self.name = name
        self.type = None
        self.pk = False          # declared in DDL
        self.fk = False          # declared in DDL
        self.ddl_order = None
        self.show_pk = False     # what the diagram shows (declared or inferred)
        self.show_fk = False
        self.nullable = None     # app addition: False = NOT NULL / PK, True = DDL column, None = unknown
        self.unique = False      # app addition: unique by itself (UNIQUE on the column, UNIQUE (col), a unique index)


class Table:
    def __init__(self, parts):
        self.name = parts[-1]
        self.schema = '.'.join(parts[:-1]) or None
        self.aliases = []
        self.columns = OrderedDict()
        self.from_ddl = False
        self.ddl_pk = False

    def col(self, name):
        key = name.upper()
        c = self.columns.get(key)
        if c is None:
            c = self.columns[key] = Column(name)
        return c

    def add_alias(self, alias):
        if alias.upper() != self.name.upper() and alias.upper() not in (a.upper() for a in self.aliases):
            self.aliases.append(alias)


class Rel:
    def __init__(self, a, b):
        self.a, self.b = a, b    # (Table, COLUMN_KEY)
        self.kinds = set()
        self.one = None          # endpoint on the "one" side, None = unknown
        self.declared = False
        self.by_values = False   # app addition: a link accepted from value overlap (drawn dotted)
        self.one_to_one = False  # app addition: one row on each side for a key (drawn 1 - 1, not 1 - N)


class Model:
    def __init__(self):
        self.tables = []
        self.by_key = {}
        self.rels = OrderedDict()
        self.pending_fks = []
        self.declared_fks = []   # app addition: (table, cols, ref table, ref cols), filled by finalize()

    def table(self, parts):
        key = '.'.join(p.upper() for p in parts)
        t = self.by_key.get(key)
        if t is not None:
            return t
        short = parts[-1].upper()
        if len(parts) == 1:                       # "ACCOUNT" after "RDB_OWNER.ACCOUNT"
            same = [x for x in self.tables if x.name.upper() == short]
            if len(same) == 1:
                self.by_key[key] = same[0]
                return same[0]
        else:                                     # "RDB_OWNER.ACCOUNT" after "ACCOUNT"
            t = self.by_key.get(short)
            if t is not None and t.schema is None:
                t.schema = '.'.join(parts[:-1])
                self.by_key[key] = t
                return t
        t = Table(parts)
        self.tables.append(t)
        self.by_key[key] = t
        return t

    def relate(self, x, y, kind, one=None):
        if x == y:
            return
        k = frozenset((x, y))
        r = self.rels.get(k)
        if r is None:
            r = self.rels[k] = Rel(x, y)
        r.kinds.add(kind)
        if one is not None:
            r.one, r.declared = one, True
