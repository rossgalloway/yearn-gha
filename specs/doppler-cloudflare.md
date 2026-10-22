# Cloudflare Workers Deployment Operating Guide (Doppler)

- **Date:** 2026-08-18
- **Status:** Implemented in `yearn/yearn-gha`. Complete the per-app migration runbook before cutting a worker over.
- **Context:** Companion to `specs/doppler-vercel.md`. Same threat model (TanStack npm supply-chain compromise of May 2026), same Doppler OIDC pattern, applied to Cloudflare Workers. It matches the reusable workflow in `.github/workflows/cloudflare-deploy.yml`.

## Decision

GitHub Actions builds the worker on the runner and deploys it with wrangler. GitHub Actions authenticates to Doppler with OIDC, so GitHub stores no long-lived Doppler credential. The runner receives the shared Cloudflare deploy credentials and the worker's runtime secrets. It pushes the runtime secrets to Cloudflare with `wrangler secret bulk`, then runs `wrangler deploy` (see "Application secrets" below).

**Production only — there are no previews.** The workers fleet has no preview environment, so the only supported trigger is a push to the caller repository's default branch. Every other event (`pull_request`, `pull_request_target`, `workflow_dispatch`, `schedule`, non-default-branch pushes) is rejected before Doppler authentication. This is the main event-model difference from the Vercel design.

The fleet uses one **shared account-scoped Cloudflare API token**. Store `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` once in `webops-shared-prod` / `cloudflare-deploy-configs`. The reusable workflow hardcodes both slugs (`.github/workflows/cloudflare-deploy.yml:34-35`) — callers do not pass them. A leaked token can reach every worker in the account.

**The Cloudflare config is separate from the Vercel `deploy-configs`.** `dopplerhq/secrets-fetch-action` exports every value in the fetched config. One combined config would put the Vercel token on every Cloudflare runner and the Cloudflare token on every Vercel runner. A separate config keeps each platform's blast radius to its own token.

There is no per-app Doppler deploy config. Vercel needs a per-app `VERCEL_PROJECT_ID`; a worker's identity is its name in the app repository's `wrangler.toml`. The `project` input names only the Doppler project that holds the worker's runtime secrets in config `prd` (hardcoded at `.github/workflows/cloudflare-deploy.yml:36`).

The guide distinguishes three kinds of data:

- **Deployment secret:** `CLOUDFLARE_API_TOKEN`. Long-lived Cloudflare credential even though Doppler authentication is short-lived.
- **Deployment identifier:** `CLOUDFLARE_ACCOUNT_ID`. Selects the account; configuration, not authentication.
- **Application secrets:** API tokens, RPC credentials, signing keys and similar values the worker reads at runtime. They live in `<project>` / `prd` and pass through the runner on every deploy.

## Application secrets: synced by the workflow

Doppler has a managed sync integration for Cloudflare **Pages**, but not for Cloudflare **Workers** — Workers is documented under Doppler's DIY syncs (`doppler secrets --json | jq … | wrangler secret bulk`). The workflow runs that flow on each deploy and uses the OIDC identity instead of a service token. After the shared fetch (step `doppler`) and `Validate Cloudflare credentials`, two steps run before `Deploy to Cloudflare Workers`:

1. **`Load worker runtime secrets from Doppler`** (step `worker-secrets`). `dopplerhq/secrets-fetch-action` v2.0.0 with `auth-method: oidc`, the same `identity-id`, `doppler-project: ${{ inputs.project }}`, `doppler-config: prd`, and `inject-env-vars: false`. The values are step outputs, not job env.
2. **`Push worker runtime secrets to Cloudflare`.** A `run` step. Its `env` maps `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` from the shared fetch outputs and `WORKER_SECRETS: ${{ toJSON(steps.worker-secrets.outputs) }}`. It then:
   - removes the Doppler meta keys with `jq 'del(.DOPPLER_PROJECT, .DOPPLER_CONFIG, .DOPPLER_ENVIRONMENT)'`. The fetch action emits these keys as unmasked outputs; they are not worker secrets.
   - fails with `::error::` if zero secrets remain. This catches a wrong `project` or an empty `prd` config.
   - pipes the JSON to `bun run wrangler secret bulk` on stdin. wrangler prints key names only.

