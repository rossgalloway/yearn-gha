# Reusable PR-check workflow (test/lint/format/typecheck)

## Context

gha repo has one reusable workflow (`vercel-deploy.yml`, OIDC+Doppler, pinned actions).
Consumer repos (e.g. yearn-practice-dummy) call it by ref. Need a matching reusable PR-check
pipeline: run test/lint/format/typecheck only when the npm script exists.

The fleet is bun-only, so the workflow is bun-only. No package-manager detection.

Dummy repo: bun (`bun.lock`), Next 16, only `lint` script today — workflow must skip missing
scripts gracefully, not fail.

## Approach

### 1. New reusable workflow: `gha/.github/workflows/pr-checks.yml`

- `on: workflow_call`, input `bun-version` (default pinned), no secrets.
- Single job `checks`, `ubuntu-latest`, `timeout-minutes: 15`, `permissions: contents: read`.
- Steps:
  1. `actions/checkout@v7` (SHA-pinned, `persist-credentials: false`).
  2. `oven-sh/setup-bun` at the pinned `bun-version`.
  3. `actions/cache` over `~/.bun/install/cache` keyed on the lockfile hash, then
     `bun install --frozen-lockfile`.
  4. One step per check — `lint`, `format:check`, `typecheck` (fallback `type-check`), `test` —
     each guarded: `jq -e '.scripts["<name>"] // empty | select(length > 0)' package.json` →
     run `bun run <name>`, else log "skipped".
     Separate steps keep per-check status visible in the PR UI.
- Dummy repo has only a `lint` script, so `format`/`typecheck`/`test` report "skipped" on every PR.
  A green PR-checks run there does not mean the code typechecks; the Vercel build covers that.
- No `format` fallback: a write-mode formatter exits 0 after rewriting files, so it can never fail a PR.
- Skipped: `build` check — Vercel remote build (existing deploy workflow) already builds
  every PR; `next build` also covers typechecking for the dummy.

### 2. Example: `gha/examples/pr-checks.yml`

Caller snippet mirroring existing examples:
`on: pull_request` → `uses: yearn/yearn-gha/.github/workflows/pr-checks.yml@<sha>`.

### 3. Wire dummy repo: `yearn-practice-dummy/.github/workflows/pr-checks.yml`

Caller on `pull_request`, pinned to the new gha commit SHA (same convention as its
`vercel-deploy.yml` caller).

## Files

- `gha/.github/workflows/pr-checks.yml` (new)
- `gha/examples/pr-checks.yml` (new)
- `yearn-practice-dummy/.github/workflows/pr-checks.yml` (new)

## Verification

- `actionlint` on the new workflow files if available.
- Local dry-run of guard logic: `jq -e '.scripts["lint"]' package.json` in dummy repo.
- Real e2e: after commit/push (user-approved), open a PR on dummy → `lint` runs,
  test/format/typecheck show "skipped".
