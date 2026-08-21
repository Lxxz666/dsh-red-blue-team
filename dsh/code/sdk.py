"""
dsh.code.sdk —— SDK 提示分节渲染（Python 风格）。

对应 TS 版 py-types.ts：把可见工具的 schema 投影为程序可编程的 Python 声明块
（mode: 'code' 下这是模型获知每个工具参数/输出形状的唯一来源）：

- 每个工具的 object 参数/输出（含嵌套 object）渲染为具名 ``TypedDict``；
- ``const``/``enum`` → ``Literal[...]``；``oneOf`` → ``A | B``；array → ``list[T]``；
- 工具名是合法裸标识符且非关键字/下划线开头 → ``async def name(...)`` 方法；
  否则渲染为下标注释 ``tools["name"](args: X) -> Y``；
- ``Tools(Protocol)`` + ``tools: Tools`` 单例 + ``ToolCallError`` 声明；
- 说明文本折叠空白、转义反斜杠/引号与控制字符，保证生成块始终是合法 Python。

与 TS 版的简化：渲染器与执行器跑在**同一 CPython** 上，所以 ``isidentifier()``
即与解释器一致（无 Unicode 表版本偏移问题）；退化为 ``Any`` 而不是抛错。
"""
from __future__ import annotations

import keyword
import re
import unicodedata
from typing import Any, Dict, List, Optional, Set, Tuple

_MAX_CLASS_NAME_BASE = 120
_MAX_LIST_NESTING = 180

_RESERVED = set(keyword.kwlist) | {"True", "False", "None", "__debug__"}
_TYPING_ORDER = ["Any", "Literal", "NotRequired", "Protocol", "TypedDict"]

_UNPRINTABLE = re.compile(r"[\x00-\x08\x0e-\x1f\x7f-\x9f]")

SDK_INSTRUCTIONS = """## Writing code for run_code

`run_code` takes two required arguments: `code` — the body of an async Python function (top-level `await` and `return` both work) — and `description`, a short summary of what the program does. At run time exactly two of the names declared below are bound: `tools` and `ToolCallError`. Everything else is a STATIC STUB describing argument and return types — in particular the `TypedDict` classes do NOT exist at run time, so build arguments as plain `dict`/`list` JSON values: `await tools.name({"field": 1})`, never `FooArgs(field=1)`, which raises `NameError`. Inside the program:

- Call tools as `await tools.name(args)` — subscript access for exotic, reserved, or underscore-leading names: `await tools["my-tool"](args)`. Every call resolves to the tool's typed canonical JSON value (each method's return type below). Tool arguments must be lossless JSON.
- A FAILED tool call raises `ToolCallError`, whose `toolName` identifies the failed tool and whose message is human-readable — wrap in `try/except` to handle and continue.
- Independent read-only calls MAY overlap under `asyncio.gather` (safe calls run concurrently; mutating calls run alone, in submission order). Sequence dependent work with `await`.
- Emit the run's answer with `print(...)` and/or a top-level `return <value>`; the returned value must be lossless JSON. ONLY what you print and the returned value come back — intermediate tool results never enter the conversation, so extract just what you need.

The available tools:"""


def _is_bare_identifier(name: str) -> bool:
    """能否裸发射为 Python 标识符（isidentifier + NFKC 稳定 + 非关键字）。"""
    return (name.isidentifier() and name not in _RESERVED
            and unicodedata.normalize("NFKC", name) == name)


def _describe(schema: Any) -> Optional[str]:
    """折叠一行的 description（控制字符转义为反斜杠-x 形式；空则 None）。"""
    description = (schema or {}).get("description") if isinstance(schema, dict) else None
    if not isinstance(description, str):
        return None
    collapsed = re.sub(r"\s+", " ", description)
    collapsed = _UNPRINTABLE.sub(
        lambda m: "\\x%02x" % ord(m.group(0)), collapsed).strip()
    return collapsed or None


def _doc_lines(description: Any, indent: int) -> List[str]:
    """工具描述 → 一行 docstring 列表（反斜杠/引号转义，保证合法 Python）。"""
    collapsed = _describe({"description": description})
    if collapsed is None:
        return []
    escaped = collapsed.replace("\\", "\\\\").replace('"', '\\"')
    return [f"{'    ' * indent}\"\"\"{escaped}\"\"\""]


def _camel_case(raw: str) -> str:
    """名字 → 类名片段（非标识符字符切词 + 前缀保证）。"""
    parts = [p for p in re.split(r"[^0-9A-Za-z\u0080-\uffff_]+|_+", raw) if p]
    joined = "".join(p[0].upper() + p[1:] for p in parts)
    joined = unicodedata.normalize("NFKC", joined)
    if joined and (joined[0].isidentifier() or joined[0] == "_"):
        return joined
    return "Tool" + joined


def _cap_name_base(base: str) -> str:
    return base[:_MAX_CLASS_NAME_BASE]


def _child_name(base: str, segment: str) -> str:
    return _cap_name_base(unicodedata.normalize("NFKC", base + segment))


class _RenderState:
    def __init__(self) -> None:
        self.classes: List[str] = []
        self.used_names: Set[str] = set()
        self.counters: Dict[str, int] = {}
        self.typing: Set[str] = set()

    def allocate(self, base: str) -> str:
        base = _cap_name_base(base)
        name = base
        if name in self.used_names:
            n = self.counters.get(base, 2)
            while f"{base}{n}" in self.used_names:
                n += 1
            name = f"{base}{n}"
            self.counters[base] = n + 1
        self.used_names.add(name)
        return name


