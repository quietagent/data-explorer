"""Works out the text of a Python string expression by reading the code only.

value_of(node) returns one of:
    StrValue    the text is known; run-time values (f-string fields, .format()
                and % fields, non-string operands of +) are Hole objects in it
    RunTime     the text is only known when the script runs; .reason says why
    None        the expression is not a string at all (a number, bytes ...)
    HANDLED_ELSEWHERE   the expression is itself a text(...) / execute(...) call,
                        which is read where it stands

Variables are followed back to their assignment when that is certain: the
nearest assignment before the use in straight-line code, or a single
module-level assignment. An assignment inside if/for/while/try, an augmented
assignment (+=) or a function parameter make the value RunTime.
"""

import ast
import re
import textwrap
from dataclasses import dataclass, field

from sql_lineage import settings

HANDLED_ELSEWHERE = object()

_PERCENT_FIELD = re.compile(r"%(?:\(([^)]*)\))?[-#0 +]*(?:\d+|\*)?(?:\.(?:\d+|\*))?[a-zA-Z%]")
_FORMAT_FIELD = re.compile(r"\{\{|\}\}|\{([^{}]*)\}")


@dataclass
class Hole:
    """A value only known at run time, standing at this place in the text."""
    label: str


@dataclass
class StrValue:
    parts: list                         # str and Hole, in order
    line_base: int | None               # file line of the first character, when certain
    literals: list = field(default_factory=list)   # the ast nodes the text came from

    def text_only(self):
        """The text with every hole left out (used to test the first word)."""
        return "".join(p for p in self.parts if isinstance(p, str))


@dataclass
class RunTime:
    reason: str


