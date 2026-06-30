#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path

import yaml


def load_yaml(path_value: str) -> dict:
    path = Path(path_value)
    if not path.exists():
      raise SystemExit(f"context file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def main() -> int:
    context_path = os.environ.get("ORCHESTRATOR_CONTEXT_PATH", "contexts/demo.yaml")
    cfg = load_yaml(context_path)
    summary = {
        "message": "Terraform bootstrap placeholder executed successfully.",
        "context": context_path,
        "repository_url": cfg.get("git", {}).get("repository_url"),
        "branch": cfg.get("git", {}).get("branch"),
        "aidp_ocid": cfg.get("aidp", {}).get("ocid"),
        "source_workspace_name": cfg.get("source", {}).get("workspace_name"),
        "target_workspace_name": cfg.get("target", {}).get("workspace_name"),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
