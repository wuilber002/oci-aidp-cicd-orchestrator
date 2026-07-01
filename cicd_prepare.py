#!/usr/bin/env python3
"""Prepare the source or target workspace used by the transport flow."""

from __future__ import annotations

import argparse
import logging
import os
from types import SimpleNamespace
from typing import Any, Dict, Optional, Sequence

import yaml
from core.console_logging import LOGGER_NAME, log_phase_header, poll_with_progress, run_with_logged_errors, setup_logging
from core.contexts import config_paths_for_context, context_auth_method, context_demo_mode, context_role_settings, load_context
from core.settings import (
    DEFAULT_AUTH_METHOD,
    DEFAULT_BUNDLE_API_VERSION,
    DEFAULT_BUNDLE_NAME,
    DEFAULT_BUNDLE_PATH_PREFIX,
    DEFAULT_DEPLOY_BUNDLE_API_VERSION,
    DEFAULT_DEPLOY_BUNDLE_PATH_PREFIX,
    DEFAULT_DEMO_SOURCE_CONFIG_PATH,
    DEFAULT_DEMO_TARGET_CONFIG_PATH,
    DEFAULT_GIT_BRANCH,
    DEFAULT_GIT_PARENT_DIR,
    DEFAULT_HTTP_TIMEOUT_SECS,
    DEFAULT_PATH_PREFIX,
    DEFAULT_POLL_HTTP_TIMEOUT_SECS,
    DEFAULT_POLL_INTERVAL_SECS,
    DEFAULT_POLL_TIMEOUT_SECS,
    DEFAULT_SOURCE_CONFIG_PATH,
    DEFAULT_STAGE_BUNDLE_NAME,
    DEFAULT_TARGET_CONFIG_PATH,
    DEFAULT_VERIFY_TLS,
    PREPARE_BOOTSTRAP_JOB_DESCRIPTION,
    PREPARE_BOOTSTRAP_JOB_NAME,
    PREPARE_BOOTSTRAP_JOB_PATH,
    PREPARE_SOURCE_WORKSPACE_DESCRIPTION,
    PREPARE_TARGET_WORKSPACE_DESCRIPTION,
    PREPARE_WORKSPACE_ITEM_TYPE_TIMEOUT_SECS,
)

from cicd_deploy import (
    AidpClient,
    _async_key,
    _git_repo_key,
    apply_config_defaults,
    build_signer,
    load_config,
    log_debug_context,
    resolve_folder_path,
    resolve_versioned_bundle_path,
)
from core.publish_flow import (
    BUNDLE_METADATA_NAME,
    BUNDLE_DEPLOY_PRESERVE_NAMES,
    classify_workspace_folder,
    ensure_children_absent,
    ensure_path_absent,
    rest_create_bundle,
    workspace_item,
    _workspace_item_type,
)

log = logging.getLogger(LOGGER_NAME)
DEMO_GIT_CREDENTIAL_NAME = "demo_cicd"
DEMO_SOURCE_WORKSPACE_PREFIX = "demo-cicd-source"
DEMO_TARGET_WORKSPACE_PREFIX = "demo-cicd-target"
DEMO_DESCRIPTION_PREFIX = "DEMO - DO NOT USE IN PRODUCTION"
DEMO_FIXED_SUFFIX = "1604"


def default_demo_suffix() -> str:
    return DEMO_FIXED_SUFFIX


def build_demo_workspace_name(prefix: str, suffix: str) -> str:
    return "{}-{}".format(prefix, suffix)


def apply_demo_profile(cfg: Dict[str, Any], *, mode: str, suffix: str) -> Dict[str, Any]:
    workspace_prefix = DEMO_SOURCE_WORKSPACE_PREFIX if mode == "source" else DEMO_TARGET_WORKSPACE_PREFIX
    cfg.setdefault("aidp", {})
    cfg.setdefault("git", {})
    cfg.setdefault("options", {})
    cfg["aidp"]["workspace_name"] = build_demo_workspace_name(workspace_prefix, suffix)
    cfg["aidp"]["workspace_description"] = "{} - {}".format(
        DEMO_DESCRIPTION_PREFIX,
        "Source workspace for demo transport validation" if mode == "source" else "Target workspace for demo transport validation",
    )
    cfg["git"]["credential_name"] = DEMO_GIT_CREDENTIAL_NAME
    cfg["options"]["demo_mode"] = True
    cfg["options"]["demo_suffix"] = suffix
    return cfg


