#!/usr/bin/env python3
"""Central configuration shared by the CI/CD workspace transport scripts."""

from __future__ import annotations

import configparser
import os
from typing import List


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_INI_PATH = os.path.join(SCRIPT_DIR, "cicd-orchestrator.ini")

_DEFAULT_INI = {
    "logging": {
        "max_log_files_per_command": "5",
    },
    "defaults": {
        "source_config_path": "source-workspace.yaml",
        "target_config_path": "target-workspace.yaml",
        "auth_method": "api_key",
    },
    "aidp": {
        "path_prefix": "dataLakes",
        "bundle_path_prefix": "dataLakes",
        "bundle_api_version": "20240831",
        "deploy_bundle_path_prefix": "dataLakes",
        "deploy_bundle_api_version": "20240831",
        "verify_tls": "true",
        "http_timeout_secs": "60",
        "poll_http_timeout_secs": "10",
        "poll_interval_secs": "5",
        "poll_timeout_secs": "900",
    },
    "git": {
        "branch": "main",
        "parent_dir": "/Workspace/cicd",
        "bundle_name": "bundle",
        "stage_bundle_name": "bundle_stage",
    },
    "prepare": {
        "bootstrap_job_name": "cicd_deploy_bundle_bootstrap_workflow",
        "bootstrap_job_description": "Temporary workflow used to bootstrap the target deploy bundle shell",
        "bootstrap_job_path": "jobs",
        "workspace_item_type_timeout_secs": "60",
        "source_workspace_description": "Source workspace used by the CI/CD transport orchestrator flow",
        "target_workspace_description": "Target workspace used by the CI/CD transport orchestrator flow",
    },
    "promote": {
        "deploy_bundle_preserve_names": ".aidp,aidp_workbench.yaml,.gitignore",
        "git_commit_stabilization_secs": "20",
        "cleanup_retry_delays_secs": "5,10,15",
        "copy_cleanup_retry_delays_secs": "5,10,15",
        "bundle_create_retry_delays_secs": "5,10,15",
        "spinner_frame_interval_secs": "0.2",
    },
    "deploy": {
        "git_operation_parse_retry_delays_secs": "5,10,15",
    },
}


def _load_parser() -> configparser.ConfigParser:
    """Load the INI once, applying hardcoded defaults before local overrides."""
    parser = configparser.ConfigParser()
    parser.read_dict(_DEFAULT_INI)
    if os.path.exists(SETTINGS_INI_PATH):
        parser.read(SETTINGS_INI_PATH, encoding="utf-8")
    return parser


_PARSER = _load_parser()


def get_str(section: str, key: str, fallback: str) -> str:
    return _PARSER.get(section, key, fallback=fallback).strip()


def get_int(section: str, key: str, fallback: int) -> int:
    try:
        value = _PARSER.getint(section, key, fallback=fallback)
    except ValueError:
        return fallback
    return value


def get_float(section: str, key: str, fallback: float) -> float:
    try:
        return _PARSER.getfloat(section, key, fallback=fallback)
    except ValueError:
        return fallback


def get_bool(section: str, key: str, fallback: bool) -> bool:
    try:
        return _PARSER.getboolean(section, key, fallback=fallback)
    except ValueError:
        return fallback


def get_csv(section: str, key: str, fallback: str) -> List[str]:
    raw = get_str(section, key, fallback)
    return [item.strip() for item in raw.split(",") if item.strip()]


def get_int_csv(section: str, key: str, fallback: str) -> List[int]:
    values: List[int] = []
    for item in get_csv(section, key, fallback):
        try:
            values.append(int(item))
        except ValueError:
            continue
    if values:
        return values
    return [int(item) for item in fallback.split(",") if item.strip()]


DEFAULT_SOURCE_CONFIG_PATH = os.path.abspath(get_str("defaults", "source_config_path", "source-workspace.yaml"))
DEFAULT_TARGET_CONFIG_PATH = os.path.abspath(get_str("defaults", "target_config_path", "target-workspace.yaml"))
DEFAULT_DEMO_SOURCE_CONFIG_PATH = os.path.abspath("source-workspace.demo.yaml")
DEFAULT_DEMO_TARGET_CONFIG_PATH = os.path.abspath("target-workspace.demo.yaml")
DEFAULT_AUTH_METHOD = get_str("defaults", "auth_method", "api_key")