class PythonStrings:
    """Reads string expressions of one parsed Python file."""

    def __init__(self, tree, source):
        self.tree = tree
        self.source = source
        self.parents = {}
        self.blocks = {}    # id(statement) -> (list it sits in, index, owner node)
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                self.parents[id(child)] = node
            for name in ("body", "orelse", "finalbody"):
                block = getattr(node, name, None)
                if isinstance(block, list):
                    for index, child in enumerate(block):
                        if isinstance(child, ast.stmt):
                            self.blocks[id(child)] = (block, index, node)

    def parent(self, node):
        return self.parents.get(id(node))

    # -- evaluation ---------------------------------------------------------

    def value_of(self, node, hops=0):
        """The text of expression `node` (see the module docstring for the result types)."""
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                return StrValue([node.value], self._literal_line_base(node, node.value), [node])
            return None
        if isinstance(node, ast.JoinedStr):
            return self._joined_str(node)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self._concat(node, hops)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            if _is_number(node.left):
                return None
            if not isinstance(node.left, (ast.Constant, ast.JoinedStr, ast.BinOp)):
                return RunTime(f"{settings.REASON_SQL_AT_RUN_TIME} (% formatting of a variable)")
            left = self.value_of(node.left, hops)
            if isinstance(left, StrValue):
                return _percent_format(left)
            return left
        if isinstance(node, ast.Call):
            return self._call(node, hops)
        if isinstance(node, ast.Name):
            return self._name(node, hops)
        if isinstance(node, (ast.Subscript, ast.Attribute)):
            return RunTime(f"{settings.REASON_SQL_AT_RUN_TIME} (taken from a container or attribute)")
        if isinstance(node, ast.IfExp):
            return RunTime(f"{settings.REASON_SQL_AT_RUN_TIME} (chosen by a condition)")
        if isinstance(node, (ast.List, ast.Tuple, ast.ListComp, ast.GeneratorExp)):
            return RunTime(f"{settings.REASON_SQL_AT_RUN_TIME} (built from a list)")
        return RunTime(f"{settings.REASON_SQL_AT_RUN_TIME} ({type(node).__name__} expression)")

    def _literal_line_base(self, node, value):
        """Line of the literal's first character, if its lines match the file's lines."""
        segment = ast.get_source_segment(self.source, node)
        if segment is None or segment.count("\n") != value.count("\n"):
            return None
        return node.lineno

    def _joined_str(self, node):
        parts = []
        newlines = 0
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(value.value)
                newlines += value.value.count("\n")
            else:
                parts.append(Hole(_label(value.value)))
        segment = ast.get_source_segment(self.source, node)
        line_base = node.lineno if segment is not None and segment.count("\n") == newlines else None
        return StrValue(parts, line_base, [node])

    def _concat(self, node, hops):
        if _is_number(node.left) and _is_number(node.right):
            return None
        left = self._operand(node.left, hops)
        right = self._operand(node.right, hops)
        if isinstance(left, Hole) and isinstance(right, Hole):
            return RunTime(settings.REASON_SQL_AT_RUN_TIME)
        left_value = left if isinstance(left, StrValue) else StrValue([left], None)
        right_value = right if isinstance(right, StrValue) else StrValue([right], None)
        line_base = None
        if left_value.line_base is not None and right_value.line_base is not None:
            expected = left_value.line_base + "".join(
                p for p in left_value.parts if isinstance(p, str)).count("\n")
            if right_value.line_base == expected:
                line_base = left_value.line_base
        return StrValue(left_value.parts + right_value.parts, line_base,
                        left_value.literals + right_value.literals)

    def _operand(self, node, hops):
        """One side of +: its StrValue, or a Hole when its value is only known at run time."""
        value = self.value_of(node, hops)
        if isinstance(value, StrValue):
            return value
        return Hole(_label(node))

    def _call(self, node, hops):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in settings.PYTHON_SQL_CALLS:
            return HANDLED_ELSEWHERE
        if isinstance(func, ast.Attribute) and name == "format":
            if isinstance(func.value, (ast.Constant, ast.JoinedStr, ast.BinOp)):
                inner = self.value_of(func.value, hops)
                return _brace_format(inner) if isinstance(inner, StrValue) else inner
            return RunTime(f"{settings.REASON_SQL_AT_RUN_TIME} (.format() on a variable)")
        if isinstance(func, ast.Attribute) and name in ("strip", "lstrip", "rstrip") and not node.args:
            inner = self.value_of(func.value, hops)
            return _strip(inner, name) if isinstance(inner, StrValue) else inner
        if name == "dedent" and len(node.args) == 1:
            inner = self.value_of(node.args[0], hops)
            return _dedent(inner) if isinstance(inner, StrValue) else inner
        if isinstance(func, ast.Attribute) and name == "join":
            return RunTime(f"{settings.REASON_SQL_AT_RUN_TIME} (str.join)")
        return RunTime(f"{settings.REASON_SQL_AT_RUN_TIME} (returned by {name or 'a call'}())")

    # -- variables ----------------------------------------------------------

    def _name(self, node, hops):
        if hops >= settings.PYTHON_MAX_VARIABLE_HOPS:
            return RunTime(f"{settings.REASON_SQL_AT_RUN_TIME} (too many variable hops)")
        statement = self.enclosing_statement(node)
        found = self._search_back(node.id, statement)
        if isinstance(found, RunTime):
            return found
        return self.value_of(found, hops + 1)

    def enclosing_statement(self, node):
        """The statement (ast.stmt listed in a block) that holds `node`."""
        while node is not None and id(node) not in self.blocks:
            node = self.parent(node)
        return node

    def _search_back(self, name, statement):
        """The expression last assigned to `name` before `statement`, or RunTime."""
        current = statement
        while current is not None:
            block, index, owner = self.blocks[id(current)]
            for earlier in reversed(block[:index]):
                found = self._assignment_in(earlier, name)
                if found is not None:
                    return found
            if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                arguments = owner.args
                params = [a.arg for a in arguments.posonlyargs + arguments.args + arguments.kwonlyargs]
                params += [a.arg for a in (arguments.vararg, arguments.kwarg) if a]
                if name in params:
                    return RunTime(f"{settings.REASON_SQL_AT_RUN_TIME} (passed in as parameter '{name}')")
                return self._module_level(name)
            if isinstance(owner, ast.ClassDef):
                return self._module_level(name)
            if isinstance(owner, ast.Module):
                return RunTime(f"{settings.REASON_SQL_AT_RUN_TIME} ('{name}' is not assigned before this line)")
            current = self.enclosing_statement(owner)
        return RunTime(f"{settings.REASON_SQL_AT_RUN_TIME} ('{name}' not found)")

    def _module_level(self, name):
        """The single module-level assignment of `name`, or RunTime."""
        found = [f for f in (self._assignment_in(s, name) for s in self.tree.body) if f is not None]
        if not found:
            return RunTime(f"{settings.REASON_SQL_AT_RUN_TIME} ('{name}' is not assigned in this file)")
        if len(found) > 1:
            return RunTime(f"{settings.REASON_SQL_AT_RUN_TIME} ('{name}' is assigned more than once)")
        return found[0]

    def _assignment_in(self, statement, name):
        """What `statement` does to `name`: an expression, a RunTime, or None (nothing)."""
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return statement.value
                if _binds(target, name):
                    return RunTime(f"{settings.REASON_SQL_AT_RUN_TIME} ('{name}' is unpacked)")
            return None
        if isinstance(statement, ast.AnnAssign):
            if isinstance(statement.target, ast.Name) and statement.target.id == name:
                return statement.value
            return None
        if isinstance(statement, ast.AugAssign):
            if isinstance(statement.target, ast.Name) and statement.target.id == name:
                return RunTime(f"{settings.REASON_SQL_AT_RUN_TIME} ('{name}' is extended with +=)")
            return None
        if isinstance(statement, (ast.With, ast.AsyncWith)):
            if any(item.optional_vars is not None and _binds(item.optional_vars, name)
                   for item in statement.items):
                return RunTime(f"{settings.REASON_SQL_AT_RUN_TIME} ('{name}' set by a with statement)")
            for earlier in reversed(statement.body):
                found = self._assignment_in(earlier, name)
                if found is not None:
                    return found
            return None
        if isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
            if _binds(statement, name):
                return RunTime(f"{settings.REASON_SQL_AT_RUN_TIME} ('{name}' is set in a loop)")
            return None
        if isinstance(statement, (ast.If, ast.Try, ast.Match)) or type(statement).__name__ == "TryStar":
            if _binds(statement, name):
                return RunTime(f"{settings.REASON_SQL_AT_RUN_TIME} ('{name}' is set in a branch)")
            return None
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if statement.name == name:
                return RunTime(f"'{name}' is a function or class, not a string")
            return None
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            for alias in statement.names:
                if (alias.asname or alias.name.split(".")[0]) == name:
                    return RunTime(f"{settings.REASON_SQL_AT_RUN_TIME} ('{name}' is imported)")
            return None
        if isinstance(statement, ast.Expr) and _binds(statement, name):
            return RunTime(f"{settings.REASON_SQL_AT_RUN_TIME} ('{name}' is set by :=)")
        return None