### Workers with no runtime secrets

Some workers have none — every binding they read is declared in `wrangler.toml`
(plain `[vars]`, a KV namespace, an R2 bucket). `yearn/yearn-uptime-kuma-status`
is the fleet's example: hostnames, a status-page slug and a cache TTL, all
non-secret.

For those, the caller sets `sync-secrets: false` and omits `project`. Both the
`<project>` / `prd` fetch and the push step are skipped, so **no application
secret reaches the runner at all** — a strictly smaller blast radius than a
worker that syncs. The deploy identity then needs read on
`webops-shared-prod` / `cloudflare-deploy-configs` only.

Do not reach for this to work around an empty or mis-named `prd` config. The
zero-secret check below exists to catch exactly that, and `sync-secrets: false`
is an explicit, reviewable assertion that the worker has no runtime secrets —
not a way to silence the check.

Behaviour to know:

- **Zero secrets is an error, not a no-op.** With `sync-secrets` left at `true`, a `prd` config that resolves empty fails the deploy. That catches a wrong `project` or an unpopulated config, rather than shipping a worker whose secrets never arrived.
- **First deploy.** If the worker does not exist yet, `wrangler secret bulk` creates a draft worker in non-interactive mode. `wrangler deploy` then uploads the code.
- **Existing secrets.** `wrangler deploy` keeps the secrets already on the worker.
- **Additive.** `secret bulk` adds and updates keys. It does not delete a key removed from Doppler. Remove stale keys by hand with `wrangler secret delete`.
- **Masking.** The fetch action log-masks every value except values with Doppler visibility `Unmasked`. Set app secrets to Masked.

## What this design protects—and what it does not

Provided controls:

- No static Doppler credential in GitHub; OIDC tokens are short-lived.
- The shared Cloudflare token is stored once, in one Doppler config, isolated from the Vercel credentials.
- Worker runtime secrets have one source of truth (`<project>` / `prd`) and reach Cloudflare on every deploy, with no operator step.
- One trusted reusable workflow implements event checks, action pins, the bun pin, and validation.
- Callers pin the reusable workflow to a full commit SHA; Doppler identities bind `job_workflow_ref` to that SHA.
- All events except a push to the default branch are rejected before Doppler authentication. Pull requests never obtain credentials, so fork-PR and pwn-request paths never reach Doppler.

Not eliminated:

- `CLOUDFLARE_API_TOKEN` is still a long-lived credential exposed to wrangler on the runner. Compromise is account-wide.
- **The build runs on the runner.** Unlike Vercel remote builds, wrangler bundles the worker on the runner, so third-party dependency code executes during the deploy. The workflow narrows the exposure: the app's own `bun install` runs before the Doppler fetches, and all values are read as step outputs (`inject-env-vars: false`) rather than injected into the job environment, so that install's scripts never see them; after the fetches, only the validate step and the push step (explicit env mapping) and the wrangler-action inputs receive them. The workflow also passes no `wranglerVersion`, and before any credential is fetched runs `bun run wrangler --version` — the exact command wrangler-action runs for a bun caller to decide whether to install — failing unless it exits 0 and stdout carries a parseable wrangler version, the same test wrangler-action applies, so wrangler-action runs no install of its own in the step that holds the token. Residual risk: dependency code that wrangler-action bundles still runs in that step, and wrangler runs in the push step; a compromised dependency can exfiltrate the token there. Mitigation is dependency hygiene (lockfiles, review of lockfile diffs), not this workflow.
- **App secrets land on the deploy runner.** They are step outputs of `worker-secrets` and go to one `run` step through explicit env. A compromised dependency in the push step or the wrangler-action step can read them.
- **Step ordering is not an authorization boundary.** `id-token: write` is job-scoped, so any code that runs earlier in the job can mint the same OIDC token and fetch from Doppler itself — `cloudflare-deploy-configs` and `<project>` / `prd` alike. Running the install first keeps the fetched values out of the install step's environment; it does not keep dependency code away from the credential or the app secrets.
- **Stale secrets stay.** `secret bulk` is additive. A key removed from Doppler stays on the worker until an operator deletes it.
- **Secrets land before code.** `secret bulk` creates a worker version with the new secrets before `wrangler deploy` uploads the new code. For a short window the old code runs with the new secrets. Keep secret changes backward compatible across one deploy.
- OIDC policy is an authorization boundary only when all relevant claims are checked; identity IDs are public metadata.

