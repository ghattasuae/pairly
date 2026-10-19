// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { useEffect, useState } from "react";

/**
 * The foundations of the "ink and saffron" system: every colour token (read live from
 * globals.css, so this page never drifts) and the type scale. Flip the theme and locale
 * in the toolbar to see the dark values and the Arabic fallbacks.
 */

const GROUPS: { title: string; note: string; tokens: string[] }[] = [
  {
    title: "Paper",
    note: "Warm bone surfaces. Cards are a touch lighter than the page and rely on the hairline.",
    tokens: ["background", "surface", "surface-sunken", "popover", "muted", "accent", "sidebar", "sidebar-active"],
  },
  {
    title: "Ink",
    note: "Near-black warm text, and the muted ink for secondary lines.",
    tokens: ["foreground", "muted-foreground", "secondary", "secondary-foreground"],
  },
  {
    title: "Saffron",
    note: "The one accent: primary actions, focus rings, the active nav marker, key numbers.",
    tokens: ["signal", "action", "action-hover", "signal-soft", "signal-ink", "ring"],
  },
  {
    title: "Slate",
    note: "Links, chart lines and informational badges, so the saffron stays rare.",
    tokens: ["primary", "primary-foreground", "info-soft"],
  },
  {
    title: "Status",
    note: "Muted and semantic. Fills, text-strength inks and soft tints, AA in both themes.",
    tokens: [
      "success",
      "success-ink",
      "success-soft",
      "warning",
      "warning-ink",
      "warning-soft",
      "destructive",
      "danger-ink",
      "danger-soft",
    ],
  },
  { title: "Rules", note: "Hairlines between things, and the edge of inputs.", tokens: ["border", "rule", "input"] },
];

function useTokenValue(name: string): string {
  const [value, setValue] = useState("");
  useEffect(() => {
    const read = () => setValue(getComputedStyle(document.documentElement).getPropertyValue(`--${name}`).trim());
    read();
    const observer = new MutationObserver(read);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, [name]);
  return value;
}

function Swatch({ name }: { name: string }) {
  const value = useTokenValue(name);
  return (
    <li className="space-y-2">
      <span aria-hidden className="block h-14 rounded-md border border-border" style={{ background: `var(--${name})` }} />
      <span className="block font-mono text-xs text-foreground" dir="ltr">
        --{name}
      </span>
      <span className="block font-mono text-[0.68rem] text-muted-foreground" dir="ltr">
        {value}
      </span>
    </li>
  );
}

function Colours() {
  return (
    <div className="space-y-10 p-8">
      {GROUPS.map((group) => (
        <section key={group.title} className="space-y-3">
          <div className="space-y-1">
            <h2 className="font-display text-2xl">{group.title}</h2>
            <p className="text-sm text-muted-foreground">{group.note}</p>
          </div>
          <ul className="grid grid-cols-2 gap-4 sm:grid-cols-4 lg:grid-cols-6">
            {group.tokens.map((token) => (
              <Swatch key={token} name={token} />
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

const SCALE: { label: string; className: string; sample: string }[] = [
  { label: "Display 1, Fraunces", className: "font-display text-6xl leading-[1.05]", sample: "Humans decide." },
  { label: "Display 2, Fraunces (page title)", className: "font-display text-[2.6rem] leading-[1.1]", sample: "Invoices to approve" },
  { label: "Heading, Fraunces", className: "font-display text-[1.75rem] leading-tight", sample: "Accounts payable" },
  { label: "Big number, Fraunces", className: "figure text-5xl leading-none", sample: "1,284" },
  { label: "Body, Plex Sans 400", className: "text-base", sample: "Every application is read by a person." },
  { label: "UI, Plex Sans 500", className: "text-sm font-medium", sample: "Send for approval" },
  { label: "Small, Plex Sans 400, muted", className: "text-xs text-muted-foreground", sample: "Posted 1 October 2026" },
  { label: "Eyebrow", className: "eyebrow", sample: "Waiting on you" },
  { label: "Mono, Plex Mono (ids, amounts)", className: "font-mono text-sm", sample: "DEC-7K2Q · AED 30,000.00 · 9f2c1a" },
  { label: "Arabic, Plex Sans Arabic", className: "text-base", sample: "كل قرار هنا يتخذه إنسان." },
  { label: "Arabic heading, Plex Sans Arabic 600", className: "font-display text-[1.75rem]", sample: "الذكاء الاصطناعي يقرأ. والإنسان يقرر." },
];

function Type() {
  return (
    <div className="divide-y divide-border p-8">
      {SCALE.map((row) => (
        <div key={row.label} className="grid gap-2 py-5 sm:grid-cols-[14rem_minmax(0,1fr)] sm:items-baseline">
          <p className="font-mono text-xs text-muted-foreground" dir="ltr">
            {row.label}
          </p>
          <p className={row.className} dir="auto">
            {row.sample}
          </p>
        </div>
      ))}
    </div>
  );
}

const meta = { title: "Foundations/Tokens", component: Colours } satisfies Meta<typeof Colours>;
export default meta;
type Story = StoryObj<typeof meta>;

export const ColoursLight: Story = {};
export const ColoursDark: Story = { globals: { theme: "dark" } };
export const TypeScale: Story = { render: () => <Type /> };
export const TypeScaleDark: Story = { render: () => <Type />, globals: { theme: "dark" } };
