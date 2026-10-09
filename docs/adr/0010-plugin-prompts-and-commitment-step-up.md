# ADR 0010: Plugin prompts, and a second factor for locking and publishing

Status: accepted

## Context

Pairly Hire (M2, WP2.2 and WP2.3) is the first workflow with its own model prompts and
the first with human commitments that are neither an approval nor a decision on a
case: locking a screening rubric, and publishing a job ad. Building it showed two places
where a workflow could not plug in without editing the engine for that workflow. Both
gaps are generic: Pairly Pay and any later workflow hit them too.

1. **Prompts.** The model router loads prompts from a `PromptRegistry` built in
   `pairly_core.llm.factory.build_router`, with the core prompt directory only. A
   workflow had no way to ship its own versioned prompts (`hire_jd_draft`,
   `hire_rubric_draft`) without building a second router with its own settings, which
   would bypass the process's budget, recording and tracing wiring.
2. **Step-up.** The second-factor rule (brief 6.1) is keyed on the permission's action:
   `approve`, `decide`, `manage`, `export`. Locking a rubric (what every candidate is
   later measured against) and publishing a job (visible to everyone) carry the same
   weight, but naming them `approve` would be misleading in the audit log and the
   permission list.

## Decision

1. **`prompt_roots`, an optional plugin attribute** (`pairly_core.plugins`), read with
   `getattr` like the other optional attributes: directories of
   `<task>/<version>.md` prompt files. `plugin_prompt_roots(plugins)` collects them,
   sorted by plugin id, and fails closed: a root that is not a directory, or a task
   directory not named `<plugin id>_...`, raises `PluginError`, so a plugin can never
   replace a core prompt or another plugin's. `build_router(...)` uses the discovered
   plugins' roots when the caller passes none, so the API, the worker and the decision
   replay services get the same prompts with no change in the apps. Callers that pass
   `prompt_roots` explicitly keep full control (tests).
2. **`lock` and `publish` join the step-up actions** (`pairly_core.rbac.STEP_UP_ACTIONS`).
   The rule stays keyed on the action, so custom roles and later workflows inherit it.
   No existing permission used either action, so nothing that worked before changes.
   As before, the agent role and API keys can never hold them (they are neither
   `read`, `propose` nor `upload`).

## Consequences

* A workflow ships prompts by declaring `prompt_roots = (Path(...) / "prompts",)`; the
  guide (`docs/add-a-workflow.md`) says so. Recorded responses for them are built the
  same way as for core prompts (`pairly_hire.recordings`).
* Discovering plugins in `build_router` reads the entry points once per router built
  (a few per process). Discovery already fails closed at startup, so an invalid
  plugin cannot reach this path silently.
* `hire.rubric.lock` and `hire.job.publish` need a sign-in with TOTP, like approvals.
  `GET /v1/me` lists them under `second_factor_permissions`, so the web app asks for
  the second factor before the action.
