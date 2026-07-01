#!/usr/bin/env python3
"""Ensure a Git credential exists in AIDP using a PAT stored in OCI Vault."""

from __future__ import annotations

import argparse
import base64
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from cicd_deploy import AidpClient, apply_config_defaults, build_sdk_client_config, build_signer, log_debug_context
from core.console_logging import LOGGER_NAME, log_phase_header, run_logged_action, run_with_logged_errors, setup_logging
from core.contexts import context_auth_method, load_context

log = logging.getLogger(LOGGER_NAME)
DEFAULT_SCRIPT_AUTH_METHOD = "instance_principal"

PROVIDER_ALIASES = {
    "github": "GITHUB",
    "gitlab": "GITLAB",
    "bitbucket": "BITBUCKET",
    "oci_devops": "OCI_DEVOPS",
}


def _context_value(context: Dict[str, Any], section: str, key: str) -> str:
    block = context.get(section) or {}
    value = block.get(key) if isinstance(block, dict) else None
    return str(value or "").strip()


def build_client_config(context: Dict[str, Any]) -> Dict[str, Any]:
    region = _context_value(context, "aidp", "region")
    ocid = _context_value(context, "aidp", "ocid")
    if not region:
        raise RuntimeError("context aidp.region cannot be empty")
    if not ocid:
        raise RuntimeError("context aidp.ocid cannot be empty")
    return apply_config_defaults(
        {
            "aidp": {
                "region": region,
                "ocid": ocid,
            },
            "git": {},
            "options": {},
        }
    )

def get_secret_text(secret_ocid: str, signer, region: str) -> str:
    import oci

    client = oci.secrets.SecretsClient(build_sdk_client_config(region), signer=signer)
    response = client.get_secret_bundle(secret_ocid)
    data = getattr(response, "data", None)
    content = getattr(data, "secret_bundle_content", None)
    encoded = str(getattr(content, "content", "") or "").strip()
    if not encoded:
        raise RuntimeError("vault secret content is empty for {}".format(secret_ocid))
    try:
        decoded = base64.b64decode(encoded).decode("utf-8").strip()
    except Exception as exc:
        raise RuntimeError("failed to decode Vault secret {}: {}".format(secret_ocid, exc)) from exc
    if not decoded:
        raise RuntimeError("decoded Vault secret content is empty for {}".format(secret_ocid))
    return decoded


def resolve_provider_name(provider: str) -> str:
    normalized = str(provider or "").strip().lower()
    if normalized not in PROVIDER_ALIASES:
        raise RuntimeError("unsupported Git provider {!r}".format(provider))
    return PROVIDER_ALIASES[normalized]


def _should_fallback_user_setting(exc: Exception) -> bool:
    payload = str(exc)
    lowered = payload.lower()
    return "user_setting" in lowered or "usersettings" in lowered or "notauthorizedornotfound" in lowered


def _to_plain_data(value: Any) -> Any:
    if hasattr(value, "swagger_types"):
        result: Dict[str, Any] = {}
        for key in value.swagger_types:
            result[key] = _to_plain_data(getattr(value, key, None))
        return result
    if isinstance(value, dict):
        return {str(key): _to_plain_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain_data(item) for item in value]
    return value


def list_git_settings_with_fallback(client: AidpClient) -> List[Dict[str, Any]]:
    try:
        return client.list_git_account_settings()
    except Exception as exc:
        if not _should_fallback_user_setting(exc):
            raise
        log_debug_context("Git user settings SDK list failed; using REST fallback", error=str(exc))
    response = client.request_ok("GET", client.user_setting_url("userSettings"), ok=(200,))
    payload = response.json()
    items = payload if isinstance(payload, list) else payload.get("items") or payload.get("userSettings") or []
    return [_to_plain_data(item) for item in list(items)]


def find_git_setting_by_name(client: AidpClient, credential_name: str) -> Optional[Dict[str, Any]]:
    for item in list_git_settings_with_fallback(client):
        if str(item.get("name") or "").strip() != credential_name:
            continue
        data = item.get("data") or {}
        setting_type = str(data.get("type") or item.get("type") or "").strip().upper()
        if setting_type and setting_type != "GIT_ACCOUNT":
            continue
        return item
    return None


def create_git_setting_payload(
    client: AidpClient,
    *,
    provider_name: str,
    git_username: str,
    personal_access_token: str,
):
    sdk = client._sdk_clients()
    if not sdk:
        raise RuntimeError("AIDP SDK is required to create or update Git credentials")
    models = sdk["models"]
    payload = models.GitAccountUserSetting(
        provider_name=provider_name,
        entity_type=models.GitAccountUserSetting.ENTITY_TYPE_PERSONAL_ACCESS_TOKEN,
        username=git_username,
        personal_access_token=personal_access_token,
    )
    return sdk, payload


def create_user_setting_with_fallback(client: AidpClient, sdk, create_details) -> Dict[str, Any]:
    try:
        response = sdk["user_setting"].create_user_setting(client.resource_id, create_details)
        return _to_plain_data(getattr(response, "data", None))
    except Exception as exc:
        if not _should_fallback_user_setting(exc):
            raise
        log_debug_context("Git user setting SDK create failed; using REST fallback", error=str(exc))
    response = client.request_ok(
        "POST",
        client.user_setting_url("userSettings"),
        body=_to_plain_data(create_details),
        ok=(200, 201),
    )
    payload = response.json()
    if isinstance(payload, dict):
        return payload
    return {"data": payload}


