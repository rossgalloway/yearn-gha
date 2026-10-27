# Claude Code Review Operating Guide

- **Date:** 2026-08-15
- **Status:** Implemented in `yearn/yearn-gha`. Complete the rollout runbook before enabling a caller repository.
- **Context:** This guide matches the reusable workflow in `.github/workflows/claude-code-review.yml`. It follows the same hardening posture as `specs/doppler-vercel.md`: SHA pins, event gates, fail-fast validation, minimal caller surface.

## Decision

GitHub Actions runs an on-demand Claude review on pull requests through the reusable workflow. A collaborator comments `/review` or `/review-workflow` on a PR; the caller workflow (triggered by `issue_comment`) dispatches the reusable workflow. The first token of the comment selects the skill: `/review` invokes `review-pr` (single pass; better for small diffs), `/review-workflow` invokes `review-pr-lean` (fan-out). The workflow wraps [`anthropics/claude-code-action`](https://github.com/anthropics/claude-code-action) and is callable only through `workflow_call` — the caller supplies the trigger, the reusable workflow supplies the implementation, pins, and guards.

Authentication uses a **Claude Code OAuth token** (`claude setup-token`), not an Anthropic API key. The token is stored once in Doppler — `webops-shared-prod`, config `claude-review` — and fetched at run time: the workflow authenticates to Doppler as a service-account identity with the GitHub Actions OIDC token. The fetch runs with `inject-env-vars: false`, so the credential never enters the job env; one step reads it from the fetch step output, strips CR/LF and surrounding quotes, masks it, and re-emits it as a masked step output that feeds the action's `claude_code_oauth_token` and the posting step's leak check. Callers pass no secret at all; they only grant `id-token: write`. The workflow fails fast when the token resolves empty, so a misconfigured identity or config stops the run before the action executes.

The token gets its own config because the fetch action exports every secret in a config onto the runner: sharing `deploy-configs` would put `VERCEL_TOKEN` on review runners and the OAuth token on deploy runners.

The guide distinguishes two kinds of data:

- **Review credential:** `CLAUDE_CODE_OAUTH_TOKEN`. A long-lived credential tied to a Claude subscription. It reaches the runner and the action.
- **Review output:** PR comments. Posted with the workflow-scoped `GITHUB_TOKEN`, passed explicitly to the action as `github_token`; no extra GitHub credential is stored. Without the explicit pass the action falls back to a token from its own Claude GitHub App, whose permissions are independent of the job's `permissions` block.

## Implementation status (yearn-gha)

| Control | Status |
| ------- | ------ |
| `workflow_call` only — no direct trigger in the reusable workflow | Done |
| Reject anything that is not a `/review` or `/review-workflow` comment on a PR before the action runs | Done |
| Reject commenters without `write`, `maintain`, or `admin` permission (effective permission via the collaborators API) | Done |
| Reject fork PRs (head repo resolved via API) before the action runs | Done |
| Fetch `CLAUDE_CODE_OAUTH_TOKEN` from Doppler over OIDC (SHA-pinned `dopplerhq/secrets-fetch-action` `v2.0.0` / `451892f…`), after the event and fork gates | Done |
| Validate `CLAUDE_CODE_OAUTH_TOKEN` non-empty after the Doppler fetch and before the action runs | Done |
| `id-token: write` requested for the Doppler login only; no static credential in any caller | Done |
| SHA-pinned actions (`actions/checkout`, `anthropics/claude-code-action` `v1.0.193` / `9d7150b…`) | Done |
| Install `review-pr-lean` (+ `review-pr`, `npm-policy`) from `yearn/webops-skills` pinned by `WEBOPS_SKILLS_SHA` | Done |
| Action `prompt` invokes pinned `review-pr` (`/review`) or `review-pr-lean` (`/review-workflow`), not an inlined review rubric | Done |
| `--allowedTools` is read + Skill/Task/Agent/Workflow/TaskOutput/TaskStop; no comment/write tools. The `Bash(...)` entries (read-only `gh`, named git verbs, lint scripts) are declarative only — the sandbox auto-approves Bash, so they gate nothing | Done |
| `settings` enables the built-in Bash sandbox (no unsandboxed commands, `GITHUB_TOKEN`/`GH_TOKEN` denied to subprocesses, empty network allowlist) and denies `Read`/`Grep`/`Glob` identically on `/proc`, `/sys`, `/home/runner/work/_temp`, and `/home/runner/.config` — the sandbox is the sole boundary for Bash and subprocesses, the deny list for Claude's file tools. The checkout's `.git` is **not** denied to the file tools (removed in `8d7d2f3`); see residual risk below | Done |
| Prompt states there is no network or browser: skip visual verification, report npm-policy lookups as not verifiable in CI | Done |
| A runner step prefetches PR metadata, linked issues, diff, diffstat, changed files, PR commit list, and `bun audit` / `npm audit` (when a manifest or lockfile changed) into `.pr-context/`; the prompt names the directory, sets `run-checks=false`. For `/review-workflow`, a runner step runs `review-pr-lean` prep (`review.mjs prep --no-checkout`) with `GH_TOKEN` and a one-shot git auth header in env, and writes `run.js` and `prep.txt` to `.pr-context/lean/`. Sandbox network stays closed | Done |
| Review body from the action result text; a follow-up step posts `gh pr comment` | Done |
| Acknowledgement comment (`Review started (<skill>): <run url>`) after the token validates and before the action runs | Done |
| Result envelope (`subtype`, `is_error`, `errors`, `stop_reason`, cost, turns — never `result`: log masking is verbatim-only, so an encoded token in the review text would print) echoed to the job log | Done |
| Failure comment (`Review failed: <run url>`) when any step after the started comment fails — including before the action runs — when the action produced no result text, when the execution file cannot be parsed, or when the body contains credential material; cancelled (superseded) runs stay silent | Done |
| PR checkout `persist-credentials: false`; one-shot extraheader fetch of `PR_BASE_REF` | Done |
| Explicit `github_token: ${{ github.token }}` so the action cannot fall back to its Claude App token | Done |
| Caller examples pin reusable workflow to full commit SHA | Done (examples use the `@<approved-sha>` placeholder; replace at rollout) |
| Per-repo prompt/model/turn customization | **Not built.** No inputs; see rejected alternatives. |

Remaining work is **operational**: create the Doppler identity and the `claude-review` config, store the token there, confirm `DOPPLER_IDENTITY_ID` in the workflow matches that identity, pin the approved SHA in callers, branch protection on `yearn/yearn-gha`.

## What this design protects—and what it does not

Controls provided:

- One reviewed implementation of event gates, pins, skill invocation, and tool allowlist. There are no inputs, so callers cannot widen the action's tool access at all. `--allowedTools` covers Read/Grep/Glob/Skill/Task/Agent/Workflow/TaskOutput/TaskStop — not Write/Edit, not `WebFetch`, and not `gh pr comment`. `WebFetch` is excluded on purpose: with unscoped `Read` in the allowlist, network egress turns any file the runner writes into an exfiltration path (the sandbox's empty network allowlist is the enforcing control; the omission is defence in depth). The allowlist's `Bash(...)` entries (read-only `gh` prefixes, named git verbs, lint-only package-manager invocations) are **not** a boundary: the sandbox auto-approves Bash commands, and observed runs execute `cat`, which is not allowlisted. Treat the sandbox as the whole of the Bash boundary. The comment is posted by a later step from the action result text.
- Anything that is not a `/review` or `/review-workflow` PR comment from a commenter with `write`, `maintain`, or `admin` permission on a same-repo PR fails before the action executes, so the token is never exercised outside the intended context. Unlike `pull_request`, `issue_comment` runs with repository secrets regardless of who comments — the write-permission and fork guards are what stand between a drive-by commenter and the token.
- Full-SHA pins on both the action and the reusable workflow prevent a moved tag from silently changing executed code.
- The workflow requests only `contents: read`, `pull-requests: write`, and `id-token: write`, and passes its own `github.token` to the action, so GitHub operations are bounded by those permissions. `id-token: write` serves the Doppler login alone; the pinned action's own OIDC federation path stays unused.
- No caller repository stores the review credential. The token exists in one Doppler config, read by one identity, and reaches a runner only for the duration of a run.

