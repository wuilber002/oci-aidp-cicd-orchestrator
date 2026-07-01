#!/usr/bin/env python3
"""Publish transport content from the source workspace into the shared Git repository."""

from __future__ import annotations

import argparse
from typing import Optional, Sequence

from core.console_logging import run_with_logged_errors, setup_logging
from core.contexts import config_paths_for_context, context_auth_method, context_demo_mode, load_context
from core.settings import DEFAULT_AUTH_METHOD
from cicd_deploy import load_config
from core.publish_flow import (
    log_publish_source_summary,
    publish_source,
    resolve_config_paths,
)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context")
    parser.add_argument("--source-config")
    parser.add_argument("--target-config")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--auth-method", default=None)
    parser.add_argument(
        "--commit-message",
        default="chore: publish source workspace transport bundle",
    )
    parser.add_argument("--force-version-change", action="store_true")
    args = parser.parse_args(argv)
    setup_logging("cicd-publish-source")
    if args.force_version_change:
        from core.console_logging import LOGGER_NAME
        import logging
        logging.getLogger(LOGGER_NAME).warning("Force version-change mode is enabled for this publish-source run")
    context_name = args.context
    context_demo = False
    if context_name:
        context = load_context(context_name)
        context_demo = context_demo_mode(context)
        if not args.auth_method:
            args.auth_method = context_auth_method(context) or args.auth_method
        if not args.source_config and not args.target_config:
            args.source_config, args.target_config = config_paths_for_context(context_name, demo_mode=context_demo)
    source_path, target_path = resolve_config_paths(
        args.source_config,
        args.target_config,
        demo=bool(args.demo) or context_demo,
    )
    source_cfg = load_config(source_path)
    target_cfg = load_config(target_path)
    result = publish_source(
        source_cfg,
        target_cfg,
        auth_method=args.auth_method or DEFAULT_AUTH_METHOD,
        commit_message=args.commit_message,
        force_version_change=bool(args.force_version_change),
    )
    log_publish_source_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_with_logged_errors(main))
