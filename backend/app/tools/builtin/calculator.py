from __future__ import annotations

import ast
from typing import Any

from langchain_core.tools import BaseTool, tool

from app.tools.base import ToolContext, ToolEntryConfig, tool_error_boundary

MAX_EXPRESSION_LENGTH = 500

# 计算前先用 AST 白名单校验表达式，杜绝 parse_expr 底层 eval 被注入任意代码
_ALLOWED_CALLS = frozenset(
    {
        "abs",
        "sqrt",
        "exp",
        "log",
        "ln",
        "sin",
        "cos",
        "tan",
        "asin",
        "acos",
        "atan",
        "atan2",
        "sinh",
        "cosh",
        "tanh",
        "floor",
        "ceiling",
        "factorial",
        "gcd",
        "lcm",
        "min",
        "max",
        "Min",
        "Max",
        "Rational",
        "Integer",
        "Float",
    }
)
_ALLOWED_NAMES = frozenset({"pi", "e", "E", "tau"})
_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_ALLOWED_UNARYOPS = (ast.UAdd, ast.USub)


def _validate_expression(expression: str) -> str | None:
    """AST 白名单校验，返回错误信息；None 表示通过。"""
    if len(expression) > MAX_EXPRESSION_LENGTH:
        return f"表达式过长（超过 {MAX_EXPRESSION_LENGTH} 字符），请拆分后计算"
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError):
        return f"表达式语法错误：{expression!r}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Expression | ast.Load):
            continue
        # ast.walk 会单独访问运算符节点（如 Div/Add），先放行白名单内的运算符
        if isinstance(node, _ALLOWED_BINOPS + _ALLOWED_UNARYOPS):
            continue
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, int | float):
                return "表达式只允许出现数字常量"
            continue
        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, _ALLOWED_BINOPS):
                return f"不支持的运算符：{type(node.op).__name__}"
            continue
        if isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, _ALLOWED_UNARYOPS):
                return f"不支持的一元运算符：{type(node.op).__name__}"
            continue
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_CALLS:
                return "只允许调用白名单内的数学函数（sqrt/log/sin/cos/abs/factorial 等）"
            if node.keywords:
                return "函数调用不支持关键字参数"
            continue
        if isinstance(node, ast.Name):
            if node.id not in _ALLOWED_NAMES and node.id not in _ALLOWED_CALLS:
                return f"未知的变量或函数：{node.id}（常量仅支持 pi、e、tau）"
            continue
        return f"表达式包含不允许的语法：{type(node).__name__}"
    return None


def _evaluate(expression: str) -> Any:
    from sympy import E, pi
    from sympy.parsing.sympy_parser import convert_xor, parse_expr, standard_transformations

    local_dict = {"pi": pi, "e": E, "E": E, "tau": 2 * pi}
    return parse_expr(
        expression,
        local_dict=local_dict,
        transformations=(*standard_transformations, convert_xor),
        evaluate=True,
    )


def _strip_trailing_zeros(text: str) -> str:
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _format_result(value: Any) -> str:
    approx = value.evalf(10)
    if bool(getattr(approx, "is_Integer", False)):
        return str(int(approx))
    exact = str(value)
    approx_text = _strip_trailing_zeros(str(approx))
    if exact == approx_text:
        return approx_text
    return f"{exact} ≈ {approx_text}"


def make_calculator_tool(context: ToolContext, entry: ToolEntryConfig) -> BaseTool:
    @tool("calculator")
    @tool_error_boundary
    def calculator(expression: str) -> str:
        """用符号计算引擎精确计算数学表达式，涉及算术/开方/三角/对数运算时使用。

        支持加减乘除、乘方(^)、取余(mod)、括号，常量 pi、e、tau，
        函数 sqrt/exp/log/ln/sin/cos/tan/floor/ceiling/factorial/gcd/lcm/min/max。
        示例："(23*17+5)/3"、"sqrt(2)"、"sin(pi/6)"。

        Args:
            expression: 待计算的数学表达式。
        """
        text = expression.strip()
        if not text:
            return "表达式不能为空"
        error = _validate_expression(text)
        if error:
            return error
        value = _evaluate(text)
        free_symbols: set[Any] = getattr(value, "free_symbols", set())
        if free_symbols:
            names = ", ".join(sorted(str(symbol) for symbol in free_symbols))
            return f"表达式包含未知变量：{names}，无法得出数值结果"
        return f"{text} = {_format_result(value)}"

    return calculator


__all__ = ["make_calculator_tool"]