Risks that remain:

- `CLAUDE_CODE_OAUTH_TOKEN` is a long-lived credential on the runner for the duration of the run. The Doppler fetch is short-lived, the token it returns is not; there is no OIDC equivalent for the Claude credential itself today. Storage and rotation are now centralized (one Doppler secret, `claude setup-token` again). **Accepted risk — state it plainly:** a compromised run can read the token.
- The Doppler identity trusts every caller repository in the org, so any workflow in any org repository can authenticate as it and fetch the token directly — not only this reusable workflow. **Accepted risk — state it plainly:** the workflow's gates bound what this workflow does with the token, not who else in the org can read it.
- Claude reads the PR and runs named git verbs on the checked-out PR head. Project lint is allowlisted but usually cannot run: the workflow deliberately does not install dependencies (an install would execute PR-controlled lifecycle scripts on a runner holding the token), so lint executes only where a caller repository already has its dependencies on disk, and the review reports it as not verifiable in CI otherwise. Where it does run, those lint scripts are PR-controlled. The token is not in the job env (`inject-env-vars: false`) and never touches `$GITHUB_ENV`; the validate step re-emits the cleaned value as a masked step output, which feeds only the action input and the posting step's leak check. Two controls split the coverage. The built-in Bash sandbox (`sandbox.enabled` in `settings`, `allowUnsandboxedCommands: false`) confines every Bash command and child process: it denies writes to `.git/config` and `.git/hooks` in the working directory with no opt-out (closing the `git log --output=` write primitive), strips `GITHUB_TOKEN`/`GH_TOKEN` from subprocess environments, denies reads of `/home/runner/work/_temp`, masks the workflow token in the checkout's `.git/config` (so git still loads it), and blocks all network access (empty allowlist — the allowlisted `gh` verbs fail loudly rather than reach `api.github.com`). Claude's own `Read`/`Grep`/`Glob` are not sandboxed, so the `settings` deny rules keep them out of `/proc`, `/sys`, `/home/runner/work/_temp`, and `/home/runner/.config`. The checkout's `.git` is no longer denied (the three `.git` deny rules were removed in `8d7d2f3`), and the pinned action rewrites the origin URL to embed the workflow token in `.git/config` before Claude starts: the sandbox masks that value for Bash, but an unsandboxed `Read` of `.git/config` sees the real token. **Residual risk — state it plainly:** the posting step's verbatim/base64 credential check is the only control between that read and a posted comment. The sandbox needs `bubblewrap` and `socat`, both installed by the workflow before the Claude step, which also clears `kernel.apparmor_restrict_unprivileged_userns` (ubuntu-24.04 blocks the user namespace `bwrap` needs — clearing it weakens host-level userns isolation runner-wide for the rest of the job, a deliberate trade to enable subprocess isolation). A malicious lint script still runs, and a malicious same-repo PR can still prompt-inject. **Residual risk — state it plainly.** Push is blocked by `contents: read` (checkout also uses `persist-credentials: false`). It cannot merge.
- The commenter gate is an effective-permission check, so everyone with repository write access can spend review tokens at will; there is no rate limit beyond per-PR concurrency cancellation in the caller.
- Review comments are advisory. The workflow is not a required check and must not gate merges; Claude review does not replace human review.