def source_config_output_path(*, demo: bool = False) -> str:
    return DEFAULT_DEMO_SOURCE_CONFIG_PATH if demo else DEFAULT_SOURCE_CONFIG_PATH


def target_config_output_path(*, demo: bool = False) -> str:
    return DEFAULT_DEMO_TARGET_CONFIG_PATH if demo else DEFAULT_TARGET_CONFIG_PATH


def source_config_output_path_for_context(context_name: Optional[str], *, demo: bool = False) -> str:
    if context_name:
        return config_paths_for_context(context_name, demo_mode=demo)[0]
    return source_config_output_path(demo=demo)


def target_config_output_path_for_context(context_name: Optional[str], *, demo: bool = False) -> str:
    if context_name:
        return config_paths_for_context(context_name, demo_mode=demo)[1]
    return target_config_output_path(demo=demo)


def pruned_persisted_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    cfg = yaml.safe_load(yaml.safe_dump(payload, sort_keys=False)) or {}
    aidp = cfg.get("aidp") or {}
    git = cfg.get("git") or {}
    options = cfg.get("options") or {}

    default_aidp_values = {
        "path_prefix": DEFAULT_PATH_PREFIX,
        "bundle_path_prefix": DEFAULT_BUNDLE_PATH_PREFIX,
        "bundle_api_version": DEFAULT_BUNDLE_API_VERSION,
        "deploy_bundle_path_prefix": DEFAULT_DEPLOY_BUNDLE_PATH_PREFIX,
        "deploy_bundle_api_version": DEFAULT_DEPLOY_BUNDLE_API_VERSION,
    }
    default_git_values = {
        "branch": DEFAULT_GIT_BRANCH,
        "parent_dir": DEFAULT_GIT_PARENT_DIR,
        "bundle_path": DEFAULT_BUNDLE_NAME,
        "stage_bundle_path": DEFAULT_STAGE_BUNDLE_NAME,
    }
    default_option_values = {
        "http_timeout_secs": DEFAULT_HTTP_TIMEOUT_SECS,
        "poll_http_timeout_secs": DEFAULT_POLL_HTTP_TIMEOUT_SECS,
        "poll_interval_secs": DEFAULT_POLL_INTERVAL_SECS,
        "poll_timeout_secs": DEFAULT_POLL_TIMEOUT_SECS,
        "verify_tls": DEFAULT_VERIFY_TLS,
    }
    for key, value in default_aidp_values.items():
        if aidp.get(key) == value:
            aidp.pop(key, None)
    for key, value in default_git_values.items():
        if git.get(key) == value:
            git.pop(key, None)
    for key, value in default_option_values.items():
        if options.get(key) == value:
            options.pop(key, None)
    if not options:
        cfg.pop("options", None)
    else:
        cfg["options"] = options
    cfg["aidp"] = aidp
    cfg["git"] = git
    return cfg