## Target architecture

- A small caller workflow in each worker repository invokes the SHA-pinned reusable workflow in `yearn/yearn-gha`.
- The only supported trigger is a push to the caller repository's default branch, which runs `wrangler deploy`. Everything else is rejected before Doppler authentication.
- The workflow asserts the caller ships `bun.lock` or `bun.lockb`, installs dependencies with `bun install --frozen-lockfile` (the fleet standardizes on bun; the version is pinned in `.github/workflows/cloudflare-deploy.yml`), probes that the local wrangler runs, then fetches `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` from `webops-shared-prod` / `cloudflare-deploy-configs` as step outputs and validates them. It then fetches the worker's runtime secrets from `<project>` / `prd` as step outputs, pushes them with `bun run wrangler secret bulk`, and runs `wrangler deploy` via `cloudflare/wrangler-action`. No `wranglerVersion` is passed: wrangler-action uses the wrangler the app repository already installed, so the wrangler pin is the app's lockfile.
- **Precondition: the caller repository ships a bun lockfile** (`bun.lock` or `bun.lockb`) and a wrangler devDependency. A missing lockfile fails an explicit `bun.lock`/`bun.lockb` check before the install: `--frozen-lockfile` only rejects a lockfile that would change, so with none present bun resolves every dependency — wrangler included — fresh from the registry. A stale lockfile fails the frozen install. A lockfile that never lists a runnable wrangler fails the `bun run wrangler --version` probe (must exit 0 and print a wrangler version). All three run before Doppler is reached. `yearn/yearn-rpc-read-proxy` meets this today; `yearn/dns-bot` ships `package-lock.json` and must migrate to bun before it can call this workflow.
- Post-deploy verification (smoke tests) stays in the caller as a `needs: deploy` job — the reusable workflow is deploy-only.
- Output: `deployment-url` — the production URL parsed by the action. It is empty for a worker with no `workers.dev` subdomain or route, and is only the first target for a multi-route worker.

### Caller shape

```yaml
name: Deploy to Cloudflare Workers

on:
  push:
    branches: [main] # or master — match the repository default branch

permissions:
  contents: read
  id-token: write

cache-mode: none

jobs:
  deploy:
    uses: yearn/yearn-gha/.github/workflows/cloudflare-deploy.yml@<approved-sha> # full commit SHA only
    with:
      project: <doppler-project>
      identity-id: ${{ vars.DOPPLER_PRODUCTION_IDENTITY_ID }}
```

Repository variable: `DOPPLER_PRODUCTION_IDENTITY_ID`. Identity IDs are not secrets.

### Workflow inputs

| Name | Required | Default | Description |
| ---- | -------- | ------- | ----------- |
| `project` | unless `sync-secrets` is false | — | Doppler project holding the worker's runtime secrets in config `prd`. Every value there is pushed to the worker. |
| `sync-secrets` | no | `true` | Whether the worker has runtime secrets to push. `false` skips both the `<project>` / `prd` fetch and the push step. |
| `identity-id` | yes | — | Production Doppler service-account identity; used for the shared `cloudflare-deploy-configs` fetch and the `<project>` / `prd` fetch. |

