#!/usr/bin/env python3
"""Context profile loading for CI/CD workspace transport scripts."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import yaml


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTEXTS_DIR = os.path.join(ROOT_DIR, "contexts")

_SECTION_NAMES = ("aidp", "git", "options", "runtime")
_FLAT_KEY_MAP = {
    "region": ("aidp", "region"),
    "aidp_ocid": ("aidp", "ocid"),
    "ocid": ("aidp", "ocid"),
    "workspace_name": ("aidp", "workspace_name"),
    "workspace_description": ("aidp", "workspace_description"),
    "repository_url": ("git", "repository_url"),
    "credential_name": ("git", "credential_name"),
    "branch": ("git", "branch"),
    "parent_dir": ("git", "parent_dir"),
    "bundle_name": ("git", "bundle_path"),
    "bundle_path": ("git", "bundle_path"),
    "stage_bundle_name": ("git", "stage_bundle_path"),
    "stage_bundle_path": ("git", "stage_bundle_path"),
    "auth_method": ("runtime", "auth_method"),
    "demo_mode": ("options", "demo_mode"),
}


def normalize_context_name(name: str) -> str:
    raw = str(name or "").strip()
    if not raw:
        raise RuntimeError("context name cannot be empty")
    base = os.path.basename(raw)
    if base.endswith(".yaml"):
        base = base[:-5]
    if base.endswith(".yml"):
        base = base[:-4]
    if not base:
        raise RuntimeError("context name cannot be empty")
    return base


def resolve_context_path(name: str) -> str:
    context_name = normalize_context_name(name)
    return os.path.join(CONTEXTS_DIR, "{}.yaml".format(context_name))


def load_context(name: str) -> Dict[str, Any]:
    context_name = normalize_context_name(name)
    path = resolve_context_path(context_name)
    if not os.path.exists(path):
        raise RuntimeError(
            "context {!r} was not found at {}. Create contexts/{}.yaml first.".format(
                context_name,
                path,
                context_name,
            )
        )
    with open(path, "r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise RuntimeError("context {!r} must contain a YAML object at the root".format(context_name))
    payload["_meta"] = {
        "name": context_name,
        "path": path,
    }
    return payload


def context_auth_method(context: Dict[str, Any]) -> str:
    runtime = context.get("runtime") or {}
    return str(runtime.get("auth_method") or "").strip()


def context_demo_mode(context: Dict[str, Any]) -> bool:
    options = context.get("options") or {}
    profile = str(context.get("profile") or "").strip().lower()
    return bool(
        options.get("demo_mode")
        or profile == "demo"
        or (context.get("_meta") or {}).get("name") == "demo"
    )


def config_paths_for_context(context_name: str, demo_mode: bool = False) -> tuple[str, str]:
    name = normalize_context_name(context_name)
    if demo_mode or name == "demo":
        return (
            os.path.join(ROOT_DIR, "source-workspace.demo.yaml"),
            os.path.join(ROOT_DIR, "target-workspace.demo.yaml"),
        )
    return (
        os.path.join(ROOT_DIR, "source-workspace.{}.yaml".format(name)),
        os.path.join(ROOT_DIR, "target-workspace.{}.yaml".format(name)),
    )


def _merge_sections(target: Dict[str, Any], source: Dict[str, Any]) -> None:
    for section in _SECTION_NAMES:
        block = source.get(section)
        if isinstance(block, dict):
            target.setdefault(section, {})
            target[section].update(block)


def _apply_flat_keys(target: Dict[str, Any], source: Dict[str, Any]) -> None:
    for key, value in source.items():
        mapping = _FLAT_KEY_MAP.get(key)
        if not mapping:
            continue
        section, mapped_key = mapping
        target.setdefault(section, {})
        target[section][mapped_key] = value


def context_role_settings(context: Dict[str, Any], role: str) -> Dict[str, Any]:
    if role not in {"source", "target"}:
        raise RuntimeError("unsupported context role {!r}".format(role))
    merged: Dict[str, Any] = {section: {} for section in _SECTION_NAMES}

    _merge_sections(merged, context)
    _apply_flat_keys(merged, context)

    role_block = context.get(role) or {}
    if isinstance(role_block, dict):
        _merge_sections(merged, role_block)
        _apply_flat_keys(merged, role_block)

    enabled = role_block.get("enabled") if isinstance(role_block, dict) else None
    if enabled is not None:
        merged.setdefault("options", {})
        merged["options"]["enabled"] = bool(enabled)

    merged.setdefault("options", {})
    merged["options"]["context_name"] = str((context.get("_meta") or {}).get("name") or "").strip()
    if context_demo_mode(context):
        merged["options"]["demo_mode"] = True
    return merged
