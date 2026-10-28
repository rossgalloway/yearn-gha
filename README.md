# Deploy workflows

Reusable GitHub workflows for deploying to Vercel and Cloudflare Workers with
credentials resolved from Doppler via OIDC. No static secrets live in GitHub.

Both workflows share the same skeleton: authenticate to Doppler as a
service-account identity using the GitHub Actions OIDC token, fetch the
deploy credentials, and deploy. The Cloudflare workflow also fetches the
worker's `prd` runtime secrets and pushes them with `wrangler secret bulk`
before deploy. The deploy environment is derived from the
triggering event, never passed by the caller. Vercel deploys previews for
pull requests and production for a `push` to the caller repository's default
branch; Cloudflare Workers deploys production only. Unsupported events
(`workflow_dispatch`, `schedule`, non-default-branch pushes, fork pull
requests) are rejected before credentials are loaded.

| Workflow | Guide |
| -------- | ----- |
| `.github/workflows/vercel-deploy.yml` | `specs/doppler-vercel.md` |
| `.github/workflows/cloudflare-deploy.yml` | `specs/doppler-cloudflare.md` |

## Vercel deploy

The workflow fetches `VERCEL_TOKEN`, `VERCEL_ORG_ID` and `VERCEL_PROJECT_ID`
from the `deploy-configs` config of the given Doppler project(s), and runs
`vercel deploy` — the build happens remotely on Vercel with the env vars
synced there by Doppler Vercel integrations. App secrets never pass through
GitHub Actions.

`VERCEL_TOKEN` and `VERCEL_ORG_ID` come from the shared `webops-shared-prod`
project (`deploy-configs` config), defined in the workflow — one place to
rotate them across apps. The app project only holds its `VERCEL_PROJECT_ID`,
always read from `deploy-configs` (one Vercel project, one ID). The workflow
never fetches `prd` or `preview`.

Pull requests deploy Vercel previews (no `--prod`); a default-branch push
deploys production (`--prod`). The production identity's OIDC subject binding
(`ref:refs/heads/main`, see Doppler setup) is defense in depth on top of the
workflow's own event checks.

### Usage

```yaml
name: Deploy to Vercel

on:
  pull_request:
  push:
    branches: [main]

concurrency:
  group: vercel-deploy-${{ github.ref }}
  cancel-in-progress: true

permissions:
  contents: read
  deployments: write
  id-token: write
  pull-requests: write

cache-mode: none

jobs:
  deploy:
    uses: yearn/yearn-gha/.github/workflows/vercel-deploy.yml@<approved-sha> # pin to the approved full commit SHA
    with:
      project: my-app
      identity-id: ${{ github.event_name == 'pull_request' && vars.DOPPLER_PREVIEW_IDENTITY_ID || vars.DOPPLER_PRODUCTION_IDENTITY_ID }}
```

Caller workflows must grant `id-token: write` (OIDC login to Doppler) and
`pull-requests: write` (preview URL comments), in addition to `contents: read`
and `deployments: write`. Reusable workflows cannot elevate beyond the
caller's token permissions. Configure `DOPPLER_PREVIEW_IDENTITY_ID` and
`DOPPLER_PRODUCTION_IDENTITY_ID` as Actions repository variables in each
caller. Use two identities; the `pull_request` vs
`ref:refs/heads/<default>` subject binding is what stops a PR from deploying
production.

### Inputs

| Name          | Required | Default | Description                                                                 |
| ------------- | -------- | ------- | --------------------------------------------------------------------------- |
| `project`     | yes      | —       | Doppler project containing `VERCEL_PROJECT_ID` in `deploy-configs`.         |
| `identity-id` | yes      | —       | Doppler service-account identity for the event; loads shared + app `deploy-configs`. |
| `preview-deployment-name` | no | `preview` | GitHub deployment environment for pull request deploys. Match it to any required deployment in branch protection. |
| `production-deployment-name` | no | `production` | GitHub deployment environment for default-branch deploys. |

### Outputs

| Name             | Description                                    |
| ---------------- | ---------------------------------------------- |
| `deployment-url` | Preview or production URL returned by Vercel.  |

Consume it from a downstream job with
`${{ needs.deploy.outputs.deployment-url }}`.

### Doppler setup

