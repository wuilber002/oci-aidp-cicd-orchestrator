#!/usr/bin/env python3
"""Prepare the local Python runtime and keep the workspace SDK up to date."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import urllib.request
import venv
import zipfile
from pathlib import Path
from typing import Optional

from core.console_logging import LOGGER_NAME, run_logged_action, run_with_logged_errors, setup_logging


EXECUTION_DIR = Path.cwd().resolve()
VENV_DIR = EXECUTION_DIR / ".venv"
DOWNLOAD_DIR = EXECUTION_DIR / ".downloads"
CLIENT_RELEASES_URL = "https://github.com/oracle-samples/aidataplatform-sdk/releases/latest"
CLIENT_RELEASES_API_URL = "https://api.github.com/repos/oracle-samples/aidataplatform-sdk/releases/latest"
REQUIREMENTS_PATH = EXECUTION_DIR / "requirements.txt"
SDK_IMPORT_PATH = "aidp_python_client.aidataplatform_dp"
SDK_PACKAGE_NAMES = ("aidp-python-client", "aidp_python_client")
IMPORT_NAME_BY_REQUIREMENT = {
    "pyyaml": "yaml",
}

log = logging.getLogger(LOGGER_NAME)
USAGE_TITLE_COLOR = "\033[1;31m"
USAGE_KEYWORD_COLOR = "\033[1;35m"
USAGE_PATH_COLOR = "\033[1;37m"
USAGE_RESET_COLOR = "\033[0m"


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def run_logged(cmd: list[str], step_label: str) -> None:
    def action() -> None:
        process = subprocess.Popen(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        output, _ = process.communicate()
        return_code = process.returncode
        if return_code != 0:
            if output:
                log.debug("Command failed output:\n%s", output.rstrip())
            raise subprocess.CalledProcessError(return_code, cmd, output=output)
        if output:
            log.debug("Command output:\n%s", output.rstrip())

    run_logged_action(step_label, action, emit_initial=False, logger=log)


def run_json(cmd: list[str]) -> dict:
    completed = subprocess.run(
        cmd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return json.loads(completed.stdout)


def is_tty_stdout() -> bool:
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def flush_logging_handlers() -> None:
    root = logging.getLogger()
    for handler in root.handlers:
        try:
            handler.flush()
        except Exception:
            continue


def relative_to_execution_dir(path: Path) -> str:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(EXECUTION_DIR)
    except ValueError:
        return str(resolved)
    return "." if not str(relative) else str(relative)


def print_usage_hint(venv_dir: Path) -> None:
    if not is_tty_stdout():
        return
    flush_logging_handlers()
    activate_path = relative_to_execution_dir(venv_dir / "bin" / "activate")
    lines = (
        "",
        "{}Como usar{}".format(USAGE_TITLE_COLOR, USAGE_RESET_COLOR),
        "{}source{} {}{}{}".format(
            USAGE_KEYWORD_COLOR,
            USAGE_RESET_COLOR,
            USAGE_PATH_COLOR,
            activate_path,
            USAGE_RESET_COLOR,
        ),
        "{}python{} {}cicd_prepare.py source --help{}".format(
            USAGE_KEYWORD_COLOR,
            USAGE_RESET_COLOR,
            USAGE_PATH_COLOR,
            USAGE_RESET_COLOR,
        ),
    )
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def run_pip(venv_python: Path, args: list[str], step_label: str) -> None:
    run_logged(
        [
            str(venv_python),
            "-m",
            "pip",
            "--disable-pip-version-check",
            *args,
        ],
        step_label,
    )


def python_in_venv() -> Path:
    bindir = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    return VENV_DIR / bindir / executable


def venv_exists() -> bool:
    return python_in_venv().is_file()


def create_venv_if_needed() -> bool:
    if venv_exists():
        log.info("Virtualenv ready for use: %s", VENV_DIR)
        return False
    run_logged_action(
        "Creating local virtualenv",
        lambda: venv.EnvBuilder(with_pip=True).create(VENV_DIR),
        emit_initial=False,
        logger=log,
    )
    if not venv_exists():
        raise RuntimeError(
            "virtualenv creation finished but {} was not created; run this script with python3 and verify that the host Python has venv support".format(
                python_in_venv()
            )
        )
    return True


def fetch_latest_release_payload() -> dict:
    with urllib.request.urlopen(CLIENT_RELEASES_API_URL) as response:
        return json.load(response)


def select_latest_client_asset(payload: dict) -> tuple[str, str]:
    assets = payload.get("assets") or []
    candidates: list[tuple[int, str, str]] = []
    for asset in assets:
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        lowered = name.lower()
        if "aidp-python-client" not in lowered:
            continue
        if lowered.endswith(".whl"):
            candidates.append((0, name, url))
            continue
        if lowered.endswith(".zip"):
            candidates.append((1, name, url))
    if not candidates:
        raise RuntimeError(
            "no Python SDK asset found in latest release at {}".format(CLIENT_RELEASES_URL)
        )
    _, name, url = sorted(candidates)[0]
    return name, url


def download_client_artifact(name: str, url: str) -> Path:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = DOWNLOAD_DIR / name
    log.info("Downloading official AIDP SDK")
    log.debug("Downloading SDK artifact from %s", url)
    urllib.request.urlretrieve(url, target)
    log.info("Official AIDP SDK downloaded: %s", name)
    return target


def prune_downloaded_sdk_wheels(keep_wheel: Optional[Path]) -> None:
    # Keep a single reusable wheel cache to avoid accumulating stale versions.
    if not DOWNLOAD_DIR.exists():
        return
    keep_path = keep_wheel.resolve() if keep_wheel and keep_wheel.exists() else None
    for candidate in DOWNLOAD_DIR.iterdir():
        if not candidate.is_file():
            continue
        lowered = candidate.name.lower()
        if not lowered.endswith(".whl"):
            continue
        if "aidp_python_client" not in lowered and "aidp-python-client" not in lowered:
            continue
        if keep_path and candidate.resolve() == keep_path:
            continue
        candidate.unlink(missing_ok=True)
        log.debug("Removed stale SDK wheel cache %s", candidate)


def resolve_installable_sdk_artifact(path: Path) -> Path:
    if path.suffix.lower() == ".whl":
        return path
    if path.suffix.lower() != ".zip":
        raise RuntimeError("unsupported SDK artifact format: {}".format(path.name))
    # The upstream release currently ships a ZIP that contains the installable wheel.
    log.info("Extracting SDK wheel")
    with zipfile.ZipFile(path) as archive:
        wheel_members = [
            name
            for name in archive.namelist()
            if name.lower().endswith(".whl") and "aidp_python_client" in Path(name).name.lower()
        ]
        if not wheel_members:
            raise RuntimeError(
                "no aidp_python_client wheel found inside {}".format(path.name)
            )
        member_name = sorted(wheel_members)[0]
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        extracted_path = DOWNLOAD_DIR / Path(member_name).name
        with archive.open(member_name) as source, extracted_path.open("wb") as target:
            target.write(source.read())
    path.unlink(missing_ok=True)
    log.debug("Removed temporary SDK zip %s", path)
    log.info("SDK wheel extracted: %s", extracted_path.name)
    return extracted_path


def resolve_client_artifact(argv: list[str]) -> Path:
    if argv:
        candidate = Path(argv[0]).expanduser()
        if not candidate.is_absolute():
            candidate = EXECUTION_DIR / candidate
        if not candidate.is_file():
            raise FileNotFoundError(
                "{} not found in {}".format(argv[0], EXECUTION_DIR)
            )
        return candidate
    payload = fetch_latest_release_payload()
    name, url = select_latest_client_asset(payload)
    return download_client_artifact(name, url)


def load_requirements() -> list[tuple[str, str, str]]:
    if not REQUIREMENTS_PATH.is_file():
        raise FileNotFoundError(
            "requirements.txt not found in execution directory {}".format(EXECUTION_DIR)
        )
    requirements: list[tuple[str, str, str]] = []
    for raw_line in REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        for operator in ("==", ">=", "<=", ">", "<"):
            if operator in line:
                name, version = line.split(operator, 1)
                requirements.append((name.strip(), operator, version.strip()))
                break
        else:
            requirements.append((line, "", ""))
    return requirements


def normalize_name(value: str) -> str:
    return value.strip().lower().replace("_", "-").replace(".", "-")


def version_key(value: str) -> list[tuple[int, object]]:
    import re

    parts = re.findall(r"[0-9]+|[A-Za-z]+", value)
    normalized: list[tuple[int, object]] = []
    for part in parts:
        if part.isdigit():
            normalized.append((0, int(part)))
        else:
            normalized.append((1, part.lower()))
    return normalized


def compare_versions(left: str, right: str) -> int:
    left_key = version_key(left)
    right_key = version_key(right)
    if left_key < right_key:
        return -1
    if left_key > right_key:
        return 1
    return 0


def inspect_installed_packages(venv_python: Path) -> dict[str, str]:
    return run_json(
        [
            str(venv_python),
            "-c",
            (
                "import importlib.metadata, json; "
                "print(json.dumps({dist.metadata.get('Name', ''): dist.version "
                "for dist in importlib.metadata.distributions() if dist.metadata.get('Name')}))"
            ),
        ]
    )


def requirement_is_satisfied(installed: dict[str, str], name: str, operator: str, expected: str) -> bool:
    current_version = ""
    normalized_name = normalize_name(name)
    for installed_name, version in installed.items():
        if normalize_name(installed_name) == normalized_name:
            current_version = str(version)
            break
    if not current_version:
        return False
    if not operator:
        return True
    comparison = compare_versions(current_version, expected)
    if operator == "==":
        return comparison == 0
    if operator == ">=":
        return comparison >= 0
    if operator == "<=":
        return comparison <= 0
    if operator == ">":
        return comparison > 0
    if operator == "<":
        return comparison < 0
    return False


def requirements_are_satisfied(venv_python: Path) -> bool:
    installed = inspect_installed_packages(venv_python)
    for name, operator, expected in load_requirements():
        if not requirement_is_satisfied(installed, name, operator, expected):
            return False
    return True


def validate_runtime_imports(venv_python: Path) -> None:
    modules = []
    for name, _, _ in load_requirements():
        modules.append(IMPORT_NAME_BY_REQUIREMENT.get(normalize_name(name), name))
    modules = sorted(set(modules))
    run(
        [
            str(venv_python),
            "-c",
            "import importlib; [{}]".format(
                ",".join("importlib.import_module({!r})".format(module) for module in modules)
            ),
        ]
    )


def artifact_metadata(path: Path) -> tuple[str, str]:
    if path.suffix.lower() not in {".whl", ".zip"}:
        return "", ""
    with zipfile.ZipFile(path) as archive:
        metadata_name = ""
        for name in archive.namelist():
            upper = name.upper()
            if upper.endswith(".DIST-INFO/METADATA") or upper.endswith(".EGG-INFO/PKG-INFO") or upper.endswith("/PKG-INFO"):
                metadata_name = name
                break
        if not metadata_name:
            return "", ""
        content = archive.read(metadata_name).decode("utf-8", errors="replace")
    package_name = ""
    version = ""
    for line in content.splitlines():
        if line.startswith("Name: ") and not package_name:
            package_name = line.split(": ", 1)[1].strip()
        elif line.startswith("Version: ") and not version:
            version = line.split(": ", 1)[1].strip()
        if package_name and version:
            break
    return package_name, version


def asset_name_metadata(name: str) -> tuple[str, str]:
    path = Path(name)
    stem = path.stem
    if path.suffix.lower() == ".zip":
        match = re.match(r"(?P<package>.+)-(?P<version>[0-9][A-Za-z0-9._-]*)$", stem)
        if not match:
            return "", ""
        package_name = match.group("package").replace("_", "-")
        version = match.group("version")
        return package_name, version
    match = re.match(r"(?P<package>.+)-(?P<version>[0-9][A-Za-z0-9._-]*)-", path.name)
    if not match:
        return "", ""
    package_name = match.group("package").replace("_", "-")
    version = match.group("version")
    return package_name, version


def inspect_installed_sdk(venv_python: Path) -> dict:
    try:
        script = """
