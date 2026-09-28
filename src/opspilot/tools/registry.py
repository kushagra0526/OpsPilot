"""Tool registry: the only tools the agent may call, with typed args and base risk."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ValidationError


@dataclass(frozen=True)
class ToolContext:
    """Per-call context passed to every tool."""

    session_id: str
    turn_id: str
    operator_id: str = "operator-1"
    approved_by: str | None = None


@dataclass(frozen=True)
class ToolSpec:
    """Registry entry for one tool."""

    name: str
    description: str
    args_model: type[BaseModel]
    fn: Callable[[Any, ToolContext], dict[str, Any]]
    kind: Literal["read", "action"]
    base_risk: Literal["low", "medium", "high"]
    reversible: bool


REGISTRY: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> ToolSpec:
    """Add a tool to the registry."""
    REGISTRY[spec.name] = spec
    return spec


def get_tool(name: str) -> ToolSpec | None:
    """Look up a tool by name (None if unknown)."""
    _ensure_loaded()
    return REGISTRY.get(name)


def all_tools() -> list[ToolSpec]:
    """Every registered tool."""
    _ensure_loaded()
    return list(REGISTRY.values())


def validate_args(name: str, args: dict[str, Any]) -> BaseModel:
    """Parse args with the tool's model; raises ValueError for unknown tools or bad args."""
    spec = get_tool(name)
    if spec is None:
        raise ValueError(f"unknown tool {name!r}; allowed: {sorted(REGISTRY)}")
    try:
        return spec.args_model.model_validate(args)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise ValueError(f"invalid args for {name}: {details}") from exc


def run_tool(name: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Validate args and run the tool. Tools return {"ok", "summary", "data"}."""
    parsed = validate_args(name, args)
    spec = get_tool(name)
    assert spec is not None
    return spec.fn(parsed, ctx)


def _type_of(prop: dict[str, Any]) -> str:
    if "enum" in prop:
        return "|".join(json.dumps(v) for v in prop["enum"])
    if "anyOf" in prop:
        return "|".join(_type_of(p) for p in prop["anyOf"] if p.get("type") != "null")
    if prop.get("type") == "array":
        return f"list[{_type_of(prop.get('items', {}))}]"
    return {"number": "float", "integer": "int", "string": "str", "boolean": "bool"}.get(
        prop.get("type", "any"), prop.get("type", "any")
    )


def compact_signature(spec: ToolSpec) -> str:
    """One-line signature derived from the args JSON schema (token-cheap form for prompts)."""
    schema = spec.args_model.model_json_schema()
    required = set(schema.get("required", []))
    args = []
    for name, prop in schema.get("properties", {}).items():
        piece = f"{name}{'' if name in required else '?'}: {_type_of(prop)}"
        pattern = prop.get("pattern") or next(
            (p.get("pattern") for p in prop.get("anyOf", []) if p.get("pattern")), None
        )
        if pattern:
            piece += f" /{pattern}/"
        if prop.get("description"):
            piece += f" ({prop['description']})"
        args.append(piece)
    return f"{spec.name}({', '.join(args)}) [{spec.kind}] - {spec.description}"


def compact_catalog() -> list[str]:
    """Every tool as a compact signature line."""
    return [compact_signature(t) for t in all_tools()]


def _ensure_loaded() -> None:
    """Import both tool modules so they self-register (safe to call repeatedly)."""
    from opspilot.tools import action_tools, read_tools  # noqa: F401