def _is_number(node):
    return isinstance(node, ast.Constant) and not isinstance(node.value, str)


def _binds(node, name):
    """True when `node` assigns `name` somewhere, not counting nested functions and classes."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, ast.Name) and current.id == name and isinstance(current.ctx, ast.Store):
            return True
        for child in ast.iter_child_nodes(current):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            stack.append(child)
    return False


def _label(node):
    """A short bind name for a run-time value: the variable or attribute it comes from."""
    if isinstance(node, ast.Name):
        text = node.id
    elif isinstance(node, ast.Attribute):
        text = f"{_label(node.value)}_{node.attr}"
    elif isinstance(node, ast.Subscript):
        text = _label(node.value)
    elif isinstance(node, ast.Call):
        text = _label(node.args[0]) if node.args else _label(node.func)
    else:
        text = "value"
    text = re.sub(r"\W", "_", text)
    return text if text[:1].isalpha() else f"p_{text}"


def _percent_format(value):
    """Apply `text % values`: each %s / %(name)s field becomes a Hole, %% becomes %."""
    parts = []
    for part in value.parts:
        if not isinstance(part, str):
            parts.append(part)
            continue
        position = 0
        for match in _PERCENT_FIELD.finditer(part):
            parts.append(part[position:match.start()])
            if match.group(0) == "%%":
                parts.append("%")
            else:
                parts.append(Hole(match.group(1) or "value"))
            position = match.end()
        parts.append(part[position:])
    return StrValue([p for p in parts if p != ""], value.line_base, value.literals)


def _brace_format(value):
    """Apply `text.format(...)`: each {} / {name} field becomes a Hole, {{ and }} become braces."""
    parts = []
    counter = 0
    for part in value.parts:
        if not isinstance(part, str):
            parts.append(part)
            continue
        position = 0
        for match in _FORMAT_FIELD.finditer(part):
            parts.append(part[position:match.start()])
            if match.group(0) in ("{{", "}}"):
                parts.append(match.group(0)[0])
            else:
                field_name = re.split(r"[.\[!:]", match.group(1))[0]
                if not field_name or field_name.isdigit():
                    field_name = f"p{counter}"
                counter += 1
                parts.append(Hole(field_name))
            position = match.end()
        parts.append(part[position:])
    return StrValue([p for p in parts if p != ""], value.line_base, value.literals)


def _strip(value, how):
    """Apply .strip() / .lstrip() / .rstrip() to the outer text pieces."""
    parts = list(value.parts)
    line_base = value.line_base
    if how in ("strip", "lstrip") and parts and isinstance(parts[0], str):
        stripped = parts[0].lstrip()
        if line_base is not None:
            line_base += parts[0][:len(parts[0]) - len(stripped)].count("\n")
        parts[0] = stripped
    if how in ("strip", "rstrip") and parts and isinstance(parts[-1], str):
        parts[-1] = parts[-1].rstrip()
    return StrValue(parts, line_base, value.literals)


def _dedent(value):
    """Apply textwrap.dedent() when the text has no holes; otherwise keep it as it is."""
    if all(isinstance(p, str) for p in value.parts):
        return StrValue([textwrap.dedent("".join(value.parts))], value.line_base, value.literals)
    return value