import importlib
import importlib.metadata
import json

result = {{"import_ok": False, "versions": {{}}}}
module = importlib.import_module({sdk_import_path!r})
result["import_ok"] = module is not None
for name in {sdk_package_names!r}:
    try:
        result["versions"][name] = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        pass
print(json.dumps(result))
""".format(sdk_import_path=SDK_IMPORT_PATH, sdk_package_names=SDK_PACKAGE_NAMES)
        payload = run_json(
            [
                str(venv_python),
                "-c",
                script,
            ]
        )
    except subprocess.CalledProcessError:
        return {"import_ok": False, "versions": {}}
    return payload


def sdk_is_satisfied(venv_python: Path, package_name: str, expected_version: str) -> bool:
    payload = inspect_installed_sdk(venv_python)
    if not payload.get("import_ok"):
        return False
    if not package_name or not expected_version:
        return True
    for installed_name, installed_version in (payload.get("versions") or {}).items():
        if normalize_name(installed_name) == normalize_name(package_name):
            return compare_versions(str(installed_version), expected_version) == 0
    return False


def main(argv: list[str]) -> int:
    setup_logging("cicd-bootstrap")
    log.info("== Stage 0: prepare local Python virtualenv ==")
    create_venv_if_needed()
    venv_python = python_in_venv()
    run_pip(
        venv_python,
        ["install", "--quiet", "--upgrade", "pip", "wheel"],
        "Validate pip tooling in the virtualenv",
    )

    log.info("== Stage 1: validate orchestrator Python dependencies ==")
    if requirements_are_satisfied(venv_python):
        log.info("Python dependencies already satisfied: %s", REQUIREMENTS_PATH.name)
    else:
        run_pip(
            venv_python,
            ["install", "--quiet", "-r", str(REQUIREMENTS_PATH)],
            "Install Python dependencies from {}".format(REQUIREMENTS_PATH.name),
        )
    run_pip(
        venv_python,
        ["check"],
        "Validate dependency consistency with pip check",
    )
    log.info("Dependency consistency validated")
    validate_runtime_imports(venv_python)

    log.info("== Stage 2: validate the official AIDP SDK ==")
    artifact = None
    expected_package_name = ""
    expected_version = ""

    if argv:
        artifact = resolve_installable_sdk_artifact(resolve_client_artifact(argv))
        expected_package_name, expected_version = artifact_metadata(artifact)
    else:
        release_payload = fetch_latest_release_payload()
        latest_name, latest_url = select_latest_client_asset(release_payload)
        expected_package_name, expected_version = asset_name_metadata(latest_name)
        if not expected_package_name or not expected_version:
            artifact = resolve_installable_sdk_artifact(
                download_client_artifact(latest_name, latest_url)
            )
            expected_package_name, expected_version = artifact_metadata(artifact)
        current_sdk = inspect_installed_sdk(venv_python)
        current_versions = current_sdk.get("versions") or {}
        current_version = ""
        for installed_name, installed_version in current_versions.items():
            if normalize_name(installed_name) == normalize_name(expected_package_name):
                current_version = str(installed_version)
                break
        if current_sdk.get("import_ok") and current_version and compare_versions(current_version, expected_version) == 0:
            log.info("The AIDP SDK is already installed at the latest version: %s", expected_version)
        else:
            if artifact is None:
                artifact = resolve_installable_sdk_artifact(
                    download_client_artifact(latest_name, latest_url)
                )

    if not artifact and sdk_is_satisfied(venv_python, expected_package_name, expected_version):
        pass
    else:
        if artifact is None:
            raise RuntimeError("SDK artifact resolution failed")
        run_pip(
            venv_python,
            ["install", "--quiet", str(artifact)],
            "Install the official AIDP SDK",
        )
        log.info("The AIDP SDK was installed: %s", artifact.name)
    run_pip(
        venv_python,
        ["check"],
        "Validate final dependency consistency after SDK installation",
    )
    if not sdk_is_satisfied(venv_python, expected_package_name, expected_version):
        raise RuntimeError("SDK validation failed after installation")
    prune_downloaded_sdk_wheels(artifact)
    if artifact is not None and artifact.exists():
        log.info("The local SDK cache was updated: %s", artifact)
    log.info("== Bootstrap summary ==")
    log.info("Virtualenv ready for use: %s", VENV_DIR)
    print_usage_hint(VENV_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_with_logged_errors(main, sys.argv[1:]))