## Target architecture

- A small caller workflow in each repository triggers on `issue_comment` (`types: [created]`) and invokes the SHA-pinned reusable workflow.
- The reusable workflow owns the gates (fail closed), writes `PR_BASE_REF` from the same PR API call, checks out the PR head (`refs/pull/<n>/head`, `fetch-depth: 0`, `persist-credentials: false`), fetches the base with a one-shot `http.extraheader` basic `x-access-token`, installs `review-pr-lean` / `review-pr` / `npm-policy` from `yearn/webops-skills` at `WEBOPS_SKILLS_SHA`, then fetches the OAuth token from Doppler over OIDC and validates it non-empty.
- The first token of the comment selects the skill: `/review` → `review-pr`, `/review-workflow` → `review-pr-lean`. Any other first token fails before Doppler. The action `prompt` is built after the base is fetched, and names an immutable merge-base SHA (`Diff against <sha>.`) rather than `origin/<base>` — the skills otherwise default to `origin/HEAD`, which `actions/checkout` never creates, and the pinned action shallow-fetches its own resolved base branch before Claude runs, which can strip the merge base out of a symbolic ref mid-run. The prompt also states that there is no network and no browser, so visual verification is skipped and npm-policy lookups are reported as not verifiable in CI. The prompt tells Claude to review the PR URL with the selected skill (plain text, not a `/` slash command — those are consumed by the action and never reach the model). `/review-workflow` is told that prep already ran, to launch the `Workflow` tool on `.pr-context/lean/run.js` with the tier from `prep.txt`, and then to block on `TaskOutput` until the run finishes — the tool returns a task id and runs in the background, and CI gets no completion notification, so without the poll the model ends the turn with no review. Provenance runs `review.mjs provenance` against the merge-base SHA (the one `node` command in `--allowedTools`); `/review` is told not to start a workflow. Skills are copied into `~/.claude/skills` and loaded with `--setting-sources user` so the action's untrusted-PR `.claude` snapshot cannot replace them. Claude does not post. `--json-schema` is not used (it fails the action when the run errors, and skills often skip `structured_output`), so the review text comes only from the last non-error `result` record in the execution file. The next step posts it with `gh pr comment` — after checking the body against the OAuth and workflow tokens, verbatim and base64-encoded; other encodings pass this last-line check — or comments a run link when there is no usable review. The PR head is already checked out.
- There is no per-caller customization; the skill pin, prompt, and tool allowlist live only in the reusable workflow. Bump the skill by changing `WEBOPS_SKILLS_SHA` in a reviewed PR, same as other pins.

