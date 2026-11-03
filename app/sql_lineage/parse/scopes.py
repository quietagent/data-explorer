"""Scopes and sources: which tables a column can come from.

A Scope is one SELECT (or one UPDATE / DELETE / MERGE, or one UNION). Its
sources are what its FROM, JOIN, USING ... clauses name, by alias:

    table     a base table (key is the folded table key)
    derived   a CTE or a subquery in FROM; .scope is the Scope that defines it
    dummy     Oracle/MySQL DUAL (settings.DUMMY_TABLES): read, but not a table
    unknown   a table function, VALUES list or anything else not followed

A nested query (scalar subquery, IN, EXISTS) gets its own Scope whose parent
is the enclosing scope, so a column can be found in an outer scope.

resolve() finds the source of a column. trace() follows a column of a derived
source back to base table columns, only where that is certain: through plain
column references, aliases and *, and through every branch of a UNION.
Anything else (an expression, an aggregate over two columns ...) gives None.
"""

from dataclasses import dataclass, field

from sqlglot import exp

from sql_lineage.parse.names import fold_identifier, join_parts

YES, NO, MAYBE = "yes", "no", "maybe"
MAX_TRACE_DEPTH = 20


@dataclass
class CteDef:
    """A WITH entry. .scope is filled once its query is read (None while it is being read)."""
    name: str
    scope: "Scope | None" = None
    columns: list | None = None     # WITH x (a, b) AS ... column list


@dataclass(eq=False)
class Source:
    alias: str
    kind: str                       # table, derived, dummy, unknown
    key: str = ""                   # table key, for kind table
    name: str = ""                  # CTE or subquery name, for kind derived
    is_cte: bool = False
    cte: CteDef | None = None       # for a CTE reference
    subquery_scope: "Scope | None" = None
    written: bool = False           # the target of an UPDATE / DELETE / MERGE
    position: int = 0               # order in its scope
    line: int | None = None         # file line where it is named

    @property
    def scope(self):
        if self.cte is not None:
            return self.cte.scope
        return self.subquery_scope


@dataclass(eq=False)
class Output:
    name: str | None                # folded output name, None when unnamed
    expression: object              # the sqlglot expression (inside the alias)
    renamed: bool = False           # written as "expression AS name"


@dataclass(eq=False)
class Scope:
    kind: str                       # select, union, dml, other
    parent: "Scope | None" = None
    sources: list = field(default_factory=list)       # Source, in written order
    join_items: list = field(default_factory=list)    # (Source, sqlglot Join or None)
    outputs: list = field(default_factory=list)       # Output
    star_all: bool = False          # SELECT *
    star_aliases: list = field(default_factory=list)  # SELECT t.*
    branches: list = field(default_factory=list)      # for a union: its select scopes
    cte_columns: list | None = None
    unsure: bool = False            # a join condition here had a column whose table is not known
    link_kind: str = ""             # a nested query: how it is used (in, not_in, exists, not_exists, scalar)

    def add(self, source):
        source.position = len(self.sources)
        self.sources.append(source)

    def by_alias(self, alias):
        for source in self.sources:
            if source.alias == alias:
                return source
        return None

    def output_names(self):
        if self.kind == "union":
            return self.branches[0].output_names() if self.branches else []
        names = [o.name for o in self.outputs]
        if self.cte_columns:
            names = list(self.cte_columns) + names[len(self.cte_columns):]
        return names

    def alias_names(self):
        """Names given with AS in the select list (ORDER BY / GROUP BY may use them)."""
        return {o.name for o in self.outputs if o.renamed and o.name}