1. Create the shared `webops-shared-prod` project with `VERCEL_TOKEN` and
   `VERCEL_ORG_ID` in the `deploy-configs` config, and a project per app with
   `VERCEL_PROJECT_ID` in `deploy-configs`.
   Keep ONLY those creds in `deploy-configs` — the workflow exports every
   secret in that config onto the runner.
   Set `VERCEL_TOKEN` visibility to Masked. The fetch action registers
   GitHub log redaction only for values that are not Unmasked; an Unmasked
   token shows in plaintext if it reaches a log.
2. App secrets live in `prd` and `preview` and reach Vercel via one
   integration per env (`preview` → Vercel Preview, `prd` → Vercel
   Production), with Sensitive on. Do not attach a Vercel integration to
   `deploy-configs`.
3. Create separate preview and production service-account identities with
   OIDC. Both use discovery/issuer URL
   `https://token.actions.githubusercontent.com`. Audience must match the
   token the fetch action requests (no custom audience; typically
   `https://github.com/<org>` — confirm against a real token).
   - Preview identity subject: `repo:<org>/<repo>:pull_request`. Add claims
     `job_workflow_ref: yearn/yearn-gha/.github/workflows/vercel-deploy.yml@<approved-sha>`
     and `event_name: pull_request`.
   - Production identity subject:
     `repo:<org>/<repo>:ref:refs/heads/main` (replace `main` if the default
     branch differs). Add claims
     `job_workflow_ref: yearn/yearn-gha/.github/workflows/vercel-deploy.yml@<approved-sha>`,
     `event_name: push`, and `ref: refs/heads/<default-branch>`.

   Grant each identity read access only to the configs it will fetch:

   | Identity | Shared project | App project |
   | -------- | -------------- | ----------- |
   | Preview (`pull_request`) | `webops-shared-prod` / `deploy-configs` | `<app>` / `deploy-configs` |
   | Production (`push`) | `webops-shared-prod` / `deploy-configs` | `<app>` / `deploy-configs` |

   Do not grant the deploy identities access to `prd` or `preview`.
4. Pass the event-appropriate identity ID as `identity-id` in the caller, as
   shown above. The same identity authenticates both the shared
   (`webops-shared-prod`) and app project fetches.

#### Residual risk

Preview and production runs both read the same `deploy-configs` configs.
Those configs hold only `VERCEL_PROJECT_ID` (app) and `VERCEL_TOKEN` +
`VERCEL_ORG_ID` (shared) today; anything later added there is exported to
the runner by the same action.

### Migration from Vercel-managed env vars

1. Set up the Vercel integration in Doppler.
2. Create one integration per env:
   - `preview` → Vercel Preview
   - `prd` → Vercel Production
   Do not attach `deploy-configs` to any Vercel environment.
3. Import or re-enter live Vercel values. Sensitive Vercel values often
   import empty — re-enter them in Doppler after the import.
4. Diff Doppler against Vercel (keys and values) before treating the sync as
   authoritative.
5. Enable one integration at a time after verifying the target, key set, and
   values.
6. Put deploy credentials (`VERCEL_TOKEN`, `VERCEL_ORG_ID`,
   `VERCEL_PROJECT_ID`) in `deploy-configs` as described in Doppler setup —
   not in `prd` or `preview`.

Do the same steps for the preview environment if needed.

## Cloudflare Workers deploy

The workflow installs dependencies with bun (`--frozen-lockfile`, pinned in
`.github/workflows/cloudflare-deploy.yml`), fetches `CLOUDFLARE_API_TOKEN`
and `CLOUDFLARE_ACCOUNT_ID` as step outputs from the
`cloudflare-deploy-configs` config of the shared `webops-shared-prod`
project, fetches the worker's runtime secrets from `<project>` / `prd`,
pushes them with `wrangler secret bulk`, and runs `wrangler deploy`. No
`wranglerVersion` is passed, so wrangler-action uses the wrangler the app
repository installed — pin wrangler in the app's devDependencies and
lockfile. There is no per-app Doppler deploy config: the worker's identity
is its name in the app repository's `wrangler.toml`, so `project` only
names where the runtime secrets live.