MAX_LOG_FILES_PER_COMMAND = get_int("logging", "max_log_files_per_command", 5)

DEFAULT_PATH_PREFIX = get_str("aidp", "path_prefix", "dataLakes")
DEFAULT_BUNDLE_PATH_PREFIX = get_str("aidp", "bundle_path_prefix", DEFAULT_PATH_PREFIX)
DEFAULT_BUNDLE_API_VERSION = get_str("aidp", "bundle_api_version", "20240831")
DEFAULT_DEPLOY_BUNDLE_PATH_PREFIX = get_str("aidp", "deploy_bundle_path_prefix", DEFAULT_PATH_PREFIX)
DEFAULT_DEPLOY_BUNDLE_API_VERSION = get_str("aidp", "deploy_bundle_api_version", "20240831")
DEFAULT_VERIFY_TLS = get_bool("aidp", "verify_tls", True)
DEFAULT_HTTP_TIMEOUT_SECS = get_int("aidp", "http_timeout_secs", 60)
DEFAULT_POLL_HTTP_TIMEOUT_SECS = get_int("aidp", "poll_http_timeout_secs", 10)
DEFAULT_POLL_INTERVAL_SECS = get_int("aidp", "poll_interval_secs", 5)
DEFAULT_POLL_TIMEOUT_SECS = get_int("aidp", "poll_timeout_secs", 900)

DEFAULT_GIT_BRANCH = get_str("git", "branch", "main")
DEFAULT_GIT_PARENT_DIR = get_str("git", "parent_dir", "/Workspace/cicd")
DEFAULT_BUNDLE_NAME = get_str("git", "bundle_name", "bundle")
DEFAULT_STAGE_BUNDLE_NAME = get_str("git", "stage_bundle_name", "bundle_stage")

PREPARE_BOOTSTRAP_JOB_NAME = get_str(
    "prepare",
    "bootstrap_job_name",
    "cicd_deploy_bundle_bootstrap_workflow",
)
PREPARE_BOOTSTRAP_JOB_DESCRIPTION = get_str(
    "prepare",
    "bootstrap_job_description",
    "Temporary workflow used to bootstrap the target deploy bundle shell",
)
PREPARE_BOOTSTRAP_JOB_PATH = get_str("prepare", "bootstrap_job_path", "jobs")
PREPARE_WORKSPACE_ITEM_TYPE_TIMEOUT_SECS = get_int("prepare", "workspace_item_type_timeout_secs", 60)
PREPARE_SOURCE_WORKSPACE_DESCRIPTION = get_str(
    "prepare",
    "source_workspace_description",
    "Source workspace used by the CI/CD transport orchestrator flow",
)
PREPARE_TARGET_WORKSPACE_DESCRIPTION = get_str(
    "prepare",
    "target_workspace_description",
    "Target workspace used by the CI/CD transport orchestrator flow",
)

PROMOTE_DEPLOY_BUNDLE_PRESERVE_NAMES = tuple(
    get_csv("promote", "deploy_bundle_preserve_names", ".aidp,aidp_workbench.yaml,.gitignore")
)
PROMOTE_GIT_COMMIT_STABILIZATION_SECS = get_int("promote", "git_commit_stabilization_secs", 20)
PROMOTE_CLEANUP_RETRY_DELAYS_SECS = get_int_csv("promote", "cleanup_retry_delays_secs", "5,10,15")
PROMOTE_COPY_CLEANUP_RETRY_DELAYS_SECS = get_int_csv("promote", "copy_cleanup_retry_delays_secs", "5,10,15")
PROMOTE_BUNDLE_CREATE_RETRY_DELAYS_SECS = get_int_csv("promote", "bundle_create_retry_delays_secs", "5,10,15")
PROMOTE_SPINNER_FRAME_INTERVAL_SECS = get_float("promote", "spinner_frame_interval_secs", 0.2)

DEPLOY_GIT_OPERATION_PARSE_RETRY_DELAYS_SECS = get_int_csv(
    "deploy",
    "git_operation_parse_retry_delays_secs",
    "5,10,15",
)
