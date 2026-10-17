// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const here = path.dirname(fileURLToPath(import.meta.url));

describe("Storybook server action stand-in", () => {
  it("exports every server action, so no story bundles server code", () => {
    const dir = path.join(here, "actions");
    const actions = readdirSync(dir)
      .filter((f) => f.endsWith(".ts") && !f.endsWith(".test.ts"))
      .flatMap((f) => [...readFileSync(path.join(dir, f), "utf8").matchAll(/export async function (\w+)/g)].map((m) => m[1]));
    const mock = readFileSync(path.join(here, "../../.storybook/actions-mock.ts"), "utf8");
    const missing = actions.filter((name) => !new RegExp(`export const ${name}\\b`).test(mock));
    expect(missing).toEqual([]);
  });
});
