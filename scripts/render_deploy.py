#!/usr/bin/env python3
"""
scripts/render_deploy.py

Trigger a Render deploy for a single service and block until that *specific*
deploy reaches a terminal state. This replaces the previous "sleep then hope"
approach in the deploy workflow: the exit code reflects Render's real deploy
status, never a timer.

Behaviour
---------
1. POST https://api.render.com/v1/services/{serviceId}/deploys with the GitHub
   commit SHA as ``commitId`` (so Render deploys exactly the commit CI built).
2. Capture the returned deploy id.
3. Poll GET https://api.render.com/v1/services/{serviceId}/deploys/{deployId}
   until the deploy becomes live (exit 0) or fails/cancels/times out (exit != 0).

Environment
-----------
Required:
  RENDER_API_KEY   Render API key (Bearer token). Never printed.
  RENDER_SERVICE_ID   Target service id, e.g. "srv-xxxxxxxx".
  GITHUB_SHA       Commit SHA to deploy. Provided automatically by GitHub Actions.

Optional:
  RENDER_DEPLOY_TIMEOUT_SECONDS   Overall poll budget (default 1800).
  RENDER_DEPLOY_POLL_SECONDS      Seconds between polls (default 15).

Only the Python standard library is used so the script runs with no extra
dependencies installed.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

API_ROOT = "https://api.render.com/v1"

# Render deploy statuses. See https://api-docs.render.com/reference/get-deploy
LIVE_STATUS = "live"
IN_PROGRESS_STATUSES = {
    "created",
    "queued",
    "build_in_progress",
    "update_in_progress",
    "pre_deploy_in_progress",
}
FAILED_STATUSES = {
    "build_failed",
    "update_failed",
    "pre_deploy_failed",
    "canceled",
    "deactivated",
}


def _fail(message: str) -> "None":
    """Print an error to stderr and exit non-zero."""
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def _env(name: str, required: bool = True, default: str = "") -> str:
    value = os.environ.get(name, default)
    if required and not value:
        _fail(f"{name} environment variable is not set")
    return value


def _request(method: str, url: str, api_key: str, payload: "dict | None" = None) -> dict:
    """Make a Render API call and return the parsed JSON object.

    Raises SystemExit (via _fail) on transport errors or malformed responses.
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # Read the error body for context but never echo the API key.
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:500]
        except Exception:
            pass
        _fail(f"{method} {url} returned HTTP {exc.code}. {detail}".strip())
    except urllib.error.URLError as exc:
        _fail(f"{method} {url} failed: {exc.reason}")

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        _fail(f"{method} {url} returned a non-JSON response: {body[:200]!r}")

    return parsed


def trigger_deploy(service_id: str, api_key: str, commit_sha: str) -> str:
    """Create a deploy for ``commit_sha`` and return the new deploy id."""
    url = f"{API_ROOT}/services/{service_id}/deploys"
    payload = {"commitId": commit_sha} if commit_sha else {}
    print(f"Triggering Render deploy for service {service_id} at commit {commit_sha or '(latest)'}...")

    result = _request("POST", url, api_key, payload=payload)
    if not isinstance(result, dict):
        _fail(f"Unexpected deploy-create response (not an object): {result!r}")

    deploy_id = result.get("id")
    if not deploy_id:
        _fail(f"Deploy-create response did not contain a deploy id: {result!r}")

    print(f"Created deploy {deploy_id} (initial status: {result.get('status', 'unknown')})")
    return deploy_id


def poll_deploy(service_id: str, api_key: str, deploy_id: str, timeout_s: int, poll_s: int) -> None:
    """Poll a specific deploy until it is live, fails, or times out."""
    url = f"{API_ROOT}/services/{service_id}/deploys/{deploy_id}"
    deadline = time.monotonic() + timeout_s
    last_status = None

    while True:
        result = _request("GET", url, api_key)
        # The get-deploy endpoint may return the deploy object directly or
        # wrapped as {"deploy": {...}}; handle both shapes.
        if isinstance(result, dict) and "deploy" in result and isinstance(result["deploy"], dict):
            result = result["deploy"]
        if not isinstance(result, dict):
            _fail(f"Unexpected deploy-status response (not an object): {result!r}")

        status = result.get("status")
        if not status:
            _fail(f"Deploy-status response did not contain a status: {result!r}")

        if status != last_status:
            print(f"Deploy {deploy_id} status: {status}")
            last_status = status

        if status == LIVE_STATUS:
            print(f"Deploy {deploy_id} is live. Deployment succeeded.")
            return

        if status in FAILED_STATUSES:
            _fail(f"Deploy {deploy_id} ended in non-live status '{status}'.")

        if status not in IN_PROGRESS_STATUSES:
            # Unknown/new status: keep polling but make it visible.
            print(f"Deploy {deploy_id} reported unrecognised status '{status}'; continuing to poll.")

        if time.monotonic() >= deadline:
            _fail(f"Timed out after {timeout_s}s waiting for deploy {deploy_id} (last status: {status}).")

        time.sleep(poll_s)


def main() -> None:
    api_key = _env("RENDER_API_KEY")
    service_id = _env("RENDER_SERVICE_ID")
    commit_sha = _env("GITHUB_SHA", required=False)

    try:
        timeout_s = int(_env("RENDER_DEPLOY_TIMEOUT_SECONDS", required=False, default="1800"))
        poll_s = int(_env("RENDER_DEPLOY_POLL_SECONDS", required=False, default="15"))
    except ValueError:
        _fail("RENDER_DEPLOY_TIMEOUT_SECONDS and RENDER_DEPLOY_POLL_SECONDS must be integers")

    if poll_s <= 0:
        _fail("RENDER_DEPLOY_POLL_SECONDS must be a positive integer")

    deploy_id = trigger_deploy(service_id, api_key, commit_sha)
    poll_deploy(service_id, api_key, deploy_id, timeout_s, poll_s)


if __name__ == "__main__":
    main()
