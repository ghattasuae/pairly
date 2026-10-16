// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology

// Talking to the running stack from the specs: extra `make` arguments, the state the
// full-flow spec leaves for later specs, and the development seed for approvals. The
// native stack (`make dev`) is the default; PAIRLY_E2E_STACK=docker (or
// PAIRLY_E2E_COMPOSE_FILE) runs the same make targets against the compose stack.
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { AUTH_STATE_DIR } from "./report";

const here = path.dirname(fileURLToPath(import.meta.url));
export const REPO_ROOT = path.resolve(here, "../../../..");

/**
 * Extra `make` variables for every make call. `PAIRLY_E2E_STACK=docker` runs the make
 * targets against the compose stack (`STACK=docker`) instead of the native one.
 * `PAIRLY_E2E_COMPOSE_FILE` (implies docker) points make at extra compose files (for
 * example `infra/docker-compose.yml -f my-override.yml`), for a stack started that way.
 */
export function makeArgs(): string[] {
  const file = process.env.PAIRLY_E2E_COMPOSE_FILE;
  const docker = process.env.PAIRLY_E2E_STACK === "docker" || Boolean(file);
  return [...(docker ? ["STACK=docker"] : []), ...(file ? [`COMPOSE_FILE=${file}`] : [])];
}

export interface RunState {
  orgSlug: string;
  adminEmail: string;
  memberEmail: string;
}

const RUN_STATE = path.join(AUTH_STATE_DIR, "run-state.json");

export function saveRunState(state: RunState): void {
  writeFileSync(RUN_STATE, JSON.stringify(state));
}

export function loadRunState(): RunState | null {
  if (!existsSync(RUN_STATE)) return null;
  return JSON.parse(readFileSync(RUN_STATE, "utf8")) as RunState;
}

export interface SeededApproval {
  approval_id: string;
  link_token: string | null;
  decision_id: string | null;
}

/** One pending approval for `email` in the run's organisation (`make dev-seed-approval`). */
export function seedApproval(
  state: RunState,
  options: {
    title?: string;
    overdue?: boolean;
    link?: "approve" | "reject" | "request_changes";
    decision?: boolean;
    /** Who becomes an approver (and gets the link). Default: the run's admin. */
    email?: string;
  } = {},
): SeededApproval {
  const args = [
    ...(options.title ? ["--title", JSON.stringify(options.title)] : []),
    ...(options.overdue ? ["--overdue"] : []),
    ...(options.link ? ["--link", options.link] : []),
    ...(options.decision ? ["--decision"] : []),
  ];
  const out = execFileSync(
    "make",
    [
      "-s",
      "dev-seed-approval",
      `ORG=${state.orgSlug}`,
      `EMAIL=${options.email ?? state.adminEmail}`,
      `ARGS=${args.join(" ")}`,
      ...makeArgs(),
    ],
    { cwd: REPO_ROOT, encoding: "utf8" },
  );
  const line = out
    .trim()
    .split("\n")
    .reverse()
    .find((l) => l.startsWith("{"));
  if (!line) throw new Error(`dev-seed-approval printed no result:\n${out}`);
  return JSON.parse(line) as SeededApproval;
}