Rejected alternatives:

- **`anthropic_api_key` auth.** The fleet standard is subscription OAuth tokens; no API-key billing per repo. The action supports both; the workflow deliberately exposes only the OAuth path to keep one rotation story.
- **Automatic review on every push (`pull_request` trigger).** Reviews every commit whether wanted or not; token spend scales with push volume, and most runs are cancelled or ignored. The `/review` or `/review-workflow` comment makes each spend a deliberate human action.
- **Always invoke the fan-out skill.** The fan-out is many times more tokens than `review-pr`. Small diffs do not need five lenses and a verify pass. The commenter picks: `/review` for the single-pass skill, `/review-workflow` when the fan-out is worth it. Rejected auto-tier-inside-one-command — that still starts the expensive skill to decide it should not have.
- **Interactive `@claude` mention mode.** The action's conversational mode needs a broader permission surface than a single fire-and-forget review. Out of scope; explicitly future — the trigger plumbing (`issue_comment`, commenter gate) now exists, so it is cheap to add behind a reviewed change.
- **Direct `issue_comment` trigger inside the reusable workflow.** Would make this repo review its own PRs only. `workflow_call` keeps one implementation for many callers, matching `vercel-deploy.yml`.
- **Per-caller Actions secret (`CLAUDE_CODE_OAUTH_TOKEN`).** The pre-pivot design: each caller stored the token and passed it explicitly or through `secrets: inherit`. It puts the same long-lived credential in N repositories, and rotation means N manual updates that drift. Doppler holds one entry and one identity; callers hold nothing.
- **Per-caller `prompt` / `claude-args` inputs.** Existed in an earlier draft; removed. Every input is surface a caller can get wrong (a `claude-args` override that drops `--allowedTools` silently posts nothing; a copied allowlist drifts from the central one). Repos that need a different review skill or pin can propose it here, keeping one reviewed implementation. Explicitly future if a real need appears — the input plumbing is a small reviewed diff.
- **`review-pr-workflow` for `/review-workflow`.** Replaced by `review-pr-lean`: same lenses and verification, fewer agents and turns. The command name stays, so callers need no change.
- **`review-pr-lean` prep inside the sandbox.** Prep calls `gh` and fetches the base; the sandbox has no network and no token. The runner runs it instead.
- **Inlined quality/bugs/security/performance prompt.** Replaced by the pinned Yearn `review-pr` and `review-pr-lean` skills so the rubric lives in `yearn/webops-skills`, not this workflow. The action prompt is only the invocation plus non-interactive CI constraints.

### Caller shape

The canonical caller is `examples/claude-code-review/review.yml` — the single
source of truth for the trigger `if`, concurrency, and permissions. It is not
duplicated here on purpose: an embedded copy drifts.

`concurrency` sits on the job, behind the trigger filter. Workflow-level concurrency is claimed when the run is queued — before the reusable workflow's gates run — so without the filter any comment on the PR would cancel a review in flight. The `if` matches the two accepted commands exactly (plus their trailing-text forms), so a near-miss comment such as `/reviews` neither starts a run nor cancels one; it is a trigger filter, not a security gate — all four gates stay in the reusable workflow and still fail closed. `concurrency` groups by PR number (`issue_comment` runs on the default branch ref, so `github.ref` cannot tell PRs apart); cancellation makes a newer `/review` or `/review-workflow` supersede an in-flight review of the same PR.

### Workflow inputs and secrets

No inputs. No secrets — the workflow resolves its credential from Doppler. The caller must grant `id-token: write`; a reusable workflow cannot exceed the caller's token permissions, so without it the Doppler login fails.

