"""F# functions, members, and cyclomatic complexity.

Decision points are `if`, `elif`, `for`, `while`, each arm of `match`,
`function`, and `try ... with` (on one line or several), each `when` guard on
an arm, and each `&&` / `||`. Combinator calls such as `Option.map` and computation-expression
binds such as `let!` add nothing.

Entries are module- and type-level `let` functions, members, active patterns
(named by their pattern text, `|Even|Odd|`), and lambdas bound to a value
(`let handler = fun ...` or `= function ...`), and values bound to an object
expression, which carry the complexity of its members. A function, lambda, or
object expression inside another entry stays in that entry. Other values, such as a
partial application or a composition, are not entries. The namespace is the
`namespace` and `module` path, plus the type for a member; a file with no
`module` or `namespace` line is the implicit module named after the file.
"""

from pathlib import Path

from crapper.languages.treesitter import (
    child_of_type,
    complexity,
    descendants,
    end_line,
    node_text,
    parse,
    start_line,
)
from crapper.model import Function

_DECISIONS = {
    "if_expression",
    "elif_expression",
    "for_expression",
    "while_expression",
    "rule",
}
_LOGIC = {"&&", "||"}
_BODIES = {"function_or_value_defn", "method_or_prop_defn"}
_LAMBDAS = {"fun_expression", "function_expression"}
_FILE_SCOPES = {"namespace", "named_module"}
_NAME_NODES = ("identifier", "active_pattern", "op_identifier")


def _is_guard(data: bytes, node) -> bool:
    if node.type == "when":
        return node.parent is not None and node.parent.type == "rule"
    # On one line, the grammar reads arms after the first as infix
    # expressions, so their `when` arrives as an identifier. `when` is a
    # keyword and cannot name anything else.
    return node.type == "identifier" and node_text(data, node) == "when"


def _is_logic(data: bytes, node) -> bool:
    if node.type == "infix_op":
        # A single `|` is no F# operator: it is the next arm of a one-line
        # `match` or `try ... with` that the grammar read as an infix.
        return node_text(data, node) == "|"
    if node.type != "infix_expression":
        return False
    op = child_of_type(node, "infix_op")
    return op is not None and node_text(data, op) in _LOGIC


def _decision_test(data: bytes):
    def is_decision(node) -> bool:
        return node.type in _DECISIONS or _is_guard(data, node) or _is_logic(data, node)

    return is_decision


def _skip(_node) -> bool:
    return False


def _is_nested(node) -> bool:
    current = node.parent
    while current is not None:
        if current.type in _BODIES:
            return True
        current = current.parent
    return False


def _text(data: bytes, node) -> str | None:
    return node_text(data, node) if node is not None else None


def _implicit_module(path: str) -> str:
    stem = Path(path).stem
    return stem[:1].upper() + stem[1:]


def _scope_name(data: bytes, node) -> str | None:
    if node.type in _FILE_SCOPES:
        name = child_of_type(node, "long_identifier", "identifier")
        return _text(data, name)
    if node.type == "module_defn":
        name = child_of_type(node, "identifier")
        return _text(data, name)
    type_name = child_of_type(node, "type_name")
    if type_name is None:
        return None
    name = type_name.child_by_field_name("type_name") or child_of_type(type_name, "identifier")
    return _text(data, name)


def _namespace(data: bytes, node, implicit: str | None) -> str:
    names: list[str] = []
    current = node.parent
    while current is not None:
        name = _scope_name(data, current)
        if name:
            names.append(name)
        current = current.parent
    if implicit:
        names.append(implicit)
    names.reverse()
    return ".".join(names)


def _unwrap(node):
    """The expression inside parentheses and type annotations."""

    current = node
    while current is not None and current.type in {"paren_expression", "typecast_expression"}:
        current = next(
            (child for child in current.children if child.is_named),
            None,
        )
    return current


def _pattern_name(data: bytes, node) -> str:
    text = "".join(node_text(data, node).split())
    if text.startswith("(") and text.endswith(")"):
        return text[1:-1]
    return text