def write_yaml(path: str, payload: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(pruned_persisted_config(payload), handle, sort_keys=False)


def load_yaml_if_present(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        return None
    return payload


def resolve_demo_suffix(
    config_path: str,
    *,
    inherited_suffix: str = "",
) -> tuple[str, bool]:
    existing_cfg = load_yaml_if_present(config_path) or {}
    existing_options = existing_cfg.get("options") or {}
    existing_suffix = str(existing_options.get("demo_suffix") or "").strip()
    existing_demo_mode = bool(existing_options.get("demo_mode"))
    if existing_demo_mode and existing_suffix == DEMO_FIXED_SUFFIX:
        return existing_suffix, True
    inherited = str(inherited_suffix or "").strip()
    if inherited and inherited != DEMO_FIXED_SUFFIX:
        inherited = DEMO_FIXED_SUFFIX
    if inherited:
        return inherited, False
    return default_demo_suffix(), False


def _looks_like_repository_url(value: str) -> bool:
    candidate = str(value or "").strip()
    return candidate.startswith(("https://", "http://", "git@"))


def run_prepare_preflight(client: AidpClient, cfg: Dict[str, Any]) -> str:
    workspace_name = str((cfg.get("aidp") or {}).get("workspace_name") or "").strip()
    credential_name = str((cfg.get("git") or {}).get("credential_name") or "").strip()
    repository_url = str((cfg.get("git") or {}).get("repository_url") or "").strip()

    if not workspace_name:
        raise RuntimeError("workspace_name cannot be empty")
    if not credential_name:
        raise RuntimeError("git.credential_name cannot be empty")
    if not repository_url:
        raise RuntimeError("git.repository_url cannot be empty")
    if not _looks_like_repository_url(repository_url):
        raise RuntimeError("git.repository_url does not look valid: {}".format(repository_url))
    log.info("Validating workspace Git credential")
    credential_key = client.resolve_git_credential_key(credential_name)
    log.info("Prepare preflight validated")
    return credential_key


def list_all(client: AidpClient, path: str) -> list[Dict[str, Any]]:
    resp = client.request_ok("GET", client.ws_url(path), ok=(200,))
    payload = resp.json()
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        return [item for item in payload["items"] if isinstance(item, dict)]
    return []


def find_job_by_name(client: AidpClient, name: str) -> Optional[Dict[str, Any]]:
    for item in list_all(client, "jobs"):
        if item.get("name") == name:
            return item
    return None


def get_job(client: AidpClient, key: str) -> Dict[str, Any]:
    return client.request_ok("GET", client.ws_url("jobs", key), ok=(200,)).json()


def delete_job_if_present(client: AidpClient, name: str) -> bool:
    existing = find_job_by_name(client, name)
    if not existing:
        return False
    resp = client.request("DELETE", client.ws_url("jobs", existing["key"]))
    if resp.status_code == 404:
        return False
    if resp.status_code not in (200, 202, 204):
        raise RuntimeError("delete job {} -> HTTP {}: {}".format(name, resp.status_code, resp.text))
    key = _async_key(resp)
    if key:
        client.wait_for_async(key, purpose="Temporary bootstrap workflow removal")
    return True


def build_bootstrap_job_spec_minimal() -> Dict[str, Any]:
    return {
        "name": PREPARE_BOOTSTRAP_JOB_NAME,
        "description": PREPARE_BOOTSTRAP_JOB_DESCRIPTION,
        "path": PREPARE_BOOTSTRAP_JOB_PATH,
        "maxConcurrentRuns": 1,
        "schedule": None,
        "parameters": [],
        "jobClusters": [],
        "tasks": [],
    }


def create_bootstrap_job(client: AidpClient) -> Dict[str, Any]:
    log.info("Creating temporary bootstrap workflow using empty-workflow strategy")
    job = create_or_update_job(client, build_bootstrap_job_spec_minimal())
    log.info("Temporary bootstrap workflow created using empty-workflow strategy")
    return job


def create_or_update_job(client: AidpClient, spec: Dict[str, Any]) -> Dict[str, Any]:
    existing = find_job_by_name(client, spec["name"])
    if not existing:
        log.info("creating temporary job resource: %s", spec["name"])
        resp = client.request("POST", client.ws_url("jobs"), body=spec)
        if resp.status_code not in (200, 201, 202):
            raise RuntimeError("create job {} -> HTTP {}: {}".format(spec["name"], resp.status_code, resp.text))
        key = _async_key(resp)
        if key:
            client.wait_for_async(key, purpose="Temporary bootstrap workflow creation")
        created = find_job_by_name(client, spec["name"])
        if not created:
            raise RuntimeError("job {} created but not found afterwards".format(spec["name"]))
        return get_job(client, created["key"])
    log.info("updating temporary job resource: %s", spec["name"])
    current = get_job(client, existing["key"])
    body = {**current, **spec}
    resp = client.request_ok("PUT", client.ws_url("jobs", existing["key"]), body=body, ok=(200, 202))
    key = _async_key(resp)
    if key:
        client.wait_for_async(key, purpose="Temporary bootstrap workflow update")
    return get_job(client, existing["key"])


def wait_for_workspace_item_type(
    client: AidpClient,
    object_path: str,
    expected_type: str,
    purpose: str,
    timeout_secs: int = PREPARE_WORKSPACE_ITEM_TYPE_TIMEOUT_SECS,
) -> Dict[str, Any]:
    expected = expected_type.upper()
    def fetch_item() -> Dict[str, Any]:
        item = workspace_item(client, object_path)
        current_type = _workspace_item_type(item) if item else "ABSENT"
        return {
            "item": item,
            "current_type": current_type or "ABSENT",
        }

    def timeout_message(payload: Dict[str, Any], _elapsed: int) -> str:
        parent = os.path.dirname(object_path.rstrip("/")) or "/"
        try:
            sibling_names = [
                item.get("name") or os.path.basename(str(item.get("path") or item.get("objectPath") or ""))
                for item in client.list_workspace_objects(parent)
            ]
        except Exception:
            sibling_names = []
        return "{}: {} was not visible as {} within the expected time window; last observed type={} sibling items={}".format(
            purpose,
            object_path,
            expected,
            payload.get("current_type") or "ABSENT",
            sorted(name for name in sibling_names if name),
        )

    payload = poll_with_progress(
        purpose,
        timeout_secs=max(timeout_secs, client.poll_interval),
        fetch_interval_secs=client.poll_interval,
        fetch_fn=fetch_item,
        success_fn=lambda payload: bool(payload.get("item")) and str(payload.get("current_type") or "").upper() == expected,
        progress_suffix_fn=lambda payload: "tipo={}".format(payload.get("current_type") or "ABSENT"),
        timeout_message_fn=timeout_message,
        logger=log,
    )
    return payload["item"]


def bundle_metadata_path(bundle_path: str) -> str:
    return bundle_path.rstrip("/") + "/" + BUNDLE_METADATA_NAME


def has_bundle_metadata(client: AidpClient, bundle_path: str) -> bool:
    return workspace_item(client, bundle_metadata_path(bundle_path)) is not None


def ensure_target_deploy_bundle(client: AidpClient, cfg: Dict[str, Any]) -> str:
    bundle_path = resolve_versioned_bundle_path(cfg)
    workspace_name = str(cfg.get("aidp", {}).get("workspace_name") or "workspace").strip() or "workspace"
    item = workspace_item(client, bundle_path)
    item_type = _workspace_item_type(item) if item else ""
    metadata_present = has_bundle_metadata(client, bundle_path) if item else False
    facts = classify_workspace_folder(client, bundle_path) if item else {}
    classification = str(facts.get("classification") or "")
    log_debug_context(
        "Ensure target deploy bundle context",
        workspace_name=cfg.get("aidp", {}).get("workspace_name"),
        workspace_key=cfg.get("aidp", {}).get("workspace_key"),
        bundle_path=bundle_path,
        bundle_parent=os.path.dirname(bundle_path.rstrip("/")),
        item=item,
        item_type=item_type,
        metadata_present=metadata_present,
        classification=classification,
        facts=facts,
    )
    if item and (classification == "bundle_folder" or item_type == "BUNDLE" or metadata_present):
        if item_type == "BUNDLE":
            log.info("Deploy bundle already exists in workspace %s: %s", workspace_name, bundle_path)
        elif classification == "bundle_folder":
            log.info("Deploy bundle identified by local content/metadata in workspace %s: %s", workspace_name, bundle_path)
        else:
            log.warning(
                "Target %s was reported as %s, but it already contains %s; treating it as a valid deploy bundle shell",
                bundle_path,
                item_type or "UNKNOWN",
                BUNDLE_METADATA_NAME,
            )
        ensure_children_absent(client, bundle_path, preserve_names=BUNDLE_DEPLOY_PRESERVE_NAMES)
        return bundle_path

    bundle_parent = os.path.dirname(bundle_path.rstrip("/"))
    bundle_name = os.path.basename(bundle_path.rstrip("/"))

    log.info("Ensuring deploy bundle shell in workspace %s: %s", workspace_name, bundle_path)
    try:
        job = create_bootstrap_job(client)

        if item:
            log.warning("Target %s exists as %s; recreating the deploy bundle", bundle_path, _workspace_item_type(item))
            ensure_path_absent(client, bundle_path)

        resp = rest_create_bundle(
            client,
            bundle_name,
            bundle_parent,
            [{"resourceKey": job["key"], "resourceType": "JOB"}],
        )
        key = _async_key(resp)
        if key:
            client.wait_for_async(key, purpose="Deploy bundle creation")

        wait_for_workspace_item_type(
            client,
            bundle_path,
            "BUNDLE",
            "Deploy bundle creation confirmation",
        )
        ensure_children_absent(client, bundle_path, preserve_names=BUNDLE_DEPLOY_PRESERVE_NAMES)
        return bundle_path
    finally:
        delete_job_if_present(client, PREPARE_BOOTSTRAP_JOB_NAME)


def build_source_config(args, *, demo_suffix: str = "") -> Dict[str, Any]:
    cfg = apply_config_defaults({
        "aidp": {
            "region": args.region,
            "ocid": args.aidp_ocid,
            "workspace_name": args.workspace_name,
            "workspace_description": args.workspace_description,
        },
        "git": {
            "repository_url": args.repository_url,
            "branch": args.branch,
            "parent_dir": args.parent_dir,
            "bundle_path": args.bundle_name,
            "stage_bundle_path": args.stage_bundle_name,
            "credential_name": args.credential_name,
        },
        "options": {},
    })
    if args.demo:
        if not str(demo_suffix or "").strip():
            raise RuntimeError("demo_suffix is required to build the demo source configuration")
        cfg = apply_demo_profile(cfg, mode="source", suffix=demo_suffix)
    return cfg


def build_target_config(
    args,
    *,
    demo: bool = False,
    demo_suffix: str = "",
) -> Dict[str, Any]:
    cfg = apply_config_defaults({
        "aidp": {
            "region": args.region,
            "ocid": args.aidp_ocid,
            "workspace_name": args.workspace_name,
            "workspace_description": args.workspace_description,
        },
        "git": {
            "repository_url": args.repository_url,
            "branch": args.branch,
            "parent_dir": args.parent_dir,
            "bundle_path": args.bundle_name,
            "stage_bundle_path": args.stage_bundle_name,
            "credential_name": args.credential_name,
        },
        "options": {},
    })
    if demo:
        suffix = str(demo_suffix or "").strip()
        if not suffix:
            raise RuntimeError("demo_suffix is required to build the demo target configuration")
        cfg = apply_demo_profile(cfg, mode="target", suffix=suffix)
    return cfg


def _context_value(block: Dict[str, Any], section: str, key: str, fallback: Any = None) -> Any:
    return ((block.get(section) or {}).get(key)) if isinstance(block.get(section), dict) else fallback


def _build_source_args_from_context(args, context_name: str) -> tuple[Any, bool]:
    context = load_context(context_name)
    role = context_role_settings(context, "source")
    demo_mode = context_demo_mode(context)
    return (
        SimpleNamespace(
            demo=demo_mode,
            region=_context_value(role, "aidp", "region", args.region),
            aidp_ocid=_context_value(role, "aidp", "ocid", args.aidp_ocid),
            workspace_name=_context_value(role, "aidp", "workspace_name", args.workspace_name),
            workspace_description=_context_value(role, "aidp", "workspace_description", args.workspace_description),
            repository_url=_context_value(role, "git", "repository_url", args.repository_url),
            branch=_context_value(role, "git", "branch", args.branch),
            parent_dir=_context_value(role, "git", "parent_dir", args.parent_dir),
            bundle_name=_context_value(role, "git", "bundle_path", args.bundle_name),
            stage_bundle_name=_context_value(role, "git", "stage_bundle_path", args.stage_bundle_name),
            credential_name=_context_value(role, "git", "credential_name", args.credential_name),
            _context_payload=role,
            _context_name=context_name,
        ),
        demo_mode,
    )


def _build_target_args_from_context(args, context_name: str) -> tuple[Any, bool]:
    context = load_context(context_name)
    role = context_role_settings(context, "target")
    demo_mode = context_demo_mode(context)
    return (
        SimpleNamespace(
            demo=demo_mode,
            region=_context_value(role, "aidp", "region", getattr(args, "region", None)),
            aidp_ocid=_context_value(role, "aidp", "ocid", getattr(args, "aidp_ocid", None)),
            workspace_name=_context_value(role, "aidp", "workspace_name", args.workspace_name),
            workspace_description=_context_value(role, "aidp", "workspace_description", args.workspace_description),
            repository_url=_context_value(role, "git", "repository_url", getattr(args, "repository_url", None)),
            branch=_context_value(role, "git", "branch", getattr(args, "branch", None)),
            parent_dir=_context_value(role, "git", "parent_dir", getattr(args, "parent_dir", None)),
            bundle_name=_context_value(role, "git", "bundle_path", getattr(args, "bundle_name", None)),
            stage_bundle_name=_context_value(role, "git", "stage_bundle_path", getattr(args, "stage_bundle_name", None)),
            credential_name=_context_value(role, "git", "credential_name", getattr(args, "credential_name", None)),
            _context_payload=role,
            _context_name=context_name,
        ),
        demo_mode,
    )


def _apply_context_sections(cfg: Dict[str, Any], args) -> Dict[str, Any]:
    role = getattr(args, "_context_payload", None)
    if not isinstance(role, dict):
        return cfg
    for section in ("aidp", "git", "options"):
        current = dict(cfg.get(section) or {})
        current.update(role.get(section) or {})
        if current:
            cfg[section] = current
    return cfg


def ensure_source_git_folder(client: AidpClient, cfg: Dict[str, Any], credential_key: str) -> str:
    folder_path = resolve_folder_path(cfg)
    metadata = client.get_git_repository(folder_path, should_include_credential_key=False)
    association = client.git_folder_association(folder_path)
    repo_key = _git_repo_key(metadata) or _git_repo_key(association)
    folder_exists = client.workspace_object_exists(folder_path)
    associated = bool(repo_key)
    broken_association = bool(folder_exists and not repo_key)
    clone_needed = bool(not folder_exists and not repo_key)

    if broken_association:
        log.warning(
            "git folder %s is in a broken state: folder exists but repository key is unavailable; recreating it",
            folder_path,
        )
        deleted = client.delete_workspace_object_if_present(folder_path)
        if deleted:
            log.info("Broken local folder removed before recreating the git folder")
        else:
            log.warning(
                "could not confirm deletion of broken folder %s; attempting git-folder recreation anyway",
                folder_path,
            )
        resp = client.create_git_folder(
            folder_path,
            cfg["git"]["repository_url"],
            cfg["git"]["branch"],
            credential_key,
        )
        async_key = getattr(resp, "headers", {}).get("datalake-async-operation-key") if hasattr(resp, "headers") else None
        if async_key:
            client.wait_for_async(async_key)
        return folder_path

    if clone_needed:
        log.info("Cloning repository %s on branch %s", cfg["git"]["repository_url"], cfg["git"]["branch"])
        resp = client.create_git_folder(
            folder_path,
            cfg["git"]["repository_url"],
            cfg["git"]["branch"],
            credential_key,
        )
        async_key = getattr(resp, "headers", {}).get("datalake-async-operation-key") if hasattr(resp, "headers") else None
        if async_key:
            client.wait_for_async(async_key)
        return folder_path
    if not repo_key:
        raise RuntimeError(
            "git folder {} is not valid: repository key is unavailable after reconciliation".format(folder_path)
        )
    client.ensure_git_folder_credential(folder_path, credential_key)
    log.info("Git folder ready for use")
    return folder_path


def prepare_workspace(cfg: Dict[str, Any], auth_method: str) -> Dict[str, Any]:
    signer = build_signer(auth_method)
    client = AidpClient(cfg, signer)
    workspace_name = cfg["aidp"]["workspace_name"]
    workspace_description = cfg.get("aidp", {}).get("workspace_description")
    log_debug_context(
        "Prepare workspace context",
        auth_method=auth_method,
        workspace_name=workspace_name,
        workspace_key=cfg.get("aidp", {}).get("workspace_key"),
        folder_path=resolve_folder_path(cfg),
        deploy_bundle_path=resolve_versioned_bundle_path(cfg),
        git=cfg.get("git", {}),
        aidp=cfg.get("aidp", {}),
    )
    total_phases = 5
    log_phase_header(0, "preflight", total_phases)
    log.info("Validating basic AIDP configuration")
    log.info("AIDP endpoint context ready")

    log_phase_header(1, "ensure workspace", total_phases)
    ensured_workspace = client.ensure_workspace(workspace_name, description=workspace_description)
    workspace = ensured_workspace["workspace"]
    cfg["aidp"]["workspace_key"] = str(workspace.get("key") or cfg["aidp"].get("workspace_key") or "")
    client.workspace_key = cfg["aidp"]["workspace_key"]

    log_phase_header(2, "resolve Git credential", total_phases)
    credential_key = run_prepare_preflight(client, cfg)

    log_phase_header(3, "ensure directory", total_phases)
    log.info("Ensuring base directory %s", cfg["git"]["parent_dir"])
    client.ensure_directory(cfg["git"]["parent_dir"], purpose="Base directory setup")

    log_phase_header(4, "git folder", total_phases)
    ensure_source_git_folder(client, cfg, credential_key)

    return {
        "workspace_name": workspace_name,
        "workspace_key": cfg["aidp"]["workspace_key"],
        "workspace_created": bool(ensured_workspace.get("created")),
        "folder_path": resolve_folder_path(cfg),
        "bundle_path": cfg["git"]["bundle_path"],
        "stage_bundle_path": cfg["git"].get("stage_bundle_path"),
        "deploy_bundle_path": resolve_versioned_bundle_path(cfg),
    }


def log_prepare_summary(mode: str, result: Dict[str, Any]) -> None:
    title = result.get("workspace_name") or ("source" if mode == "source" else "target")
    log.info("== Prepare summary (%s) ==", title)
    log.info("Workspace: %s", result.get("workspace_name"))
    log.info("Workspace key: %s", result.get("workspace_key"))
    log.info("Workspace created in this run: %s", "yes" if result.get("workspace_created") else "no")
    log.info("Git folder: %s", result.get("folder_path"))
    log.info("Deploy bundle: %s", result.get("deploy_bundle_path"))
    log.info("Stage bundle: %s", result.get("stage_bundle_path"))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--auth-method", default=None)
    sub = parser.add_subparsers(dest="mode", required=True)

    src = sub.add_parser("source")
    src.add_argument("--auth-method", default=None)
    src.add_argument("--context")
    src.add_argument("--demo", action="store_true")
    src.add_argument("--region", required=False)
    src.add_argument("--aidp-ocid", required=False)
    src.add_argument("--workspace-name", required=False)
    src.add_argument("--workspace-description", default=PREPARE_SOURCE_WORKSPACE_DESCRIPTION)
    src.add_argument("--repository-url", required=False)
    src.add_argument("--branch", default=DEFAULT_GIT_BRANCH)
    src.add_argument("--parent-dir", default=DEFAULT_GIT_PARENT_DIR)
    src.add_argument("--bundle-name", default=DEFAULT_BUNDLE_NAME)
    src.add_argument("--stage-bundle-name", default=DEFAULT_STAGE_BUNDLE_NAME)
    src.add_argument("--credential-name", required=False)

    tgt = sub.add_parser("target")
    tgt.add_argument("--auth-method", default=None)
    tgt.add_argument("--context")
    tgt.add_argument("--demo", action="store_true")
    tgt.add_argument("--region", required=False)
    tgt.add_argument("--aidp-ocid", required=False)
    tgt.add_argument("--workspace-name", required=False)
    tgt.add_argument("--workspace-description", default=PREPARE_TARGET_WORKSPACE_DESCRIPTION)
    tgt.add_argument("--repository-url", required=False)
    tgt.add_argument("--branch", default=DEFAULT_GIT_BRANCH)
    tgt.add_argument("--parent-dir", default=DEFAULT_GIT_PARENT_DIR)
    tgt.add_argument("--bundle-name", default=DEFAULT_BUNDLE_NAME)
    tgt.add_argument("--stage-bundle-name", default=DEFAULT_STAGE_BUNDLE_NAME)
    tgt.add_argument("--credential-name", required=False)

    args = parser.parse_args(argv)
    setup_logging("cicd-prepare")
    cli_auth_method = getattr(args, "auth_method", None)
    auth_method = cli_auth_method or DEFAULT_AUTH_METHOD

    if args.mode == "source":
        if getattr(args, "context", None):
            context = load_context(args.context)
            args, context_demo = _build_source_args_from_context(args, args.context)
            auth_method = cli_auth_method or context_auth_method(context) or DEFAULT_AUTH_METHOD
            source_cfg_path = source_config_output_path_for_context(args._context_name, demo=context_demo)
        else:
            context_demo = bool(args.demo)
            source_cfg_path = source_config_output_path(demo=context_demo)
        if args.demo:
            generated_suffix, reused_existing_suffix = resolve_demo_suffix(source_cfg_path)
            args.workspace_name = build_demo_workspace_name(DEMO_SOURCE_WORKSPACE_PREFIX, generated_suffix)
            args.workspace_description = "{} - Source workspace for demo transport validation".format(DEMO_DESCRIPTION_PREFIX)
            args.credential_name = DEMO_GIT_CREDENTIAL_NAME
            if reused_existing_suffix:
                log.info("Demo profile reused for source: suffix=%s credential=%s", generated_suffix, args.credential_name)
            else:
                log.info("Demo profile enabled for source: suffix=%s credential=%s", generated_suffix, args.credential_name)
        else:
            if not str(args.workspace_name or "").strip():
                raise RuntimeError("--workspace-name is required when --demo is not enabled")
            if not str(args.credential_name or "").strip():
                raise RuntimeError("--credential-name is required when --demo is not enabled")
            if not str(args.region or "").strip():
                raise RuntimeError("--region is required when --demo is not enabled")
            if not str(args.aidp_ocid or "").strip():
                raise RuntimeError("--aidp-ocid is required when --demo is not enabled")
            if not str(args.repository_url or "").strip():
                raise RuntimeError("--repository-url is required when --demo is not enabled")
        cfg = build_source_config(args, demo_suffix=generated_suffix if args.demo else "")
        cfg = _apply_context_sections(cfg, args)
        log.info("== Stage 0: prepare workspace %s ==", cfg["aidp"]["workspace_name"])
        if args.demo:
            write_yaml(source_cfg_path, cfg)
            log.debug("Source configuration file persisted before remote execution")
        result = prepare_workspace(cfg, auth_method)
        write_yaml(source_cfg_path, cfg)
        log.debug("Source configuration file updated")
        log.debug("Prepare source result payload: %s", result)
        log_prepare_summary("source", result)
        return 0

    context_name = getattr(args, "context", None)
    context_demo = False
    if context_name:
        context = load_context(context_name)
        context_demo = context_demo_mode(context)
        auth_method = cli_auth_method or context_auth_method(context) or DEFAULT_AUTH_METHOD
        args, context_demo = _build_target_args_from_context(args, context_name)
    target_cfg_path = target_config_output_path_for_context(context_name, demo=bool(args.demo) or context_demo)
    if args.demo:
        generated_suffix, reused_existing_suffix = resolve_demo_suffix(target_cfg_path)
        args.workspace_name = build_demo_workspace_name(DEMO_TARGET_WORKSPACE_PREFIX, generated_suffix)
        args.workspace_description = "{} - Target workspace for demo transport validation".format(DEMO_DESCRIPTION_PREFIX)
        if reused_existing_suffix:
            log.info("Demo profile reused for target: suffix=%s credential=%s", generated_suffix, DEMO_GIT_CREDENTIAL_NAME)
        else:
            log.info("Demo profile enabled for target: suffix=%s credential=%s", generated_suffix, DEMO_GIT_CREDENTIAL_NAME)
    elif not str(args.workspace_name or "").strip():
        if not str(args.workspace_name or "").strip():
            raise RuntimeError("--workspace-name is required when --demo is not enabled")
    if not str(getattr(args, "region", "") or "").strip():
        raise RuntimeError("--region is required for target prepare")
    if not str(getattr(args, "aidp_ocid", "") or "").strip():
        raise RuntimeError("--aidp-ocid is required for target prepare")
    if not str(getattr(args, "repository_url", "") or "").strip():
        raise RuntimeError("--repository-url is required for target prepare")
    if not str(getattr(args, "credential_name", "") or "").strip():
        raise RuntimeError("--credential-name is required for target prepare")

    cfg = build_target_config(
        args,
        demo=bool(args.demo),
        demo_suffix=generated_suffix if args.demo else "",
    )
    if context_name:
        cfg = _apply_context_sections(cfg, args)
    log.info("== Stage 0: prepare workspace %s ==", cfg["aidp"]["workspace_name"])
    if args.demo:
        write_yaml(target_cfg_path, cfg)
        log.debug("Target configuration file persisted before remote execution")
    result = prepare_workspace(cfg, auth_method)
    write_yaml(target_cfg_path, cfg)
    log.debug("Target configuration file updated")
    log.debug("Prepare target result payload: %s", result)
    log_prepare_summary("target", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_with_logged_errors(main))
