#!/usr/bin/env python3
"""Publish and deploy the transport bundle into the target workspace."""

from __future__ import annotations

import argparse
import logging
import os
from typing import Optional, Sequence

from core.console_logging import run_with_logged_errors, setup_logging
from core.contexts import config_paths_for_context, context_auth_method, context_demo_mode, load_context
from core.settings import DEFAULT_AUTH_METHOD, DEFAULT_DEMO_TARGET_CONFIG_PATH, DEFAULT_TARGET_CONFIG_PATH
from cicd_deploy import load_config
from core.publish_flow import log_publish_target_summary, publish_target

log = logging.getLogger("cicd")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context")
    parser.add_argument("--target-config")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--auth-method", default=None)
    parser.add_argument(
        "--commit-message",
        default="chore: publish target workspace transport bundle",
    )
    args = parser.parse_args(argv)
    setup_logging("cicd-publish-target")
    context_name = args.context
    context_demo = False
    if context_name:
        context = load_context(context_name)
        context_demo = context_demo_mode(context)
        if not args.auth_method:
            args.auth_method = context_auth_method(context) or args.auth_method
        if not args.target_config:
            _, args.target_config = config_paths_for_context(context_name, demo_mode=context_demo)
    if not args.target_config:
        args.target_config = DEFAULT_DEMO_TARGET_CONFIG_PATH if (bool(args.demo) or context_demo) else DEFAULT_TARGET_CONFIG_PATH
    if not os.path.exists(args.target_config):
        raise RuntimeError(
            "Target config file not found: {}. Provide --target-config if needed.".format(args.target_config)
        )
    target_cfg = load_config(args.target_config)
    result = publish_target(
        target_cfg,
        auth_method=args.auth_method or DEFAULT_AUTH_METHOD,
        commit_message=args.commit_message,
    )
    log_publish_target_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_with_logged_errors(main))
