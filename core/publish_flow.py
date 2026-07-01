"""Shared publish flow for source and target workspace transport operations."""

from __future__ import annotations

import json
import logging
import os
import posixpath
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

import yaml
from core.console_logging import LOGGER_NAME, format_remaining_br, run_with_logged_errors, setup_logging
from core.settings import (
    DEFAULT_AUTH_METHOD,
    DEFAULT_DEMO_SOURCE_CONFIG_PATH,
    DEFAULT_DEMO_TARGET_CONFIG_PATH,
    PREPARE_BOOTSTRAP_JOB_DESCRIPTION,
    PREPARE_BOOTSTRAP_JOB_NAME,
    PREPARE_BOOTSTRAP_JOB_PATH,
    DEFAULT_SOURCE_CONFIG_PATH,
    DEFAULT_STAGE_BUNDLE_NAME,
    DEFAULT_TARGET_CONFIG_PATH,
    PROMOTE_BUNDLE_CREATE_RETRY_DELAYS_SECS,
    PROMOTE_CLEANUP_RETRY_DELAYS_SECS,
    PROMOTE_COPY_CLEANUP_RETRY_DELAYS_SECS,
    PROMOTE_DEPLOY_BUNDLE_PRESERVE_NAMES,
    PROMOTE_GIT_COMMIT_STABILIZATION_SECS,
)
from cicd_deploy import (
    AidpClient,
    _async_key,
    _git_repo_key,
    build_signer,
    load_config,
    log_debug_context,
    phase0_credential,
    phase1_directory,
    phase2_git_folder,
    resolve_folder_path,
    resolve_stage_bundle_path,
    resolve_versioned_bundle_path,
)

log = logging.getLogger(LOGGER_NAME)
BUNDLE_METADATA_NAME = ".aidp"
BUNDLE_DEPLOY_PRESERVE_NAMES = PROMOTE_DEPLOY_BUNDLE_PRESERVE_NAMES
GIT_COMMIT_STABILIZATION_SECS = PROMOTE_GIT_COMMIT_STABILIZATION_SECS
UUID_SUFFIX_RE = re.compile(
    r"_[0-9a-f]{8}_[0-9a-f]{4}_[0-9a-f]{4}_[0-9a-f]{4}_[0-9a-f]{12}$",
    re.IGNORECASE,
)
FORCED_VERSION_CHANGE_MARKER_NAME = "forced-version-change.json"


def list_all(client: AidpClient, path: str) -> List[Dict[str, Any]]:
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


def create_or_update_bootstrap_job(client: AidpClient, spec: Dict[str, Any]) -> Dict[str, Any]:
    existing = find_job_by_name(client, spec["name"])
    if not existing:
        log.info("creating temporary bootstrap job: %s", spec["name"])
        resp = client.request("POST", client.ws_url("jobs"), body=spec)
        if resp.status_code not in (200, 201, 202):
            raise RuntimeError("create bootstrap job {} -> HTTP {}: {}".format(spec["name"], resp.status_code, resp.text))
        key = _async_key(resp)
        if key:
            client.wait_for_async(key, purpose="Bootstrap job creation")
        created = find_job_by_name(client, spec["name"])
        if not created:
            raise RuntimeError("bootstrap job {} created but not found afterwards".format(spec["name"]))
        return get_job(client, created["key"])
    log.info("updating temporary bootstrap job: %s", spec["name"])
    current = get_job(client, existing["key"])
    body = {**current, **spec}
    resp = client.request_ok("PUT", client.ws_url("jobs", existing["key"]), body=body, ok=(200, 202))
    key = _async_key(resp)
    if key:
        client.wait_for_async(key, purpose="Bootstrap job update")
    return get_job(client, existing["key"])


def delete_job_if_present(client: AidpClient, name: str) -> bool:
    existing = find_job_by_name(client, name)
    if not existing:
        return False
    resp = client.request("DELETE", client.ws_url("jobs", existing["key"]))
    if resp.status_code == 404:
        return False
    if resp.status_code not in (200, 202, 204):
        raise RuntimeError("delete bootstrap job {} -> HTTP {}: {}".format(name, resp.status_code, resp.text))
    key = _async_key(resp)
    if key:
        client.wait_for_async(key, purpose="Bootstrap job removal")
    return True


def default_source_config_path() -> str:
    return DEFAULT_SOURCE_CONFIG_PATH


def default_target_config_path() -> str:
    return DEFAULT_TARGET_CONFIG_PATH


def resolve_config_paths(source_path: Optional[str], target_path: Optional[str], demo: bool = False) -> tuple[str, str]:
    default_source = DEFAULT_DEMO_SOURCE_CONFIG_PATH if demo else DEFAULT_SOURCE_CONFIG_PATH
    default_target = DEFAULT_DEMO_TARGET_CONFIG_PATH if demo else DEFAULT_TARGET_CONFIG_PATH
    src = os.path.abspath(source_path or default_source)
    tgt = os.path.abspath(target_path or default_target)
    missing = [path for path in (src, tgt) if not os.path.exists(path)]
    if missing:
        raise RuntimeError(
            "Config file(s) not found: {}. Provide --source-config/--target-config if needed.".format(
                ", ".join(missing)
            )
        )
    return src, tgt


def sleep_with_spinner(seconds: int, label: str) -> None:
    total = max(int(seconds), 0)
    if total <= 0:
        return
    for remaining in range(total, 0, -1):
        log.info(label.format(seconds=format_remaining_br(remaining)))
        time.sleep(1)


def ensure_matching_git_identity(source_cfg: Dict[str, Any], target_cfg: Dict[str, Any]) -> None:
    for key in ("repository_url", "branch", "bundle_path"):
        if source_cfg["git"][key] != target_cfg["git"][key]:
            raise RuntimeError("source/target git.{} mismatch".format(key))
    source_stage = str((source_cfg.get("git") or {}).get("stage_bundle_path") or DEFAULT_STAGE_BUNDLE_NAME).strip("/")
    target_stage = str((target_cfg.get("git") or {}).get("stage_bundle_path") or DEFAULT_STAGE_BUNDLE_NAME).strip("/")
    if source_stage != target_stage:
        raise RuntimeError("source/target git.stage_bundle_path mismatch")
    if source_cfg["git"]["bundle_path"].strip("/") == source_stage:
        raise RuntimeError("git.bundle_path and git.stage_bundle_path must be different")


def ensure_source_git_folder(client: AidpClient, cfg: Dict[str, Any], credential_key: str) -> str:
    folder_path = resolve_folder_path(cfg)
    workspace_name = str(cfg.get("aidp", {}).get("workspace_name") or "workspace").strip() or "workspace"
    metadata = client.get_git_repository(folder_path, should_include_credential_key=False)
    association = client.git_folder_association(folder_path)
    repo_key = _git_repo_key(association) or _git_repo_key(metadata)
    folder_exists = client.workspace_object_exists(folder_path)
    associated = bool(repo_key)
    broken_association = bool(folder_exists and not repo_key)
    clone_needed = bool(not folder_exists and not repo_key)
    if broken_association:
        raise RuntimeError(
            "git folder {} is broken: folder exists but repository key is unavailable. "
            "Run cicd_prepare.py again after removing the regular folder, or let prepare repair it.".format(folder_path)
        )
    if clone_needed:
        log.info(
            "The Git folder for workspace %s does not exist; cloning repository %s (%s)",
            workspace_name,
            cfg["git"]["repository_url"],
            cfg["git"]["branch"],
        )
        resp = client.create_git_folder(
            folder_path, cfg["git"]["repository_url"], cfg["git"]["branch"], credential_key
        )
        key = _async_key(resp) if hasattr(resp, "headers") else None
        if key:
            client.wait_for_async(key, purpose="Workspace {} Git folder clone".format(workspace_name))
        return folder_path
    if not folder_exists and associated:
        log.info("The Git folder for workspace %s is already associated; keeping the current workspace state", workspace_name)
        return folder_path
    client.ensure_git_folder_credential(folder_path, credential_key)
    log.info("Updating the existing Git folder on branch %s", cfg["git"]["branch"])
    resp = client.git_pull(folder_path, cfg["git"]["branch"])
    key = _async_key(resp) if hasattr(resp, "headers") else None
    if key:
        client.wait_for_async(key, purpose="Workspace {} Git folder update".format(workspace_name))
    log.info("The Git folder for workspace %s is ready for use", workspace_name)
    return folder_path


