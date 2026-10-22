# Reusable PR-check workflow (lint / format / typecheck / test)

- **Date:** 2026-08-20
- **Status:** Implemented and committed in `.github/workflows/pr-checks.yml`. Consumer pins (`README.md`,
  `examples/pr-checks.yml`, and each caller) are still `<approved-sha>` placeholders.
- **Context:** Companion to `specs/doppler-vercel.md`. That workflow deploys; this one gates the pull request.
  It handles no secrets and never authenticates to Doppler.

## Decision

One reusable workflow runs the four standard checks for every web app in the fleet. It installs with bun and
reads `package.json` to decide which checks exist. A check that the repository does not define reports
"skipped" instead of failing the job.

Consumers add a small caller workflow pinned to a full commit SHA, the same convention as `vercel-deploy.yml`.

Two properties define the design:

- **Discovery over configuration.** Callers pass no script names and no matrix. Adding a `typecheck` script to
  a repository is what turns the typecheck step on.
- **One package manager.** The fleet standardized on bun, so the workflow runs `bun install --frozen-lockfile`
  and `bun run <script>`. A repository without `bun.lock` fails at install, which is the intended signal.
- **Absent is not failing.** Missing scripts are normal in this fleet. The dummy repository defines only `lint`.

## Non-goals

- **No `build` check.** The Vercel deployment workflow builds every pull request already, and for Next.js
  `next build` typechecks. Adding a build step here duplicates a slower job.
- **No secrets, no OIDC, no `id-token: write`.** This workflow runs untrusted pull-request code, so it must not
  hold credentials. Anything needing a credential belongs in the deploy workflow.
- **No fork gate.** Fork pull requests are allowed to run checks; the workflow has nothing worth stealing.
- **No package-manager detection.** An earlier draft picked bun, pnpm, yarn or npm from the lockfile. It carried
  a corepack step, a conditional `setup-node` cache and four install branches to serve repositories that do not
  exist. Adding npm or pnpm support later is a small change, and it should wait for a repository that needs it.

## Workflow shape

`.github/workflows/pr-checks.yml`, `on: workflow_call`, one job `checks` on `ubuntu-latest`,
`timeout-minutes: 15`, `permissions: contents: read`.

| Input | Required | Default | Description |
| ----- | -------- | ------- | ----------- |
| `bun-version` | no | `'1.4.4'` | Bun release installed by `oven-sh/setup-bun`. Pinned, not `latest`. |

No outputs. No secrets.

### Steps

1. **Checkout** — `actions/checkout` pinned to a full SHA, with `persist-credentials: false`.
2. **Setup Bun** — `oven-sh/setup-bun` at the pinned `bun-version`.
3. **Cache bun install** — `actions/cache` over `~/.bun/install/cache`, keyed on the lockfile hash.
4. **Install dependencies** — `bun install --frozen-lockfile`.
5. **Lint / Format / Typecheck / Test** — one step each, guarded by a `jq` script lookup.

The cache holds bun's global module store, not `node_modules`. `bun install` still runs and still resolves the
lockfile; the cache only spares it the downloads. `restore-keys` lets a changed lockfile start from the previous
run's store instead of an empty one.

### Script discovery

Each check step resolves the first script name that exists:

| Step | Script names | Notes |
| ---- | ------------ | ----- |
| Lint | `lint` | |
| Format | `format:check` | No `format` fallback — see below |
| Typecheck | `typecheck`, `type-check` | |
| Test | `test` | |

The guard is `jq -e --arg s "$script" '.scripts[$s] // empty | select(length > 0)' package.json`. The
`select(length > 0)` clause matters: plain `jq -e` treats `"lint": ""` as present and would run `bun run ""`.

Each check is a separate step so the pull-request UI shows per-check status rather than one opaque pass/fail.

> A check that cannot fail must not report success. A skipped step says "skipped" in its log; a step that runs
> a write-mode command and exits 0 lies.

### Why no `format` fallback

The first implementation tried `format:check` and fell back to `format`. A conventional `format` script
(`prettier --write .`) rewrites the working tree and exits 0 whatever the input looked like, so the Format
check could only ever fail on a formatter crash. Repositories without `format:check` now report skipped.

Rejected alternatives:

- **Run `format` and then `git diff --exit-code`.** Works, but makes the workflow's verdict depend on a script
  whose contract it cannot see. A repository that wants the check adds `format:check`.
- **Append `--check` to the `format` script.** Assumes prettier's flag spelling across every formatter.

## Security posture

This workflow executes untrusted pull-request code — dependency install scripts and `package.json` scripts run
with whatever the runner holds. The controls follow from that:

- `persist-credentials: false` keeps `GITHUB_TOKEN` out of `.git/config` on the runner.
- `permissions: contents: read` at the job, nothing else. No `id-token`, no write scopes.
- No secrets are passed by callers, so `secrets: inherit` must never appear in a caller.
- Third-party actions are pinned to full commit SHAs; `bun-version` defaults to an exact release rather than
  `latest`, so a Bun release cannot change what runs without a commit here or a deliberate caller override.

Residual risk: a malicious dependency still executes on the runner. The blast radius is a read-only checkout and
the runner's own network access. Do not add credentials, deployment permissions, or a cache shared with trusted
workflows to this job.

## Caller shape

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
    uses: yearn/yearn-gha/.github/workflows/pr-checks.yml@<approved-sha> # full commit SHA only
```

Concurrency lives in the caller so a new push supersedes an in-flight run for the same ref.

## Rollout

1. Commit `.github/workflows/pr-checks.yml` in `yearn/yearn-gha`.
2. Replace `<approved-sha>` in `README.md`, `examples/pr-checks.yml`, and each consumer's caller with that full SHA.
3. Open a pull request on `yearn/yearn-practice-dummy`: `lint` runs; format, typecheck, and test report skipped.
4. Add the check to branch protection once it has passed on a real pull request.

Current state: the reusable workflow is committed; the dummy caller exists locally and is untracked. Both still
read `@<approved-sha>`, which is not a resolvable ref — step 1 already landed, step 2 has not.

## Known limits

- **The dummy repository runs one check.** It defines only `lint`, so format, typecheck, and test are skipped on
  every pull request. A green PR-checks run there does not mean the code typechecks; the Vercel build covers
  that. Adding the scripts is what enables the checks.
- **Bun only.** A repository on npm, pnpm or yarn fails at `bun install --frozen-lockfile`. That is deliberate
  for a fleet that standardized on bun; add a package-manager branch when a real repository needs one.
- **The dependency cache is shared with pull-request code.** A pull request can poison `~/.bun/install/cache`
  for later runs on the same key. The job holds no credentials and produces no artifact, so the blast radius is
  a wrong module in a later check run. Do not share this cache key with a workflow that deploys or signs.

## References

- Repo implementation: `yearn/yearn-gha` — `.github/workflows/pr-checks.yml`, `examples/pr-checks.yml`
- Design notes: `docs/plans/pr-checks.md`
- Companion spec: `specs/doppler-vercel.md`
- Harness: `yearn/yearn-practice-dummy` — `.github/workflows/pr-checks.yml`
