#!/usr/bin/env python3
"""Validate a manual Render deployment before any API request is made."""

import os
import sys
from dataclasses import dataclass
from typing import Mapping, NoReturn


@dataclass(frozen=True)
class DeployConfig:
    api_service_id: str
    worker_service_id: str
    api_url: str
    commit_sha: str


ENVIRONMENTS = {
    "staging": {
        "branch": "dev",
        "api_service": "RENDER_STAGING_SERVICE_ID",
        "worker_service": "RENDER_STAGING_WORKER_SERVICE_ID",
        "api_url": "STAGING_API_URL",
    },
    "production": {
        "branch": "main",
        "api_service": "RENDER_PRODUCTION_SERVICE_ID",
        "worker_service": "RENDER_PRODUCTION_WORKER_SERVICE_ID",
        "api_url": "PRODUCTION_API_URL",
    },
}
SMOKE_SECRETS = (
    "JWT_SECRET",
    "AZURE_SUBSCRIPTION_ID",
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
    "AZURE_TENANT_ID",
)


def fail(message: str) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def validate(environment: Mapping[str, str]) -> DeployConfig:
    target = environment.get("DEPLOY_ENVIRONMENT", "")
    mapping = ENVIRONMENTS.get(target)
    if mapping is None:
        fail("DEPLOY_ENVIRONMENT must be staging or production")

    ref_name = environment.get("GITHUB_REF_NAME", "")
    expected_branch = mapping["branch"]
    if ref_name != expected_branch:
        fail(f"{target} deployments must be dispatched from {expected_branch}; received {ref_name or '(empty)'}")

    required = {
        "RENDER_API_KEY": environment.get("RENDER_API_KEY", ""),
        mapping["api_service"]: environment.get(mapping["api_service"], ""),
        mapping["worker_service"]: environment.get(mapping["worker_service"], ""),
        mapping["api_url"]: environment.get(mapping["api_url"], ""),
        "GITHUB_SHA": environment.get("GITHUB_SHA", ""),
    }
    if environment.get("RUN_SMOKE_TESTS", "false").lower() == "true":
        required.update({name: environment.get(name, "") for name in SMOKE_SECRETS})
    missing = [name for name, value in required.items() if not value]
    if missing:
        fail(f"Missing required deployment configuration: {', '.join(missing)}")

    return DeployConfig(
        api_service_id=required[mapping["api_service"]],
        worker_service_id=required[mapping["worker_service"]],
        api_url=required[mapping["api_url"]],
        commit_sha=required["GITHUB_SHA"],
    )


def write_github_env(config: DeployConfig, path: str) -> None:
    with open(path, "a", encoding="utf-8") as output:
        output.write(f"RENDER_API_SERVICE_ID={config.api_service_id}\n")
        output.write(f"RENDER_WORKER_SERVICE_ID={config.worker_service_id}\n")
        output.write(f"API_URL={config.api_url}\n")


def main() -> None:
    config = validate(os.environ)
    github_env = os.environ.get("GITHUB_ENV", "")
    if not github_env:
        fail("GITHUB_ENV environment variable is not set")
    write_github_env(config, github_env)
    print(f"Validated {os.environ['DEPLOY_ENVIRONMENT']} deployment preflight for SHA {config.commit_sha}")


if __name__ == "__main__":
    main()
