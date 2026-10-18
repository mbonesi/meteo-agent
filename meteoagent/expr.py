"""Safe boolean expression evaluator.

Rule conditions come from a configuration file. Using eval() would mean
executing arbitrary code; instead the expression is compiled to an AST and
only the nodes present in a whitelist are accepted. The result is
deterministic and errors surface at parse time, not at runtime at 3am.

Supported grammar:
    and / or / not
    < <= > >= == != in / not in
    + - * / % (unary -)
    numeric and string literals, lists/tuples
    identifiers = weather variables of the timestep
    calls to whitelisted functions (see FUNCTIONS)
"""

from __future__ import annotations

import ast
import math
from typing import Any

# 16-point compass sectors. Index i covers [i*22.5 - 11.25, i*22.5 + 11.25).
SECTORS = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)
_SECTOR_INDEX = {name: i for i, name in enumerate(SECTORS)}


class ExprError(Exception):
    """Malformed or disallowed expression."""


def sector(degrees: float) -> str:
    """Converts degrees (0 = North, clockwise) to the 16-point compass sector."""
    if degrees is None or not math.isfinite(degrees):
        raise ExprError("sector(): invalid direction")
    return SECTORS[int(round((degrees % 360) / 22.5)) % 16]


def dir_in(degrees: float, names: list[str] | tuple[str, ...]) -> bool:
    """True if the direction falls in one of the given sectors.

    >>> dir_in(100.0, ["E", "ESE"])
    True
    """
    if isinstance(names, str):
        names = [names]
    wanted = set()
    for name in names:
        key = str(name).strip().upper().replace("O", "W")  # accepts the Italian notation (O = Ovest/West)
        if key not in _SECTOR_INDEX:
            raise ExprError(f"unknown sector: {name!r} (valid: {', '.join(SECTORS)})")
        wanted.add(key)
    return sector(degrees) in wanted


def dir_between(degrees: float, start: float, end: float) -> bool:
    """True if the direction is in the start->end arc, traversed clockwise.

    Handles wrap-around at zero: dir_between(350, 340, 20) is True.
    """
    d = degrees % 360
    start %= 360
    end %= 360
    if start <= end:
        return start <= d <= end
    return d >= start or d <= end


FUNCTIONS: dict[str, Any] = {
    "dir_in": dir_in,
    "dir_between": dir_between,
    "sector": sector,
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
}

_BIN_OPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.Mod: lambda a, b: a % b,
}

_CMP_OPS = {
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}


class Expression:
    """Compiled, reusable expression."""

    __slots__ = ("source", "_tree", "names")

    def __init__(self, source: str):
        self.source = source.strip()
        if not self.source:
            raise ExprError("empty expression")
        try:
            self._tree = ast.parse(self.source, mode="eval")
        except SyntaxError as exc:
            raise ExprError(f"syntax error: {exc.msg}") from exc
        _check(self._tree.body)
        self.names = frozenset(_collect_names(self._tree.body))

    def evaluate(self, variables: dict[str, Any]) -> bool:
        """Evaluates the expression. Raises ExprError if a variable is missing."""
        missing = self.names - set(variables)
        if missing:
            raise ExprError(f"undefined variables: {sorted(missing)}")
        return bool(_eval(self._tree.body, variables))

    def __repr__(self) -> str:
        return f"Expression({self.source!r})"


def _check(node: ast.AST) -> None:
    """Recursively rejects any node outside the whitelist."""
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float, str, bool)):
            raise ExprError(f"constant not allowed: {node.value!r}")
        return
    if isinstance(node, ast.Name):
        if isinstance(node.ctx, ast.Store):
            raise ExprError("assignments are not allowed")
        return
    if isinstance(node, (ast.List, ast.Tuple)):
        for elt in node.elts:
            _check(elt)
        return
    if isinstance(node, ast.BoolOp):
        for value in node.values:
            _check(value)
        return
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, (ast.Not, ast.USub, ast.UAdd)):
            raise ExprError(f"unary operator not allowed: {type(node.op).__name__}")
        _check(node.operand)
        return
    if isinstance(node, ast.BinOp):
        if type(node.op) not in _BIN_OPS:
            raise ExprError(f"operator not allowed: {type(node.op).__name__}")
        _check(node.left)
        _check(node.right)
        return
    if isinstance(node, ast.Compare):
        for op in node.ops:
            if type(op) not in _CMP_OPS:
                raise ExprError(f"comparison not allowed: {type(op).__name__}")
        _check(node.left)
        for comparator in node.comparators:
            _check(comparator)
        return
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ExprError("only calls to named functions are allowed")
        if node.func.id not in FUNCTIONS:
            raise ExprError(
                f"unknown function: {node.func.id}() "
                f"(available: {', '.join(sorted(FUNCTIONS))})"
            )
        if node.keywords:
            raise ExprError("keyword arguments are not allowed")
        for arg in node.args:
            _check(arg)
        return
    raise ExprError(f"construct not allowed: {type(node).__name__}")


def _collect_names(node: ast.AST) -> set[str]:
    """Variable names referenced, excluding function names."""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
            names.discard(child.func.id)
    return names - set(FUNCTIONS)


def _eval(node: ast.AST, env: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return env[node.id]
    if isinstance(node, ast.List):
        return [_eval(e, env) for e in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval(e, env) for e in node.elts)
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            # Short-circuit, as in Python.
            for value in node.values:
                if not _eval(value, env):
                    return False
            return True
        for value in node.values:
            if _eval(value, env):
                return True
        return False
    if isinstance(node, ast.UnaryOp):
        operand = _eval(node.operand, env)
        if isinstance(node.op, ast.Not):
            return not operand
        if isinstance(node.op, ast.USub):
            return -operand
        return +operand
    if isinstance(node, ast.BinOp):
        return _BIN_OPS[type(node.op)](_eval(node.left, env), _eval(node.right, env))
    if isinstance(node, ast.Compare):
        left = _eval(node.left, env)
        for op, comparator_node in zip(node.ops, node.comparators):
            right = _eval(comparator_node, env)
            if not _CMP_OPS[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Call):
        return FUNCTIONS[node.func.id](*[_eval(a, env) for a in node.args])
    raise ExprError(f"construct not allowed: {type(node).__name__}")
