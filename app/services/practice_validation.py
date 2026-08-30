from __future__ import annotations

import ast
import math
import operator
import re
from typing import Callable


_BINARY: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
}
_UNARY: dict[type[ast.unaryop], Callable[[float], float]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _latex_fractions(value: str) -> str:
    pattern = re.compile(r"\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}")
    previous = None
    while previous != value:
        previous = value
        value = pattern.sub(r"((\1)/(\2))", value)
    return value


def _normalize(value: str) -> str:
    value = _latex_fractions(value.strip())
    value = value.replace("\u00d7", "*").replace("\u00f7", "/").replace("^", "**")
    value = value.replace("\\left", "").replace("\\right", "").replace("$", "")
    return re.sub(r"\s+", "", value).strip("。；;，,")


def _eval_numeric(expression: str) -> float | None:
    expression = _normalize(expression)
    if len(expression) > 200 or not expression:
        return None
    if "=" in expression:
        expression = expression.rsplit("=", 1)[-1]
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return None

    def visit(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Pow) and (abs(right) > 12 or abs(left) > 1e6):
                raise ValueError("unsafe exponent")
            return _BINARY[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](visit(node.operand))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "sqrt":
            if len(node.args) != 1:
                raise ValueError("invalid sqrt")
            return math.sqrt(visit(node.args[0]))
        raise ValueError("unsupported expression")

    try:
        result = visit(tree)
    except (ArithmeticError, OverflowError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _reference_candidate(reference: str) -> str:
    normalized = reference.strip()
    matches = re.findall(r"(?:答案|结果|结论)\s*[:：]?\s*([^\n。；;]+)", normalized)
    return matches[-1] if matches else normalized


def validate_practice_answer(student_answer: str, answer_reference: str | None) -> tuple[bool | None, dict]:
    if not answer_reference:
        return None, {"method": "no_reference", "authoritative": False}
    student_normalized = _normalize(student_answer)
    reference_candidate = _reference_candidate(answer_reference)
    reference_normalized = _normalize(reference_candidate)
    if student_normalized and student_normalized == reference_normalized:
        return True, {"method": "normalized_exact", "authoritative": True}
    student_value = _eval_numeric(student_answer)
    reference_value = _eval_numeric(reference_candidate)
    if student_value is not None and reference_value is not None:
        tolerance = max(1e-9, abs(reference_value) * 1e-9)
        correct = abs(student_value - reference_value) <= tolerance
        return correct, {
            "method": "numeric_expression",
            "authoritative": True,
            "student_value": student_value,
            "reference_value": reference_value,
            "tolerance": tolerance,
        }
    return None, {"method": "manual_review_required", "authoritative": False}
