#!/usr/bin/env python3
"""Deploy a versioned bundle from the workspace Git folder into the target workspace."""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import quote

import requests
import yaml
from core.console_logging import LOGGER_NAME, format_elapsed_br, log_phase_header, poll_with_progress, run_with_logged_errors, setup_logging
from core.settings import (
    DEFAULT_AUTH_METHOD,
    DEFAULT_BUNDLE_API_VERSION,
    DEFAULT_BUNDLE_NAME,
    DEFAULT_BUNDLE_PATH_PREFIX,
    DEFAULT_DEPLOY_BUNDLE_API_VERSION,
    DEFAULT_DEPLOY_BUNDLE_PATH_PREFIX,
    DEFAULT_GIT_BRANCH,
    DEFAULT_GIT_PARENT_DIR,
    DEFAULT_HTTP_TIMEOUT_SECS,
    DEFAULT_PATH_PREFIX,
    DEFAULT_POLL_INTERVAL_SECS,
    DEFAULT_POLL_HTTP_TIMEOUT_SECS,
    DEFAULT_POLL_TIMEOUT_SECS,
    DEFAULT_SOURCE_CONFIG_PATH,
    DEFAULT_STAGE_BUNDLE_NAME,
    DEFAULT_TARGET_CONFIG_PATH,
    DEFAULT_VERIFY_TLS,
    DEPLOY_GIT_OPERATION_PARSE_RETRY_DELAYS_SECS,
    MAX_LOG_FILES_PER_COMMAND,
)

log = logging.getLogger(LOGGER_NAME)

_PREFIX_API_VERSION = {"aiDataPlatforms": "20260430", "dataLakes": "20240831"}
USER_SETTING_PATH_PREFIX = "aiDataPlatforms"
USER_SETTING_API_VERSION = _PREFIX_API_VERSION[USER_SETTING_PATH_PREFIX]
DEFAULT_API_VERSION = "20260430"
AIDP_RESOURCE_ID_KEYS = ("ocid", "resource_ocid", "data_lake_ocid")
VALID_AUTH_METHODS = {
    "api_key",
    "instance_principal",
    "oke_workload_identity",
    "resource_principal",
}
OCI_API_KEY_ENV_MAP = {
    "tenancy": "OCI_CLI_TENANCY",
    "user": "OCI_CLI_USER",
    "fingerprint": "OCI_CLI_FINGERPRINT",
    "key_file": "OCI_CLI_KEY_FILE",
}
WORKSPACE_OBJECT_METADATA_KEYS = (
    "system:folderType",
    "system:branch",
    "system:repoKey",
    "system:status",
    "system:bundleKey",
)


