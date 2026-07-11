# OpenShield Render deployment

OpenShield uses a manual, deterministic GitHub Actions workflow to deploy the API
and scan worker to Render. Deployments are coordinated around one immutable GitHub
SHA, but they are not atomic.

- Blueprint: [`render.yaml`](../../render.yaml)
- Workflow: [`.github/workflows/deploy.yml`](../../.github/workflows/deploy.yml)
- Deploy client: [`scripts/render_deploy.py`](../../scripts/render_deploy.py)

## Service layout

| Environment | Service | Type | Branch | Start command |
|---|---|---|---|---|
| Staging | `openshield-api-staging` | web | `dev` | `./startup.sh` |
| Staging | `openshield-worker-staging` | worker | `dev` | `python -m scanner.worker` |
| Production | `openshield-api` | web | `main` | `./startup.sh` |
| Production | `openshield-worker` | worker | `main` | `python -m scanner.worker` |

All four services use `autoDeployTrigger: "off"`. Render does not independently
deploy either worker or API service; GitHub Actions is the sole deployment
controller. The workflow may only be dispatched as follows:

- `staging` from the `dev` branch;
- `production` from the `main` branch.

Any other branch/environment combination fails during preflight before a Render
deployment is created. The job uses the selected GitHub Environment, allowing
maintainers to add approval gates and protection rules for staging or production.

The API startup applies database migrations with `alembic upgrade head` and then
executes Gunicorn. The worker runs only in its separate Render worker service.

## Required configuration

Configure these GitHub Actions secrets for every deployment:

| Secret | Purpose |
|---|---|
| `RENDER_API_KEY` | Authenticate Render API requests |
| `RENDER_STAGING_SERVICE_ID` | Staging API service ID |
| `RENDER_STAGING_WORKER_SERVICE_ID` | Staging worker service ID |
| `RENDER_PRODUCTION_SERVICE_ID` | Production API service ID |
| `RENDER_PRODUCTION_WORKER_SERVICE_ID` | Production worker service ID |
| `STAGING_API_URL` | Staging API health and smoke-test URL |
| `PRODUCTION_API_URL` | Production API health and smoke-test URL |

When `run_smoke_tests` is enabled, these existing secrets are also mandatory:

- `JWT_SECRET`
- `AZURE_SUBSCRIPTION_ID`
- `AZURE_CLIENT_ID`
- `AZURE_CLIENT_SECRET`
- `AZURE_TENANT_ID`

They are not required for a health-only deployment. The preflight validates every
value required for the selected operation before either deployment POST and never
prints secret values.

Each Render service also needs its application environment configured. Within an
environment, the API and worker must use matching `DATABASE_URL` and `JWT_SECRET`
values and the required Azure credentials. Values declared with `sync: false` in
the Blueprint must be configured in Render and are never stored in this repository.

## Coordinated deterministic deployment

For the selected environment, the workflow:

1. validates the selected branch, service IDs, API URL, SHA, Render key, and any
   enabled smoke-test credentials;
2. creates one API deployment pinned to `github.sha` and records its exact ID;
3. creates one worker deployment pinned to the same `github.sha` and records its
   exact ID;
4. polls each exact deployment ID independently until both are `live`;
5. runs the API `/health` gate only after both services are live;
6. optionally runs the smoke suite only after both deployments and health succeed.

The deployment-creation POST is attempted once. An ambiguous POST network failure
may mean Render accepted the request even though Actions did not receive its ID, so
automatically repeating the POST could create a duplicate deployment.

Polling GET requests retry transient network failures, HTTP 429, and selected HTTP
5xx responses with bounded backoff. Permanent errors, malformed responses, terminal
Render failures, and the overall timeout fail immediately. Retry time counts against
the overall timeout. Logs identify the service, SHA, deployment ID when known, and
last status without printing credentials.

## Partial failure and recovery

API and worker deployment is coordinated, not atomic. Possible partial states
include:

- the API deployment is created but worker creation fails;
- one deployment reaches `live` while the other fails;
- Actions loses contact while Render continues an accepted deployment;
- a rerun creates new deployment IDs for the same SHA.

When this happens:

1. inspect both services in Render and record the API and worker deployment IDs;
2. confirm the commit SHA attached to each deployment;
3. rerun the workflow from the correct branch, using the same commit SHA where
   possible;
4. do not manually deploy a different commit to only one service;
5. confirm both services converge on the same SHA before treating the environment
   as healthy.

If the initial POST failed ambiguously, first inspect Render for a deployment at the
requested SHA before rerunning. A rerun is safe operationally only when maintainers
understand whether the earlier request was accepted.

## Worker verification limitation

Render reporting the worker deployment as `live` proves that Render deployed and
started the service. It does not prove that the worker can connect to the database,
claim queued scans, process them, or persist completed results. This repository does
not currently expose a worker heartbeat or queue-processing health endpoint.
Application-level worker verification therefore remains a separate live step: queue
a controlled scan and confirm it is claimed and completed successfully.

## Blueprint validation

Local YAML parsing verifies syntax only. It is not equivalent to validation against
Render's current Blueprint schema. Maintainers may validate the Blueprint through
the Render dashboard preview or Render's supported Blueprint validation API before
syncing services. Live validation requires Render credentials and must not be
reported as completed unless the live API or dashboard was actually used.

## Manual deployment and verification

1. Open **Actions > Deploy API and worker to Render > Run workflow**.
2. Select `dev` with `staging`, or `main` with `production`.
3. Choose whether to run the real-scan smoke tests.
4. Record the GitHub SHA and both Render deployment IDs from the workflow logs.
5. Confirm the API health result and, if enabled, the smoke-test result.
6. Perform the application-level worker verification described above.
7. Confirm both Render services report the same SHA.

## Rollback

Rollback must keep the API and worker aligned. Dispatch or manually invoke the
deployment client for the same known-good SHA against both service IDs, capture both
new deployment IDs, and wait for both to become live. Do not roll back only one
service to a different commit.

The client supports separate creation and waiting phases:

```bash
RENDER_SERVICE_ID="<service-id>" GITHUB_SHA="<known-good-sha>" \
  python scripts/render_deploy.py create

RENDER_SERVICE_ID="<service-id>" RENDER_DEPLOY_ID="<deployment-id>" \
  GITHUB_SHA="<known-good-sha>" python scripts/render_deploy.py wait
```

The default `deploy` command creates and waits for one service and is useful for
manual recovery, but maintainers must repeat it for the matching API or worker so
both converge on the same SHA.