Callers must ship a bun lockfile and a wrangler devDependency. A missing
lockfile fails an explicit `bun.lock`/`bun.lockb` check before the install —
`--frozen-lockfile` only rejects a lockfile that *would change*, so with none
present bun would resolve wrangler fresh from the registry; a stale lockfile
fails the frozen install itself; and a lockfile without a runnable wrangler
fails a `bun run wrangler --version` probe — the exact command
wrangler-action runs for a bun caller to decide whether to install its own —
which must exit 0 and print a parseable wrangler version. All three run
before Doppler is reached, so wrangler-action never installs an unpinned
wrangler in the step that holds the token. The
workflow is deploy-only — run smoke tests in a `needs: deploy` job in the
caller (see `examples/rpc-read-proxy/deploy.yml`).

Production only — the workers fleet has no preview environment. The only
supported trigger is a push to the caller repository's default branch;
`pull_request`, `workflow_dispatch`, `schedule`, and non-default-branch
pushes are rejected before Doppler authentication.

Unlike Vercel there is no remote build: wrangler bundles the worker on the
runner. Worker runtime secrets pass through the runner: before
`wrangler deploy` the workflow fetches `<project>` / `prd` as step outputs,
drops the Doppler meta keys (`DOPPLER_PROJECT`, `DOPPLER_CONFIG`,
`DOPPLER_ENVIRONMENT`), fails if no secret remains, and pipes the rest to
`bun run wrangler secret bulk` — Doppler's DIY Workers flow, with the OIDC
identity instead of a service token. On a first deploy wrangler creates a
draft worker. The sync is additive: a key removed from Doppler stays on the
worker until deleted by hand. See `specs/doppler-cloudflare.md` for the full
risk discussion.

A worker with no runtime secrets — every binding declared in its
`wrangler.toml` — sets `sync-secrets: false` and omits `project`. Both the
`<project>` / `prd` fetch and the push step are skipped, so no application
secret reaches the runner, and the deploy identity needs read on
`cloudflare-deploy-configs` only (see
`examples/uptime-kuma-status/deploy.yml`). With `sync-secrets` left at its
`true` default, a `prd` config that resolves empty still fails the deploy —
that check is what catches a wrong `project`.

The Cloudflare config is deliberately separate from the Vercel
`deploy-configs` so neither platform's deploy exports the other's
credentials onto its runner.

### Usage

```yaml
name: Deploy to Cloudflare Workers

on:
  push:
    branches: [main]

permissions:
  contents: read
  id-token: write

cache-mode: none

jobs:
  deploy:
    uses: yearn/yearn-gha/.github/workflows/cloudflare-deploy.yml@<approved-sha> # pin to the approved full commit SHA
    with:
      project: my-worker
      identity-id: ${{ vars.DOPPLER_PRODUCTION_IDENTITY_ID }}
```

Deploys are serialized by the reusable workflow itself (one job-level
concurrency group per caller repository, without cancellation), so callers
need no concurrency block.

Callers grant only `contents: read` and `id-token: write` — wrangler-action
posts no PR comments and creates no GitHub deployments. Configure
`DOPPLER_PRODUCTION_IDENTITY_ID` as an Actions repository variable in each
caller (a repository deploys to one platform, so the name does not collide
with Vercel callers).

### Inputs

| Name          | Required | Default | Description                                                          |
| ------------- | -------- | ------- | -------------------------------------------------------------------- |
| `project`     | unless `sync-secrets` is false | — | Doppler project holding the worker's runtime secrets in `prd`; every value there is pushed to the worker. |
| `sync-secrets` | no      | `true`  | Whether the worker has runtime secrets to push. `false` skips both the `<project>` / `prd` fetch and the push step. |
| `identity-id` | yes      | —       | Production Doppler service-account identity; loads the shared `cloudflare-deploy-configs`, and `<project>` / `prd` unless `sync-secrets` is false. |

### Outputs

| Name             | Description                                              |
| ---------------- | -------------------------------------------------------- |
| `deployment-url` | Production URL from wrangler. Empty for a worker with no `workers.dev` subdomain or route; the first target only for a multi-route worker. |

### Doppler setup

1. In `webops-shared-prod`, create the `cloudflare-deploy-configs` config
   with `CLOUDFLARE_API_TOKEN` (visibility Masked; Cloudflare permissions:
   Workers Scripts Edit) and `CLOUDFLARE_ACCOUNT_ID`. Keep ONLY those two
   values there — every value in the config is fetched onto the runner as a
   step output.
