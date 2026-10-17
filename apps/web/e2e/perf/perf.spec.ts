// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology

// Page timings for docs/performance.md, on a stack seeded by `make demo` (org "demo").
// For each main page: the server response time of the document (requestStart to
// responseStart), the total load (navigation start to the load event), and the
// cumulative layout shift over the first 3 seconds (layout-shift entries without
// recent input). Each page loads once to warm up, then RUNS times; the median of each
// number is written to PAIRLY_PERF_OUT (JSON). Staff sign in with a magic link from
// the dev outbox; the candidate and vendor portals with a link the demo seed mailed
// (candidate) or printed (PAIRLY_PERF_VENDOR_LINK).
import { existsSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";

import { expect, test, type Browser, type Page } from "@playwright/test";

import { extractLink, OUTBOX_DIR, toReachableUrl } from "../utils/mail";

const RUNS = Number(process.env.PAIRLY_PERF_RUNS ?? "3");
const OUT = process.env.PAIRLY_PERF_OUT ?? path.join(process.cwd(), "perf.json");
const SETTLE_MS = 3000;

interface Sample {
  ttfb: number;
  load: number;
  ready: number;
  cls: number;
  shifts: string[];
}
interface Result {
  page: string;
  url: string;
  ttfb_ms: number;
  load_ms: number;
  ready_ms: number;
  cls: number;
  runs: Sample[];
}

const median = (xs: number[]) => {
  const s = [...xs].sort((a, b) => a - b);
  return s.length ? s[Math.floor((s.length - 1) / 2)]! : 0;
};

function outboxMessages(): Array<{ id: string; to: string[]; html: string }> {
  return readdirSync(OUTBOX_DIR)
    .filter((f) => f.endsWith(".json"))
    .sort()
    .reverse()
    .map((f) => JSON.parse(readFileSync(path.join(OUTBOX_DIR, f), "utf8")) as { id: string; to: string[]; html: string });
}

const STATE_DIR = process.env.PAIRLY_PERF_STATE ?? path.join(path.dirname(OUT), "perf-state");

async function signIn(browser: Browser, email: string): Promise<Page> {
  mkdirSync(STATE_DIR, { recursive: true });
  const state = path.join(STATE_DIR, `${email}.json`);
  if (existsSync(state) && Date.now() - statSync(state).mtimeMs < 4 * 3600_000) {
    const saved = await browser.newContext({ storageState: state });
    await saved.addInitScript(clsObserver);
    return saved.newPage();
  }
  const context = await browser.newContext();
  await context.addInitScript(clsObserver);
  const page = await context.newPage();
  const since = Date.now() - 1000;
  await page.goto("/en/sign-in");
  await page.getByRole("textbox").first().fill(email);
  await page.keyboard.press("Enter");
  await page.waitForURL(/\/check-email/, { timeout: 30_000 });
  let link: string | null = null;
  for (let i = 0; i < 60 && !link; i++) {
    const found = outboxMessages().find(
      (m) => Number(m.id.slice(0, 13)) >= since && m.to.some((t) => t.toLowerCase() === email) && m.html.includes("sign-in/confirm"),
    );
    if (found) link = extractLink(found.html, "sign-in/confirm");
    else await page.waitForTimeout(500);
  }
  expect(link, `a sign-in email for ${email}`).toBeTruthy();
  await page.goto(toReachableUrl(link!));
  await page.getByRole("link", { name: /sign in/i }).click();
  await page.waitForURL((u) => !/sign-in|check-email/.test(u.pathname), { timeout: 30_000 });
  await context.storageState({ path: state });
  return page;
}

// Runs in the page before any script: collects layout shifts not caused by input.
function clsObserver(): void {
  const w = window as unknown as { __cls: number; __shifts: string[] };
  w.__cls = 0;
  w.__shifts = [];
  try {
    new PerformanceObserver((list) => {
      type Shift = PerformanceEntry & { value: number; hadRecentInput: boolean; sources?: Array<{ node?: Node | null }> };
      for (const entry of list.getEntries() as Shift[]) {
        if (entry.hadRecentInput) continue;
        w.__cls += entry.value;
        for (const source of entry.sources ?? []) {
          const el = source.node instanceof Element ? source.node : source.node?.parentElement;
          if (!el) continue;
          const attrs = Array.from(el.attributes).filter((a) => a.name.startsWith("data-") || a.name === "class").map((a) => `${a.name}=${a.value.slice(0, 60)}`);
          w.__shifts.push(`${entry.value.toFixed(4)} ${el.tagName.toLowerCase()} ${attrs.join(" ")}`);
        }
      }
    }).observe({ type: "layout-shift", buffered: true });
  } catch {
    // not supported
  }
}

async function measureOnce(page: Page, url: string): Promise<Sample> {
  const started = Date.now();
  await page.goto(url, { waitUntil: "load", timeout: 60_000 });
  // Ready: no request in flight for 500 ms (the data a client component fetches
  // after hydration included), minus that quiet period.
  await page.waitForLoadState("networkidle", { timeout: 30_000 }).catch(() => undefined);
  const ready = Date.now() - started - 500;
  await page.waitForTimeout(SETTLE_MS);
  // The page itself, never a sign-in, two-step or error page in its place.
  expect(new URL(page.url()).pathname, `${url} stayed where it was asked`).not.toMatch(/sign-in|two-factor|login|check-email/);
  const timing = await page.evaluate(() => {
    const nav = performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming;
    const w = window as unknown as { __cls: number; __shifts: string[] };
    return {
      ttfb: nav.responseStart - nav.requestStart,
      load: nav.loadEventEnd - nav.startTime,
      cls: w.__cls ?? 0,
      shifts: w.__shifts ?? [],
    };
  });
  return { ...timing, ready: Math.max(ready, timing.load) };
}

/** Ask a portal for a sign-in link for `email`, then open it from the dev outbox. */
async function portalSignIn(browser: Browser, loginPath: string, email: string, linkRe: RegExp): Promise<Page> {
  const context = await browser.newContext();
  await context.addInitScript(clsObserver);
  const page = await context.newPage();
  const since = Date.now() - 1000;
  await page.goto(loginPath);
  await page.locator('input[type="email"]').first().fill(email);
  await page.keyboard.press("Enter");
  let link: string | null = null;
  for (let i = 0; i < 360 && !link; i++) {
    const found = outboxMessages().find(
      (m) => Number(m.id.slice(0, 13)) >= since && m.to.some((t) => t.toLowerCase() === email.toLowerCase()) && linkRe.test(m.html),
    );
    if (found) link = linkRe.exec(found.html)![0].replace(/&amp;/g, "&");
    else await page.waitForTimeout(500);
  }
  expect(link, `a portal sign-in email for ${email}`).toBeTruthy();
  await page.goto(toReachableUrl(link!));
  await page.waitForURL((u) => !/login/.test(u.pathname), { timeout: 30_000 });
  return page;
}

const results: Result[] = [];

async function measure(page: Page, name: string, url: string): Promise<void> {
  await measureOnce(page, url); // warm up (compiles nothing in start mode; fills caches)
  const runs: Sample[] = [];
  for (let i = 0; i < RUNS; i++) runs.push(await measureOnce(page, url));
  const r: Result = {
    page: name,
    url,
    ttfb_ms: Math.round(median(runs.map((s) => s.ttfb))),
    load_ms: Math.round(median(runs.map((s) => s.load))),
    ready_ms: Math.round(median(runs.map((s) => s.ready))),
    cls: Number(median(runs.map((s) => s.cls)).toFixed(4)),
    runs,
  };
  results.push(r);
  writeFileSync(OUT, JSON.stringify(results, null, 2));
  console.log(
    `${name.padEnd(28)} ttfb ${String(r.ttfb_ms).padStart(5)} ms  load ${String(r.load_ms).padStart(5)} ms  ready ${String(r.ready_ms).padStart(5)} ms  cls ${r.cls}`,
  );
  if (r.cls > 0) console.log(`  shifts: ${[...new Set(runs.flatMap((x) => x.shifts))].slice(0, 6).join(" | ")}`);
}

async function firstHref(page: Page, url: string, pattern: RegExp): Promise<string> {
  await page.goto(url, { waitUntil: "load" });
  await page.waitForTimeout(1000);
  const hrefs = await page.locator("a[href]").evaluateAll((as) => as.map((a) => a.getAttribute("href") ?? ""));
  const found = hrefs.find((h) => pattern.test(h));
  expect(found, `a link matching ${pattern} on ${url}`).toBeTruthy();
  return found!;
}

test("page timings on the demo data", async ({ browser }) => {
  const recruiter = await signIn(browser, "demo-hire-recruiter@example.test");
  await measure(recruiter, "home (recruiter)", "/en");
  const job = (await firstHref(recruiter, "/en/hire", /\/hire\/jobs\/[0-9a-f-]{36}$/)).replace(/^\/en/, "");
  await measure(recruiter, "hire jobs", "/en/hire");
  await measure(recruiter, "screening mode", `/en${job}/screen`);
  await measure(recruiter, "pipeline board", `/en${job}/pipeline`);
  await measure(recruiter, "hire analytics", "/en/hire/analytics");

  const manager = await signIn(browser, "demo-hire-manager@example.test");
  await measure(manager, "home (hiring manager)", "/en");
  await measure(manager, "inbox (hiring manager)", "/en/inbox");

  const clerk = await signIn(browser, "demo-pay-clerk@example.test");
  await measure(clerk, "home (AP clerk)", "/en");
  await measure(clerk, "invoices", "/en/pay/invoices");
  const invoice = await firstHref(clerk, "/en/pay/invoices", /\/pay\/invoices\/[0-9a-f-]{36}/);
  await measure(clerk, "invoice review", invoice.startsWith("/en") ? invoice : `/en${invoice}`);
  await measure(clerk, "exceptions", "/en/pay/exceptions");
  await measure(clerk, "pay analytics", "/en/pay/analytics");

  const approver = await signIn(browser, "demo-pay-approver@example.test");
  await measure(approver, "home (approver)", "/en");
  await measure(approver, "inbox (approver)", "/en/inbox");

  const admin = await signIn(browser, "demo-admin@example.test");
  await measure(admin, "home (admin)", "/en");
  await measure(admin, "audit", "/en/audit");

  // Candidate portal: a fresh magic link for a candidate the demo seed acknowledged.
  const ack = outboxMessages().find((m) => /\/portal\/login#/.test(m.html));
  expect(ack, "an acknowledgement with a portal link").toBeTruthy();
  const candidate = await portalSignIn(browser, "/en/portal?org=demo", ack!.to[0]!, /https?:\/\/[^\s<>"]*\/portal\/login#[A-Za-z0-9._~-]+/);
  await measure(candidate, "candidate portal", "/en/portal");
  const application = await firstHref(candidate, "/en/portal", /\/portal\/applications\/[0-9a-f-]{36}$/);
  await measure(candidate, "candidate application", application.startsWith("/en") ? application : `/en${application}`);

  const vendor = await portalSignIn(
    browser,
    `/en/vendor-portal?org=${process.env.PAIRLY_PERF_VENDOR_ORG ?? ""}`,
    process.env.PAIRLY_PERF_VENDOR_EMAIL ?? "omar.nasser@seltus.example",
    /https?:\/\/[^\s<>"]*\/vendor-portal\/login#[A-Za-z0-9._~-]+/,
  );
  await measure(vendor, "vendor portal", "/en/vendor-portal");
  await measure(vendor, "vendor portal details", "/en/vendor-portal/details");
  writeFileSync(OUT, JSON.stringify(results, null, 2));
});
