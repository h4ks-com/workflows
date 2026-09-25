import ast
import operator
from collections.abc import Callable

from pydantic import JsonValue

from workflows.db import JsonObject
from workflows.jobtypes.probe import Probe
from workflows.jobtypes.probe import ProbeError

DURATION = "duration"
ARITHMETIC: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
COMPARISONS: dict[type[ast.cmpop], Callable[[JsonValue, JsonValue], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
}
ORDERINGS: dict[type[ast.cmpop], Callable[[float, float], bool]] = {
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


class PriceError(Exception):
    pass


def _number(value: JsonValue) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PriceError(f"{value!r} is not a number")
    return value


def _duration(probe: Probe | None) -> float:
    if probe is None:
        raise ProbeError("this job type needs a probed input")
    return probe.duration_seconds


def _duration_field(node: ast.Call) -> str:
    if not (
        isinstance(node.func, ast.Name)
        and node.func.id == DURATION
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and not node.keywords
    ):
        raise PriceError("the only call allowed is duration(<field>)")
    return node.args[0].id


def _check(node: ast.AST) -> None:
    match node:
        case ast.Constant(value=int() | float() | str()) | ast.Name():
            return
        case ast.BinOp(op=op) if type(op) in ARITHMETIC:
            children: list[ast.AST] = [node.left, node.right]
        case ast.UnaryOp(op=ast.USub(), operand=operand):
            children = [operand]
        case ast.IfExp(test=test, body=body, orelse=orelse):
            children = [test, body, orelse]
        case ast.Compare(ops=[op], comparators=[right]) if type(op) in COMPARISONS | ORDERINGS:
            children = [node.left, right]
        case ast.Call():
            _duration_field(node)
            return
        case _:
            raise PriceError(f"{type(node).__name__} is not allowed in a price")
    for child in children:
        _check(child)


class PriceRule:
    """A price in credits, written as a safe expression over the params.

    It allows numbers, field names, `+ - * /`, comparisons, `a if condition else b` and
    `duration(field)`, the length in seconds of the media behind a link field.
    """

    def __init__(self, expression: str) -> None:
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError as error:
            raise PriceError(f"cannot read the price {expression!r}") from error
        _check(tree.body)
        self.expression = expression
        self._body = tree.body
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        media = {_duration_field(call) for call in calls}
        if len(media) > 1:
            raise PriceError("a price can read the duration of one field only")
        self.media_field = next(iter(media), None)
        callees = {id(call.func) for call in calls}
        self.fields = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and id(node) not in callees
        }

    def evaluate(self, params: JsonObject, probe: Probe | None) -> int:
        """Compute the price in whole credits.

        :param probe: the probe of the `duration` field's media, when the rule reads one.
        """
        return round(_number(self._value(self._body, params, probe)))

    def _value(self, node: ast.expr, params: JsonObject, probe: Probe | None) -> JsonValue:
        match node:
            case ast.Constant(value=int() | float() | str() as value):
                return value
            case ast.Name(id=name):
                if name not in params:
                    raise PriceError(f"the price reads {name}, which the form does not have")
                return params[name]
            case ast.IfExp(test=test, body=body, orelse=orelse):
                branch = body if self._value(test, params, probe) else orelse
                return self._value(branch, params, probe)
            case ast.Compare(left=left, ops=[op], comparators=[right]):
                return self._compare(
                    op, self._value(left, params, probe), self._value(right, params, probe)
                )
            case ast.Call():
                return _duration(probe)
        return self._arithmetic(node, params, probe)

    def _arithmetic(self, node: ast.expr, params: JsonObject, probe: Probe | None) -> float:
        match node:
            case ast.BinOp(left=left, op=op, right=right):
                values = self._value(left, params, probe), self._value(right, params, probe)
                return ARITHMETIC[type(op)](_number(values[0]), _number(values[1]))
            case ast.UnaryOp(operand=operand):
                return -_number(self._value(operand, params, probe))
        raise PriceError(f"{type(node).__name__} is not allowed in a price")

    @staticmethod
    def _compare(op: ast.cmpop, left: JsonValue, right: JsonValue) -> bool:
        equality = COMPARISONS.get(type(op))
        if equality is not None:
            return equality(left, right)
        return ORDERINGS[type(op)](_number(left), _number(right))
