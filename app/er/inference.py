"""Key and direction inference: pk_like, guess_one, finalize.

Copied from tools/sql_to_drawio.py and tools/sql_to_erd.py (identical in both).
App additions: finalize() also records each resolved foreign key in
model.declared_fks so multi-column keys stay together, pk_like() also
singularises "-es" plurals (BRANCHES -> BRANCH_ID), and its name test is
available on its own as names_table() (same result, used by app.model to link
<table>_id columns to a declared primary key whatever that key is called).
"""

def names_table(tbl, key):
    """True when `key` is <table>_ID / <table>ID / <table>_KEY for this table's name (or its singular)."""
    stems = set()
    for s in (tbl.name.upper(), tbl.name.upper().rsplit('_', 1)[-1]):
        stems.add(s)
        if s.endswith('IES'):
            stems.add(s[:-3] + 'Y')
        elif s.endswith('S') and not s.endswith('SS'):
            stems.add(s[:-1])
            if s.endswith('ES'):                  # app addition: BRANCHES -> BRANCH, ADDRESSES -> ADDRESS
                stems.add(s[:-2])
    return any(key in (s + '_ID', s + 'ID', s + '_KEY') for s in stems)


def pk_like(tbl, key):
    c = tbl.columns.get(key)
    if tbl.ddl_pk:
        return bool(c and c.pk)
    if c is not None and c.pk:
        return True
    if key in ('ID', 'OBJECT_ID', 'OBJID', 'OID', 'UUID', 'GUID', 'ROW_ID'):
        return True
    return names_table(tbl, key)


def guess_one(a, b):
    pa, pb = pk_like(*a), pk_like(*b)
    if pa and not pb:
        return a
    if pb and not pa:
        return b
    if pa and pb and a[0] is not b[0]:
        na, nb = a[0].name.upper(), b[0].name.upper()
        if nb.startswith(na) and nb != na:        # R_OE_BPI  <-  R_OE_BPI_SUSPEND_REASON
            return a
        if na.startswith(nb) and na != nb:
            return b
    return None


def finalize(model):
    for tbl, cols, rt, refcols in model.pending_fks:
        if not refcols:                           # REFERENCES t  (no column list)
            pk = [c.name for c in rt.columns.values() if c.pk]
            guess = [c.name for k, c in rt.columns.items() if pk_like(rt, k)]
            refcols = pk if len(pk) == len(cols) else guess if len(guess) == len(cols) else cols
        model.declared_fks.append((tbl, list(cols), rt, list(refcols)[:len(cols)]))   # app addition
        for c, rc in zip(cols, refcols):
            rt.col(rc)
            model.relate((tbl, c.upper()), (rt, rc.upper()), 'fk', one=(rt, rc.upper()))
    many_side = set()
    for r in model.rels.values():
        if r.one is None:
            r.one = guess_one(r.a, r.b)
        if r.one is not None:
            many_side.add(r.b if r.one == r.a else r.a)
    for t in model.tables:
        for key, c in t.columns.items():
            if t.ddl_pk or c.pk:
                c.show_pk = c.pk
            else:
                c.show_pk = pk_like(t, key) and (t, key) not in many_side
            c.show_fk = c.fk or (t, key) in many_side