def _py_scalar(value: Any) -> str:
    if value is True:
        return "True"
    if value is False:
        return "False"
    if value is None:
        return "None"
    if isinstance(value, str):
        import json
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _render_type(schema: Any, class_name: str, state: _RenderState,
                 list_depth: int = 0) -> str:
    """一个 schema 节点 → Python 类型表达式（收集 TypedDict 声明）。"""
    try:
        if not isinstance(schema, dict):
            return "Any"
        if "oneOf" in schema:
            return " | ".join(
                _render_type(branch, _child_name(class_name, str(i + 1)),
                             state, list_depth)
                for i, branch in enumerate(schema["oneOf"]))
        node_type = schema.get("type")
        if node_type is None:
            if "enum" in schema or "const" in schema:
                state.typing.add("Literal")
                if "const" in schema:
                    return f"Literal[{_py_scalar(schema['const'])}]"
                return "Literal[" + ", ".join(_py_scalar(v)
                                              for v in schema["enum"]) + "]"
            return "Any"
        if node_type == "string":
            return _constrained(schema, "str", state)
        if node_type == "number":
            return _constrained(schema, "float", state)
        if node_type == "integer":
            return _constrained(schema, "int", state)
        if node_type == "boolean":
            return _constrained(schema, "bool", state)
        if node_type == "null":
            return "None"
        if node_type == "array":
            items = schema.get("items")
            if items is None:
                state.typing.add("Any")
                return "list[Any]"
            if list_depth >= _MAX_LIST_NESTING:
                state.typing.add("Any")
                return "Any"
            return "list[" + _render_type(items, class_name, state,
                                          list_depth + 1) + "]"
        if node_type == "object":
            entries = list((schema.get("properties") or {}).items())
            degradable = (class_name == ""
                          or not all(_is_bare_identifier(n)
                                     and not (n.startswith("__")
                                              and not n.endswith("__"))
                                     for n, _ in entries))
            if degradable:
                state.typing.add("Any")
                return "dict[str, Any]"
            if not entries and schema.get("additionalProperties") is not False:
                state.typing.add("Any")
                return "dict[str, Any]"
            name = state.allocate(class_name)
            state.typing.add("TypedDict")
            required = set(schema.get("required") or [])
            lines = [f"class {name}(TypedDict):"]
            for field, field_schema in entries:
                desc = _describe(field_schema)
                if desc is not None:
                    lines.append(f"    # {desc}")
                field_type = _render_type(
                    field_schema, _child_name(name, _camel_case(field)),
                    state, 1)
                if field in required:
                    lines.append(f"    {field}: {field_type}")
                else:
                    state.typing.add("NotRequired")
                    lines.append(f"    {field}: NotRequired[{field_type}]")
            if schema.get("additionalProperties") is not False:
                lines.append("    # Additional keys beyond those declared are allowed.")
            if len(lines) == 1:
                lines.append("    pass")
            state.classes.append("\n".join(lines))
            return name
        return "Any"
    except Exception:
        state.typing.add("Any")
        return "Any"


def _constrained(schema: Dict[str, Any], broad: str,
                 state: _RenderState) -> str:
    if "const" in schema:
        state.typing.add("Literal")
        return f"Literal[{_py_scalar(schema['const'])}]"
    if "enum" in schema:
        state.typing.add("Literal")
        return "Literal[" + ", ".join(_py_scalar(v)
                                      for v in schema["enum"]) + "]"
    return broad


def json_schema_to_py(schema: Any) -> str:
    """无上下文节点 → Python 类型表达式（object 退化为 dict[str, Any]）。"""
    return _render_type(schema, "", _RenderState())


def render_tools_sdk_py(schemas: List[Dict[str, Any]]) -> str:
    """渲染完整 tools:sdk 分节（确定性：工具按名字典序）。"""
    sorted_schemas = sorted(schemas, key=lambda s: s.get("name", ""))
    state = _RenderState()
    state.typing.add("Protocol")
    members: List[str] = []
    statements = 0
    for schema in sorted_schemas:
        name = schema.get("name", "")
        arg_type = _render_type(schema.get("parameters"),
                                _camel_case(name) + "Args", state)
        output_type = _render_type(schema.get("output"),
                                   _camel_case(name) + "Output", state)
        if _is_bare_identifier(name) and not name.startswith("_"):
            doc = _doc_lines(schema.get("description"), 2)
            signature = (f"    async def {name}(self, args: {arg_type})"
                         f" -> {output_type}:")
            if doc:
                members.append(signature)
                members.extend(doc)
            else:
                members.append(signature + " ...")
            statements += 1
        else:
            import json
            members.append(
                f'    # tools[{json.dumps(name, ensure_ascii=False)}]'
                f"(args: {arg_type}) -> {output_type}")
            desc = _describe(schema)
            if desc is not None:
                members.append(f"    #   {desc}")
    if statements == 0:
        members.insert(0, "    pass")
    imports = [sym for sym in _TYPING_ORDER if sym in state.typing]
    class_block = "\n\n".join(state.classes)
    declaration = (
        f"from typing import {', '.join(imports)}\n\n"
        "class ToolCallError(Exception):\n"
        "    toolName: str\n"
        + (f"\n\n{class_block}\n\n" if class_block else "\n")
        + "class Tools(Protocol):\n"
        + "\n".join(members)
        + "\n\ntools: Tools")
    return f"{SDK_INSTRUCTIONS}\n\n```python\n{declaration}\n```"