def _named_function(data: bytes, name) -> str | None:
    """The name of a `let` with arguments, or None for a union case pattern.

    The grammar reads `let Some y = opt` as a function named `Some`. F# names
    `let` functions in camelCase, so an uppercase name is a union case.
    """

    text = node_text(data, name)
    return None if text.split(".")[-1][:1].isupper() else text


def _function_name(data: bytes, left) -> str | None:
    name = child_of_type(left, *_NAME_NODES)
    if name is None:
        return None
    if name.type == "identifier":
        return _named_function(data, name)
    return _pattern_name(data, name)


def _name_pattern(left):
    pattern = child_of_type(left, "identifier_pattern", "attribute_pattern")
    if pattern is not None and pattern.type == "attribute_pattern":
        return child_of_type(pattern, "identifier_pattern")
    return pattern


def _is_entry_value(value) -> bool:
    """A lambda, or an object expression whose members carry the logic."""

    if value is None:
        return False
    if value.type == "brace_expression":
        return child_of_type(value, "object_expression") is not None
    return value.type in _LAMBDAS


def _value_name(data: bytes, left, body) -> str | None:
    pattern = _name_pattern(left)
    if pattern is None:
        return None
    # The grammar reads some functions with a return type, `let f x : T =`,
    # as a value whose pattern is the name followed by its arguments.
    if pattern.named_child_count > 1:
        return _named_function(data, pattern.named_children[0])
    if not _is_entry_value(_unwrap(body)):
        return None
    return node_text(data, pattern)


def _binding_name(data: bytes, left, body) -> str | None:
    if left.type == "function_declaration_left":
        return _function_name(data, left)
    return _value_name(data, left, body)


def _bindings(defn):
    """(start node, left, body) for each binding of `let` / `let rec ... and`."""

    start = defn
    left = None
    for index, child in enumerate(defn.children):
        if child.type == "and":
            start = child
        elif child.type in {"function_declaration_left", "value_declaration_left"}:
            left = child
        elif left is not None and defn.field_name_for_child(index) == "body":
            yield start, left, child
            left = None


def _member_name(data: bytes, defn) -> str | None:
    name = child_of_type(defn, "property_or_ident")
    if name is None:
        return None
    ident = name.child_by_field_name("method") or child_of_type(name, "identifier")
    return _text(data, ident)


class _Reader:
    def __init__(self, data: bytes, path: str, implicit: str | None):
        self.data = data
        self.path = path
        self.implicit = implicit
        self.is_decision = _decision_test(data)
        self.found: list[Function] = []

    def add(self, name: str, owner, first, last, body) -> None:
        self.found.append(
            Function(
                name=name,
                namespace=_namespace(self.data, owner, self.implicit),
                complexity=complexity(body, self.is_decision, _skip),
                start_line=start_line(first),
                end_line=end_line(last),
                path=self.path,
                language="fsharp",
            )
        )

    def read_let(self, defn) -> None:
        for start, left, body in _bindings(defn):
            name = _binding_name(self.data, left, body)
            if name is not None:
                self.add(name, defn, start, body, body)

    def read_member(self, defn) -> None:
        name = _member_name(self.data, defn)
        if name is None:
            return
        owner = defn.parent if defn.parent is not None else defn
        self.add(name, defn, owner, defn, defn)

    def read(self, node) -> None:
        if node.type not in _BODIES or _is_nested(node):
            return
        if node.type == "function_or_value_defn":
            self.read_let(node)
        else:
            self.read_member(node)


def _has_module_line(root) -> bool:
    return any(child.type in _FILE_SCOPES for child in root.children)


def functions_in_source(
    source: str, path: str, project_root: str | None = None
) -> list[Function]:
    data, tree = parse(source, "fsharp")
    root = tree.root_node
    implicit = None if _has_module_line(root) else _implicit_module(path)
    reader = _Reader(data, path, implicit)
    for node in descendants(root):
        reader.read(node)
    return reader.found
