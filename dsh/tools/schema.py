"""
dsh.tools.schema —— 强制的原始 JSON Schema 子集校验（对应 TS 版 enforced subset）。

支持关键字: type(object/array/string/number/integer/boolean/null)、properties、
required、additionalProperties、items、enum、const、oneOf；description/title/
default/examples 为注解（不参与校验，但必须是无损失 JSON）。

未支持的关键字一律拒绝（fail loud），绝不静默接受而不执行。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..errors import ToolArgsError, ToolOutputError
from ..session.events import is_json_value

_SCALAR_TYPES = ("string", "number", "integer", "boolean", "null")
_ALLOWED_KEYS = {"type", "properties", "required", "additionalProperties",
                 "items", "enum", "const", "oneOf", "description", "title",
                 "default", "examples"}


def assert_supported_schema(schema: Any, path: str = "root") -> None:
    """
    校验一个 schema 是否属于受支持子集（结构性 + 关键字白名单）。

    :raises ToolArgsError: 非法/不支持的关键字组合。
    """
    if schema is None:
        return  # 空注解节点 = 不约束
    if not isinstance(schema, dict):
        raise ToolArgsError(f"{path}: schema node must be a mapping")
    unknown = set(schema.keys()) - _ALLOWED_KEYS
    if unknown:
        raise ToolArgsError(f"{path}: unsupported keywords {sorted(unknown)}")
    if "type" in schema and schema["type"] not in _SCALAR_TYPES + ("object", "array"):
        raise ToolArgsError(f"{path}: unsupported type {schema['type']!r}")
    if "oneOf" in schema:
        branches = schema["oneOf"]
        if not isinstance(branches, list) or len(branches) < 2:
            raise ToolArgsError(f"{path}: oneOf needs at least two branches")
        for i, branch in enumerate(branches):
            assert_supported_schema(branch, f"{path}.oneOf[{i}]")
    for key in ("properties",):
        if key in schema:
            for prop, prop_schema in schema[key].items():
                assert_supported_schema(prop_schema, f"{path}.{key}.{prop}")
    if "items" in schema:
        assert_supported_schema(schema["items"], f"{path}.items")
    if "required" in schema and "properties" not in schema:
        raise ToolArgsError(f"{path}: 'required' needs 'properties'")
    # object 结构关键字必须伴随 type:"object"（否则 _matches 会静默忽略整个
    # 结构、任何值都通过——fail loud 而非假校验）
    if (("properties" in schema or "additionalProperties" in schema)
            and schema.get("type") != "object"):
        raise ToolArgsError(
            f"{path}: 'properties'/'additionalProperties' require "
            'type: "object"')
    for key in ("default", "examples"):
        if key in schema and not is_json_value(schema[key]):
            raise ToolArgsError(f"{path}: {key} must be lossless JSON")


def _matches(node: Any, schema: Dict[str, Any]) -> bool:
    """递归匹配一个值（schema 已通过 assert_supported_schema）。"""
    if schema.get("type") == "null":
        return node is None
    if schema.get("type") == "object":
        if not isinstance(node, dict):
            return False
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        for name in required:
            if name not in node:
                return False
        if schema.get("additionalProperties") is False:
            if not set(node.keys()).issubset(properties.keys()):
                return False
        for name, value in node.items():
            if name in properties and properties[name] is not None:
                if not _matches(value, properties[name]):
                    return False
            elif name in properties and properties[name] is None:
                continue
        return True
    if schema.get("type") == "array":
        if not isinstance(node, list):
            return False
        items = schema.get("items")
        if items is None:
            return True
        return all(_matches(item, items) for item in node)
    if schema.get("type") == "string":
        if not isinstance(node, str):
            return False
        return _match_scalar_constraints(node, schema)
    if schema.get("type") == "integer":
        return isinstance(node, int) and not isinstance(node, bool) \
            and _match_scalar_constraints(node, schema)
    if schema.get("type") == "number":
        if isinstance(node, bool) or not isinstance(node, (int, float)):
            return False
        return _match_scalar_constraints(node, schema)
    if schema.get("type") == "boolean":
        return isinstance(node, bool) and _match_scalar_constraints(node, schema)
    if "oneOf" in schema:
        return sum(1 for branch in schema["oneOf"]
                   if _matches(node, branch)) == 1
    # 无 type 注解节点
    if "enum" in schema:
        return node in schema["enum"]
    if "const" in schema:
        return node == schema["const"]
    return True


def _match_scalar_constraints(node: Any, schema: Dict[str, Any]) -> bool:
    if "enum" in schema and node not in schema["enum"]:
        return False
    if "const" in schema and node != schema["const"]:
        return False
    return True


def validate_value(value: Any, schema: Any, *,
                   error_cls: type = ToolOutputError,
                   what: str = "value") -> Any:
    """
    校验一个值并返回它（通过时不改动）。

    :param schema: None = 不约束。
    :param error_cls: 参数校验用 ToolArgsError，输出校验用 ToolOutputError。
    :raises: 校验失败时抛出 error_cls。
    """
    if schema is None:
        return value
    assert_supported_schema(schema)
    if not _matches(value, schema):
        raise error_cls(f"{what} does not match schema: {schema!r}")
    return value


def parameter_schema(spec: Dict[str, Any]) -> Dict[str, Any]:
    """
    把「隐式开放对象」参数声明编译为标准 JSON Schema。

    输入形如 ``{"path": {"type": "string", "required": True}}``；
    输出为 ``{"type": "object", "properties": {...}, "required": [...]}``。
    """
    properties: Dict[str, Any] = {}
    required: List[str] = []
    for name, raw in (spec or {}).items():
        prop = dict(raw)
        if prop.pop("required", False):
            required.append(name)
        properties[name] = prop or None
    return {"type": "object", "properties": properties, "required": required}
