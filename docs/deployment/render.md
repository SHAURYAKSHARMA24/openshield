# OpenShield — Render Deployment

This document describes how OpenShield is deployed to [Render](https://render.com)
using a **deterministic, GitHub Actions-controlled** pipeline. Deploys succeed or
fail based on Render's *actual* deploy status, never on a fixed timer.

- Blueprint: [`render.yaml`](../../render.yaml)
- Deploy script: [`scripts/render_deploy.py`](../../scripts/render_deploy.py)
- Workflow: [`.github/workflows/deploy.yml`](../../.github/workflows/deploy.yml)

> **Deploys are currently MANUAL only.** The workflow runs solely via
> `workflow_dispatch` (Actions → *Deploy API to Render* → *Run workflow*), where you
> pick the target environment (`staging` or `production`). There is **no automatic
> deploy on push or merge**: the staging/production Render services, database, and
> secrets must exist first, so auto-deploying before that infrastructure is
> provisioned would fail. Automatic push deploys can be re-enabled later once
> maintainers confirm the staging/prod Render setup.

---

## 1. Service layout

The blueprint declares four services across two environments:

| Service | Type | Branch | Start command | Purpose |
|---|---|---|---|---|
| `openshield-api-staging` | web | `dev` | `./startup.sh` | Staging API (Gunicorn) |
| `openshield-worker-staging` | worker | `dev` | `python -m scanner.worker` | Staging scan worker |
| `openshield-api` | web | `main` | `./startup.sh` | Production API (Gunicorn) |
| `openshield-worker` | worker | `main` | `python -m scanner.worker` | Production scan worker |

### Staging / production mapping

The blueprint tracks branches so that, once synced, each Render service builds from
the right branch:

- `dev` → **staging** (`openshield-api-staging`, `openshield-worker-staging`)
- `main` → **production** (`openshield-api`, `openshield-worker`)

When you run the manual deploy workflow you choose the environment explicitly
(`staging` or `production`); the workflow deploys that environment's service and
health-checks/smoke-tests it against its own URL. Staging and production are served
from **separate URLs** — they do not share a fallback production URL.

### The worker is a separate service

The scan worker runs as its own Render **background worker** service, not as a
subshell inside the web container (`startup.sh` no longer launches it). Consequences:

- Restarting, redeploying, or scaling the **web** service does **not** kill the
  worker process.
- Interrupted scans are recovered by the worker itself — `scanner.worker` calls
  `db.recover_stale_scans(timeout_minutes=60)` on every poll loop.
- Web and worker in the same environment **must share the same `DATABASE_URL` and
  `JWT_SECRET`**. These are declared per service with `sync: false`, so set them to
  identical values in the dashboard for the web and worker of the same environment.
  (Render does not allow `sync: false` inside env var groups, so the values cannot
  be centralised in the blueprint — this pairing is enforced by convention.)

---

## 2. Deploy runtime (native Python)

Each service uses Render's native Python runtime:

- **Build command:** `pip install -r requirements.txt`
- **Web start command:** `./startup.sh` (DB init, then Gunicorn)
- **Worker start command:** `python -m scanner.worker`

> The repository also ships a `Dockerfile` for local `docker-compose`. If you
> prefer to deploy on Render with Docker instead, set each service's `runtime` to
> `docker` and provide a `dockerCommand` (`./startup.sh` for web,
> `python -m scanner.worker` for the worker). The native-Python setup above matches
> the currently documented Render configuration.

### Validating the blueprint

`render.yaml` is checked in CI only for YAML validity — there is no offline Render
schema validator (the Render CLI does not provide a `blueprint validate` command).
**Maintainers must validate the blueprint against Render's live schema before
relying on it**, by either:

1. Opening the repo as a **Blueprint** in the Render dashboard
   (*New → Blueprint*), which parses `render.yaml` and previews the four services
   before creating anything; or
2. Running the blueprint sync in a throwaway Render workspace first.

Render treats `autoDeployTrigger` as the current field and ignores the deprecated
`autoDeploy`; the values used here are `"off"` (web) and `commit` (worker).

---

## 3. GitHub Actions secrets

Configure these under **Settings → Secrets and variables → Actions**. No values
are stored in the repo; `render.yaml` declares secrets with `sync: false` so
Render never overwrites the values you set in its dashboard.

| Secret | Used for |
|---|---|
| `RENDER_API_KEY` | Authenticating Render API deploy calls |
| `RENDER_STAGING_SERVICE_ID` | Service id of `openshield-api-staging` |
| `RENDER_PRODUCTION_SERVICE_ID` | Service id of `openshield-api` |
| `STAGING_API_URL` | Staging base URL (health gate + smoke tests) |
| `PRODUCTION_API_URL` | Production base URL (health gate + smoke tests) |
| `JWT_SECRET` | Signing smoke-test JWTs (must match the value set in Render) |
| `AZURE_SUBSCRIPTION_ID` | Real-scan smoke tests |
| `AZURE_CLIENT_ID` | Real-scan smoke tests |
| `AZURE_CLIENT_SECRET` | Real-scan smoke tests |
| `AZURE_TENANT_ID` | Real-scan smoke tests |

The workflow selects the service id and API URL from the environment you pick when
running it: `staging` uses the staging pair, `production` uses the production pair.

---

## 4. How deterministic deploy polling works

The deploy workflow is triggered **manually** (`workflow_dispatch`) with an
`environment` input (`staging` or `production`) and an optional `run_smoke_tests`
toggle. It runs
[`scripts/render_deploy.py`](../../scripts/render_deploy.py) with `RENDER_API_KEY`,
the selected environment's `RENDER_SERVICE_ID`, and `GITHUB_SHA` (the commit the
workflow was dispatched on). The script:

1. **Triggers a deploy** pinned to the commit CI built:
   `POST https://api.render.com/v1/services/{serviceId}/deploys` with
   `{"commitId": "<GITHUB_SHA>"}`.
2. **Captures the returned deploy id.**
3. **Polls that specific deploy:**
   `GET https://api.render.com/v1/services/{serviceId}/deploys/{deployId}`.
4. Continues while the status is in progress (`created`, `queued`,
   `build_in_progress`, `update_in_progress`, `pre_deploy_in_progress`).
5. **Exits 0 only when the deploy becomes `live`.**
6. **Exits non-zero** if the deploy is `build_failed`, `update_failed`,
   `pre_deploy_failed`, `canceled`, or `deactivated`, or if the overall timeout
   (`RENDER_DEPLOY_TIMEOUT_SECONDS`, default 1800s) is exceeded.

Poll interval and timeout are tunable via `RENDER_DEPLOY_POLL_SECONDS`
(default 15) and `RENDER_DEPLOY_TIMEOUT_SECONDS` (default 1800). The API key is
never printed.

Only after the deploy is live does the workflow run the **health gate**
(`GET {API_URL}/health` must return 200) and, when `run_smoke_tests` is enabled, the
**smoke tests** (`tests/smoke_test.py`) against the selected environment's URL. Any
of these failing fails the workflow run.

### Deploy control (`autoDeployTrigger`) and avoiding duplicate deploys

`render.yaml` uses `autoDeployTrigger` (the current field; it replaces the
deprecated `autoDeploy`):

- **Web services → `autoDeployTrigger: "off"`.** The manual deploy workflow is the
  single source of truth for web deploys, so Render's own auto-deploy is turned off.
  This also avoids duplicate/racing deploys if a `push:` trigger is added later
  (Render auto-deploy *and* the Actions deploy firing for the same commit). `"off"`
  is quoted because unquoted `off` is parsed as the boolean `false` by YAML.