class Resolver:
    """Answers column questions with the dialect's folding and the optional schema."""

    def __init__(self, dialect, schema):
        self.dialect = dialect
        self.schema = schema

    # -- does a source have a column ------------------------------------------

    def has_column(self, source, name, depth=0):
        """YES, NO or MAYBE: can column `name` come from `source`?"""
        if depth > MAX_TRACE_DEPTH:
            return MAYBE
        if source.kind == "table":
            if self.schema is None:
                return MAYBE
            columns = self.schema.columns_of(source.key)
            if columns is None:
                return MAYBE
            return YES if name in columns else NO
        if source.kind == "derived":
            scope = source.scope
            if scope is None:
                return MAYBE
            return self.scope_has_output(scope, name, depth + 1)
        return MAYBE

    def scope_has_output(self, scope, name, depth=0):
        if scope.kind == "union":
            return self.scope_has_output(scope.branches[0], name, depth + 1) if scope.branches else MAYBE
        names = scope.output_names()
        if name in names:
            return YES
        if scope.star_all:
            answers = [self.has_column(s, name, depth + 1) for s in scope.sources]
        elif scope.star_aliases:
            answers = [self.has_column(s, name, depth + 1) for s in scope.sources if s.alias in scope.star_aliases]
        else:
            return NO
        if YES in answers:
            return YES
        if MAYBE in answers:
            return MAYBE
        return NO

    # -- which source a column comes from --------------------------------------

    def resolve(self, scope, column):
        """The Source of an sqlglot Column, or a string saying why it is not known."""
        name = fold_identifier(column.this, self.dialect)
        qualifier = fold_identifier(column.args.get("table"), self.dialect)
        db = fold_identifier(column.args.get("db"), self.dialect)
        if qualifier:
            current = scope
            while current is not None:
                if db:
                    key = join_parts("", db, qualifier, self.dialect)
                    for source in current.sources:
                        if source.kind == "table" and source.key == key:
                            return source
                else:
                    found = current.by_alias(qualifier)
                    if found is not None:
                        return found
                current = current.parent
            return f"'{qualifier}' is not a table or alias of this statement"

        current = scope
        while current is not None:
            answers = [(s, self.has_column(s, name)) for s in current.sources]
            yes = [s for s, a in answers if a == YES]
            maybe = [s for s, a in answers if a == MAYBE]
            if len(yes) == 1 and not maybe:
                return yes[0]
            if len(yes) > 1:
                return "found in " + " and ".join(_describe(s) for s in yes)
            if yes and maybe:
                return "found in " + _describe(yes[0]) + ", and " + _describe(maybe[0]) + " has no known columns"
            if len(maybe) == 1:
                return maybe[0]
            if len(maybe) > 1:
                return "no table given and " + str(len(maybe)) + " tables could hold it"
            current = current.parent
        return "not in any table of this statement"

    # -- following a derived column back to base tables -------------------------

    def trace(self, source, name, depth=0):
        """[(table key, column)] that `source`.`name` certainly comes from, or None."""
        if depth > MAX_TRACE_DEPTH:
            return None
        if source.kind == "table":
            return [(source.key, name)]
        if source.kind != "derived" or source.scope is None:
            return None
        return self.trace_output(source.scope, name, depth + 1)

    def trace_output(self, scope, name, depth):
        if scope.kind == "union":
            names = scope.output_names()
            if name not in names:
                return None
            position = names.index(name)
            found = []
            for branch in scope.branches:
                traced = self._trace_position(branch, position, depth + 1)
                if traced is None:
                    return None
                found += [t for t in traced if t not in found]
            return found
        names = scope.output_names()
        if name in names:
            return self._trace_expression(scope, scope.outputs[names.index(name)].expression, depth + 1)
        if not (scope.star_all or scope.star_aliases):
            return None
        candidates = scope.sources if scope.star_all else [
            s for s in scope.sources if s.alias in scope.star_aliases]
        answers = [(s, self.has_column(s, name, depth + 1)) for s in candidates]
        yes = [s for s, a in answers if a == YES]
        maybe = [s for s, a in answers if a == MAYBE]
        if len(candidates) == 1 and (yes or maybe):
            return self.trace(candidates[0], name, depth + 1)
        if len(yes) == 1 and not maybe:
            return self.trace(yes[0], name, depth + 1)
        return None

    def _trace_position(self, scope, position, depth):
        """Trace the output at `position` (UNION branches line up by position)."""
        if scope.kind == "union":
            names = scope.output_names()
            return self.trace_output(scope, names[position], depth) if position < len(names) else None
        if position >= len(scope.outputs):
            return None
        if any(is_star(o.expression) for o in scope.outputs[:position + 1]):
            return None     # after a *, positions are not known
        return self._trace_expression(scope, scope.outputs[position].expression, depth + 1)

    def _trace_expression(self, scope, expression, depth):
        """A plain column reference is followed on; anything else is not certain."""
        if not isinstance(expression, exp.Column) or isinstance(expression.this, exp.Star):
            return None
        source = self.resolve(scope, expression)
        if isinstance(source, str):
            return None
        return self.trace(source, fold_identifier(expression.this, self.dialect), depth + 1)


def is_star(expression):
    """True for * and t.*"""
    return isinstance(expression, exp.Star) or (
        isinstance(expression, exp.Column) and isinstance(expression.this, exp.Star))


def _describe(source):
    if source.kind == "table":
        return source.key
    return source.name or source.alias