Output: `deployment-url` (production URL).

### Rejected alternatives

- **PR preview deploys (`wrangler versions upload`).** The fleet has no preview environment. Worker secrets are also per-worker, not per-version, so a PR preview version would run against production secrets — same-repo PR code could read them at runtime. If previews are ever wanted, add them as a separately reviewed change that confronts that risk directly.
- **Cloudflare Pages.** The fleet's Cloudflare footprint is Workers (`yearn/yearn-rpc-read-proxy`, and `yearn/dns-bot` once it moves to bun); Cloudflare's own direction folds Pages into Workers (static assets on Workers). Doppler's managed Pages sync does not apply to Workers projects.
- **Per-app Cloudflare API tokens.** Cloudflare tokens scope to account/zone, not to a single worker, so per-app tokens buy little isolation at a real management cost. Revisit if Cloudflare ships per-worker token scoping.
- **Per-app Doppler deploy config.** Nothing app-specific to store for the deploy — the worker name lives in `wrangler.toml`. An empty per-app fetch is pure surface. The per-app `<project>` / `prd` fetch holds runtime secrets only.
- **`workflow_dispatch` manual deploys.** Same rationale as Vercel: broadens deployment authority and bypasses the event gate. Add a separately reviewed, approval-gated manual path only if an operational need emerges.
- **Out-of-band operator sync (previous design).** An operator ran the DIY `wrangler secret bulk` flow by hand with a config-scoped Doppler token. Doppler and the worker drifted between manual runs, and a deploy gave no guarantee that the worker had the current secrets. Superseded by the in-workflow sync.
- **`wrangler deploy --secrets-file`.** Atomic with the deploy, so it closes the secrets-before-code window. It requires writing the secrets to a file on disk in the wrangler-action step, and a per-app check that the app's pinned wrangler supports the flag. Revisit when the fleet pins a wrangler version that ships it.

## Doppler configuration

Service Account Identities require a Doppler Team or Enterprise workplace.

### Shared deployment project

In the existing `webops-shared-prod` Doppler project, add environment / config `cloudflare-deploy-configs`:

- `CLOUDFLARE_API_TOKEN`: shared account-scoped token. Cloudflare permissions: Workers Scripts Edit (plus Workers Routes Edit if workers manage routes). Set Doppler visibility to **Masked** — the fetch action registers GitHub log redaction only for values that are not Unmasked.
- `CLOUDFLARE_ACCOUNT_ID`

Keep only those two values in the config: every value in it is fetched onto the runner as a step output. Do not add the Vercel credentials here, and do not add the Cloudflare credentials to the Vercel `deploy-configs`.

### Application project

One Doppler project per worker, passed as the `project` input. Its `prd` config holds the worker's runtime secrets. The workflow pushes every value in `prd` to the worker with `wrangler secret bulk` on each deploy, so keep only runtime secrets there. Set each value to Masked. The deploy identity gets read access to `<project>` / `prd`.

A worker with no runtime secrets needs no application project at all: its caller sets `sync-secrets: false`, and its identity is granted read on `webops-shared-prod` / `cloudflare-deploy-configs` only. Do not create an empty `prd` config for it.

### Service accounts, identities, and OIDC

Create one production Doppler service-account identity per worker repository. Grant the underlying service account read-only access to exactly two configs: `webops-shared-prod` / `cloudflare-deploy-configs` and `<project>` / `prd`. For a `sync-secrets: false` caller, grant the first only.

The identity uses:

- Provider: GitHub (`https://token.actions.githubusercontent.com`)
- Audience matching the action's token (verify against a real token; usually `https://github.com/<org>`)
- Subject: `repo:<org>/<repo>:ref:refs/heads/<default-branch>` (or the immutable-format equivalent)
- Exact additional claims: `event_name=push`, `ref=refs/heads/<default-branch>`, and `job_workflow_ref=yearn/yearn-gha/.github/workflows/cloudflare-deploy.yml@<approved-sha>`
- No wildcards on claims.

