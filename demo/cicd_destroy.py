#!/usr/bin/env python3
"""Destroy demo/test workspaces created only for transport-flow validation."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.console_logging import LOGGER_NAME, run_with_logged_errors, setup_logging
from core.contexts import config_paths_for_context, context_auth_method, context_demo_mode, load_context
from cicd_prepare import (
    DEMO_FIXED_SUFFIX,
    DEMO_SOURCE_WORKSPACE_PREFIX,
    DEMO_TARGET_WORKSPACE_PREFIX,
    build_demo_workspace_name,
)
from cicd_deploy import (
    AidpClient,
    build_signer,
    load_config,
    log_debug_context,
)
from core.settings import DEFAULT_AUTH_METHOD, DEFAULT_DEMO_SOURCE_CONFIG_PATH, DEFAULT_DEMO_TARGET_CONFIG_PATH

log = logging.getLogger(LOGGER_NAME)
PURPLE = "\033[1;35m"
RED = "\033[1;31m"
RESET = "\033[0m"
DESTROYED_BANNER = r"""
██████╗ ███████╗███████╗████████╗██████╗ ██╗   ██╗██╗██████╗  ██████╗
██╔══██╗██╔════╝██╔════╝╚══██╔══╝██╔══██╗██║   ██║██║██╔══██╗██╔═══██╗
██║  ██║█████╗  ███████╗   ██║   ██████╔╝██║   ██║██║██║  ██║██║   ██║
██║  ██║██╔══╝  ╚════██║   ██║   ██╔══██╗██║   ██║██║██║  ██║██║   ██║
██████╔╝███████╗███████║   ██║   ██║  ██║╚██████╔╝██║██████╔╝╚██████╔╝
╚═════╝ ╚══════╝╚══════╝   ╚═╝   ╚═╝  ╚═╝ ╚═════╝ ╚═╝╚═════╝  ╚═════╝
""".strip("\n")


def is_tty_stdout() -> bool:
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def print_destroy_banner() -> None:
    if not is_tty_stdout():
        print("Your demo environment will be DESTROYED. Wait to continue or cancel.")
        return
    lines = (
        "",
        "{}Your DEMO environment will be{}".format(PURPLE, RESET),
        "{}{}{}".format(RED, DESTROYED_BANNER, RESET),
        "{}wait to continue or cancel{}".format(PURPLE, RESET),
        "",
    )
    sys.stdout.write("\n".join(lines))
    sys.stdout.flush()


def demo_source_workspace_name() -> str:
    return build_demo_workspace_name(DEMO_SOURCE_WORKSPACE_PREFIX, DEMO_FIXED_SUFFIX)


def demo_target_workspace_name() -> str:
    return build_demo_workspace_name(DEMO_TARGET_WORKSPACE_PREFIX, DEMO_FIXED_SUFFIX)


def load_demo_base_config(source_cfg_path: str, target_cfg_path: str) -> Dict[str, Any]:
    candidates = [source_cfg_path, target_cfg_path]
    last_error: Optional[Exception] = None
    for path in candidates:
        try:
            cfg = load_config(path)
        except Exception as exc:
            last_error = exc
            continue
        if str((cfg.get("aidp") or {}).get("region") or "").strip() and str((cfg.get("aidp") or {}).get("ocid") or "").strip():
            return cfg
    raise RuntimeError(
        "could not load a valid base configuration to destroy the demo environment; "
        "check {} or {}".format(source_cfg_path, target_cfg_path)
    ) from last_error


def build_demo_destroy_plan() -> list[Dict[str, str]]:
    return [
        {
            "label": "source",
            "workspace_name": demo_source_workspace_name(),
            "workspace_key": "",
        },
        {
            "label": "target",
            "workspace_name": demo_target_workspace_name(),
            "workspace_key": "",
        },
    ]


def log_destroy_warning(plan: Sequence[Dict[str, str]]) -> None:
    log.warning("This operation will permanently delete only the demo resources below:")
    for item in plan:
        log.warning(
            "workspace=%s key=%s",
            item.get("workspace_name") or "(no name)",
            item.get("workspace_key") or "(no key)",
        )


def confirm_destroy(plan: Sequence[Dict[str, str]], auto_confirm: bool) -> None:
    print_destroy_banner()
    log_destroy_warning(plan)
    if auto_confirm:
        for remaining in range(5, 0, -1):
            log.warning("Destroy starts in %ss", remaining)
            time.sleep(1)
        return
    answer = input("Confirm permanent destruction? Type 'yes' to continue: ").strip().lower()
    if answer != "yes":
        raise RuntimeError("Destruction cancelled by user.")


def destroy_workspace(cfg: Dict[str, Any], workspace_name: str, auth_method: str) -> Dict[str, Any]:
    cfg = {
        **cfg,
        "aidp": {
            **(cfg.get("aidp") or {}),
            "workspace_name": workspace_name,
        },
    }
    signer = build_signer(auth_method)
    client = AidpClient(cfg, signer)
    workspace_key = str(cfg.get("aidp", {}).get("workspace_key") or "").strip()
    if not workspace_name:
        raise RuntimeError("demo workspace_name cannot be empty")
    log_debug_context(
        "Destroy workspace context",
        workspace_name=workspace_name,
        workspace_key=workspace_key,
        resource_id=client.resource_id,
        region=client.region,
    )
    return client.delete_workspace_by_name(workspace_name)


def log_destroy_summary(result: Dict[str, Dict[str, Any]]) -> None:
    log.info("== Destroy summary ==")
    for label in ("source", "target"):
        item = result.get(label)
        if not item:
            continue
        if not item.get("existed"):
            log.info("%s: workspace already absent", item.get("workspace_name"))
            continue
        log.info(
            "%s: workspace removed (key=%s)",
            item.get("workspace_name"),
            item.get("workspace_key") or "(no key)",
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context")
    parser.add_argument("--source-config", default=DEFAULT_DEMO_SOURCE_CONFIG_PATH)
    parser.add_argument("--target-config", default=DEFAULT_DEMO_TARGET_CONFIG_PATH)
    parser.add_argument("--auth-method", default=None)
    parser.add_argument("--skip-source", action="store_true")
    parser.add_argument("--skip-target", action="store_true")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)

    setup_logging("cicd-destroy")

    if args.skip_source and args.skip_target:
        raise RuntimeError("Nothing to do: --skip-source and --skip-target were both provided.")

    if args.context:
        context = load_context(args.context)
        context_demo = context_demo_mode(context)
        if args.source_config == DEFAULT_DEMO_SOURCE_CONFIG_PATH and args.target_config == DEFAULT_DEMO_TARGET_CONFIG_PATH:
            args.source_config, args.target_config = config_paths_for_context(args.context, demo_mode=context_demo)
        if not args.auth_method:
            args.auth_method = context_auth_method(context) or args.auth_method

    auth_method = args.auth_method or DEFAULT_AUTH_METHOD
    base_cfg = load_demo_base_config(args.source_config, args.target_config)
    plan = []
    full_demo_plan = build_demo_destroy_plan()
    if not args.skip_source:
        plan.append(next(item for item in full_demo_plan if item["label"] == "source"))
    if not args.skip_target:
        plan.append(next(item for item in full_demo_plan if item["label"] == "target"))
    confirm_destroy(plan, auto_confirm=args.yes)

    result: Dict[str, Dict[str, Any]] = {}

    if not args.skip_source:
        log.info("== Stage 1: destroy workspace %s ==", demo_source_workspace_name())
        result["source"] = destroy_workspace(base_cfg, demo_source_workspace_name(), auth_method)
    if not args.skip_target:
        log.info("== Stage 2: destroy workspace %s ==", demo_target_workspace_name())
        result["target"] = destroy_workspace(base_cfg, demo_target_workspace_name(), auth_method)

    log_debug_context("Destroy result payload", result=result)
    log_destroy_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_with_logged_errors(main))
