#!/usr/bin/env python3
"""Dump raw workspace object payloads for manual inspection and classification review."""

from __future__ import annotations

import argparse
import json
import posixpath
import sys
from pathlib import Path
from typing import Optional, Sequence

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.console_logging import LOGGER_NAME, run_with_logged_errors, setup_logging
from cicd_deploy import AidpClient, build_signer, load_config
from core.settings import DEFAULT_AUTH_METHOD, DEFAULT_TARGET_CONFIG_PATH


def _print_block(title: str, payload) -> None:
    print("\n=== {} ===".format(title))
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))


def inspect_object(cfg_path: str, auth_method: str, object_path: str) -> int:
    cfg = load_config(cfg_path)
    signer = build_signer(auth_method)
    client = AidpClient(cfg, signer)

    workspace_name = cfg["aidp"]["workspace_name"]
    if not cfg["aidp"].get("workspace_key"):
        cfg["aidp"]["workspace_key"] = client.resolve_workspace_key_by_name(workspace_name)
        client.workspace_key = cfg["aidp"]["workspace_key"]

    normalized = object_path.rstrip("/")
    parent = posixpath.dirname(normalized) or "/"
    name = posixpath.basename(normalized)

    parent_resp = client.request_ok("GET", client.ws_url("objects"), params={"path": parent}, ok=(200,))
    parent_payload = parent_resp.json()
    _print_block("RAW parent listing for {}".format(parent), parent_payload)

    matched = None
    items = parent_payload if isinstance(parent_payload, list) else parent_payload.get("items", [])
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            current = item.get("path") or item.get("objectPath") or item.get("name")
            if current == normalized or current == name:
                matched = item
                break
    _print_block("MATCHED item for {}".format(normalized), matched)
    _print_block(
        "MATCHED item metadata for {}".format(normalized),
        client.workspace_item_metadata(matched),
    )

    child_resp = client.request_ok("GET", client.ws_url("objects"), params={"path": normalized}, ok=(200,))
    child_payload = child_resp.json()
    _print_block("RAW child listing for {}".format(normalized), child_payload)
    _print_block("CLASSIFICATION for {}".format(normalized), client.classify_workspace_folder(normalized))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--auth-method", default=DEFAULT_AUTH_METHOD)
    parser.add_argument("--config", default=DEFAULT_TARGET_CONFIG_PATH)
    parser.add_argument("--path", required=True)
    args = parser.parse_args(argv)

    setup_logging("cicd-inspect-object")
    return inspect_object(args.config, args.auth_method, args.path)


if __name__ == "__main__":
    raise SystemExit(run_with_logged_errors(main))