No outputs.

## GitHub controls

- Pin `anthropics/claude-code-action`, `actions/checkout`, `dopplerhq/secrets-fetch-action`, and `WEBOPS_SKILLS_SHA` to full commit SHAs; version comments are informational only. Upgrade through reviewed changes.
- Callers pin the reusable workflow to a full commit SHA. Update in a reviewed rollout.
- Protect the default branch of `yearn/yearn-gha`; this repo is shared CI infrastructure. CODEOWNERS on `.github/workflows/**`.
- Store `CLAUDE_CODE_OAUTH_TOKEN` only in Doppler (`webops-shared-prod` / `claude-review`), with Masked visibility so the fetch action registers GitHub log redaction. Do not commit it, and do not add it as an Actions secret or variable in any caller.
- Keep the `claude-review` config to that one secret; the fetch action exports the whole config onto the runner.
- Do not use `pull_request_target`. Do not mark the review job as a required status check.
- Grant callers only `contents: read`, `pull-requests: write`, and `id-token: write`.

## Rollout runbook

1. Generate the token: `claude setup-token` (requires a Claude subscription). Create the `claude-review` config in `webops-shared-prod` and store the token there as `CLAUDE_CODE_OAUTH_TOKEN`, visibility Masked.
2. Create the Doppler service-account identity with OIDC (discovery/issuer URL `https://token.actions.githubusercontent.com`), trust the caller repositories, grant it read on `webops-shared-prod` / `claude-review` only, and confirm `DOPPLER_IDENTITY_ID` in the workflow matches that identity. Binding `job_workflow_ref` to this reusable workflow is optional; without it any org workflow with `id-token: write` can fetch the token (accepted risk above).
3. Merge the reusable workflow; record the approved full commit SHA.
4. Add the caller workflow (see `examples/claude-code-review/`), pinned to that SHA, granting `id-token: write`.
5. Open a test PR and comment `/review`; confirm it invokes `review-pr` and posts a top-level comment. Repeat with `/review-workflow` and confirm it invokes `review-pr-lean`. Confirm a second command on the same PR cancels the in-flight run, and that either command from a non-collaborator account fails at the gate.
6. When the token owner leaves or the token leaks, regenerate with `claude setup-token` and update the one Doppler secret; old tokens are revoked from the Claude account settings.

## References

- [claude-code-action](https://github.com/anthropics/claude-code-action)
- [claude-code-action usage docs](https://github.com/anthropics/claude-code-action/blob/main/docs/usage.md)
- [claude-code-action solutions (automated review example)](https://github.com/anthropics/claude-code-action/blob/main/docs/solutions.md)
- [Reusing workflows (secrets and permissions)](https://docs.github.com/en/actions/using-workflows/reusing-workflows)
- Skills: `yearn/webops-skills` — `review-pr-lean`, `review-pr`, `npm-policy` (pin `WEBOPS_SKILLS_SHA`)
- Repo implementation: `yearn/yearn-gha` — `.github/workflows/claude-code-review.yml`
- Caller examples: `examples/claude-code-review/`

## Review output contract

Both `/review` (`review-pr`) and `/review-workflow` (`review-pr-lean`) are prompted
to return a summary, issues/findings, a review-method section and an unambiguous
`APPROVE`, `REQUEST_CHANGES` or `COMMENT` verdict. Validation accepts the Verdict
section and standalone bold/plain Verdict lines; it does not impose one Markdown
placement. Quoted or fenced examples are excluded from verdict extraction.
Missing/empty required sections, unsupported/conflicting verdicts and a producer's
own publication-status line block posting and fail the workflow. Failure comments
contain a controlled reason and run URL, not the rejected report body.

The original validated report is preserved. The workflow appends its own
publication attribution and run URL; the producer is told to leave posting status
out. This avoids a posted report claiming it was not posted. The footer appears
in GitHub only if the comment write succeeds. Existing raw/base64 credential
checks remain in front of the write. Credential diagnostics are separately
hardened by the reader-guard change tracked in yearn-metrics #49.

Run `python3 -m unittest discover -s tests -p 'test_review_report.py' -v`. These
offline fixtures execute the actual posting shell block for both command paths,
cover both supported verdict formats, malformed/ambiguous output, producer
publication claims and the final posted body. `tests/review_fixture.py` is shared
byte-for-byte with the credential-guard PR so both changes can be tested
independently and combined without duplicate fixture implementations.
