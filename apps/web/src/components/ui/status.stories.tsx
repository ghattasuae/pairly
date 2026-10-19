// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { AlertTriangle, Check, CheckCircle2, Hourglass, Info, Lock } from "lucide-react";
import { useLocale } from "next-intl";

import { chipClass } from "./chip";
import { Callout, ConfidenceMeter, Figure, StepList } from "./status";

const COPY = {
  en: {
    lockedT: "Payments to this vendor are locked",
    lockedB: "Nothing is paid until the number on file is called and two people approve.",
    coolT: "Approved, cooling off",
    coolB: "The new details are not used for 24 hours.",
    activeT: "The new details are active",
    infoT: "Sent for approval",
    infoB: "Another person approves the message before it goes out.",
    neutralT: "Nothing to review",
    callouts: "Callouts",
    figures: "Figures",
    runTotal: "Run total",
    twoInv: "2 invoices",
    notMatched: "Debits not matched",
    matched: "Lines matched",
    confidence: "Confidence",
    conf: "Confidence {n}%",
    steps: "Steps",
    where: "Where the change stands",
    requested: "Requested",
    date: "Oct 14, 2026",
    call: "Call back",
    callB: "Confirmed by the vendor",
    approvals: "Two approvals",
    approvalsB: "Waiting for the second approver",
    blocked: "Second approver unavailable",
    active: "Active",
    chips: "Chips",
    status: "Status",
    all: "All",
    review: "Needs review",
  },
  ar: {
    lockedT: "الدفع لهذا المورد مقفل",
    lockedB: "لا يُدفع أي مبلغ حتى يتصل أحد بالرقم المسجل ويعتمد التغيير شخصان.",
    coolT: "معتمد، في فترة التهدئة",
    coolB: "لا تُستخدم البيانات الجديدة قبل 24 ساعة.",
    activeT: "البيانات الجديدة سارية",
    infoT: "أُرسلت للاعتماد",
    infoB: "يعتمد شخص آخر الرسالة قبل إرسالها.",
    neutralT: "لا شيء للمراجعة",
    callouts: "تنبيهات",
    figures: "أرقام",
    runTotal: "إجمالي الدفعة",
    twoInv: "فاتورتان",
    notMatched: "مدفوعات غير مطابقة",
    matched: "أسطر مطابقة",
    confidence: "الثقة",
    conf: "الثقة {n}%",
    steps: "الخطوات",
    where: "أين وصل التغيير",
    requested: "مطلوب",
    date: "14 أكتوبر 2026",
    call: "الاتصال المرتد",
    callB: "أكده المورد",
    approvals: "اعتمادان",
    approvalsB: "بانتظار المعتمد الثاني",
    blocked: "المعتمد الثاني غير متاح",
    active: "سارٍ",
    chips: "شرائح التصفية",
    status: "الحالة",
    all: "الكل",
    review: "بحاجة إلى مراجعة",
  },
};

/**
 * Status patterns: callouts in each tone, labelled big numbers, the confidence meter,
 * the step list and filter chips. Tones are semantic; saffron stays for actions.
 */
function StatusPatterns() {
  const c = useLocale() === "ar" ? COPY.ar : COPY.en;
  return (
    <div className="space-y-10 p-8">
      <section className="space-y-3">
        <p className="eyebrow">{c.callouts}</p>
        <div className="grid gap-3 md:grid-cols-2">
          <Callout tone="danger" icon={<Lock aria-hidden />} title={c.lockedT}>
            <p className="text-danger-ink">{c.lockedB}</p>
          </Callout>
          <Callout tone="warning" icon={<Hourglass aria-hidden />} title={c.coolT}>
            <p className="text-warning-ink">{c.coolB}</p>
          </Callout>
          <Callout tone="success" icon={<CheckCircle2 aria-hidden />} title={c.activeT} />
          <Callout tone="info" icon={<Info aria-hidden />} title={c.infoT}>
            <p>{c.infoB}</p>
          </Callout>
          <Callout tone="neutral" icon={<AlertTriangle aria-hidden />} title={c.neutralT} />
        </div>
      </section>

      <section className="space-y-3">
        <p className="eyebrow">{c.figures}</p>
        <div className="grid gap-6 sm:grid-cols-3">
          <Figure label={c.runTotal} value="AED 28,188.43" hint={c.twoInv} />
          <Figure label={c.notMatched} value="1" tone="danger" />
          <Figure label={c.matched} value="42" tone="success" />
        </div>
      </section>

      <section className="space-y-3">
        <p className="eyebrow">{c.confidence}</p>
        <div className="flex flex-col gap-2 text-xs text-muted-foreground">
          <ConfidenceMeter value={0.97} tone="success">{c.conf.replace("{n}", "97")}</ConfidenceMeter>
          <ConfidenceMeter value={0.84} tone="neutral">{c.conf.replace("{n}", "84")}</ConfidenceMeter>
          <ConfidenceMeter value={0.41} tone="warning">{c.conf.replace("{n}", "41")}</ConfidenceMeter>
          <ConfidenceMeter value={0} tone="danger">{c.conf.replace("{n}", "0")}</ConfidenceMeter>
        </div>
      </section>

      <section className="space-y-3">
        <p className="eyebrow">{c.steps}</p>
        <StepList
          label={c.where}
          steps={[
            { key: "requested", state: "done", title: c.requested, body: c.date, icon: <Check /> },
            { key: "call", state: "done", title: c.call, body: c.callB, icon: <Check /> },
            { key: "approvals", state: "current", title: c.approvals, body: c.approvalsB },
            { key: "blocked", state: "blocked", title: c.blocked },
            { key: "active", state: "todo", title: c.active },
          ]}
        />
      </section>

      <section className="space-y-3">
        <p className="eyebrow">{c.chips}</p>
        <nav aria-label={c.status} className="flex flex-wrap gap-2">
          <a href="#all" aria-current="page" className={chipClass(true)}>
            {c.all} <span className="tabular-nums text-muted-foreground">12</span>
          </a>
          <a href="#review" className={chipClass(false)}>
            {c.review} <span className="tabular-nums text-muted-foreground">3</span>
          </a>
        </nav>
      </section>
    </div>
  );
}

const meta = { title: "UI/Status", component: StatusPatterns } satisfies Meta<typeof StatusPatterns>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Light: Story = {};
export const Dark: Story = { globals: { theme: "dark" } };
export const Arabic: Story = { globals: { locale: "ar" } };
export const ArabicDark: Story = { globals: { locale: "ar", theme: "dark" } };
