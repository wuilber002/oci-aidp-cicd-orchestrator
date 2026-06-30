#!/usr/bin/env python3
"""Create a minimal dataset of transportable resources for end-to-end validation."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.console_logging import LOGGER_NAME, format_elapsed_br, poll_with_progress, run_with_logged_errors, setup_logging
from core.contexts import config_paths_for_context, context_auth_method, context_demo_mode, load_context
from core.settings import DEFAULT_AUTH_METHOD, DEFAULT_DEMO_SOURCE_CONFIG_PATH, DEFAULT_SOURCE_CONFIG_PATH
from cicd_deploy import AidpClient, _async_key, build_signer, load_config, log_debug_context, resolve_folder_path

log = logging.getLogger(LOGGER_NAME)

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")


def notebook_payload(title: str, message: str) -> str:
    notebook = {
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [f"# {title}\n", f"{message}\n"],
            },
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": ["print('CI/CD workspace transport seed')\n"],
            },
        ],
        "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    return json.dumps(notebook, ensure_ascii=False, indent=2)


def put_workspace_file(client: AidpClient, path: str, content: str) -> None:
    file_type = "NOTEBOOK" if path.endswith(".ipynb") else "FILE"
    log.info("Creating %s in workspace: %s", file_type.lower(), path)
    client.put_workspace_file(
        path,
        content.encode("utf-8"),
        object_type=file_type,
        overwrite=True,
    )
    log.info("File uploaded: %s", path)


def read_template(name: str) -> Dict[str, Any]:
    path = os.path.join(TEMPLATES_DIR, name)
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def list_all(client: AidpClient, path: str) -> List[Dict[str, Any]]:
    resp = client.request_ok("GET", client.ws_url(path), ok=(200,))
    payload = resp.json()
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        items = payload.get("items")
        if isinstance(items, list):
            return items
    return []


def find_cluster_by_name(client: AidpClient, name: str) -> Optional[Dict[str, Any]]:
    for item in list_all(client, "clusters"):
        if item.get("displayName") == name:
            return item
    return None


def get_cluster(client: AidpClient, key: str) -> Dict[str, Any]:
    return client.request_ok("GET", client.ws_url("clusters", key), ok=(200,)).json()


def wait_for_cluster_ready(client: AidpClient, key: str) -> Dict[str, Any]:
    def fetch_cluster() -> Dict[str, Any]:
        return get_cluster(client, key)

    def cluster_state(cluster: Dict[str, Any]) -> str:
        return str(cluster.get("state") or cluster.get("lifecycleState") or "")

    return poll_with_progress(
        "cluster {}".format(key),
        timeout_secs=client.poll_timeout,
        fetch_interval_secs=client.poll_interval,
        fetch_fn=fetch_cluster,
        success_fn=lambda cluster: cluster_state(cluster) in {"ACTIVE", "STOPPED"},
        progress_suffix_fn=lambda cluster: "state={}".format(cluster_state(cluster) or "UNKNOWN"),
        failure_message_fn=lambda cluster, elapsed: (
            "cluster {} entered FAILED after {}".format(key, format_elapsed_br(elapsed))
            if cluster_state(cluster) == "FAILED"
            else None
        ),
        timeout_message_fn=lambda _cluster, elapsed: "cluster {} did not reach a ready state after {}".format(
            key,
            format_elapsed_br(elapsed),
        ),
        logger=log,
    )


def ensure_cluster_created(client: AidpClient, spec: Dict[str, Any]) -> Dict[str, Any]:
    existing = find_cluster_by_name(client, spec["displayName"])
    log_debug_context("Seed cluster spec", spec=spec, existing=existing)
    if not existing:
        log.info("creating cluster resource: %s", spec["displayName"])
        resp = client.request("POST", client.ws_url("clusters"), body=spec)
        if resp.status_code not in (200, 201, 202):
            raise RuntimeError("create cluster {} -> HTTP {}: {}".format(spec["displayName"], resp.status_code, resp.text))
        key = _async_key(resp)
        if key:
            client.wait_for_async(key)
        created = find_cluster_by_name(client, spec["displayName"])
        if not created:
            raise RuntimeError("cluster {} created but not found afterwards".format(spec["displayName"]))
        cluster = get_cluster(client, created["key"])
        return cluster
    log.info("updating cluster resource: %s", spec["displayName"])
    current = get_cluster(client, existing["key"])
    body = {**current, **spec}
    resp = client.request_ok("PUT", client.ws_url("clusters", existing["key"]), body=body, ok=(200, 202))
    key = _async_key(resp)
    if key:
        client.wait_for_async(key)
    cluster = get_cluster(client, existing["key"])
    return cluster


def find_job_by_name(client: AidpClient, name: str) -> Optional[Dict[str, Any]]:
    for item in list_all(client, "jobs"):
        if item.get("name") == name:
            return item
    return None


def get_job(client: AidpClient, key: str) -> Dict[str, Any]:
    return client.request_ok("GET", client.ws_url("jobs", key), ok=(200,)).json()


def inject_cluster_key(job_spec: Dict[str, Any], cluster_key: str) -> Dict[str, Any]:
    payload = json.loads(json.dumps(job_spec))
    for item in payload.get("jobClusters") or []:
        item["clusterKey"] = cluster_key
    for task in payload.get("tasks") or []:
        cluster = task.get("cluster")
        if isinstance(cluster, dict):
            cluster["clusterKey"] = cluster_key
    return payload


def create_or_update_job(client: AidpClient, spec: Dict[str, Any]) -> Dict[str, Any]:
    existing = find_job_by_name(client, spec["name"])
    log_debug_context("Seed job spec", spec=spec, existing=existing)
    if not existing:
        log.info("creating job resource: %s", spec["name"])
        resp = client.request("POST", client.ws_url("jobs"), body=spec)
        if resp.status_code not in (200, 201, 202):
            raise RuntimeError("create job {} -> HTTP {}: {}".format(spec["name"], resp.status_code, resp.text))
        key = _async_key(resp)
        if key:
            client.wait_for_async(key)
        created = find_job_by_name(client, spec["name"])
        if not created:
            raise RuntimeError("job {} created but not found afterwards".format(spec["name"]))
        job = get_job(client, created["key"])
        return job
    log.info("updating job resource: %s", spec["name"])
    current = get_job(client, existing["key"])
    body = {**current, **spec}
    resp = client.request_ok("PUT", client.ws_url("jobs", existing["key"]), body=body, ok=(200, 202))
    key = _async_key(resp)
    if key:
        client.wait_for_async(key)
    job = get_job(client, existing["key"])
    return job


def build_bundle_manifest(job_names: List[str], cluster_dependency_name: str) -> str:
    lines = [
        "bundle:",
        '  name: "bundle"',
        "  jobs:",
    ]
    for name in job_names:
        lines.append('  - "{}"'.format(name))
    lines.extend(
        [
            "  jobDependencies:",
            '  - "{}"'.format(cluster_dependency_name),
            "",
        ]
    )
    return "\n".join(lines)


def render_job_template(template_name: str, prepare_path: str, validate_path: str) -> Dict[str, Any]:
    payload = read_template(template_name)
    raw = json.dumps(payload)
    raw = raw.replace("__NOTEBOOK_PREPARE__", prepare_path)
    raw = raw.replace("__NOTEBOOK_VALIDATE__", validate_path)
    return json.loads(raw)


def ensure_seed_workspace(cfg: Dict[str, Any], auth_method: str) -> None:
    signer = build_signer(auth_method)
    client = AidpClient(cfg, signer)
    folder = resolve_folder_path(cfg)
    prepare_nb = folder + "/src/prepare_dummy_data.ipynb"
    validate_nb = folder + "/src/validate_dummy_data.ipynb"
    log_debug_context(
        "Seed workspace context",
        auth_method=auth_method,
        workspace_name=cfg.get("aidp", {}).get("workspace_name"),
        workspace_key=cfg.get("aidp", {}).get("workspace_key"),
        folder=folder,
        prepare_nb=prepare_nb,
        validate_nb=validate_nb,
    )

    log.info("== Stage 1: prepare basic workspace structure ==")
    git_repo = client.get_git_repository(folder, should_include_credential_key=False)
    client.ensure_directory(folder + "/src")
    client.ensure_directory(folder + "/shared")

    cluster_spec = read_template("cluster.json")
    primary_job = render_job_template("job_primary.json", prepare_nb, validate_nb)
    alt_job = render_job_template("job_alt.json", prepare_nb, validate_nb)

    log.info("== Stage 1.5: start cluster creation/update as early as possible ==")
    cluster = ensure_cluster_created(client, cluster_spec)
    cluster_key = cluster["key"]
    cluster_state = cluster.get("state") or cluster.get("lifecycleState")
    log.info("Cluster ready for reference: %s (key=%s current_state=%s)", cluster_spec["displayName"], cluster_key, cluster_state)

    log.info("== Stage 2: create support files and notebooks ==")
    put_workspace_file(client, prepare_nb, notebook_payload("Prepare Dummy Data", "Initial seed notebook."))
    put_workspace_file(client, validate_nb, notebook_payload("Validate Dummy Data", "Validation seed notebook."))
    put_workspace_file(client, folder + "/shared/seed_readme.txt", "seed workspace created by demo/cicd_seed.py\n")

    log.info("Creating primary workspace manifest")
    put_workspace_file(
        client,
        folder + "/aidp_workbench.yaml",
        "version: 1\nname: cicd-orchestrator-samples\nresources:\n  jobs:\n    - cicd_seed_workflow\n    - cicd_seed_workflow_alt\n",
    )

    log.info("== Stage 3: create transportable AIDP resources ==")
    create_or_update_job(client, inject_cluster_key(primary_job, cluster_key))
    create_or_update_job(client, inject_cluster_key(alt_job, cluster_key))

    log.info("== Stage 4: validate final cluster state before finishing ==")
    cluster = wait_for_cluster_ready(client, cluster_key)
    cluster_state = cluster.get("state") or cluster.get("lifecycleState")
    log.info("Cluster ready at the end of seed: %s (state=%s)", cluster_spec["displayName"], cluster_state)

    log.info("== Seed summary ==")
    log.info("Workspace root: %s", folder)
    log.info("Cluster: %s", cluster_spec["displayName"])
    log.info("Jobs: %s", ["cicd_seed_workflow", "cicd_seed_workflow_alt"])


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context")
    parser.add_argument("--auth-method", default=None)
    parser.add_argument("--config", default=DEFAULT_SOURCE_CONFIG_PATH)
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args(argv)
    setup_logging("cicd-seed")
    config_path = args.config
    context_demo = False
    if args.context:
        context = load_context(args.context)
        context_demo = context_demo_mode(context)
        if config_path == DEFAULT_SOURCE_CONFIG_PATH:
            config_path = config_paths_for_context(args.context, demo_mode=context_demo)[0]
        if not args.auth_method:
            args.auth_method = context_auth_method(context) or args.auth_method
    elif args.demo and config_path == DEFAULT_SOURCE_CONFIG_PATH:
        config_path = DEFAULT_DEMO_SOURCE_CONFIG_PATH
    cfg = load_config(config_path)
    ensure_seed_workspace(cfg, args.auth_method or DEFAULT_AUTH_METHOD)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_with_logged_errors(main))