2. Keep each worker's runtime secrets in `<project>` / `prd` (visibility
   Masked). Keep ONLY runtime secrets there — every value is pushed to the
   worker. The production identity gets read access to it. A worker with no
   runtime secrets skips this step: its caller sets `sync-secrets: false`.
   Do not create an empty `prd` config for it.
3. Create one production identity per repository, as in the Vercel
   production setup: subject `repo:<org>/<repo>:ref:refs/heads/<default>`,
   claims `event_name: push`, `ref: refs/heads/<default>`, and
   `job_workflow_ref: yearn/yearn-gha/.github/workflows/cloudflare-deploy.yml@<approved-sha>`,
   with read access to `webops-shared-prod` / `cloudflare-deploy-configs`
   and `<project>` / `prd` (the first only for a `sync-secrets: false` caller).
4. Pass the identity ID as `identity-id` and the Doppler project as
   `project` in the caller, or `sync-secrets: false` and no `project` for a
   worker with no runtime secrets.

# Claude code review

Reusable GitHub workflow (`.github/workflows/claude-code-review.yml`) that runs
an automated Claude review on pull requests with
[`anthropics/claude-code-action`](https://github.com/anthropics/claude-code-action).
It is callable only through `workflow_call` — the caller supplies the trigger —
and authenticates with a Claude Code OAuth token resolved from Doppler via
OIDC. No static secret lives in the caller.

Reviews are on demand. A collaborator comments `/review` or
`/review-workflow` on a pull request, and the caller workflow dispatches
the reusable workflow. The first token selects the skill:
`/review` runs `review-pr` (single pass; better for small diffs);
`/review-workflow` runs `review-pr-lean` (fan-out; its prep runs on the
runner, outside the sandbox). The workflow
checks out the PR head, installs `review-pr`, `review-pr-lean`, and
`npm-policy` from `yearn/webops-skills` at a pinned SHA, and reads the
review from the action's result text. A follow-up step posts that body
with `gh pr comment`. The action prompt is only the invocation plus CI
constraints; it is not an inlined rubric. The tool allowlist grants no
Write/Edit, no `WebFetch`, and no comment tools.

The built-in Bash sandbox (enabled in `settings`) is the only boundary on
Bash. It confines every Bash command and child process: it denies writes to
`.git/config` and `.git/hooks`, strips `GITHUB_TOKEN`/`GH_TOKEN` from
subprocesses, masks the workflow token in the checkout's `.git/config`,
denies reads of the runner file-command directory, and blocks all network
access. It also auto-approves commands, so the `Bash(...)` entries in
`--allowedTools` describe intent, not an enforced boundary — observed runs
run `cat`, which is not allowlisted.

Because the sandbox has no network and no token, a step before the action
prefetches what the skills would otherwise fetch with `gh` into
`.pr-context/` in the workspace: `pr.json`, `issues.json`, `pr.diff`,
`diffstat.txt`, `changed-files.txt`, `blame-commits.txt`, and `audit.json`
(`bun audit` or `npm audit`, only when a manifest or lockfile changed). The prompt
points the skill at that directory and sets `run-checks=false`, since
dependencies are not installed.

Claude's own `Read`/`Grep`/`Glob` are not sandboxed. `settings` deny rules
keep them out of `/proc`, `/sys`, the runner file-command directory, and
`.config`. The pinned action writes an authenticated remote URL into
`.git/config` before Claude starts. Relative and absolute Read deny rules now
protect that file from Read; Grep/Glob coverage was not exercised by the recorded
runtime probe. Bash retains its separate credential-mask configuration, but
successful Bash masking also remains unverified by that probe.
Before posting, the workflow requires exactly one complete `sentinel` or
`unreadable` diagnostic and rejects raw or base64-encoded credential values.
The diagnostic is model-reported and cannot prove isolation or prevent a token
from reaching a model request. File-reader access is not assumed to inherit
the Bash sandbox's substitution.

Anything other than a `/review` or `/review-workflow` comment on a pull
request fails before the action runs. Because `issue_comment` runs with
repository secrets no matter who comments, only commenters with `write`,
`maintain`, or `admin` permission are accepted, and fork pull requests
are rejected.

Full operating guide: `specs/claude-code-review.md`.

## Usage

Copy `examples/claude-code-review/review.yml` — the canonical caller and the
single source of truth for the trigger `if`, concurrency, and permissions —
and replace `@<approved-sha>` with the approved full commit SHA. The snippet
is not duplicated here on purpose: an embedded copy drifts.

Caller workflows must grant `id-token: write` (OIDC login to Doppler) and
`pull-requests: write` (posting review comments), in addition to
`contents: read`. Reusable workflows cannot elevate beyond the caller's token
permissions; the workflow passes its own `github.token` to the action so
GitHub operations stay within these permissions.

## Doppler setup

The Claude Code OAuth token is stored once, as `CLAUDE_CODE_OAUTH_TOKEN` in
the `claude-review` config of the shared `webops-shared-prod` project. The
workflow authenticates to Doppler as a service-account identity with the
GitHub Actions OIDC token and fetches it at run time; no caller repository
holds an Actions secret.

Keep ONLY that token in `claude-review` — the fetch action exports every
secret in the config onto the runner. That is also why the review does not
reuse `deploy-configs`: sharing it would put `VERCEL_TOKEN` on review runners
and the OAuth token on deploy runners. Set the token's visibility to Masked;
the fetch action registers GitHub log redaction only for values that are not
Unmasked.

Create the identity with OIDC (discovery/issuer URL
`https://token.actions.githubusercontent.com`), trust the caller repositories
in the org, and grant it read access to `webops-shared-prod` /
`claude-review` only. Confirm `DOPPLER_IDENTITY_ID` in the reusable workflow
matches that identity. The identity is org-trusted: any workflow in a trusted
repo that grants `id-token: write` can fetch the token, not only this
reusable workflow. The `/review` / `/review-workflow` and fork gates bound this workflow only.

To rotate, run `claude setup-token` again (requires a Claude subscription) and
update that one Doppler secret; every caller picks up the new value on its
next run. The workflow fails fast if the token resolves empty.

There are no inputs; the prompt, tool allowlist, and gates live only in the
reusable workflow. The caller supplies the `issue_comment` trigger, permissions,
and SHA pin. The reusable workflow checks the event, the `/review` or
`/review-workflow` command, the commenter's access, and the PR origin,
and fails closed.

See `examples/` for the current Katana APR, yvUSD APR, fapy-hook (Vercel),
rpc-read-proxy (Cloudflare Workers) and claude-code-review shapes.
See `specs/doppler-vercel.md`, `specs/doppler-cloudflare.md` and
`specs/claude-code-review.md` for the full operating guides.

# PR checks

Reusable GitHub workflow that runs lint, format, typecheck and test on pull
requests. It handles no secrets and requests no OIDC token.

The workflow installs with bun, caching bun's global module store between runs, and reads the caller repository's
`package.json` to decide which checks exist. A check whose script is absent
reports "skipped" instead of failing the job, so a repository that defines
only `lint` still gets a green run. Adding a `typecheck` script is what turns
the typecheck step on.

Because the job executes pull-request code, it runs with `contents: read`
only, checks out with `persist-credentials: false`, and pins both the actions
and the bun version. Never pass secrets to it.

There is no `build` check: the Vercel deploy workflow above already builds every pull
request on Vercel, and `next build` typechecks.

Full design: `specs/pr-checks.md`.

## Usage

```yaml
name: PR checks

on: pull_request

concurrency:
  group: pr-checks-${{ github.ref }}
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  checks:
    uses: yearn/yearn-gha/.github/workflows/pr-checks.yml@<approved-sha> # pin to the approved full commit SHA
```

## Inputs

| Name | Required | Default | Description |
| ---- | -------- | ------- | ----------- |
| `bun-version` | no | `1.4.4` | Bun release installed by `oven-sh/setup-bun`. |

No outputs. No secrets.

## Checks

| Step | Script names | Behaviour when absent |
| ---- | ------------ | --------------------- |
| Lint | `lint` | skipped |
| Format | `format:check` | skipped |
| Typecheck | `typecheck`, `type-check` | skipped |
| Test | `test` | skipped |

There is no `format` fallback. A conventional `format` script rewrites files
and exits 0 whatever the input looked like, so the check could only fail on a
formatter crash.