- **Worker services → `autoDeployTrigger: commit`.** Once the blueprint is synced
  and the worker infrastructure exists, the workers auto-deploy on push to their
  branch; nothing else deploys them, so there is no duplication. Until that
  infrastructure exists, no worker deploys happen at all. The health gate and smoke
  tests do **not** cover the worker; to deploy a worker deterministically (e.g. to
  gate or roll it back precisely), run `scripts/render_deploy.py` with
  `RENDER_SERVICE_ID` set to the worker's service id.

In short: **web = deployed by the manual workflow; worker = auto-deployed by Render on
push** (and optionally by the same script).

---

## 5. Manual verification

### Staging

```bash
# Health
curl -fsS "$STAGING_API_URL/health"          # expect {"status":"ok"}

# Smoke suite
API_URL="$STAGING_API_URL" JWT_SECRET="<staging-jwt-secret>" \
  python tests/smoke_test.py
```

Confirm staging is deployable: in **Actions → *Deploy API to Render* → *Run
workflow***, choose `environment: staging` (from the `dev` branch), watch the deploy
step reach *live*, then re-run the health check.

### Production

```bash
# Health
curl -fsS "$PRODUCTION_API_URL/health"        # expect {"status":"ok"}

# Smoke suite
API_URL="$PRODUCTION_API_URL" JWT_SECRET="<prod-jwt-secret>" \
  python tests/smoke_test.py
```

Confirm the worker is separate: restart the web service in the Render dashboard —
the worker service stays running and continues processing the scan queue.

---

## 6. Rollback — redeploy a previous commit via the Render API

Because deploys are pinned to a `commitId`, rolling back is just deploying an
older commit. Use the same mechanism CI uses.

**Option A — the deploy script (recommended):**

```bash
export RENDER_API_KEY="<render-api-key>"
export RENDER_SERVICE_ID="<staging-or-production-service-id>"
export GITHUB_SHA="<known-good-commit-sha>"
python scripts/render_deploy.py
```

The script triggers the deploy, waits for it to go live, and exits non-zero if the
rollback deploy fails.

**Option B — raw Render API:**

```bash
# Trigger a deploy of a known-good commit
curl -X POST "https://api.render.com/v1/services/$RENDER_SERVICE_ID/deploys" \
  -H "Authorization: Bearer $RENDER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"commitId": "<known-good-commit-sha>"}'
# -> note the returned "id" (dep-...), then poll it:

curl "https://api.render.com/v1/services/$RENDER_SERVICE_ID/deploys/<deploy-id>" \
  -H "Authorization: Bearer $RENDER_API_KEY"
# repeat until "status": "live"
```

You can also **Rollback** to a prior successful deploy from the Render dashboard
(service → *Deploys* → previous deploy → *Rollback*).

Roll back the matching **worker** service the same way (with its own service id) if
a bad commit changed worker behaviour.

---

## 7. What NOT to do

- ❌ **Do not reintroduce a fixed `sleep` to "wait for the deploy"** in the
  workflow. Gate on Render's real deploy status via `scripts/render_deploy.py`.
- ❌ **Do not run the scan worker inside the web container** (no background
  subshell in `startup.sh`). It must stay a separate Render worker service.
- ❌ **Do not add a `push:` trigger to the deploy workflow** until maintainers have
  confirmed the staging/prod Render services, database, and secrets exist. Auto-
  deploying before the infrastructure is provisioned will fail. Deploys stay manual
  (`workflow_dispatch`) until then.
- ❌ **Do not set the web services to `autoDeployTrigger: commit`** (or re-add the
  deprecated `autoDeploy: true`) while GitHub Actions also deploys them — that
  causes duplicate/racing deploys. Keep web services on `autoDeployTrigger: "off"`.
- ❌ **Do not put secret values or real service ids** in `render.yaml` or the
  workflow. Use GitHub secrets and `sync: false` env var declarations.
- ❌ **Do not point `dev` and `main` at the same URL.** Staging and production are
  distinct services with distinct URLs.
