# ADR 0006: Recorded model mode for demos, tests and evals

Status: accepted

Date: 2026-10-07

## Context

Pairly uses Anthropic and OpenAI models only, no local models. `make demo` must still work on a fresh clone with no API key, CI must not call a paid model on every pull request, and replay (brief 2.3.4) needs stored responses anyway.

## Decision

* The model router has three modes (`PAIRLY_MODEL_MODE`): `live` calls providers and stores every response, `record` does the same and marks the rows for export, and `recorded` (the default) answers only from stored responses, keyed on the hash of the exact request (prompt file, model, inputs, settings). A missing recording raises `RecordingMissing`, which fails closed like any model outage.
* A recorded response is validated exactly like a live one, so recorded runs take the same routing decisions.
* Recordings travel as bundles (`pairly_core.llm.bundle`). Import recomputes every request hash and refuses the whole bundle if one does not match. Imported rows cost 0.
* Seed data ships bundles: synthetic inputs with scripted answers. Tests check that the committed bundles still answer every request the code makes today.

## Consequences

* Demos, tests and the offline evals need no key and spend nothing.
* Scripted answers are not model output. Evals that need a model are reported as pending until a live run measures them (see [evals.md](../evals.md)).
* Changing a prompt, a schema or a parser changes request hashes: the recordings must be rebuilt, and a test says so.
