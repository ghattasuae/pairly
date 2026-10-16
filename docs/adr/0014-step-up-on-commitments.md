# ADR 0014: A second factor for every approve, decide, manage, export, lock, publish and verify

Status: accepted

Date: 2026-10-07

## Context

Brief 6.1: TOTP is optional for staff and required for approvers and admins. Checking role names would miss custom roles and new workflows, and a stolen session or approval link must not be enough to commit the organisation.

## Decision

* The rule is keyed on the permission's action, not on roles (`pairly_core.rbac.STEP_UP_ACTIONS`). A person may use a permission whose action is `approve`, `decide`, `manage` or `export` only when the sign-in that minted the token included a TOTP code (`amr` contains `otp`).
* `lock` and `publish` were added in [ADR 0010](0010-plugin-prompts-and-commitment-step-up.md) (locking a rubric, publishing a job). `verify` was added in [ADR 0012](0012-verify-step-up.md) (recording the call back before a vendor's bank details change).
* A token without `amr`, or with a malformed one, fails closed.
* Agents and API keys can never hold these actions at all ([ADR 0002](0002-better-auth-jwks-api-rbac.md)).
* Signed approval links count as the bound person's decision only after a TOTP-backed sign-in ([ADR 0008](0008-workflow-runtime-approvals-signed-links.md)).

## Consequences

* Custom roles and future workflows inherit the rule with no code.
* The executor re-checks that every approving decision was made with a second factor before a final action runs.
* People who only read never need TOTP.