def update_user_setting_with_fallback(client: AidpClient, sdk, setting_key: str, update_details) -> Dict[str, Any]:
    try:
        response = sdk["user_setting"].update_user_setting(client.resource_id, setting_key, update_details)
        return _to_plain_data(getattr(response, "data", None))
    except Exception as exc:
        if not _should_fallback_user_setting(exc):
            raise
        log_debug_context(
            "Git user setting SDK update failed; using REST fallback",
            error=str(exc),
            setting_key=setting_key,
        )
    response = client.request_ok(
        "PUT",
        client.user_setting_url("userSettings", setting_key),
        body=_to_plain_data(update_details),
        ok=(200, 201),
    )
    payload = response.json()
    if isinstance(payload, dict):
        return payload
    return {"data": payload}


def ensure_git_credential(
    client: AidpClient,
    *,
    credential_name: str,
    provider_name: str,
    git_username: str,
    personal_access_token: str,
    is_default: bool,
) -> Dict[str, Any]:
    sdk, payload = create_git_setting_payload(
        client,
        provider_name=provider_name,
        git_username=git_username,
        personal_access_token=personal_access_token,
    )
    models = sdk["models"]

    existing = find_git_setting_by_name(client, credential_name)
    if existing:
        setting_key = str(existing.get("key") or "").strip()
        if not setting_key:
            raise RuntimeError("existing Git credential {!r} has no key".format(credential_name))
        log_debug_context(
            "Update Git credential context",
            credential_name=credential_name,
            setting_key=setting_key,
            provider_name=provider_name,
            git_username=git_username,
            is_default=is_default,
        )
        update_details = models.UpdateUserSettingDetails(
            name=credential_name,
            is_default=is_default,
            data=payload,
        )
        update_user_setting_with_fallback(client, sdk, setting_key, update_details)
        log.info("Git credential updated: %s", credential_name)
        return {"action": "updated", "name": credential_name, "key": setting_key}

    log_debug_context(
        "Create Git credential context",
        credential_name=credential_name,
        provider_name=provider_name,
        git_username=git_username,
        is_default=is_default,
    )
    create_details = models.CreateUserSettingDetails(
        name=credential_name,
        is_default=is_default,
        data=payload,
    )
    created = create_user_setting_with_fallback(client, sdk, create_details)
    setting_key = str((created or {}).get("key") or "").strip()
    if not setting_key:
        refreshed = find_git_setting_by_name(client, credential_name)
        setting_key = str((refreshed or {}).get("key") or "").strip()
    if not setting_key:
        raise RuntimeError("Git credential {!r} was created but no key was returned".format(credential_name))
    log.info("Git credential created: %s", credential_name)
    return {"action": "created", "name": credential_name, "key": setting_key}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", required=True)
    parser.add_argument("--credential-name", required=True)
    parser.add_argument("--vault-secret-ocid", required=True)
    parser.add_argument("--git-username", required=True)
    parser.add_argument("--provider", default="github", choices=sorted(PROVIDER_ALIASES))
    parser.add_argument("--auth-method", default=None)
    parser.add_argument("--set-default", action="store_true")
    args = parser.parse_args(argv)

    setup_logging("cicd-create-git-credential")

    total_phases = 2
    log_phase_header(0, "load context and resolve secret", total_phases)

    context = load_context(args.context)
    auth_method = args.auth_method or context_auth_method(context) or DEFAULT_SCRIPT_AUTH_METHOD
    cfg = build_client_config(context)
    signer = build_signer(auth_method)
    client = AidpClient(cfg, signer)
    provider_name = resolve_provider_name(args.provider)

    log_debug_context(
        "Git credential ensure context",
        context_name=args.context,
        auth_method=auth_method,
        aidp_ocid=cfg["aidp"]["ocid"],
        region=cfg["aidp"]["region"],
        credential_name=args.credential_name,
        provider_name=provider_name,
        git_username=args.git_username,
        vault_secret_ocid=args.vault_secret_ocid,
        is_default=bool(args.set_default),
    )

    log.info("Using context %s", args.context)
    log.info("Loading Git PAT from Vault secret %s", args.vault_secret_ocid)
    personal_access_token = run_logged_action(
        "Resolve Git PAT from Vault",
        lambda: get_secret_text(args.vault_secret_ocid, signer, cfg["aidp"]["region"]),
        logger=log,
    )

    log_phase_header(1, "ensure Git credential", total_phases)
    result = run_logged_action(
        "Ensure Git credential",
        lambda: ensure_git_credential(
            client,
            credential_name=args.credential_name,
            provider_name=provider_name,
            git_username=args.git_username,
            personal_access_token=personal_access_token,
            is_default=bool(args.set_default),
        ),
        logger=log,
    )

    log.info("== Summary ==")
    log.info("Context: %s", args.context)
    log.info("AIDP OCID: %s", cfg["aidp"]["ocid"])
    log.info("Region: %s", cfg["aidp"]["region"])
    log.info("Credential name: %s", result["name"])
    log.info("Credential key: %s", result["key"])
    log.info("Action: %s", result["action"])
    return 0


if __name__ == "__main__":
    raise SystemExit(run_with_logged_errors(main))