def ensure_directory(client: AidpClient, path: str) -> None:
    client.ensure_directory(path)


def list_collection(client: AidpClient, path: str) -> List[Dict[str, Any]]:
    resp = client.request_ok("GET", client.ws_url(path), ok=(200,))
    payload = resp.json()
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        items = payload.get("items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def list_workspace_objects(client: AidpClient, parent_path: str) -> List[Dict[str, Any]]:
    try:
        resp = client.request_ok("GET", client.ws_url("objects"), params={"path": parent_path}, ok=(200,))
    except Exception as exc:
        message = str(exc)
        if "HTTP 404" in message and "Unknown resource" in message:
            return []
        raise
    payload = resp.json()
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        items = payload.get("items")
        if isinstance(items, list):
            return items
    return []


def get_job_details(client: AidpClient, key: str) -> Dict[str, Any]:
    return client.request_ok("GET", client.ws_url("jobs", key), ok=(200,)).json()


def extract_job_cluster_names(job_payload: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    for item in job_payload.get("jobClusters") or []:
        if not isinstance(item, dict):
            continue
        value = item.get("clusterName") or item.get("clusterDisplayName") or item.get("name")
        if isinstance(value, str) and value.strip():
            names.append(value.strip())
    for task in job_payload.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        cluster = task.get("cluster")
        if not isinstance(cluster, dict):
            continue
        value = cluster.get("clusterName") or cluster.get("clusterDisplayName") or cluster.get("name")
        if isinstance(value, str) and value.strip():
            names.append(value.strip())
    return sorted(set(names))


def collect_expected_deployed_resource_names(
    source_client: AidpClient,
    bundled_resources: Sequence[Dict[str, str]],
) -> Dict[str, Any]:
    jobs: List[Dict[str, Any]] = []
    cluster_names: List[str] = []
    for item in bundled_resources:
        if str(item.get("resourceType") or "").upper() != "JOB":
            continue
        key = item.get("resourceKey")
        if not key:
            continue
        payload = get_job_details(source_client, key)
        name = payload.get("name")
        if isinstance(name, str) and name.strip():
            current_clusters = extract_job_cluster_names(payload)
            jobs.append(
                {
                    "name": name.strip(),
                    "cluster_names": current_clusters,
                }
            )
            cluster_names.extend(current_clusters)
    return {
        "jobs": jobs,
        "clusters": sorted(set(cluster_names)),
    }


def _parse_job_name_from_bundle_file(bundle_job_path: str, payload: Dict[str, Any]) -> str:
    explicit = str(payload.get("name") or "").strip()
    if explicit:
        return explicit
    filename = posixpath.basename(bundle_job_path.rstrip("/"))
    for suffix in (".job.json", ".json", ".job", ".yaml", ".yml"):
        if filename.endswith(suffix):
            candidate = filename[: -len(suffix)].strip()
            if candidate:
                return candidate
    for candidate in (filename,):
        candidate = candidate.strip()
        if candidate:
            return candidate
    raise RuntimeError("could not derive a job name from bundle file {}".format(bundle_job_path))


def _parse_cluster_names_from_job_spec(payload: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    for item in payload.get("jobClusters") or []:
        if not isinstance(item, dict):
            continue
        value = item.get("clusterName") or item.get("clusterDisplayName") or item.get("name")
        if isinstance(value, str) and value.strip():
            names.append(value.strip())
    for task in payload.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        cluster = task.get("cluster")
        if not isinstance(cluster, dict):
            continue
        value = cluster.get("clusterName") or cluster.get("clusterDisplayName") or cluster.get("name")
        if isinstance(value, str) and value.strip():
            names.append(value.strip())
    return sorted(set(names))


def collect_expected_names_from_stage_bundle(client: AidpClient, stage_bundle_path: str) -> Dict[str, Any]:
    jobs_root = stage_bundle_path.rstrip("/") + "/jobs"
    job_items = list_workspace_objects(client, jobs_root)
    jobs: List[Dict[str, Any]] = []
    cluster_names: List[str] = []
    for item in job_items:
        item_type = _workspace_item_type(item)
        if item_type == "FOLDER":
            continue
        job_path = _workspace_item_path(item, jobs_root)
        if not job_path:
            continue
        raw = client.get_workspace_file_text(job_path)
        try:
            payload = json.loads(raw)
        except Exception:
            try:
                payload = yaml.safe_load(raw) or {}
            except Exception as exc:
                raise RuntimeError("could not parse bundle job file {}".format(job_path)) from exc
        if not isinstance(payload, dict):
            continue
        job_name = _parse_job_name_from_bundle_file(job_path, payload)
        current_clusters = _parse_cluster_names_from_job_spec(payload)
        jobs.append(
            {
                "name": job_name,
                "cluster_names": current_clusters,
            }
        )
        cluster_names.extend(current_clusters)
    if not jobs:
        raise RuntimeError("no job specifications were found under {}".format(jobs_root))
    summary = {
        "jobs": jobs,
        "clusters": sorted(set(cluster_names)),
    }
    log_debug_context(
        "Expected names derived from stage bundle",
        stage_bundle_path=stage_bundle_path,
        expected=summary,
    )
    return summary


def _expected_name_variants(expected_name: str) -> List[str]:
    raw = str(expected_name or "").strip()
    if not raw:
        return []
    variants: List[str] = []

    def add(value: str) -> None:
        candidate = value.strip()
        if candidate and candidate not in variants:
            variants.append(candidate)

    add(raw)
    root, ext = posixpath.splitext(raw)
    if ext.lower() in {".job", ".json", ".yaml", ".yml"}:
        add(root)
    if raw.endswith(".job.json"):
        add(raw[: -len(".job.json")])
    if raw.endswith(".json"):
        add(raw[: -len(".json")])
    if raw.endswith(".yaml"):
        add(raw[: -len(".yaml")])
    if raw.endswith(".yml"):
        add(raw[: -len(".yml")])
    return variants


def _generated_name_candidates(names: Sequence[str], stage_bundle_name: str, expected_name: str) -> List[str]:
    candidates: List[str] = []
    for variant in _expected_name_variants(expected_name):
        pattern = re.compile(
            r"^{}_{}{}".format(
                re.escape(stage_bundle_name),
                re.escape(variant),
                UUID_SUFFIX_RE.pattern,
            ),
            re.IGNORECASE,
        )
        for name in names:
            if pattern.match(name) and name not in candidates:
                candidates.append(name)
    return sorted(candidates)


def _target_job_name(item: Dict[str, Any]) -> str:
    return str(item.get("name") or "").strip()


def _target_cluster_name(item: Dict[str, Any]) -> str:
    return str(item.get("displayName") or item.get("name") or "").strip()


def get_cluster_details(client: AidpClient, key: str) -> Dict[str, Any]:
    return client.request_ok("GET", client.ws_url("clusters", key), ok=(200,)).json()


def rename_cluster(client: AidpClient, cluster_summary: Dict[str, Any], expected_name: str) -> Dict[str, Any]:
    key = cluster_summary.get("key")
    if not key:
        raise RuntimeError("Cluster summary has no key for rename {!r}".format(expected_name))
    current = get_cluster_details(client, key)
    body = dict(current)
    body["displayName"] = expected_name
    resp = client.request_ok("PUT", client.ws_url("clusters", key), body=body, ok=(200, 202))
    async_key = _async_key(resp)
    if async_key:
        client.wait_for_async(async_key, purpose="Deployed cluster rename")
    return get_cluster_details(client, key)


def rename_job(
    client: AidpClient,
    job_summary: Dict[str, Any],
    expected_name: str,
    cluster_name_map: Dict[str, str],
) -> Dict[str, Any]:
    key = job_summary.get("key")
    if not key:
        raise RuntimeError("Job summary has no key for rename {!r}".format(expected_name))
    current = get_job_details(client, key)
    body = dict(current)
    body["name"] = expected_name
    for item in body.get("jobClusters") or []:
        if not isinstance(item, dict):
            continue
        current_name = item.get("clusterName")
        if isinstance(current_name, str) and current_name in cluster_name_map:
            item["clusterName"] = cluster_name_map[current_name]
    for task in body.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        cluster = task.get("cluster")
        if not isinstance(cluster, dict):
            continue
        current_name = cluster.get("clusterName")
        if isinstance(current_name, str) and current_name in cluster_name_map:
            cluster["clusterName"] = cluster_name_map[current_name]
    resp = client.request_ok("PUT", client.ws_url("jobs", key), body=body, ok=(200, 202))
    async_key = _async_key(resp)
    if async_key:
        client.wait_for_async(async_key, purpose="Deployed job rename")
    return get_job_details(client, key)


def reconcile_post_deploy_resource_names(
    target_client: AidpClient,
    expected: Dict[str, Any],
    stage_bundle_path: str,
) -> Dict[str, Any]:
    log.info("== Stage 7: reconcile post-deploy resource names ==")
    stage_bundle_name = posixpath.basename(stage_bundle_path.rstrip("/"))
    log_debug_context(
        "Post-deploy reconciliation context",
        stage_bundle_path=stage_bundle_path,
        stage_bundle_name=stage_bundle_name,
        expected=expected,
    )
    renamed_clusters: List[Dict[str, str]] = []
    renamed_jobs: List[Dict[str, str]] = []
    cluster_name_map: Dict[str, str] = {}

    target_clusters = list_collection(target_client, "clusters")
    target_cluster_names = {_target_cluster_name(item): item for item in target_clusters}
    for expected_name in expected["clusters"]:
        if expected_name in target_cluster_names:
            continue
        generated = _generated_name_candidates(list(target_cluster_names.keys()), stage_bundle_name, expected_name)
        if not generated:
            raise RuntimeError("cluster {!r} was not found in the target workspace for reconciliation".format(expected_name))
        if len(generated) > 1:
            raise RuntimeError(
                "cluster {!r} has multiple generated variants in the target workspace: {}".format(
                    expected_name,
                    ", ".join(generated),
                )
            )
        current_name = generated[0]
        renamed = rename_cluster(target_client, target_cluster_names[current_name], expected_name)
        cluster_name_map[current_name] = expected_name
        renamed_clusters.append({"from": current_name, "to": expected_name, "key": renamed.get("key", "")})

    target_jobs = list_collection(target_client, "jobs")
    target_job_names = {_target_job_name(item): item for item in target_jobs}
    for job in expected["jobs"]:
        expected_name = job["name"]
        if expected_name in target_job_names:
            continue
        generated = _generated_name_candidates(list(target_job_names.keys()), stage_bundle_name, expected_name)
        if not generated:
            raise RuntimeError("job {!r} was not found in the target workspace for reconciliation".format(expected_name))
        if len(generated) > 1:
            raise RuntimeError(
                "job {!r} has multiple generated variants in the target workspace: {}".format(
                    expected_name,
                    ", ".join(generated),
                )
            )
        current_name = generated[0]
        renamed = rename_job(target_client, target_job_names[current_name], expected_name, cluster_name_map)
        renamed_jobs.append({"from": current_name, "to": expected_name, "key": renamed.get("key", "")})

    summary = {
        "expected_jobs": [item["name"] for item in expected["jobs"]],
        "expected_clusters": expected["clusters"],
        "renamed_jobs": renamed_jobs,
        "renamed_clusters": renamed_clusters,
        "stage_bundle_name": stage_bundle_name,
    }
    log.info(
        "Post-deploy reconciliation completed: jobs renamed=%s clusters renamed=%s",
        len(renamed_jobs),
        len(renamed_clusters),
    )
    log.debug("post-deploy name reconciliation summary: %s", summary)
    return summary


def _log_reconciliation_details(result: Dict[str, Any]) -> None:
    reconciliation = result.get("name_reconciliation") or {}
    renamed_jobs = reconciliation.get("renamed_jobs") or []
    renamed_clusters = reconciliation.get("renamed_clusters") or []
    log.info("Post-deploy renamed resources: jobs=%s clusters=%s", len(renamed_jobs), len(renamed_clusters))
    if renamed_jobs:
        log.info("Reconciled jobs:")
        for item in renamed_jobs:
            log.info("  - %s -> %s", item.get("from"), item.get("to"))
    if renamed_clusters:
        log.info("Reconciled clusters:")
        for item in renamed_clusters:
            log.info("  - %s -> %s", item.get("from"), item.get("to"))


def log_publish_source_summary(result: Dict[str, Any]) -> None:
    changed_files = result.get("changed_files") or []
    reused_existing_commit = bool(result.get("reused_existing_commit"))
    deploy_skipped_no_changes = bool(result.get("deploy_skipped_no_changes"))
    force_version_change = bool(result.get("force_version_change"))
    stage_bundle_path = str(result.get("stage_bundle_path") or "").strip()
    log.info("== Publish source summary ==")
    if stage_bundle_path:
        log.info("Stage bundle: %s", stage_bundle_path)
    log.info("Force version-change mode: %s", "enabled" if force_version_change else "disabled")
    if reused_existing_commit:
        log.info("Source publication: reused the latest content already versioned in the repository")
    if deploy_skipped_no_changes:
        log.info("Source publication: no real changes and no pending deploy")
    log.info("Files versioned in the commit: %s", ", ".join(changed_files) if changed_files else "(none)")


def log_publish_target_summary(result: Dict[str, Any]) -> None:
    changed_files = result.get("changed_files") or []
    reused_existing_commit = bool(result.get("reused_existing_commit"))
    deploy_skipped_no_changes = bool(result.get("deploy_skipped_no_changes"))
    log.info("== Publish target summary ==")
    if result.get("canonical_target_bundle_path"):
        log.info("Deploy bundle: %s", result.get("canonical_target_bundle_path"))
    if reused_existing_commit:
        log.info("Target publication: reused the latest content already versioned in the repository")
    if deploy_skipped_no_changes:
        log.info("Target deploy: no pending deploy and no real changes")
    if changed_files:
        log.info("Files associated with the current publication: %s", ", ".join(changed_files))
    _log_reconciliation_details(result)


def log_promote_summary(result: Dict[str, Any]) -> None:
    changed_files = result.get("changed_files") or []
    reused_existing_commit = bool(result.get("reused_existing_commit"))
    deploy_skipped_no_changes = bool(result.get("deploy_skipped_no_changes"))
    log.info("== Promotion summary ==")
    if result.get("canonical_target_bundle_path"):
        log.info("Deploy bundle: %s", result.get("canonical_target_bundle_path"))
    if reused_existing_commit:
        log.info("Target publication: reused the latest content already versioned in the repository")
    if deploy_skipped_no_changes:
        log.info("Target deploy: no pending deploy and no real changes")
    log.info("Files versioned in the commit: %s", ", ".join(changed_files) if changed_files else "(none)")
    _log_reconciliation_details(result)


def _workspace_item_path(item: Dict[str, Any], parent_path: Optional[str] = None) -> str:
    current = item.get("path") or item.get("objectPath")
    if isinstance(current, str) and current.strip():
        return current
    name = item.get("name")
    if isinstance(name, str) and name.strip() and parent_path:
        return parent_path.rstrip("/") + "/" + name
    return ""


def _workspace_item_type(item: Dict[str, Any]) -> str:
    metadata = (item or {}).get("metadata") or {}
    if isinstance(metadata, dict):
        if metadata.get("system:bundleKey"):
            return "BUNDLE"
        if metadata.get("system:repoKey") or metadata.get("system:branch") or metadata.get("system:folderType"):
            return "FOLDER"
    resource_type = str(item.get("resourceType") or item.get("resource_type") or "").upper()
    if resource_type == "BUNDLE":
        return "BUNDLE"
    return str(
        item.get("type")
        or item.get("objectType")
        or item.get("object_type")
        or item.get("resourceType")
        or item.get("resource_type")
        or ""
    ).upper()


def workspace_item(client: AidpClient, object_path: str) -> Optional[Dict[str, Any]]:
    return client.workspace_item(object_path)


def object_exists(client: AidpClient, object_path: str) -> bool:
    return workspace_item(client, object_path) is not None


def classify_workspace_folder(client: AidpClient, folder_path: str) -> Dict[str, Any]:
    return client.classify_workspace_folder(folder_path)


def delete_workspace_object_if_present(client: AidpClient, object_path: str) -> bool:
    return client.delete_workspace_object_if_present(object_path)


def _normalized_name_set(names: Optional[Sequence[str]]) -> set[str]:
    return {str(name).strip("/") for name in (names or []) if str(name).strip("/")}


def _normalized_path_set(paths: Optional[Sequence[str]]) -> set[str]:
    return {str(path).rstrip("/") for path in (paths or []) if str(path).rstrip("/")}


def remove_workspace_children_if_present(
    client: AidpClient,
    root_path: str,
    preserve_names: Optional[Sequence[str]] = None,
    ghost_paths: Optional[set[str]] = None,
    log_preserved: bool = True,
) -> bool:
    if not object_exists(client, root_path):
        return False
    removed_any = False
    preserved = _normalized_name_set(preserve_names)
    try:
        entries = list_workspace_objects(client, root_path)
    except Exception:
        entries = []
    for item in entries:
        child_path = _workspace_item_path(item, root_path)
        if not child_path:
            continue
        child_name = posixpath.basename(child_path.rstrip("/"))
        if child_name in preserved:
            if log_preserved:
                log.info("Preserving %s inside %s", child_name, root_path)
            continue
        item_type = _workspace_item_type(item)
        if item_type == "FOLDER":
            removed_any = remove_workspace_tree_if_present(client, child_path) or removed_any
            continue
        deleted = delete_workspace_object_if_present(client, child_path)
        purged = purge_bundle_if_present(client, child_path)
        removed_any = deleted or removed_any
        removed_any = purged or removed_any
        if object_exists(client, child_path):
            if not deleted and not purged and ghost_paths is not None:
                log.warning(
                    "Treating %s as a ghost item: listing still returns the path, but delete/purge did not remove the object",
                    child_path,
                )
                ghost_paths.add(child_path.rstrip("/"))
                continue
            current = workspace_item(client, child_path)
            if current and _workspace_item_type(current) == "FOLDER":
                removed_any = remove_workspace_tree_if_present(client, child_path) or removed_any
    return removed_any


def remove_workspace_tree_if_present(client: AidpClient, root_path: str) -> bool:
    if not object_exists(client, root_path):
        return False
    removed_any = remove_workspace_children_if_present(client, root_path)
    try:
        removed_any = client.delete_workspace_object_via_legacy_api(root_path) or removed_any
    except Exception as exc:
        log.debug("Legacy DELETE failed for %s during generic removal: %s", root_path, exc)
    removed_any = delete_workspace_object_if_present(client, root_path) or removed_any
    removed_any = purge_bundle_if_present(client, root_path) or removed_any
    return removed_any


def copy_workspace_object(client: AidpClient, from_path: str, to_path: str) -> None:
    normalized_target = to_path.rstrip("/") or "/"
    parent_dir = posixpath.dirname(normalized_target) or "/"
    ensure_directory(client, parent_dir)
    body = {"fromPath": from_path, "toPath": normalized_target, "isOverWrite": True}
    try:
        resp = client.request_ok(
            "POST",
            client.ws_url("actions", "copyObject"),
            body=body,
            ok=(200, 202),
        )
    except Exception as exc:
        log.debug(
            "REST copyObject failed from %s to %s; body=%s",
            from_path,
            to_path,
            json.dumps(body, sort_keys=True),
        )
        raise RuntimeError(
            "Failed to copy workspace content from {} to {}: {}".format(
                from_path,
                normalized_target,
                exc,
            )
        ) from exc
    key = _async_key(resp) if hasattr(resp, "headers") else None
    if key:
        client.wait_for_async(key, purpose="Workspace content copy")
    log.info("Copy completed to %s", normalized_target)


def copy_workspace_children(
    client: AidpClient,
    from_root: str,
    to_root: str,
    skip_names: Optional[Sequence[str]] = None,
) -> None:
    skipped = _normalized_name_set(skip_names)
    entries = list_workspace_objects(client, from_root)
    log_debug_context(
        "Copy workspace children context",
        from_root=from_root,
        to_root=to_root,
        skip_names=sorted(skipped),
        entry_count=len(entries),
        entries=entries,
    )
    for item in entries:
        child_path = _workspace_item_path(item, from_root)
        if not child_path:
            continue
        child_name = posixpath.basename(child_path.rstrip("/"))
        if child_name in skipped:
            log.info("Skipping %s while copying %s -> %s", child_name, from_root, to_root)
            continue
        dest_path = to_root.rstrip("/") + "/" + posixpath.basename(child_path.rstrip("/"))
        copy_workspace_object(client, child_path, dest_path)


def _list_child_names(client: AidpClient, root_path: str) -> List[str]:
    names: List[str] = []
    for item in list_workspace_objects(client, root_path):
        child_path = _workspace_item_path(item, root_path)
        if not child_path:
            continue
        names.append(posixpath.basename(child_path.rstrip("/")))
    return sorted(set(names))


def _detect_root_path_collisions(client: AidpClient, root_path: str) -> List[str]:
    root = root_path.rstrip("/")
    parent = posixpath.dirname(root) or "/"
    collisions: List[str] = []
    for item in list_workspace_objects(client, parent):
        item_path = _workspace_item_path(item, parent).rstrip("/")
        if not item_path or item_path == root:
            continue
        if item_path.startswith(root) and not item_path.startswith(root + "/"):
            collisions.append(item_path)
    return sorted(set(collisions))


def validate_deploy_bundle_sync(
    client: AidpClient,
    transport_bundle_path: str,
    deploy_bundle_path: str,
    preserve_names: Optional[Sequence[str]] = None,
    skip_names: Optional[Sequence[str]] = None,
) -> None:
    preserved = _normalized_name_set(preserve_names)
    skipped = _normalized_name_set(skip_names)
    expected_children = set(_list_child_names(client, transport_bundle_path))
    expected_children.difference_update(skipped)
    expected_children.update(preserved)
    actual_children = set(_list_child_names(client, deploy_bundle_path))
    missing = sorted(expected_children - actual_children)
    unexpected = sorted(actual_children - expected_children)
    collisions = [
        path
        for path in _detect_root_path_collisions(client, deploy_bundle_path)
        if path.rstrip("/") != transport_bundle_path.rstrip("/")
    ]
    deploy_facts = classify_workspace_folder(client, deploy_bundle_path)
    log_debug_context(
        "Deploy bundle sync validation",
        transport_bundle_path=transport_bundle_path,
        deploy_bundle_path=deploy_bundle_path,
        expected_children=sorted(expected_children),
        actual_children=sorted(actual_children),
        missing_children=missing,
        unexpected_children=unexpected,
        root_collisions=collisions,
        deploy_classification=deploy_facts.get("classification"),
    )
    if deploy_facts.get("classification") != "bundle_folder":
        raise RuntimeError(
            "Deploy bundle {} lost its bundle classification after synchronization: {}".format(
                deploy_bundle_path,
                deploy_facts.get("classification"),
            )
        )
    if collisions:
        raise RuntimeError(
            "Bundle synchronization generated invalid paths alongside root {}: {}".format(
                deploy_bundle_path,
                ", ".join(collisions),
            )
        )
    if missing:
        raise RuntimeError(
            "Deploy bundle {} is missing expected items after the copy: {}".format(
                deploy_bundle_path,
                ", ".join(missing),
            )
        )
    if unexpected:
        raise RuntimeError(
            "Deploy bundle {} received unexpected items after the copy: {}".format(
                deploy_bundle_path,
                ", ".join(unexpected),
            )
        )


def rest_create_bundle(
    client: AidpClient,
    name: str,
    path: str,
    bundled_resources: List[Dict[str, str]],
):
    body = {"name": name, "path": path, "bundledResources": bundled_resources}
    log.debug("bundle create request body=%s", json.dumps(body, sort_keys=True))
    return client.request_ok("POST", client.bundle_ws_url("bundles"), body=body, ok=(200, 202))


def rest_purge_bundle(client: AidpClient, bundle_path: str):
    body = {"path": bundle_path}
    log.debug("bundle purge request body=%s", json.dumps(body, sort_keys=True))
    return client.request_ok(
        "POST", client.bundle_ws_url("bundles", "actions", "purge"), body=body, ok=(200, 202)
    )


def purge_bundle_if_present(client: AidpClient, bundle_path: str) -> bool:
    try:
        log.info("Requesting bundle purge at %s", bundle_path)
        resp = rest_purge_bundle(client, bundle_path)
        key = _async_key(resp)
        if key:
            client.wait_for_async(key, purpose="Previous bundle removal")
        log.info("Bundle removed from %s", bundle_path)
        return True
    except Exception:
        return False


def purge_bundle_if_deployed(client: AidpClient, bundle_path: str) -> bool:
    try:
        payload = client.fetch_bundle_deployment_status(bundle_path)
    except Exception as exc:
        log.debug("Could not query the deployment status for bundle %s: %s", bundle_path, exc)
        return False
    status = str(payload.get("status") or "").upper()
    if not status:
        return False
    log.info(
        "Bundle deployment status before removal for %s: %s",
        bundle_path,
        status,
    )
    if status == "NOT_DEPLOYED":
        return False
    return purge_bundle_if_present(client, bundle_path)


def delete_bundle_root_if_present(client: AidpClient, bundle_path: str) -> bool:
    facts = classify_workspace_folder(client, bundle_path)
    if not facts.get("exists"):
        return False
    if facts.get("classification") != "bundle_folder":
        return False
    purge_bundle_if_deployed(client, bundle_path)
    try:
        log.info("Removing bundle root via legacy DELETE: %s", bundle_path)
        deleted = client.delete_workspace_object_via_legacy_api(bundle_path)
    except Exception as exc:
        log.debug("Legacy DELETE failed for bundle root %s: %s", bundle_path, exc)
        deleted = False
    if not deleted:
        deleted = delete_workspace_object_if_present(client, bundle_path)
    return deleted


def empty_bundle_contents_if_present(
    client: AidpClient,
    bundle_path: str,
    preserve_names: Optional[Sequence[str]] = None,
) -> bool:
    facts = classify_workspace_folder(client, bundle_path)
    if not facts.get("exists"):
        return False
    if facts.get("classification") != "bundle_folder":
        return False
    preserved = _normalized_name_set(preserve_names)
    deleted_any = False
    try:
        entries = client.list_workspace_objects_via_legacy_api(bundle_path)
    except Exception as exc:
        log.debug("Legacy list failed while emptying bundle %s: %s", bundle_path, exc)
        return False
    log.info(
        "Emptying bundle content in %s via legacy listing, preserving=%s",
        bundle_path,
        ", ".join(sorted(preserved)) or "(nothing)",
    )
    for item in entries:
        child_path = _workspace_item_path(item, bundle_path)
        if not child_path:
            continue
        child_name = posixpath.basename(child_path.rstrip("/"))
        if child_name in preserved:
            log.info("Preserving %s inside %s", child_name, bundle_path)
            continue
        try:
            client.delete_workspace_object_via_legacy_api(child_path)
            deleted_any = True
            log.info("Removed from bundle via legacy DELETE: %s", child_path)
        except Exception as exc:
            log.debug("Legacy DELETE failed for bundle child %s: %s", child_path, exc)
    return deleted_any


def ensure_path_absent(client: AidpClient, object_path: str) -> None:
    delays = PROMOTE_CLEANUP_RETRY_DELAYS_SECS
    total = len(delays)
    for idx, delay in enumerate(delays, start=1):
        log.info("Cleaning path %s: attempt %s/%s", object_path, idx, total)
        bundle_removed = delete_bundle_root_if_present(client, object_path)
        bundle_present = purge_bundle_if_present(client, object_path) if not bundle_removed else True
        object_present = remove_workspace_tree_if_present(client, object_path) if not bundle_removed else bundle_removed
        if not object_exists(client, object_path):
            log.info("Path released: %s", object_path)
            return
        log.warning(
            "Path %s still exists after attempt %s/%s; retrying in %ss",
            object_path,
            idx,
            total,
            delay,
        )
        log.debug(
            "cleanup state for %s: workspace_object=%s bundle=%s",
            object_path,
            object_present,
            bundle_present,
        )
        time.sleep(delay)
    raise RuntimeError("Path {} still exists after cleanup attempts".format(object_path))


def ensure_children_absent(
    client: AidpClient,
    root_path: str,
    preserve_names: Optional[Sequence[str]] = None,
) -> None:
    if not object_exists(client, root_path):
        return
    delays = PROMOTE_CLEANUP_RETRY_DELAYS_SECS
    total = len(delays)
    ghost_paths: set[str] = set()
    for idx, delay in enumerate(delays, start=1):
        log.info("Cleaning content in %s: attempt %s/%s", root_path, idx, total)
        emptied_bundle = empty_bundle_contents_if_present(
            client,
            root_path,
            preserve_names=preserve_names,
        )
        remove_workspace_children_if_present(
            client,
            root_path,
            preserve_names=preserve_names,
            ghost_paths=ghost_paths,
            log_preserved=not emptied_bundle,
        )
        if not tree_has_material_content(
            client,
            root_path,
            ignore_names=preserve_names,
            ignore_paths=ghost_paths,
        ):
            log.info("Content removed and root preserved: %s", root_path)
            return
        if emptied_bundle:
            log.debug("Legacy bundle cleanup removed part of the content from %s", root_path)
        log.warning(
            "Content in %s still exists after attempt %s/%s; retrying in %ss",
            root_path,
            idx,
            total,
            delay,
        )
        time.sleep(delay)
    raise RuntimeError("Path {} still has material content after cleanup attempts".format(root_path))


def tree_has_material_content(
    client: AidpClient,
    root_path: str,
    ignore_names: Optional[Sequence[str]] = None,
    ignore_paths: Optional[Sequence[str]] = None,
) -> bool:
    if not object_exists(client, root_path):
        return False
    ignored = _normalized_name_set(ignore_names)
    ignored_paths = _normalized_path_set(ignore_paths)
    try:
        entries = list_workspace_objects(client, root_path)
    except Exception:
        return True
    if not entries:
        return False
    for item in entries:
        child_path = _workspace_item_path(item, root_path)
        if not child_path:
            continue
        if child_path.rstrip("/") in ignored_paths:
            continue
        child_name = posixpath.basename(child_path.rstrip("/"))
        if child_name in ignored:
            continue
        if _workspace_item_type(item) == "FOLDER":
            if tree_has_material_content(
                client,
                child_path,
                ignore_names=ignore_names,
                ignore_paths=ignore_paths,
            ):
                return True
            continue
        return True
    return False


def best_effort_prepare_copy_destination(client: AidpClient, object_path: str) -> None:
    delays = PROMOTE_COPY_CLEANUP_RETRY_DELAYS_SECS
    total = len(delays)
    for idx, delay in enumerate(delays, start=1):
        log.info("Preparatory cleanup of target %s: attempt %s/%s", object_path, idx, total)
        purge_bundle_if_present(client, object_path)
        remove_workspace_tree_if_present(client, object_path)
        if not object_exists(client, object_path):
            log.info("Target released: %s", object_path)
            return
        if not tree_has_material_content(client, object_path):
            log.warning(
                "Target %s kept only residual empty directories; continuing with the copy and overwriting the path",
                object_path,
            )
            return
        log.warning(
            "Target %s still contains files/objects; retrying in %ss",
            object_path,
            delay,
        )
        time.sleep(delay)
    if tree_has_material_content(client, object_path):
        raise RuntimeError(
            "Path {} still has material content after cleanup attempts".format(object_path)
        )
    log.warning(
            "Target %s kept only residual empty directories after cleanup; continuing with copy",
        object_path,
    )


def create_bundle_with_retries(
    client: AidpClient,
    details: Dict[str, Any],
    bundle_path: str,
):
    delays = PROMOTE_BUNDLE_CREATE_RETRY_DELAYS_SECS
    last_err = None
    total = len(delays)
    for idx, delay in enumerate(delays, start=1):
        try:
            log.info("Bundle creation %s: attempt %s/%s", bundle_path, idx, total)
            return rest_create_bundle(client, details["name"], details["path"], details["bundledResources"])
        except Exception as exc:
            last_err = exc
            message = str(exc)
            lowered = message.lower()
            if "failed to create directory in volume" in lowered or "failed to upload file to volume" in lowered:
                log.warning(
                    "Bundle %s hit a transient workspace-volume error; retrying in %ss",
                    bundle_path,
                    delay,
                )
                log_debug_context(
                    "Bundle creation transient volume failure",
                    bundle_path=bundle_path,
                    attempt=idx,
                    total_attempts=total,
                    retry_delay_secs=delay,
                    error=message,
                )
                client.ensure_directory(details["path"], purpose="Stage bundle area setup")
                ensure_path_absent(client, bundle_path)
                time.sleep(delay)
                continue
            if "already exists" not in lowered:
                raise
            log.warning(
                "Bundle %s still appears as existing; retrying in %ss",
                bundle_path,
                delay,
            )
            log.debug("bundle create retry %s/%s for %s", idx, total, bundle_path)
            purge_bundle_if_present(client, bundle_path)
            time.sleep(delay)
    raise last_err


def create_bundle_with_fallback_paths(
    client: AidpClient,
    candidates: List[Dict[str, str]],
    bundled_resources: List[Dict[str, str]],
) -> str:
    last_err = None
    total = len(candidates)
    for index, candidate in enumerate(candidates, start=1):
        bundle_name = candidate["name"]
        bundle_parent = candidate["path"]
        bundle_path = bundle_parent.rstrip("/") + "/" + bundle_name
        log.info("Trying bundle creation on candidate path %s/%s", index, total)
        log.info("Candidate target: %s", bundle_path)
        ensure_directory(client, bundle_parent)
        ensure_path_absent(client, bundle_path)
        try:
            resp = create_bundle_with_retries(
                client,
                {"name": bundle_name, "path": bundle_parent, "bundledResources": bundled_resources},
                bundle_path,
            )
            key = _async_key(resp)
            if key:
                client.wait_for_async(key, purpose="Bundle creation")
            return bundle_path
        except Exception as exc:
            last_err = exc
            message = str(exc)
            if (
                "path must exist under a git folder" in message
                or "Failed to upload file to volume" in message
            ):
                log.warning("Path %s was rejected; trying the next fallback", bundle_path)
                continue
            raise
    raise last_err

def select_jobs_for_bundle(source_client: AidpClient) -> List[Dict[str, str]]:
    resp = source_client.request_ok("GET", source_client.ws_url("jobs"), ok=(200,))
    payload = resp.json()
    items = payload if isinstance(payload, list) else payload.get("items", [])
    bundled = []
    for item in items:
        key = item.get("key")
        if key:
            bundled.append({"resourceKey": key, "resourceType": "JOB"})
    if not bundled:
        raise RuntimeError("No jobs found in source workspace to bundle.")
    return bundled


def collect_git_diff_paths(
    source_client: AidpClient,
    cfg: Dict[str, Any],
    include_prefixes: Optional[List[str]] = None,
    exclude_prefixes: Optional[List[str]] = None,
) -> List[str]:
    folder_path = resolve_folder_path(cfg)
    diffs = source_client.list_git_diffs(folder_path, cfg["git"]["branch"], compare_to="HEAD")
    include_prefixes = include_prefixes or []
    exclude_prefixes = exclude_prefixes or []
    changed = []
    for item in diffs:
        path = item.get("gitFilePath") or item.get("git_file_path")
        if not path:
            continue
        if include_prefixes and not any(_git_path_matches_scope(path, prefix) for prefix in include_prefixes):
            continue
        if any(_git_path_matches_scope(path, prefix) for prefix in exclude_prefixes):
            continue
        changed.append(path)
    return sorted(set(changed))


def workspace_tree_git_paths(
    client: AidpClient,
    git_root: str,
    root_path: str,
) -> List[str]:
    collected: List[str] = []
    try:
        entries = list_workspace_objects(client, root_path)
    except Exception:
        return collected
    for item in entries:
        path = _workspace_item_path(item, root_path)
        if not path:
            continue
        if _workspace_item_type(item) == "FOLDER":
            collected.extend(workspace_tree_git_paths(client, git_root, path))
            continue
        if path.startswith(git_root.rstrip("/") + "/"):
            collected.append(path[len(git_root.rstrip("/")) + 1 :])
    return sorted(set(collected))


def _is_ephemeral_git_path(path: str) -> bool:
    parts = [part for part in path.split("/") if part]
    return any(part.startswith("._tmp") or part.startswith(".~") for part in parts)


def _git_path_matches_scope(path: str, scope: str) -> bool:
    normalized_scope = scope.strip("/")
    normalized_path = path.strip("/")
    return normalized_path == normalized_scope or normalized_path.startswith(normalized_scope + "/")


def _is_within_or_equal(root_path: str, candidate_path: str) -> bool:
    root = root_path.rstrip("/")
    candidate = candidate_path.rstrip("/")
    return candidate == root or candidate.startswith(root + "/")


def current_bundle_git_paths(
    client: AidpClient,
    git_root: str,
    bundle_path: str,
) -> List[str]:
    paths = workspace_tree_git_paths(client, git_root, bundle_path)
    return [path for path in paths if not _is_ephemeral_git_path(path)]


def _git_relpath(folder_path: str, object_path: str) -> str:
    root = folder_path.rstrip("/") + "/"
    if not object_path.startswith(root):
        raise RuntimeError("Path {} is not under git folder {}".format(object_path, folder_path))
    return object_path[len(root) :]


def _is_forced_version_change_marker_path(path: str) -> bool:
    normalized = str(path or "").strip().strip("/")
    if not normalized:
        return False
    return normalized.endswith("/" + FORCED_VERSION_CHANGE_MARKER_NAME) or normalized == FORCED_VERSION_CHANGE_MARKER_NAME


def _existing_force_marker_anchor(source_client: AidpClient, folder_path: str) -> str:
    preferred = [".github", ".cicd", "shared", "src"]
    for name in preferred:
        candidate = folder_path.rstrip("/") + "/" + name
        if source_client.workspace_object_exists(candidate):
            return candidate

    for item in list_workspace_objects(source_client, folder_path):
        item_path = _workspace_item_path(item, folder_path)
        if not item_path:
            continue
        if _workspace_item_type(item) != "FOLDER":
            continue
        return item_path.rstrip("/")

    raise RuntimeError(
        "Could not find an existing repository directory under {} to store the forced version-change marker".format(
            folder_path
        )
    )


def force_versionable_marker_update(source_client: AidpClient, cfg: Dict[str, Any], commit_message: str) -> str:
    folder_path = resolve_folder_path(cfg)
    marker_anchor = _existing_force_marker_anchor(source_client, folder_path)
    marker_dir = marker_anchor.rstrip("/")
    marker_path = marker_dir + "/" + FORCED_VERSION_CHANGE_MARKER_NAME
    payload = {
        "updated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "reason": "forced_version_change",
        "commit_message": commit_message,
        "workspace_name": cfg.get("aidp", {}).get("workspace_name"),
        "marker_anchor": _git_relpath(folder_path, marker_anchor),
    }
    source_client.put_workspace_file(
        marker_path,
        json.dumps(payload, ensure_ascii=True, indent=2).encode("utf-8"),
        object_type="FILE",
        overwrite=True,
    )
    log.info("File uploaded: %s", marker_path)
    return _git_relpath(folder_path, marker_path)


def commit_and_push_bundle(
    source_client: AidpClient,
    cfg: Dict[str, Any],
    commit_message: str,
    stage_bundle_path: Optional[str] = None,
    force_version_change: bool = False,
) -> List[str]:
    workspace_name = str(cfg.get("aidp", {}).get("workspace_name") or "workspace").strip() or "workspace"
    log.info("== Stage 3: version changes in the repository for workspace %s ==", workspace_name)
    wait_label = "git diff/commit stabilization: {seconds} remaining"
    log.info(
        "Stabilizing workspace state before git diff/commit (%ss)",
        GIT_COMMIT_STABILIZATION_SECS,
    )
    sleep_with_spinner(GIT_COMMIT_STABILIZATION_SECS, wait_label)
    folder_path = resolve_folder_path(cfg)
    stage_bundle_rel = (
        _git_relpath(folder_path, stage_bundle_path)
        if stage_bundle_path
        else cfg["git"].get("stage_bundle_path", DEFAULT_STAGE_BUNDLE_NAME).strip("/")
    )
    forced_marker_rel: Optional[str] = None
    if force_version_change:
        log.info("Force version-change mode enabled; injecting a marker file before git diff")
        forced_marker_rel = force_versionable_marker_update(source_client, cfg, commit_message)
    changed_files = collect_git_diff_paths(
        source_client,
        cfg,
        include_prefixes=["src", "shared", "aidp_workbench.yaml", stage_bundle_rel, ".cicd"],
    )
    changed_files = [path for path in changed_files if not _is_ephemeral_git_path(path)]
    changed_files = [path for path in changed_files if not _is_forced_version_change_marker_path(path)]
    if forced_marker_rel:
        changed_files.append(forced_marker_rel)
    changed_files = sorted(set(changed_files))
    log_debug_context(
        "Commit/push stage context",
        folder_path=folder_path,
        branch=cfg["git"]["branch"],
        commit_message=commit_message,
        stage_bundle_path=stage_bundle_path,
        stage_bundle_rel=stage_bundle_rel,
        changed_files=changed_files,
    )
    log.debug("git diff candidates for commit: %s", changed_files)
    if not changed_files:
        log.info("No versionable changes were found in workspace %s", workspace_name)
        return []
    resp = source_client.commit_push_git_repository(
        folder_path,
        cfg["git"]["branch"],
        changed_files,
        commit_message,
    )
    key = _async_key(resp) if hasattr(resp, "headers") else None
    if key:
        source_client.wait_for_async(key, purpose="Commit and push changes")
    log.info("Commit and push completed with %s file(s)", len(changed_files))
    return changed_files


def recreate_bundle_from_workspace(source_cfg: Dict[str, Any], source_client: AidpClient) -> Dict[str, Any]:
    workspace_name = str(source_cfg.get("aidp", {}).get("workspace_name") or "workspace").strip() or "workspace"
    log.info("== Stage 2: recreate the fixed stage bundle in workspace %s ==", workspace_name)
    bundled_resources = select_jobs_for_bundle(source_client)
    git_root = resolve_folder_path(source_cfg).rstrip("/")
    transport_bundle_path = resolve_stage_bundle_path(source_cfg)
    transport_bundle_parent = posixpath.dirname(transport_bundle_path.rstrip("/"))
    transport_bundle_name = posixpath.basename(transport_bundle_path.rstrip("/"))

    for label, path in (
        ("transport_bundle_parent", transport_bundle_parent),
        ("transport_bundle_path", transport_bundle_path),
    ):
        if not _is_within_or_equal(git_root, path):
            raise RuntimeError("{} must stay under git folder {}: {}".format(label, git_root, path))

    log.info("Preparing the fixed stage bundle at %s", transport_bundle_path)
    log_debug_context(
        "Stage bundle recreation context",
        git_root=git_root,
        transport_bundle_parent=transport_bundle_parent,
        transport_bundle_path=transport_bundle_path,
        transport_bundle_name=transport_bundle_name,
        bundled_resources=bundled_resources,
    )

    source_client.ensure_directory(transport_bundle_parent, purpose="Stage bundle area setup")
    ensure_path_absent(source_client, transport_bundle_path)

    resp = create_bundle_with_retries(
        source_client,
        {
            "name": transport_bundle_name,
            "path": transport_bundle_parent,
            "bundledResources": bundled_resources,
        },
        transport_bundle_path,
    )
    key = _async_key(resp)
    if key:
        source_client.wait_for_async(key, purpose="Fixed stage bundle creation")
    log.info("The bundle was recreated successfully from %s job(s) in workspace %s", len(bundled_resources), workspace_name)
    return {
        "bundled_jobs": bundled_resources,
        "stage_bundle_path": transport_bundle_path,
    }


def publish_source(
    source_cfg: Dict[str, Any],
    target_cfg: Dict[str, Any],
    auth_method: str,
    commit_message: str,
    force_version_change: bool = False,
) -> Dict[str, Any]:
    ensure_matching_git_identity(source_cfg, target_cfg)
    signer = build_signer(auth_method)
    source_client = AidpClient(source_cfg, signer)
    source_workspace_name = str(source_cfg.get("aidp", {}).get("workspace_name") or "workspace").strip() or "workspace"

    log.info("== Stage 0: validate the Git credential for workspace %s ==", source_workspace_name)
    credential_key = phase0_credential(source_client, source_cfg)
    log.info("== Stage 1: validate the Git folder for workspace %s ==", source_workspace_name)
    phase1_directory(source_client, source_cfg)
    ensure_source_git_folder(source_client, source_cfg, credential_key)

    bundle_result = recreate_bundle_from_workspace(source_cfg, source_client)
    expected_names = collect_expected_deployed_resource_names(source_client, bundle_result["bundled_jobs"])
    changed_files = commit_and_push_bundle(
        source_client,
        source_cfg,
        commit_message,
        stage_bundle_path=bundle_result["stage_bundle_path"],
        force_version_change=force_version_change,
    )

    return {
        "bundled_jobs": bundle_result["bundled_jobs"],
        "expected_resource_names": expected_names,
        "changed_files": changed_files,
        "stage_bundle_path": bundle_result["stage_bundle_path"],
        "force_version_change": bool(force_version_change),
        "reused_existing_commit": False,
        "deploy_skipped_no_changes": False,
    }


def ensure_target_deploy_bundle_shell(
    target_client: AidpClient,
    target_cfg: Dict[str, Any],
    deploy_bundle_path: str,
) -> None:
    workspace_name = str(target_cfg.get("aidp", {}).get("workspace_name") or "workspace").strip() or "workspace"
    facts = classify_workspace_folder(target_client, deploy_bundle_path)
    item = facts.get("workspace_item")
    item_type = _workspace_item_type(item) if item else ""
    if facts.get("classification") == "bundle_folder":
        return
    if item and item_type == "BUNDLE":
        return
    log.info("Ensuring deploy bundle shell in workspace %s", workspace_name)
    bootstrap_job = None
    try:
        bootstrap_job = create_or_update_bootstrap_job(target_client, build_bootstrap_job_spec_minimal())
        if item:
            log.warning(
                "Target %s exists as %s and will be recreated as a deploy bundle shell",
                deploy_bundle_path,
                item_type or facts.get("classification") or "UNKNOWN",
            )
            ensure_path_absent(target_client, deploy_bundle_path)
        bundle_parent = os.path.dirname(deploy_bundle_path.rstrip("/"))
        bundle_name = os.path.basename(deploy_bundle_path.rstrip("/"))
        resp = rest_create_bundle(
            target_client,
            bundle_name,
            bundle_parent,
            [{"resourceKey": bootstrap_job["key"], "resourceType": "JOB"}],
        )
        key = _async_key(resp)
        if key:
            target_client.wait_for_async(key, purpose="Deploy bundle shell creation")
        refreshed = classify_workspace_folder(target_client, deploy_bundle_path)
        refreshed_item = refreshed.get("workspace_item")
        refreshed_type = _workspace_item_type(refreshed_item) if refreshed_item else ""
        if refreshed.get("classification") != "bundle_folder" and refreshed_type != "BUNDLE":
            raise RuntimeError(
                "Deploy bundle shell {} was created but was not recognized as a bundle. classification={} type={}".format(
                    deploy_bundle_path,
                    refreshed.get("classification"),
                    refreshed_type or "UNKNOWN",
                )
            )
    finally:
        delete_job_if_present(target_client, PREPARE_BOOTSTRAP_JOB_NAME)


def sync_stage_bundle_into_deploy_bundle(
    target_client: AidpClient,
    target_cfg: Dict[str, Any],
    transport_bundle_path: str,
) -> str:
    deploy_bundle_path = resolve_versioned_bundle_path(target_cfg)
    log.info("== Stage 5: synchronize stage bundle content into the deploy bundle ==")
    log_debug_context(
        "Deploy bundle sync context",
        transport_bundle_path=transport_bundle_path,
        deploy_bundle_path=deploy_bundle_path,
        preserve_names=list(BUNDLE_DEPLOY_PRESERVE_NAMES),
    )
    ensure_target_deploy_bundle_shell(target_client, target_cfg, deploy_bundle_path)
    ensure_children_absent(target_client, deploy_bundle_path, preserve_names=BUNDLE_DEPLOY_PRESERVE_NAMES)
    copy_workspace_children(
        target_client,
        transport_bundle_path,
        deploy_bundle_path,
        skip_names=[BUNDLE_METADATA_NAME],
    )
    validate_deploy_bundle_sync(
        target_client,
        transport_bundle_path,
        deploy_bundle_path,
        preserve_names=BUNDLE_DEPLOY_PRESERVE_NAMES,
        skip_names=[BUNDLE_METADATA_NAME],
    )
    if not tree_has_material_content(target_client, deploy_bundle_path, ignore_names=BUNDLE_DEPLOY_PRESERVE_NAMES):
        raise RuntimeError("The deploy bundle did not receive content from the stage bundle: {}".format(deploy_bundle_path))
    return deploy_bundle_path


def publish_target(
    source_cfg: Dict[str, Any],
    target_cfg: Dict[str, Any],
    auth_method: str,
    commit_message: str = "",
) -> Dict[str, Any]:
    ensure_matching_git_identity(source_cfg, target_cfg)
    target_workspace_name = str(target_cfg.get("aidp", {}).get("workspace_name") or "workspace").strip() or "workspace"
    target_stage_bundle_path = resolve_stage_bundle_path(target_cfg)

    log.info("== Stage 4: publish the updated bundle in workspace %s ==", target_workspace_name)
    target_signer = build_signer(auth_method)
    target_client = AidpClient(target_cfg, target_signer)
    target_credential_key = phase0_credential(target_client, target_cfg)
    target_parent_was_absent = phase1_directory(target_client, target_cfg)
    phase2_git_folder(
        target_client,
        target_cfg,
        target_credential_key,
        parent_was_absent=target_parent_was_absent,
    )
    if not object_exists(target_client, target_stage_bundle_path):
        raise RuntimeError(
            "The stage bundle did not reach the target workspace after the pull: {}".format(target_stage_bundle_path)
        )
    canonical_target_bundle_path = sync_stage_bundle_into_deploy_bundle(
        target_client,
        target_cfg,
        target_stage_bundle_path,
    )
    log.info("== Stage 6: execute the canonical bundle deploy in workspace %s ==", target_workspace_name)
    log.info("Triggering deploy of the canonical bundle %s", canonical_target_bundle_path)
    target_client.deploy_bundle(canonical_target_bundle_path)

    expected_names = collect_expected_names_from_stage_bundle(target_client, target_stage_bundle_path)
    name_reconciliation = reconcile_post_deploy_resource_names(
        target_client,
        expected_names,
        target_stage_bundle_path,
    )
    return {
        "bundled_jobs": [],
        "changed_files": [],
        "stage_bundle_path": resolve_stage_bundle_path(source_cfg),
        "target_stage_bundle_path": target_stage_bundle_path,
        "canonical_target_bundle_path": canonical_target_bundle_path,
        "name_reconciliation": name_reconciliation,
        "reused_existing_commit": False,
        "deploy_skipped_no_changes": False,
    }


def promote(
    source_cfg: Dict[str, Any],
    target_cfg: Dict[str, Any],
    auth_method: str,
    commit_message: str,
) -> Dict[str, Any]:
    log_debug_context(
        "Promote context",
        auth_method=auth_method,
        commit_message=commit_message,
        source_workspace=source_cfg.get("aidp", {}).get("workspace_name"),
        source_workspace_key=source_cfg.get("aidp", {}).get("workspace_key"),
        target_workspace=target_cfg.get("aidp", {}).get("workspace_name"),
        target_workspace_key=target_cfg.get("aidp", {}).get("workspace_key"),
        source_folder=resolve_folder_path(source_cfg),
        target_folder=resolve_folder_path(target_cfg),
        source_stage_bundle=resolve_stage_bundle_path(source_cfg),
        target_stage_bundle=resolve_stage_bundle_path(target_cfg),
        target_deploy_bundle=resolve_versioned_bundle_path(target_cfg),
        source_repository=source_cfg.get("git", {}).get("repository_url"),
        branch=source_cfg.get("git", {}).get("branch"),
    )
    source_result = publish_source(source_cfg, target_cfg, auth_method, commit_message)
    target_result = publish_target(source_cfg, target_cfg, auth_method, commit_message)
    return {
        "bundled_jobs": source_result.get("bundled_jobs") or [],
        "changed_files": source_result.get("changed_files") or [],
        "stage_bundle_path": source_result.get("stage_bundle_path") or resolve_stage_bundle_path(source_cfg),
        "target_stage_bundle_path": target_result.get("target_stage_bundle_path") or "",
        "canonical_target_bundle_path": target_result.get("canonical_target_bundle_path") or "",
        "name_reconciliation": target_result.get("name_reconciliation") or {},
        "reused_existing_commit": bool(source_result.get("reused_existing_commit")),
        "deploy_skipped_no_changes": bool(target_result.get("deploy_skipped_no_changes")),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-config")
    parser.add_argument("--target-config")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--auth-method", default=None)
    parser.add_argument(
        "--commit-message",
        default="chore: promote source workspace to target",
    )
    args = parser.parse_args(argv)
    setup_logging("cicd-promote")

    source_path, target_path = resolve_config_paths(
        args.source_config,
        args.target_config,
        demo=args.demo,
    )
    source_cfg = load_config(source_path)
    target_cfg = load_config(target_path)
    result = promote(
        source_cfg,
        target_cfg,
        auth_method=args.auth_method or DEFAULT_AUTH_METHOD,
        commit_message=args.commit_message,
    )
    log.debug("Promocao result payload: %s", result)
    log_promote_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_with_logged_errors(main))
