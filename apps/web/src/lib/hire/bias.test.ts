// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { describe, expect, it } from "vitest";

import { biasExportQuery, biasQuery, judgementTone } from "./bias";

const JOB = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

describe("biasQuery", () => {
  it("keeps valid filters only", () => {
    expect(biasQuery({ job_id: JOB, since: "2026-01-01", min_sample: "50" })).toEqual({
      job_id: JOB,
      since: "2026-01-01",
      min_sample: 50,
    });
    expect(biasQuery({ job_id: "x", min_sample: "3", until: "2026-13" })).toEqual({});
  });
});

describe("biasExportQuery", () => {
  it("forwards validated filters and nothing else", () => {
    const params = new URLSearchParams({ job_id: JOB, min_sample: "40", evil: "1", since: "bad" });
    expect(biasExportQuery(params)).toBe(`?job_id=${JOB}&min_sample=40`);
    expect(biasExportQuery(new URLSearchParams())).toBe("");
  });
});

describe("judgementTone", () => {
  it("makes only a result below four-fifths stand out", () => {
    expect(judgementTone("below_four_fifths")).toBe("warning");
    expect(judgementTone("meets_four_fifths")).toBe("default");
    expect(judgementTone("too_small_to_judge")).toBe("outline");
    expect(judgementTone("reference")).toBe("default");
  });
});