The `job_workflow_ref` condition is mandatory — without it, any workflow in the repository with `id-token: write` can request the identity. Bind it to the same full commit SHA callers pin in `uses:`; update caller pins and identity claims together in a reviewed rollout. The subject and `ref` bindings are defense in depth on top of the workflow's own default-branch check.

## GitHub controls

Same as the Vercel guide (`specs/doppler-vercel.md` — branch protection, no `pull_request_target`, SHA-pinned actions). Workers-specific additions:

- The reusable workflow owns deploy serialization: one job-level concurrency group per caller repository, `cancel-in-progress: false`. Cancellation is deliberately not inherited from the Vercel guide — a cancelled `wrangler deploy` has no defined outcome mid-upload, and GitHub supersedes the queued run anyway, so the newest commit still wins. Callers need no concurrency block.
- Pin bun in the reusable workflow (`1.4.4` today) and wrangler in each app repository's devDependencies plus lockfile; upgrade either through a reviewed change.
- Review lockfile diffs: dependency code runs during the `wrangler secret bulk` step and the wrangler-action bundle step, which hold the deploy token (and, in the push step, the app secrets).
- A person who can land code on the default branch can deploy the worker. Default-branch protection is the production authorization boundary — there is no other deploy path through this workflow.

## Cloudflare controls

- One account-scoped token for the fleet, stored only in `webops-shared-prod` / `cloudflare-deploy-configs`. A leak is account-wide; rotate from one place.
- Scope the token to the narrowest permissions that deploy (Workers Scripts Edit; add Routes only if needed).
- Remove unnecessary Cloudflare account memberships; deploys should flow through this workflow.

## Migration runbook

1. Put `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` in `webops-shared-prod` / `cloudflare-deploy-configs`.
2. Create the production Doppler identity. Bind subject, audience, `event_name`, `ref`, and `job_workflow_ref` to the pinned workflow SHA. Grant read only on `webops-shared-prod` / `cloudflare-deploy-configs` and `<project>` / `prd`.
3. Create the worker's Doppler project and populate its `prd` config with the runtime secrets only. Grant the identity read on `<project>` / `prd`. Before the first run, diff the Doppler key set against `wrangler secret list` on the live worker: the sync adds and overwrites keys but deletes none.
4. Confirm the repository has a bun lockfile and a pinned wrangler devDependency; migrate off npm/yarn first if not. Then replace the hand-rolled deploy workflow with the caller shape above, pass `project`, pin the reusable SHA, set the `DOPPLER_PRODUCTION_IDENTITY_ID` var, and remove the `CLOUDFLARE_*` GitHub secrets. Retire the manual operator sync command. Carry any post-deploy step from the old workflow (e.g. `bun run smoke`) into a `needs: deploy` job in the caller — the reusable workflow runs nothing after `wrangler deploy`.
5. Confirm a push to the default branch deploys, and that a pull request run fails at the event check without touching Doppler.

## References

- [Doppler service account identities (OIDC)](https://docs.doppler.com/docs/service-account-identities)
- [Doppler Secrets Fetch Action](https://github.com/DopplerHQ/secrets-fetch-action)
- [Doppler Cloudflare Workers (DIY sync)](https://docs.doppler.com/docs/cloudflare-workers)
- [cloudflare/wrangler-action](https://github.com/cloudflare/wrangler-action)
- [wrangler secret bulk](https://developers.cloudflare.com/workers/wrangler/commands/#secret)
- [Cloudflare API token permissions](https://developers.cloudflare.com/fundamentals/api/reference/permissions/)
- Repo implementation: `yearn/yearn-gha` — `.github/workflows/cloudflare-deploy.yml`
- Sibling guide: `specs/doppler-vercel.md`