def _json_log_value(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    except Exception:
        return repr(value)


def log_debug_context(title: str, **fields: Any) -> None:
    log.debug("%s | %s", title, _json_log_value(fields))


def _friendly_async_status(status: Optional[str]) -> str:
    value = str(status or "").upper()
    mapping = {
        "IN_PROGRESS": "in progress",
        "SUCCEEDED": "succeeded",
        "FAILED": "failed",
        "CANCELED": "canceled",
    }
    return mapping.get(value, value.lower() or "unknown")


def _friendly_bundle_deploy_status(status: Optional[str]) -> str:
    value = str(status or "").upper()
    mapping = {
        "IN_PROGRESS": "in progress",
        "SUCCEEDED": "succeeded",
        "FAILED": "failed",
        "NOT_DEPLOYED": "not deployed",
        "CANCELED": "canceled",
    }
    return mapping.get(value, value.lower() or "unknown")


def _is_not_found_like_error(exc: Exception) -> bool:
    lowered = str(exc).lower()
    return (
        "http 404" in lowered
        or "notauthorizedornotfound" in lowered
        or "unknown resource" in lowered
    )


def _git_repo_key(payload: Dict[str, Any]) -> Optional[str]:
    return (
        payload.get("key")
        or payload.get("repoKey")
        or payload.get("gitRepositoryKey")
        or payload.get("git_repository_key")
    )


def _git_credential_key(payload: Dict[str, Any]) -> Optional[str]:
    return (
        payload.get("credentialKey")
        or payload.get("credential_key")
        or payload.get("gitCredentialKey")
        or payload.get("git_credential_key")
    )

REQUIRED_CONFIG_KEYS = [
    ("aidp", "region"),
    ("aidp", "path_prefix"),
    ("aidp", "workspace_key"),
    ("git", "repository_url"),
    ("git", "branch"),
    ("git", "parent_dir"),
    ("git", "bundle_path"),
]


def default_api_version_for(path_prefix: str) -> str:
    return _PREFIX_API_VERSION.get(path_prefix, DEFAULT_API_VERSION)


def has_git_credential_name(cfg: Dict[str, Any]) -> bool:
    git = cfg.get("git")
    return isinstance(git, dict) and bool(str(git.get("credential_name", "")).strip())


def resolve_aidp_resource_id(cfg: Dict[str, Any]) -> str:
    aidp = cfg.get("aidp") or {}
    for key in AIDP_RESOURCE_ID_KEYS:
        value = aidp.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError(
        "Missing required config key: one of aidp.ocid, aidp.resource_ocid, aidp.data_lake_ocid"
    )


def _repo_basename(repository_url: str) -> str:
    repo = repository_url.rstrip("/").split("/")[-1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    return repo


def resolve_folder_path(cfg: Dict[str, Any]) -> str:
    git = cfg["git"]
    repo = _repo_basename(git["repository_url"])
    return git["parent_dir"].rstrip("/") + "/" + repo


def resolve_versioned_bundle_path(cfg: Dict[str, Any]) -> str:
    return resolve_folder_path(cfg).rstrip("/") + "/" + cfg["git"]["bundle_path"].strip("/")


def resolve_stage_bundle_path(cfg: Dict[str, Any]) -> str:
    git = cfg["git"]
    stage_bundle_path = git.get("stage_bundle_path") or DEFAULT_STAGE_BUNDLE_NAME
    return resolve_folder_path(cfg).rstrip("/") + "/" + str(stage_bundle_path).strip("/")


def apply_config_defaults(cfg: Dict[str, Any]) -> Dict[str, Any]:
    cfg.setdefault("aidp", {})
    cfg.setdefault("git", {})
    cfg.setdefault("options", {})
    cfg["aidp"].setdefault("path_prefix", DEFAULT_PATH_PREFIX)
    cfg["aidp"].setdefault("bundle_path_prefix", DEFAULT_BUNDLE_PATH_PREFIX)
    cfg["aidp"].setdefault("bundle_api_version", DEFAULT_BUNDLE_API_VERSION)
    cfg["aidp"].setdefault("deploy_bundle_path_prefix", DEFAULT_DEPLOY_BUNDLE_PATH_PREFIX)
    cfg["aidp"].setdefault("deploy_bundle_api_version", DEFAULT_DEPLOY_BUNDLE_API_VERSION)
    cfg["git"].setdefault("branch", DEFAULT_GIT_BRANCH)
    cfg["git"].setdefault("parent_dir", DEFAULT_GIT_PARENT_DIR)
    cfg["git"].setdefault("bundle_path", DEFAULT_BUNDLE_NAME)
    cfg["git"].setdefault("stage_bundle_path", DEFAULT_STAGE_BUNDLE_NAME)
    cfg["options"].setdefault("http_timeout_secs", DEFAULT_HTTP_TIMEOUT_SECS)
    cfg["options"].setdefault("poll_http_timeout_secs", DEFAULT_POLL_HTTP_TIMEOUT_SECS)
    cfg["options"].setdefault("poll_interval_secs", DEFAULT_POLL_INTERVAL_SECS)
    cfg["options"].setdefault("poll_timeout_secs", DEFAULT_POLL_TIMEOUT_SECS)
    cfg["options"].setdefault("verify_tls", DEFAULT_VERIFY_TLS)
    return cfg


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle) or {}
    cfg = apply_config_defaults(cfg)
    missing = []
    for section, key in REQUIRED_CONFIG_KEYS:
        value = ((cfg.get(section) or {}).get(key)) if isinstance(cfg.get(section), dict) else None
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append("{}.{}".format(section, key))
    if missing:
        raise ValueError("Missing required config keys: " + ", ".join(missing))
    if not has_git_credential_name(cfg):
        raise ValueError("Missing required config key: git.credential_name")
    resolve_aidp_resource_id(cfg)
    return cfg


def select_auth_method(env: Dict[str, str]) -> str:
    explicit = env.get("AIDP_AUTH_METHOD", "").strip().lower()
    if explicit:
        return explicit
    if all(env.get(v) for v in OCI_API_KEY_ENV_MAP.values()):
        return "api_key"
    if env.get("OCI_RESOURCE_PRINCIPAL_VERSION"):
        return "resource_principal"
    return "instance_principal"


def build_api_key_config_from_env() -> Dict[str, str]:
    cfg = {}
    missing = []
    for key, env_name in OCI_API_KEY_ENV_MAP.items():
        value = os.environ.get(env_name, "").strip()
        if not value:
            missing.append(env_name)
        else:
            cfg[key] = value
    cfg["region"] = os.environ.get("OCI_CLI_REGION", "").strip()
    if not cfg["region"]:
        missing.append("OCI_CLI_REGION")
    if missing:
        raise RuntimeError(
            "Missing OCI API-key environment variables: {}".format(", ".join(sorted(missing)))
        )
    return cfg


def build_api_key_config() -> Dict[str, str]:
    try:
        return build_api_key_config_from_env()
    except RuntimeError as env_exc:
        import oci

        config_path = os.path.expanduser("~/.oci/config")
        if os.path.exists(config_path):
            profile = os.environ.get("OCI_CLI_PROFILE", "DEFAULT")
            cfg = oci.config.from_file(file_location=config_path, profile_name=profile)
            if not cfg.get("region"):
                raise RuntimeError(
                    "OCI config profile {!r} in {} is missing region".format(profile, config_path)
                )
            log.info("Using API-key config from %s profile %s.", config_path, profile)
            return cfg
        raise env_exc


def build_sdk_client_config(region: str) -> Dict[str, str]:
    # Some OCI SDK clients validate config keys even when a signer is supplied.
    # Reuse any available API-key/profile config and only fall back to region-only
    # when those values are truly unavailable.
    try:
        cfg = build_api_key_config()
    except RuntimeError:
        cfg = {}
    cfg["region"] = region
    return cfg


def build_signer(method: Optional[str] = None):
    import oci

    chosen = (method or select_auth_method(os.environ)).strip().lower()
    if chosen not in VALID_AUTH_METHODS:
        raise RuntimeError("Unsupported auth method: {}".format(chosen))
    if chosen == "api_key":
        oci_cfg = build_api_key_config()
        if all(oci_cfg.get(k) for k in ("tenancy", "user", "fingerprint", "key_file")):
            log.info("Using API-key signer from OCI config values.")
        return oci.signer.Signer(
            tenancy=oci_cfg["tenancy"],
            user=oci_cfg["user"],
            fingerprint=oci_cfg["fingerprint"],
            private_key_file_location=oci_cfg["key_file"],
        )
    if chosen == "oke_workload_identity":
        log.info("Using OKE workload-identity signer.")
        return oci.auth.signers.get_oke_workload_identity_resource_principal_signer()
    if chosen == "resource_principal":
        log.info("Using resource-principal signer.")
        return oci.auth.signers.get_resource_principals_signer()
    log.info("Using instance-principal signer.")
    return oci.auth.signers.InstancePrincipalsSecurityTokenSigner()


def _async_key(resp) -> Optional[str]:
    return (
        resp.headers.get("datalake-async-operation-key")
        or resp.headers.get("aidp-async-operation-key")
        or resp.headers.get("opc-work-request-id")
    )


def _find_setting_key_by_name(settings: Iterable[Dict[str, Any]], name: str) -> Optional[str]:
    for item in settings:
        current_name = item.get("name") or item.get("display_name")
        if current_name == name:
            return item.get("key")
    return None


def _ws_relpath(path: str) -> str:
    rel = path.lstrip("/")
    if rel.startswith("Workspace/"):
        rel = rel[len("Workspace/") :]
    return rel


class AidpClient:
    def __init__(self, cfg: Dict[str, Any], signer, dry_run: bool = False) -> None:
        aidp = cfg["aidp"]
        self.cfg = cfg
        self.region = aidp["region"]
        self.resource_id = resolve_aidp_resource_id(cfg)
        self.path_prefix = aidp["path_prefix"]
        self.api_version = str(aidp.get("api_version") or default_api_version_for(self.path_prefix))
        self.bundle_path_prefix = aidp.get("bundle_path_prefix", self.path_prefix)
        self.bundle_api_version = str(
            aidp.get("bundle_api_version") or default_api_version_for(self.bundle_path_prefix)
        )
        self.deploy_bundle_path_prefix = aidp.get("deploy_bundle_path_prefix", self.path_prefix)
        self.deploy_bundle_api_version = str(
            aidp.get("deploy_bundle_api_version")
            or default_api_version_for(self.deploy_bundle_path_prefix)
        )
        self.workspace_key = aidp.get("workspace_key")
        self.signer = signer
        self.dry_run = dry_run
        self.verify_tls = bool(cfg.get("options", {}).get("verify_tls", DEFAULT_VERIFY_TLS))
        self.http_timeout = int(cfg.get("options", {}).get("http_timeout_secs", DEFAULT_HTTP_TIMEOUT_SECS))
        self.poll_http_timeout = int(
            cfg.get("options", {}).get("poll_http_timeout_secs", DEFAULT_POLL_HTTP_TIMEOUT_SECS)
        )
        self.poll_interval = int(cfg.get("options", {}).get("poll_interval_secs", DEFAULT_POLL_INTERVAL_SECS))
        self.poll_timeout = int(cfg.get("options", {}).get("poll_timeout_secs", DEFAULT_POLL_TIMEOUT_SECS))
        self._sdk = None

    def _surface_url(self, api_version: str, path_prefix: str, *parts: str) -> str:
        base = "https://aidp.{}.oci.oraclecloud.com/{}/{}/{}".format(
            self.region, api_version, path_prefix, self.resource_id
        )
        if not parts:
            return base
        return "/".join([base] + [p.strip("/") for p in parts])

    def lake_url(self, *parts: str) -> str:
        return self._surface_url(self.api_version, self.path_prefix, *parts)

    def user_setting_url(self, *parts: str) -> str:
        return self._surface_url(USER_SETTING_API_VERSION, USER_SETTING_PATH_PREFIX, *parts)

    def ws_url(self, *parts: str) -> str:
        if not self.workspace_key:
            raise RuntimeError("aidp.workspace_key is required for workspace-scoped operations")
        return self.lake_url("workspaces", self.workspace_key, *parts)

    def bundle_ws_url(self, *parts: str) -> str:
        if not self.workspace_key:
            raise RuntimeError("aidp.workspace_key is required for workspace-scoped operations")
        return self._surface_url(
            self.bundle_api_version,
            self.bundle_path_prefix,
            "workspaces",
            self.workspace_key,
            *parts,
        )

    def deploy_bundle_ws_url(self, *parts: str) -> str:
        if not self.workspace_key:
            raise RuntimeError("aidp.workspace_key is required for workspace-scoped operations")
        return self._surface_url(
            self.deploy_bundle_api_version,
            self.deploy_bundle_path_prefix,
            "workspaces",
            self.workspace_key,
            *parts,
        )

    def _oci_cfg(self) -> Dict[str, str]:
        return build_sdk_client_config(self.region)

    def _find_value_recursively(self, payload: Any, keys: Sequence[str]) -> Optional[Any]:
        normalized_keys = {str(key).lower() for key in keys}
        if isinstance(payload, dict):
            for key, value in payload.items():
                if str(key).lower() in normalized_keys and value not in (None, ""):
                    return value
            for value in payload.values():
                found = self._find_value_recursively(value, keys)
                if found not in (None, ""):
                    return found
            return None
        if isinstance(payload, list):
            for value in payload:
                found = self._find_value_recursively(value, keys)
                if found not in (None, ""):
                    return found
        return None

    def _sdk_clients(self) -> Dict[str, Any]:
        if self._sdk is not None:
            return self._sdk
        try:
            oci = importlib.import_module("oci")
            aidp_mod = importlib.import_module("aidp_python_client.aidataplatform_dp")
            sdk = {
                "workspace": aidp_mod.workspace_client.WorkspaceClient(self._oci_cfg(), signer=self.signer),
                "git": aidp_mod.git_client.GitClient(self._oci_cfg(), signer=self.signer),
                "bundle": aidp_mod.bundle_client.BundleClient(self._oci_cfg(), signer=self.signer),
                "workspace_object": aidp_mod.workspace_object_client.WorkspaceObjectClient(
                    self._oci_cfg(), signer=self.signer
                ),
                "user_setting": aidp_mod.user_setting_client.UserSettingClient(
                    self._oci_cfg(), signer=self.signer
                ),
                "async": aidp_mod.async_operations_client.AsyncOperationsClient(
                    self._oci_cfg(), signer=self.signer
                ),
                "models": aidp_mod.models,
                "oci": oci,
            }
            self._sdk = sdk
        except Exception as exc:
            log.info("AIDP generated SDK unavailable; REST fallback will be used. reason=%s", exc)
            self._sdk = {}
        return self._sdk

    def request(
        self,
        method: str,
        url: str,
        body: Optional[dict] = None,
        params: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> requests.Response:
        headers = {"accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["content-type"] = "application/json"
        log_debug_context(
            "HTTP request",
            method=method,
            url=url,
            params=params,
            body=body,
            verify_tls=self.verify_tls,
            workspace_key=self.workspace_key,
            resource_id=self.resource_id,
        )
        resp = requests.request(
            method,
            url,
            data=data,
            params=params,
            headers=headers,
            auth=self.signer,
            verify=self.verify_tls,
            timeout=timeout if timeout is not None else self.http_timeout,
        )
        log_debug_context(
            "HTTP response",
            method=method,
            url=url,
            status_code=resp.status_code,
            opc_request_id=resp.headers.get("opc-request-id"),
            async_key=_async_key(resp),
            response_text=resp.text,
        )
        return resp

    def request_ok(
        self,
        method: str,
        url: str,
        body: Optional[dict] = None,
        params: Optional[dict] = None,
        ok: Sequence[int] = (200, 201, 202, 204),
        timeout: Optional[float] = None,
    ) -> requests.Response:
        resp = self.request(method, url, body=body, params=params, timeout=timeout)
        if resp.status_code not in ok:
            log_debug_context(
                "HTTP request rejected",
                method=method,
                url=url,
                expected_statuses=list(ok),
                status_code=resp.status_code,
                response_text=resp.text,
                opc_request_id=resp.headers.get("opc-request-id"),
            )
            raise RuntimeError(
                "{} {} -> HTTP {}: {} (opc-request-id={})".format(
                    method, url, resp.status_code, resp.text, resp.headers.get("opc-request-id")
                )
            )
        return resp

    def get_resource_details(self) -> Dict[str, Any]:
        try:
            oci = importlib.import_module("oci")
            client = oci.ai_data_platform.AiDataPlatformClient(self._oci_cfg(), signer=self.signer)
            resp = client.get_ai_data_platform(self.resource_id)
            data = getattr(resp, "data", None)
            if data is not None:
                payload = self._model_to_dict(data)
                if isinstance(payload, dict) and payload:
                    log_debug_context(
                        "AIDP resource details fetched via OCI API",
                        resource_id=self.resource_id,
                        payload=payload,
                    )
                    return payload
        except Exception as exc:
            log.debug("AIDP resource details via OCI API failed for %s: %s", self.resource_id, exc)
        payload = self.request_ok("GET", self.lake_url(), ok=(200,)).json()
        if isinstance(payload, dict):
            log_debug_context("AIDP resource details fetched via REST fallback", resource_id=self.resource_id, payload=payload)
            return payload
        return {}

    def get_resource_compartment_id(self) -> str:
        payload = self.get_resource_details()
        value = self._find_value_recursively(payload, ("compartmentId", "compartment_id"))
        compartment_id = str(value or "").strip()
        if not compartment_id:
            raise RuntimeError("Could not automatically identify the compartment for the AIDP resource.")
        log_debug_context(
            "AIDP resource compartment resolved",
            resource_id=self.resource_id,
            compartment_id=compartment_id,
        )
        return compartment_id

    def list_all(self, url: str, params: Optional[dict] = None) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        page = None
        while True:
            merged = dict(params or {})
            if page:
                merged["page"] = page
            resp = self.request_ok("GET", url, params=merged, ok=(200,))
            payload = resp.json()
            if isinstance(payload, list):
                items.extend(payload)
            elif isinstance(payload, dict):
                block = payload.get("items")
                if isinstance(block, list):
                    items.extend(block)
            page = resp.headers.get("opc-next-page")
            if not page:
                return items

    def list_workspace_objects(self, parent_path: str) -> List[Dict[str, Any]]:
        params = {
            "path": parent_path,
            "metadataKeys": ",".join(WORKSPACE_OBJECT_METADATA_KEYS),
        }
        try:
            resp = self.request_ok("GET", self.ws_url("objects"), params=params, ok=(200,))
        except Exception as exc:
            message = str(exc)
            if "HTTP 404" in message and "Unknown resource" in message:
                return []
            raise
        payload = resp.json()
        if isinstance(payload, list):
            items = [item for item in payload if isinstance(item, dict)]
            log_debug_context("Workspace objects listed", parent_path=parent_path, count=len(items), items=items)
            return items
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            items = [item for item in payload["items"] if isinstance(item, dict)]
            log_debug_context("Workspace objects listed", parent_path=parent_path, count=len(items), items=items)
            return items
        log_debug_context("Workspace objects listed", parent_path=parent_path, count=0, payload=payload)
        return []

    def list_workspace_objects_via_legacy_api(self, parent_path: str, limit: int = 200) -> List[Dict[str, Any]]:
        encoded_path = quote(_ws_relpath(parent_path), safe="")
        metadata_keys = quote(",".join(WORKSPACE_OBJECT_METADATA_KEYS), safe="")
        url = "{}?path={}&limit={}&metadataKeys={}".format(
            self.ws_url("objects"),
            encoded_path,
            limit,
            metadata_keys,
        )
        log.debug("GET %s", url)
        resp = requests.get(
            url,
            headers={"accept": "application/json"},
            auth=self.signer,
            verify=self.verify_tls,
            timeout=60,
        )
        log.debug("-> HTTP %s opc-request-id=%s", resp.status_code, resp.headers.get("opc-request-id"))
        if resp.status_code == 404 and "Unknown resource" in (resp.text or ""):
            return []
        if resp.status_code != 200:
            raise RuntimeError(
                "Legacy list {} -> HTTP {}: {}".format(parent_path, resp.status_code, resp.text)
            )
        payload = resp.json()
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            items = [item for item in payload["items"] if isinstance(item, dict)]
            log_debug_context("Legacy workspace objects listed", parent_path=parent_path, limit=limit, count=len(items), items=items)
            return items
        if isinstance(payload, list):
            items = [item for item in payload if isinstance(item, dict)]
            log_debug_context("Legacy workspace objects listed", parent_path=parent_path, limit=limit, count=len(items), items=items)
            return items
        log_debug_context("Legacy workspace objects listed", parent_path=parent_path, limit=limit, count=0, payload=payload)
        return []

    def workspace_item_metadata(self, item: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not isinstance(item, dict):
            return {}
        metadata = item.get("metadata")
        if isinstance(metadata, dict):
            return metadata
        normalized = {}
        for key in WORKSPACE_OBJECT_METADATA_KEYS:
            value = item.get(key)
            if value not in (None, ""):
                normalized[key] = value
        return normalized

    def workspace_item(self, object_path: str) -> Optional[Dict[str, Any]]:
        parent = os.path.dirname(object_path.rstrip("/")) or "/"
        name = os.path.basename(object_path.rstrip("/"))
        for item in self.list_workspace_objects(parent):
            current = item.get("path") or item.get("objectPath") or item.get("name")
            if current == object_path or current == name:
                log_debug_context("Workspace item resolved", object_path=object_path, parent=parent, item=item)
                return item
        log_debug_context("Workspace item not found", object_path=object_path, parent=parent)
        return None

    def workspace_object_exists(self, object_path: str) -> bool:
        return self.workspace_item(object_path) is not None

    def workspace_object_head(self, object_path: str, should_include_metadata: bool = True) -> Dict[str, Any]:
        params = {"path": object_path}
        if should_include_metadata:
            params["shouldIncludeMetadata"] = "true"
        attempts = (
            (self.ws_url("objects", "head"), params),
            (self.ws_url("objects", "actions", "head"), params),
            (self.ws_url("objects", _ws_relpath(object_path)), {"shouldIncludeMetadata": "true"} if should_include_metadata else None),
        )
        for url, current_params in attempts:
            try:
                resp = self.request_ok("GET", url, params=current_params, ok=(200,))
            except Exception as exc:
                log.debug("workspace object head probe failed for %s via %s: %s", object_path, url, exc)
                continue
            payload = resp.json()
            if isinstance(payload, dict):
                return payload
        return {}

    def _metadata_has_git_evidence(self, value: Any) -> bool:
        if isinstance(value, dict):
            lowered = {str(key).lower(): nested for key, nested in value.items()}
            repo_key = lowered.get("repokey")
            is_associated = lowered.get("isassociated")
            if repo_key and str(repo_key).strip():
                return True
            if isinstance(is_associated, bool) and is_associated:
                return True
            if isinstance(is_associated, str) and is_associated.strip().lower() == "true":
                return True
            return any(self._metadata_has_git_evidence(nested) for nested in value.values())
        if isinstance(value, list):
            return any(self._metadata_has_git_evidence(item) for item in value)
        return False

    def classify_workspace_folder(self, folder_path: str) -> Dict[str, Any]:
        folder_path = folder_path.rstrip("/") or "/"
        item = self.workspace_item(folder_path)
        head_payload = self.workspace_object_head(folder_path, should_include_metadata=True)
        git_metadata = self.git_folder_association(folder_path)
        item_metadata = self.workspace_item_metadata(item)
        item_type = str(
            item.get("type")
            or item.get("objectType")
            or item.get("object_type")
            or item.get("resourceType")
            or item.get("resource_type")
            or ""
        ).upper() if isinstance(item, dict) else ""
        aidp_dir = folder_path.rstrip("/") + "/.aidp"
        workbench_path = folder_path.rstrip("/") + "/aidp_workbench.yaml"
        origins_path = aidp_dir + "/resource_origins.yaml"
        workbench_present = self.workspace_object_exists(workbench_path)
        aidp_dir_present = self.workspace_object_exists(aidp_dir)
        origins_present = self.workspace_object_exists(origins_path)
        bundle_key = item_metadata.get("system:bundleKey")
        repo_key = item_metadata.get("system:repoKey")
        branch_name = item_metadata.get("system:branch")
        folder_type = item_metadata.get("system:folderType")
        is_git_backed = bool(repo_key or branch_name) or self._metadata_has_git_evidence(head_payload) or self._metadata_has_git_evidence(git_metadata)
        classification = "normal_folder"
        if item_type == "FILE":
            classification = "git_file" if is_git_backed else "normal_file"
        elif bundle_key or workbench_present or origins_present:
            classification = "bundle_folder"
        elif is_git_backed:
            classification = "git_folder"
        elif folder_type:
            classification = "folder:{}".format(folder_type)
        facts = {
            "path": folder_path,
            "exists": item is not None,
            "workspace_item": item,
            "workspace_item_metadata": item_metadata,
            "workspace_item_type": item_type,
            "workspace_head": head_payload,
            "git_metadata": git_metadata,
            "is_git_backed": is_git_backed,
            "bundle_key": bundle_key,
            "repo_key": repo_key,
            "branch": branch_name,
            "folder_type": folder_type,
            "has_aidp_dir": aidp_dir_present,
            "has_aidp_workbench": workbench_present,
            "has_resource_origins": origins_present,
            "classification": classification,
        }
        log_debug_context("Workspace folder classified", **facts)
        return facts

    def delete_workspace_object_if_present(self, object_path: str) -> bool:
        if not self.workspace_object_exists(object_path):
            log_debug_context("Workspace object already absent", object_path=object_path)
            return False
        sdk = self._sdk_clients()
        if sdk:
            try:
                resp = sdk["workspace_object"].delete_workspace_object(
                    self.resource_id,
                    self.workspace_key,
                    object_path,
                )
                key = _async_key(resp)
                if key:
                    self.wait_for_async(key, purpose="Workspace object removal")
                log_debug_context("Workspace object removed via SDK", object_path=object_path, async_key=key)
                return True
            except Exception as exc:
                log.debug(
                    "SDK delete_workspace_object failed for %s; using REST fallback. error=%s",
                    object_path,
                    exc,
                )
        resp = self.request("POST", self.ws_url("actions", "deleteObject"), body={"path": object_path})
        if resp.status_code == 404:
            log.warning(
                "deleteObject returned 404 for %s after existence check; treating as already absent/inconsistent backend state",
                object_path,
            )
            return False
        if resp.status_code not in (200, 202, 204):
            raise RuntimeError(
                "deleteObject {} -> HTTP {}: {}".format(object_path, resp.status_code, resp.text)
            )
        key = _async_key(resp)
        if key:
            self.wait_for_async(key, purpose="Workspace object removal")
        log_debug_context("Workspace object removed via REST", object_path=object_path, async_key=key)
        return True

    def _model_to_dict(self, value: Any) -> Dict[str, Any]:
        if hasattr(value, "swagger_types"):
            result = {}
            for key in value.swagger_types:
                result[key] = getattr(value, key, None)
            return result
        if isinstance(value, dict):
            return value
        return json.loads(json.dumps(value, default=str))

    def list_git_account_settings(self) -> List[Dict[str, Any]]:
        sdk = self._sdk_clients()
        if sdk:
            try:
                resp = sdk["user_setting"].list_user_settings(self.resource_id)
                data = getattr(resp, "data", None)
                if isinstance(data, list):
                    items = data
                else:
                    items = (
                        getattr(data, "items", None)
                        or getattr(data, "user_settings", None)
                        or []
                    )
                return [self._model_to_dict(item) for item in list(items)]
            except Exception as exc:
                log_debug_context(
                    "Git user settings listing via SDK failed; using REST fallback",
                    resource_id=self.resource_id,
                    error=str(exc),
                )
        return self.list_all(self.user_setting_url("userSettings"))

    def resolve_workspace_key_by_name(self, workspace_name: str) -> str:
        item = self.find_workspace_by_name(workspace_name)
        if item:
            key = item.get("key")
            if key:
                return str(key)
        raise RuntimeError("Workspace named {!r} was not found.".format(workspace_name))

    def list_workspaces(self, timeout: Optional[float] = None) -> List[Dict[str, Any]]:
        sdk = self._sdk_clients()
        if sdk:
            try:
                resp = sdk["workspace"].list_workspaces(self.resource_id)
                data = getattr(resp, "data", None)
                if isinstance(data, list):
                    items = data
                else:
                    items = (
                        getattr(data, "items", None)
                        or getattr(data, "workspaces", None)
                        or []
                    )
                result = [self._model_to_dict(item) for item in list(items)]
                log_debug_context("Workspaces listed", resource_id=self.resource_id, count=len(result), items=result)
                return result
            except Exception as exc:
                log_debug_context(
                    "Workspace listing via SDK failed; using REST fallback",
                    resource_id=self.resource_id,
                    error=str(exc),
                )
        resp = self.request_ok("GET", self.lake_url("workspaces"), ok=(200,), timeout=timeout)
        payload = resp.json()
        if isinstance(payload, list):
            result = [item for item in payload if isinstance(item, dict)]
        elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
            result = [item for item in payload["items"] if isinstance(item, dict)]
        else:
            result = []
        log_debug_context("Workspaces listed", resource_id=self.resource_id, count=len(result), items=result)
        return result

    def find_workspace_by_name(self, workspace_name: str, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        for item in self.list_workspaces(timeout=timeout):
            current_name = (
                item.get("displayName")
                or item.get("display_name")
                or item.get("name")
            )
            if current_name == workspace_name:
                log_debug_context("Workspace resolved by name", workspace_name=workspace_name, workspace=item)
                return item
        log_debug_context("Workspace not found by name", workspace_name=workspace_name)
        return None

    def get_workspace(self, workspace_key: str) -> Dict[str, Any]:
        sdk = self._sdk_clients()
        if sdk and hasattr(sdk.get("workspace"), "get_workspace"):
            resp = sdk["workspace"].get_workspace(self.resource_id, workspace_key)
            data = getattr(resp, "data", None)
            if data is not None:
                payload = self._model_to_dict(data)
                log_debug_context("Workspace fetched", workspace_key=workspace_key, workspace=payload)
                return payload
        payload = self.request_ok("GET", self.lake_url("workspaces", workspace_key), ok=(200,)).json()
        log_debug_context("Workspace fetched", workspace_key=workspace_key, workspace=payload)
        return payload if isinstance(payload, dict) else {}

    def create_workspace(self, workspace_name: str, description: Optional[str] = None) -> Dict[str, Any]:
        sdk = self._sdk_clients()
        log_debug_context(
            "Create workspace requested",
            workspace_name=workspace_name,
            description=description,
            resource_id=self.resource_id,
        )
        if sdk and hasattr(sdk.get("workspace"), "create_workspace"):
            models = sdk.get("models")
            details = None
            if models and hasattr(models, "CreateWorkspaceDetails"):
                details = models.CreateWorkspaceDetails(
                    display_name=workspace_name,
                    description=description,
                )
            if details is not None:
                resp = sdk["workspace"].create_workspace(self.resource_id, details)
                async_key = _async_key(resp)
                if async_key:
                    self.wait_for_async(async_key, purpose="Workspace creation")
                created = self.find_workspace_by_name(workspace_name)
                if created:
                    return created
        body_candidates = [
            {"displayName": workspace_name, "description": description},
            {"name": workspace_name, "description": description},
            {"display_name": workspace_name, "description": description},
        ]
        last_error: Optional[Exception] = None
        for body in body_candidates:
            try:
                resp = self.request_ok("POST", self.lake_url("workspaces"), body=body, ok=(200, 201, 202))
                async_key = _async_key(resp)
                if async_key:
                    self.wait_for_async(async_key, purpose="Workspace creation")
                created = self.find_workspace_by_name(workspace_name)
                if created:
                    return created
            except Exception as exc:
                last_error = exc
                log_debug_context("Create workspace candidate failed", body=body, error=str(exc))
                continue
        if last_error:
            raise last_error
        raise RuntimeError("Workspace {!r} could not be created.".format(workspace_name))

    def wait_for_workspace_visible(
        self,
        workspace_name: str,
        purpose: str,
    ) -> Dict[str, Any]:
        return poll_with_progress(
            purpose,
            timeout_secs=self.poll_timeout,
            fetch_interval_secs=self.poll_interval,
            fetch_fn=lambda: self.find_workspace_by_name(workspace_name, timeout=self.poll_http_timeout),
            success_fn=lambda current: bool(current),
            timeout_message_fn=lambda _current, elapsed: "{}: workspace {!r} was not visible after {}".format(
                purpose,
                workspace_name,
                format_elapsed_br(elapsed),
            ),
            logger=log,
        )

    def wait_for_workspace_absent(
        self,
        workspace_name: str,
        purpose: str,
    ) -> None:
        poll_with_progress(
            purpose,
            timeout_secs=self.poll_timeout,
            fetch_interval_secs=self.poll_interval,
            fetch_fn=lambda: self.find_workspace_by_name(workspace_name, timeout=self.poll_http_timeout),
            progress_suffix_fn=lambda current: (
                "state={}".format(
                    str((current or {}).get("lifecycle_state") or (current or {}).get("lifecycle_details") or "ABSENT")
                )
                if current
                else "state=ABSENT"
            ),
            success_fn=lambda current: not bool(current),
            checkpoint_interval_secs=30,
            checkpoint_message_fn=lambda current, elapsed: (
                "{} still in progress after {} ({})".format(
                    purpose,
                    format_elapsed_br(elapsed),
                    "state={}".format(
                        str((current or {}).get("lifecycle_state") or (current or {}).get("lifecycle_details") or "UNKNOWN")
                    )
                    if current
                    else "state=ABSENT",
                )
            ),
            timeout_message_fn=lambda _current, elapsed: "{}: workspace {!r} still exists after {}".format(
                purpose,
                workspace_name,
                format_elapsed_br(elapsed),
            ),
            logger=log,
        )

    def ensure_workspace(
        self,
        workspace_name: str,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        existing = self.find_workspace_by_name(workspace_name)
        if existing:
            workspace_key = existing.get("key")
            if workspace_key:
                self.workspace_key = str(workspace_key)
            log.info("Workspace ready for use: %s", workspace_name)
            log_debug_context(
                "Workspace already existed",
                workspace_name=workspace_name,
                workspace_key=workspace_key,
                workspace=existing,
            )
            return {"workspace": existing, "created": False}
        log.info("Creating workspace %s", workspace_name)
        try:
            created = self.create_workspace(workspace_name, description=description)
        except Exception as exc:
            message = str(exc)
            if "Workspace Exists with the same name" in message or "'code': 'Conflict'" in message or '"code" : "Conflict"' in message:
                log.info("Workspace name already exists; reusing the existing workspace when it becomes visible")
                visible = self.wait_for_workspace_visible(workspace_name, "Workspace conflict reconciliation")
                workspace_key = visible.get("key")
                if workspace_key:
                    self.workspace_key = str(workspace_key)
                log_debug_context(
                    "Workspace conflict reconciled to existing workspace",
                    workspace_name=workspace_name,
                    workspace_key=workspace_key,
                    workspace=visible,
                    error=message,
                )
                return {"workspace": visible, "created": False}
            raise
        visible = created or self.wait_for_workspace_visible(workspace_name, "Workspace creation")
        workspace_key = visible.get("key")
        if workspace_key:
            self.workspace_key = str(workspace_key)
        log_debug_context(
            "Workspace created",
            workspace_name=workspace_name,
            workspace_key=workspace_key,
            workspace=visible,
        )
        return {"workspace": visible, "created": True}

    def delete_workspace(self, workspace_key: str) -> bool:
        sdk = self._sdk_clients()
        log_debug_context(
            "Delete workspace requested",
            workspace_key=workspace_key,
            resource_id=self.resource_id,
        )
        if sdk and hasattr(sdk.get("workspace"), "delete_workspace"):
            try:
                resp = sdk["workspace"].delete_workspace(self.resource_id, workspace_key)
                async_key = _async_key(resp)
                if async_key:
                    log_debug_context(
                        "Workspace removal accepted via SDK",
                        workspace_key=workspace_key,
                        async_key=async_key,
                    )
                return True
            except Exception as exc:
                log_debug_context("Delete workspace via SDK failed", workspace_key=workspace_key, error=str(exc))
        resp = self.request("DELETE", self.lake_url("workspaces", workspace_key))
        if resp.status_code == 404:
            return False
        if resp.status_code not in (200, 202, 204):
            raise RuntimeError(
                "delete workspace {} -> HTTP {}: {}".format(workspace_key, resp.status_code, resp.text)
            )
        async_key = _async_key(resp)
        if async_key:
            log_debug_context(
                "Workspace removal accepted via REST",
                workspace_key=workspace_key,
                async_key=async_key,
            )
        return True

    def delete_workspace_by_name(self, workspace_name: str) -> Dict[str, Any]:
        existing = self.find_workspace_by_name(workspace_name)
        if not existing:
            log.info("Workspace already absent: %s", workspace_name)
            log_debug_context("Workspace already absent before destroy", workspace_name=workspace_name)
            return {"workspace_name": workspace_name, "workspace_key": "", "deleted": False, "existed": False}
        workspace_key = str(existing.get("key") or "")
        deleted = self.delete_workspace(workspace_key) if workspace_key else False
        self.wait_for_workspace_absent(workspace_name, "Workspace removal")
        log_debug_context(
            "Workspace removed",
            workspace_name=workspace_name,
            workspace_key=workspace_key,
            workspace=existing,
            deleted=deleted,
        )
        return {
            "workspace_name": workspace_name,
            "workspace_key": workspace_key,
            "deleted": deleted,
            "existed": True,
        }

    def resolve_git_credential_key(self, credential_name: str) -> str:
        settings = self.list_git_account_settings()
        key = _find_setting_key_by_name(settings, credential_name)
        if not key:
            raise RuntimeError(
                "AIDP git credential {!r} was not found. Create it in AIDP first.".format(
                    credential_name
                )
            )
        log.info("Git credential validated: %s", credential_name)
        log.debug("resolved git credential %r -> key %s", credential_name, key)
        return key

    def ensure_directory(self, path: str, purpose: Optional[str] = None) -> None:
        delays = DEPLOY_GIT_OPERATION_PARSE_RETRY_DELAYS_SECS
        last_error = None
        label = purpose or "ensure directory"
        verbose = bool(purpose)
        for idx, delay in enumerate(delays, start=1):
            if idx > 1 and delay:
                time.sleep(delay)
            log_debug_context(
                "Ensure directory attempt",
                path=path,
                purpose=label,
                attempt=idx,
                total_attempts=len(delays),
                retry_delay_secs=delay,
            )
            resp = self.request("POST", self.ws_url("actions", "mkdir"), body={"path": path})
            if resp.status_code == 201:
                if verbose:
                    log.info("%s: directory created at %s", label, path)
                else:
                    log.debug("%s: directory created at %s", label, path)
                return True
            if resp.status_code == 409:
                if verbose:
                    log.info("%s: directory already exists at %s", label, path)
                else:
                    log.debug("%s: directory already exists at %s", label, path)
                return False
            body = resp.text or ""
            if resp.status_code == 400 and "CannotParseRequest" in body and idx < len(delays):
                log.warning(
                    "%s: attempt %s/%s failed due to a transient error; retrying in %ss",
                    label,
                    idx,
                    len(delays),
                    delays[idx],
                )
                last_error = RuntimeError("mkdir {} -> HTTP {}: {}".format(path, resp.status_code, body))
                continue
            raise RuntimeError(
                "mkdir {} -> HTTP {}: {}".format(path, resp.status_code, body)
            )
        if last_error:
            raise last_error

    def git_folder_metadata(
        self,
        folder_path: Optional[str] = None,
        resource_type: str = "FOLDER",
    ) -> Any:
        params = {
            "resourceType": resource_type,
            "folderPath": _ws_relpath(folder_path or "/"),
        }
        resp = self.request_ok("GET", self.ws_url("gitFolderMetadata"), params=params, ok=(200,))
        return resp.json()

    def _iter_git_folder_metadata_items(self, payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            items = payload.get("items")
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
            return [payload]
        return []

    def git_folder_association(self, folder_path: str) -> Dict[str, Any]:
        payload = self.git_folder_metadata(folder_path=folder_path)
        items = self._iter_git_folder_metadata_items(payload)
        rel_path = _ws_relpath(folder_path)
        for item in items:
            current = item.get("gitFolderPath") or item.get("git_folder_path") or item.get("folderPath")
            if current in (folder_path, rel_path):
                if isinstance(payload, dict):
                    merged = dict(payload)
                    merged.update(item)
                    log_debug_context("Git folder association resolved", folder_path=folder_path, association=merged)
                    return merged
                log_debug_context("Git folder association resolved", folder_path=folder_path, association=item)
                return item
        if isinstance(payload, dict):
            log_debug_context("Git folder association fallback payload", folder_path=folder_path, association=payload)
            return payload
        log_debug_context("Git folder association empty", folder_path=folder_path, payload=payload)
        return {}

    def create_git_folder(
        self, folder_path: str, repository_url: str, branch: str, credential_key: str
    ) -> Any:
        log_debug_context(
            "Create git folder requested",
            folder_path=folder_path,
            repository_url=repository_url,
            branch=branch,
            credential_key=credential_key,
        )
        sdk = self._sdk_clients()
        if sdk and hasattr(sdk.get("git"), "create_git_folder"):
            try:
                details = sdk["models"].CreateGitFolderDetails(
                    folder_path=folder_path,
                    git_repository_url=repository_url,
                    branch_name=branch,
                    credential_key=credential_key,
                )
                return sdk["git"].create_git_folder(self.resource_id, self.workspace_key, details)
            except Exception as exc:
                raise RuntimeError(
                    "create git folder {} failed. Likely a STALE git association "
                    "(docs/aidp-git-folder-issue.md): remove the gitRepository server-side, "
                    "or point git.parent_dir at a fresh path. Details: {}".format(folder_path, exc)
                )
        elif sdk:
            log.info("generated SDK GitClient has no create_git_folder(); using REST fallback")
        body = {
            "folderPath": _ws_relpath(folder_path),
            "gitRepositoryUrl": repository_url,
            "branchName": branch,
            "credentialKey": credential_key,
            "description": None,
            "gitProviderKey": None,
        }
        return self.request_ok("POST", self.ws_url("gitFolders"), body=body, ok=(200, 201, 202))

    def git_pull(self, folder_path: str, branch: str) -> Any:
        sdk = self._sdk_clients()
        metadata = self.get_git_repository(folder_path, should_include_credential_key=False)
        repo_key = _git_repo_key(metadata)
        if not repo_key:
            raise RuntimeError("git repository key not found for {}".format(folder_path))
        log_debug_context("Git pull requested", folder_path=folder_path, branch=branch, repo_key=repo_key, metadata=metadata)
        body = {"gitFolderPath": _ws_relpath(folder_path), "branchName": branch, "pullAction": "PULL"}
        if sdk and hasattr(sdk.get("git"), "pull_git_repository"):
            details = sdk["models"].GitPullDetails(
                git_folder_path=_ws_relpath(folder_path),
                branch_name=branch,
                pull_action="PULL",
            )
            return sdk["git"].pull_git_repository(
                self.resource_id, self.workspace_key, repo_key, details
            )
        return self.request_ok(
            "POST",
            self.ws_url("gitRepositories", repo_key, "actions", "pull"),
            body=body,
            ok=(200, 202),
        )

    def get_git_repository(
        self, folder_path: str, should_include_credential_key: bool = True
    ) -> Dict[str, Any]:
        item = self.git_folder_association(folder_path)
        repo_key = _git_repo_key(item)
        if repo_key:
            params = {"shouldIncludeCredentialKey": "true"} if should_include_credential_key else None
            payload = self.request_ok(
                "GET",
                self.ws_url("gitRepositories", repo_key),
                params=params,
                ok=(200,),
            ).json()
            log_debug_context(
                "Git repository resolved",
                folder_path=folder_path,
                repo_key=repo_key,
                include_credential_key=should_include_credential_key,
                repository=payload,
            )
            return payload
        if item:
            log_debug_context(
                "Git repository inferred from association payload",
                folder_path=folder_path,
                include_credential_key=should_include_credential_key,
                repository=item,
            )
            return item
        log_debug_context("Git repository not found", folder_path=folder_path, include_credential_key=should_include_credential_key)
        return {}

    def reassociate_git_credential(self, folder_path: str, credential_key: str) -> None:
        repo = self.get_git_repository(folder_path, should_include_credential_key=False)
        repo_key = _git_repo_key(repo)
        if not repo_key:
            log.warning(
                "git repository key not found for %s; skipping credential re-association",
                folder_path,
            )
            return
        body = {"credentialKey": credential_key}
        log_debug_context("Reassociate git credential", folder_path=folder_path, repo_key=repo_key, credential_key=credential_key)
        self.request_ok(
            "PUT",
            self.ws_url("gitRepositories", repo_key),
            body=body,
            ok=(200, 202, 204),
        )

    def ensure_git_folder_credential(self, folder_path: str, credential_key: str) -> None:
        repo = self.get_git_repository(folder_path, should_include_credential_key=True)
        repo_key = _git_repo_key(repo)
        current = _git_credential_key(repo)
        if current == credential_key:
            log.info("git folder credential already correct (%s)", credential_key)
            return
        if not repo_key:
            log.warning(
                "git folder %s is associated but repository key is unavailable; leaving credential unchanged",
                folder_path,
            )
            return
        self.reassociate_git_credential(folder_path, credential_key)
        log.info("re-associated git folder credential to %s", credential_key)

    def put_workspace_file(
        self,
        path: str,
        content: bytes,
        object_type: str = "FILE",
        overwrite: bool = True,
        description: Optional[str] = None,
        base64_encoded: bool = False,
    ) -> Any:
        log_debug_context(
            "Put workspace file requested",
            path=path,
            object_type=object_type,
            overwrite=overwrite,
            description=description,
            base64_encoded=base64_encoded,
            content_bytes=len(content or b""),
        )
        sdk = self._sdk_clients()
        if sdk:
            return sdk["workspace_object"].create_workspace_object(
                self.resource_id,
                self.workspace_key,
                content,
                path,
                type=object_type,
                is_overwrite=overwrite,
                is_upload_file_base64_encoded=base64_encoded,
                object_description=description,
                should_update_recent=False,
            )
        headers = {"type": object_type, "path": path}
        if overwrite:
            headers["is-overwrite"] = "true"
        if base64_encoded:
            headers["is-upload-file-base64-encoded"] = "true"
        params = {}
        if description:
            params["objectDescription"] = description
        log.debug("POST %s", self.bundle_ws_url("objects"))
        resp = requests.post(
            self.bundle_ws_url("objects"),
            params=params or None,
            data=content,
            headers=headers,
            auth=self.signer,
            verify=self.verify_tls,
            timeout=60,
        )
        log.debug("-> HTTP %s opc-request-id=%s", resp.status_code, resp.headers.get("opc-request-id"))
        if resp.status_code not in (200, 201, 202):
            raise RuntimeError(
                "POST {} -> HTTP {}: {} (opc-request-id={})".format(
                    self.bundle_ws_url("objects"),
                    resp.status_code,
                    resp.text,
                    resp.headers.get("opc-request-id"),
                )
        )
        return resp

    def get_workspace_file_text(self, object_path: str) -> str:
        relpath = _ws_relpath(object_path)
        attempts = (
            (self.bundle_ws_url("objects", relpath), None),
            (self.ws_url("objects", relpath), None),
            (self.ws_url("objects"), {"path": object_path}),
            (self.ws_url("objects", "content"), {"path": object_path}),
            (self.ws_url("objects", "actions", "content"), {"path": object_path}),
        )
        last_error: Optional[Exception] = None
        for url, params in attempts:
            try:
                resp = self.request("GET", url, params=params)
            except Exception as exc:
                last_error = exc
                log.debug("workspace file fetch failed for %s via %s: %s", object_path, url, exc)
                continue
            if resp.status_code != 200:
                last_error = RuntimeError(
                    "GET {} -> HTTP {}: {}".format(url, resp.status_code, resp.text)
                )
                log.debug("workspace file fetch failed for %s via %s: HTTP %s", object_path, url, resp.status_code)
                continue
            content_type = str(resp.headers.get("content-type") or "").lower()
            if "application/json" in content_type:
                try:
                    payload = resp.json()
                except Exception:
                    payload = None
                if isinstance(payload, dict):
                    for key in ("content", "text", "body"):
                        value = payload.get(key)
                        if isinstance(value, str):
                            return value
            return resp.text
        raise RuntimeError(
            "could not read workspace file {} through the available APIs".format(object_path)
        ) from last_error

    def list_git_diffs(self, folder_path: str, branch: str, compare_to: str = "HEAD") -> List[Dict[str, Any]]:
        repo = self.get_git_repository(folder_path, should_include_credential_key=False)
        repo_key = _git_repo_key(repo)
        if not repo_key:
            raise RuntimeError("git repository key not found for {}".format(folder_path))
        sdk = self._sdk_clients()
        rel_path = _ws_relpath(folder_path)
        if sdk and hasattr(sdk.get("git"), "list_git_diffs"):
            resp = sdk["git"].list_git_diffs(
                self.resource_id,
                self.workspace_key,
                repo_key,
                rel_path,
                branch,
                compare_to=compare_to,
                filter="DIFF_ONLY",
            )
            data = getattr(resp, "data", None)
            items = getattr(data, "items", None) or []
            result = [self._model_to_dict(item) for item in items]
            log_debug_context(
                "Git diff listed",
                folder_path=folder_path,
                branch=branch,
                compare_to=compare_to,
                repo_key=repo_key,
                count=len(result),
                items=result,
            )
            return result
        resp = self.request_ok(
            "GET",
            self.ws_url("gitRepositories", repo_key, "actions", "gitDiff"),
            params={
                "gitFolderPath": rel_path,
                "branchName": branch,
                "compareTo": compare_to,
                "filter": "DIFF_ONLY",
            },
            ok=(200,),
        )
        payload = resp.json()
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            result = payload["items"]
            log_debug_context(
                "Git diff listed",
                folder_path=folder_path,
                branch=branch,
                compare_to=compare_to,
                repo_key=repo_key,
                count=len(result),
                items=result,
            )
            return result
        if isinstance(payload, list):
            log_debug_context(
                "Git diff listed",
                folder_path=folder_path,
                branch=branch,
                compare_to=compare_to,
                repo_key=repo_key,
                count=len(payload),
                items=payload,
            )
            return payload
        log_debug_context(
            "Git diff listed",
            folder_path=folder_path,
            branch=branch,
            compare_to=compare_to,
            repo_key=repo_key,
            count=0,
            payload=payload,
        )
        return []

    def commit_push_git_repository(
        self,
        folder_path: str,
        branch: str,
        files: List[str],
        commit_message: str,
        commit_description: Optional[str] = None,
    ) -> Any:
        repo = self.get_git_repository(folder_path, should_include_credential_key=False)
        repo_key = _git_repo_key(repo)
        if not repo_key:
            raise RuntimeError("git repository key not found for {}".format(folder_path))
        body = {
            "gitFolderPath": _ws_relpath(folder_path),
            "branchName": branch,
            "files": files,
            "commitMessage": commit_message,
        }
        if commit_description:
            body["commitDescription"] = commit_description
        log_debug_context(
            "Commit/push requested",
            folder_path=folder_path,
            branch=branch,
            repo_key=repo_key,
            files=files,
            commit_message=commit_message,
            commit_description=commit_description,
        )
        sdk = self._sdk_clients()
        if sdk and hasattr(sdk.get("git"), "commit_push_git_repository"):
            details = sdk["models"].CommitPushDetails(
                git_folder_path=_ws_relpath(folder_path),
                branch_name=branch,
                files=files,
                commit_message=commit_message,
                commit_description=commit_description,
            )
            return sdk["git"].commit_push_git_repository(
                self.resource_id,
                self.workspace_key,
                repo_key,
                details,
            )
        return self.request_ok(
            "POST",
            self.ws_url("gitRepositories", repo_key, "actions", "commitPush"),
            body=body,
            ok=(200, 202),
        )

    def wait_for_async(self, async_key: str, purpose: Optional[str] = None) -> Dict[str, Any]:
        deadline = time.time() + self.poll_timeout
        last = None
        label = purpose or "asynchronous operation"
        started_at = time.time()
        log.info("%s", label)
        log_debug_context(
            "Async wait started",
            async_key=async_key,
            purpose=label,
            poll_interval_secs=self.poll_interval,
            poll_timeout_secs=self.poll_timeout,
        )
        def fetch_async() -> Dict[str, Any]:
            resp = self.request_ok(
                "GET",
                self.lake_url("asyncOperations", async_key),
                ok=(200,),
                timeout=self.poll_http_timeout,
            )
            payload = resp.json()
            log.debug("async %s status=%s payload=%s", async_key, str(payload.get("status") or ""), payload)
            return payload

        def async_status(payload: Dict[str, Any]) -> str:
            return str(payload.get("status") or "")

        return poll_with_progress(
            label,
            timeout_secs=self.poll_timeout,
            fetch_interval_secs=self.poll_interval,
            fetch_fn=fetch_async,
            success_fn=lambda payload: async_status(payload) == "SUCCEEDED",
            progress_suffix_fn=lambda payload: _friendly_async_status(async_status(payload)),
            checkpoint_interval_secs=30,
            checkpoint_message_fn=lambda payload, elapsed: (
                "{} still in progress after {} (status={} async_key={})".format(
                    label,
                    format_elapsed_br(elapsed),
                    async_status(payload) or "UNKNOWN",
                    async_key,
                )
                if async_status(payload) not in ("SUCCEEDED", "FAILED", "CANCELED")
                else None
            ),
            failure_message_fn=lambda payload, elapsed: (
                "async {} ended {} after {}: {} | payload={}".format(
                    async_key,
                    async_status(payload),
                    format_elapsed_br(elapsed),
                    payload.get("errorMessage") or payload.get("message"),
                    payload,
                )
                if async_status(payload) in ("FAILED", "CANCELED")
                else None
            ),
            timeout_message_fn=lambda payload, elapsed: "async {} timed out after {}; last={}".format(
                async_key,
                format_elapsed_br(elapsed),
                payload,
            ),
            logger=log,
        )

    def _wait_if_async(self, resp: Any, purpose: Optional[str] = None) -> Any:
        if hasattr(resp, "headers"):
            async_key = _async_key(resp)
        else:
            async_key = None
        if async_key:
            self.wait_for_async(async_key, purpose=purpose)
        return resp

    def _bundle_deploy_surfaces(self) -> List[tuple[str, str]]:
        endpoints = []
        seen = set()

        def add(url: str, label: str) -> None:
            if url not in seen:
                seen.add(url)
                endpoints.append((url, label))

        add(
            self._surface_url(
                "20240831",
                "aiDataPlatforms",
                "workspaces",
                self.workspace_key,
                "bundles",
                "actions",
                "deploy",
            ),
            "aidp-20240831",
        )
        add(
            self._surface_url(
                "20240831",
                "aiDataPlatforms",
                "workspaces",
                self.workspace_key,
                "actions",
                "deployBundle",
            ),
            "aidp-20240831-legacy",
        )
        add(self.deploy_bundle_ws_url("bundles", "actions", "deploy"), "configured-deploy-surface")
        add(self.bundle_ws_url("bundles", "actions", "deploy"), "bundle-surface")
        add(self.ws_url("bundles", "actions", "deploy"), "workspace-surface")
        add(
            self._surface_url(
                "20240831",
                "dataLakes",
                "workspaces",
                self.workspace_key,
                "bundles",
                "actions",
                "deploy",
            ),
            "datalakes-20240831",
        )
        add(
            self._surface_url(
                "20260430",
                "aiDataPlatforms",
                "workspaces",
                self.workspace_key,
                "bundles",
                "actions",
                "deploy",
            ),
            "aidp-20260430",
        )
        return endpoints

    def _bundle_deployment_status_surfaces(self) -> List[tuple[str, str]]:
        endpoints = []
        seen = set()

        def add(url: str, label: str) -> None:
            if url not in seen:
                seen.add(url)
                endpoints.append((url, label))

        for prefix, version, label in (
            ("aiDataPlatforms", "20240831", "aidp-20240831"),
            ("dataLakes", "20240831", "datalakes-20240831"),
            ("aiDataPlatforms", "20260430", "aidp-20260430"),
        ):
            add(
                self._surface_url(
                    version,
                    prefix,
                    "workspaces",
                    self.workspace_key,
                    "bundles",
                    "actions",
                    "getDeploymentStatus",
                ),
                label,
            )
        return endpoints

    def fetch_bundle_deployment_status(self, bundle_path: str) -> Dict[str, Any]:
        sdk = self._sdk_clients()
        if sdk:
            try:
                details = sdk["models"].FetchBundleDeploymentStatusDetails(path=bundle_path)
                resp = sdk["bundle"].fetch_bundle_deployment_status(
                    self.resource_id,
                    self.workspace_key,
                    details,
                )
                data = getattr(resp, "data", None)
                if data is not None:
                    return self._model_to_dict(data)
            except Exception as exc:
                if not _is_not_found_like_error(exc):
                    raise
                log.debug("SDK fetch_bundle_deployment_status unavailable for %s: %s", bundle_path, exc)
        last_err = None
        for url, label in self._bundle_deployment_status_surfaces():
            try:
                log_debug_context("Bundle deployment status probe", bundle_path=bundle_path, surface=label, url=url)
                resp = self.request_ok("POST", url, body={"path": bundle_path}, ok=(200,))
                payload = resp.json()
                log.debug("bundle deployment status payload via %s: %s", label, payload)
                if isinstance(payload, dict):
                    return payload
            except Exception as exc:
                last_err = exc
                log.debug("bundle deployment status fetch failed via %s for %s: %s", label, bundle_path, exc)
                continue
        if last_err:
            raise last_err
        return {}

    def delete_workspace_object_via_legacy_api(self, object_path: str) -> bool:
        relative_path = _ws_relpath(object_path)
        encoded_path = quote(relative_path, safe="")
        url = self.ws_url("objects", encoded_path)
        log_debug_context("Legacy DELETE requested", object_path=object_path, relative_path=relative_path, url=url)
        log.debug("DELETE %s", url)
        resp = requests.delete(
            url,
            headers={"accept": "application/json"},
            auth=self.signer,
            verify=self.verify_tls,
            timeout=60,
        )
        log.debug("-> HTTP %s opc-request-id=%s", resp.status_code, resp.headers.get("opc-request-id"))
        if resp.status_code == 404:
            log.warning(
                "Legacy DELETE returned 404 for %s; treating as already absent/inconsistent backend state",
                object_path,
            )
            return False
        if resp.status_code not in (200, 202, 204):
            raise RuntimeError(
                "Legacy DELETE {} -> HTTP {}: {}".format(object_path, resp.status_code, resp.text)
            )
        return True

    def wait_for_bundle_deployment(self, bundle_path: str) -> Dict[str, Any]:
        surfaces = self._bundle_deployment_status_surfaces()
        log.info("Bundle deploy: waiting for the final deployment status")
        log_debug_context(
            "Bundle deployment wait started",
            bundle_path=bundle_path,
            poll_interval_secs=self.poll_interval,
            poll_timeout_secs=self.poll_timeout,
            surfaces=surfaces,
        )

        def fetch_status() -> Dict[str, Any]:
            last_err = None
            for url, label in surfaces:
                try:
                    resp = self.request_ok("POST", url, body={"path": bundle_path}, ok=(200,))
                    payload = resp.json()
                    log.debug("bundle deployment status payload via %s: %s", label, payload)
                    payload["_surface_label"] = label
                    return payload
                except Exception as exc:
                    last_err = exc
                    log.debug("bundle deployment status poll failed via %s: %s", label, exc)
                    continue

            return {
                "status": "POLL_ERROR",
                "last_error": str(last_err) if last_err else "",
            }

        def status_value(payload: Dict[str, Any]) -> str:
            return str(payload.get("status") or "")

        return poll_with_progress(
            "Bundle deploy",
            timeout_secs=self.poll_timeout,
            fetch_interval_secs=self.poll_interval,
            fetch_fn=fetch_status,
            success_fn=lambda payload: status_value(payload) == "SUCCEEDED",
            progress_suffix_fn=lambda payload: "status {}".format(
                _friendly_bundle_deploy_status(status_value(payload))
            ),
            failure_message_fn=lambda payload, _elapsed: (
                "Bundle deployment failed: {}".format(payload.get("message") or payload)
                if status_value(payload) == "FAILED"
                else None
            ),
            timeout_message_fn=lambda payload, _elapsed: (
                "Timed out waiting for bundle deployment status for {}. last_error={} last_payload={}".format(
                    bundle_path,
                    payload.get("last_error") or "",
                    payload,
                )
                if status_value(payload) == "POLL_ERROR"
                else "Timed out waiting for bundle deployment status for {}. last_payload={}".format(
                    bundle_path,
                    payload,
                )
            ),
            logger=log,
        )

    def deploy_bundle(self, bundle_path: str) -> Any:
        sdk = self._sdk_clients()
        log_debug_context("Deploy bundle requested", bundle_path=bundle_path, workspace_key=self.workspace_key)
        if sdk:
            try:
                details = sdk["models"].DeployBundleDetails(path=bundle_path)
                resp = sdk["bundle"].deploy_bundle(self.resource_id, self.workspace_key, details)
                return self._wait_if_async(resp, purpose="Bundle deploy")
            except Exception as exc:
                message = str(exc).lower()
                if (
                    "unknown resource" in message
                    or "notauthorizedornotfound" in message
                    or "internalerror" in message
                    or "volume error" in message
                ):
                    log.warning(
                        "SDK deploy_bundle failed for %s; trying REST fallback. error=%s",
                        bundle_path,
                        exc,
                    )
                else:
                    raise
        endpoints = self._bundle_deploy_surfaces()
        last_err = None
        total = len(endpoints)
        for index, (url, label) in enumerate(endpoints, start=1):
            try:
                log.info(
                    "Bundle deploy: trying surface %s (%s/%s)",
                    label,
                    index,
                    total,
                )
                resp = self.request_ok(
                    "POST",
                    url,
                    body={"path": bundle_path},
                    ok=(200, 202),
                )
                async_key = _async_key(resp)
                if async_key:
                    log.info("Bundle deploy: operation accepted; tracking deployment via bundle status")
                return self.wait_for_bundle_deployment(bundle_path)
            except Exception as exc:
                last_err = exc
                message = str(exc).lower()
                if (
                    "unknown resource" in message
                    or "notauthorizedornotfound" in message
                    or "internalerror" in message
                    or "volume error" in message
                ):
                    log.warning(
                        "Bundle deploy: surface %s failed; trying fallback %s/%s",
                        label,
                        min(index + 1, total),
                        total,
                    )
                    log.debug("Failure detail on %s: %s", label, exc)
                    continue
                log.debug("Bundle deploy failed via %s: %s", label, exc)
                raise
        raise last_err


def phase0_credential(client: AidpClient, cfg: Dict[str, Any]) -> str:
    log_phase_header(0, "resolve git credential", 3)
    log.info("Validating workspace Git credential")
    log_debug_context(
        "Phase 0 context",
        workspace_name=cfg.get("aidp", {}).get("workspace_name"),
        workspace_key=cfg.get("aidp", {}).get("workspace_key"),
        credential_name=cfg.get("git", {}).get("credential_name"),
    )
    return client.resolve_git_credential_key(cfg["git"]["credential_name"])


def phase1_directory(client: AidpClient, cfg: Dict[str, Any]) -> bool:
    log_phase_header(1, "ensure directory", 3)
    log.info("Ensuring base directory %s", cfg["git"]["parent_dir"])
    log_debug_context(
        "Phase 1 context",
        workspace_name=cfg.get("aidp", {}).get("workspace_name"),
        workspace_key=cfg.get("aidp", {}).get("workspace_key"),
        parent_dir=cfg.get("git", {}).get("parent_dir"),
        folder_path=resolve_folder_path(cfg),
    )
    return client.ensure_directory(cfg["git"]["parent_dir"], purpose="Base directory setup")


def phase2_git_folder(
    client: AidpClient, cfg: Dict[str, Any], credential_key: str, parent_was_absent: bool = False
) -> None:
    log_phase_header(2, "git folder (create or pull)", 3)
    folder_path = resolve_folder_path(cfg)
    metadata = client.get_git_repository(folder_path, should_include_credential_key=False)
    association = client.git_folder_association(folder_path)
    log_debug_context(
        "Phase 2 context",
        workspace_name=cfg.get("aidp", {}).get("workspace_name"),
        workspace_key=cfg.get("aidp", {}).get("workspace_key"),
        folder_path=folder_path,
        repository_url=cfg.get("git", {}).get("repository_url"),
        branch=cfg.get("git", {}).get("branch"),
        credential_key=credential_key,
        parent_was_absent=parent_was_absent,
        git_repository=metadata,
        git_association=association,
    )
    associated = bool(
        association.get("isAssociated")
        and (_git_repo_key(association) or _git_repo_key(metadata) or metadata)
    )
    if associated and parent_was_absent:
        log.warning(
            "git folder %s reports associated, but its parent was just created -> stale association; cloning instead",
            folder_path,
        )
        associated = False
    if not metadata and not associated:
        log.info("git folder absent; cloning")
        log.info("Cloning repository %s on branch %s", cfg["git"]["repository_url"], cfg["git"]["branch"])
        resp = client.create_git_folder(
            folder_path,
            cfg["git"]["repository_url"],
            cfg["git"]["branch"],
            credential_key,
        )
        if hasattr(resp, "headers") and _async_key(resp):
            client.wait_for_async(_async_key(resp), purpose="Clonagem inicial da git folder")
        log.info("created git folder %s (cloning async)", folder_path)
        return
    if not metadata and associated:
        log.info("git folder already associated at %s; skipping clone", folder_path)
        return
    client.ensure_git_folder_credential(folder_path, credential_key)
    log.info("Atualizando git folder existente pela branch %s", cfg["git"]["branch"])
    resp = client.git_pull(folder_path, cfg["git"]["branch"])
    if hasattr(resp, "headers") and _async_key(resp):
        client.wait_for_async(_async_key(resp), purpose="Git folder update")


def phase3_bundle(client: AidpClient, cfg: Dict[str, Any]) -> None:
    log_phase_header(3, "deploy bundle", 4)
    log.info("Triggering deploy of the canonical bundle %s", resolve_versioned_bundle_path(cfg))
    client.deploy_bundle(resolve_versioned_bundle_path(cfg))


def run(
    cfg: Dict[str, Any],
    dry_run: bool = False,
    auth_method: Optional[str] = None,
    bundle_path_override: Optional[str] = None,
) -> int:
    method = auth_method or DEFAULT_AUTH_METHOD
    signer = None if dry_run else build_signer(method)
    client = AidpClient(cfg, signer, dry_run=dry_run)
    if dry_run:
        log.info("dry-run: config parsed successfully")
        return 0
    credential_key = phase0_credential(client, cfg)
    parent_was_absent = phase1_directory(client, cfg)
    phase2_git_folder(client, cfg, credential_key, parent_was_absent=parent_was_absent)
    if bundle_path_override:
        log_phase_header(3, "deploy bundle", 4)
        log.info("Triggering deploy of the canonical bundle %s", bundle_path_override)
        client.deploy_bundle(bundle_path_override)
    else:
        phase3_bundle(client, cfg)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=DEFAULT_TARGET_CONFIG_PATH)
    parser.add_argument("--auth-method", choices=sorted(VALID_AUTH_METHODS), default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    setup_logging("cicd-deploy")
    cfg = load_config(args.config)
    return run(cfg, dry_run=args.dry_run, auth_method=args.auth_method)


if __name__ == "__main__":
    raise SystemExit(run_with_logged_errors(main))
