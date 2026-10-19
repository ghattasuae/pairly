// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { useFormatter, useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";
import { chainNames, rulesLines } from "@/lib/pay/payments";
import type { PayRules } from "@/lib/pay/types";

import { Panel } from "./common";

function Yaml({ content }: { content: string }) {
  return (
    <pre
      dir="ltr"
      className="max-h-[32rem] overflow-auto rounded-lg border border-border bg-surface-sunken p-3 text-start font-mono text-xs leading-relaxed"
      data-rules-yaml
    >
      {rulesLines(content).map((line, i) => (
        <span key={i} className={line.comment ? "block text-muted-foreground" : "block"}>
          {line.text || " "}
        </span>
      ))}
    </pre>
  );
}

/**
 * The approval rules Pay uses, read only (policy as code, brief 2.3): for each kind of
 * approval, the version in force with its chains, and every version. A new version is
 * published by an admin; approvals keep pointing at the version they used.
 */
export function ApprovalRulesView({ items }: { items: PayRules[] }) {
  const t = useTranslations("Pay.rules");
  const format = useFormatter();
  return (
    <div className="space-y-6">
      {items.map((rules) => {
        const content = rules.active?.content ?? rules.default_content;
        const chains = chainNames(content);
        return (
          <Panel key={rules.approval_type} title={rules.title || rules.approval_type} data-rules={rules.approval_type}>
            <p className="flex flex-wrap items-center gap-2 text-sm">
              <span className="font-mono text-xs text-muted-foreground" dir="ltr">
                {rules.approval_type}
              </span>
              {rules.active ? (
                <Badge data-rules-version={rules.active.version}>
                  {t("version", { version: rules.active.version })}
                </Badge>
              ) : (
                <Badge variant="outline" data-rules-version="default">
                  {t("notUsed")}
                </Badge>
              )}
              {rules.active && (
                <span className="text-muted-foreground">
                  {t("since", { when: format.dateTime(new Date(rules.active.created_at), { dateStyle: "medium" }) })}
                </span>
              )}
            </p>
            {!rules.active && <p className="text-sm text-muted-foreground">{t("defaultHelp")}</p>}
            {chains.length > 0 && (
              <p className="text-sm">
                {t("chains")}{" "}
                {chains.map((name, i) => (
                  <span key={name}>
                    {i > 0 && ", "}
                    <code dir="ltr" className="text-xs">
                      {name}
                    </code>
                  </span>
                ))}
              </p>
            )}
            <Yaml content={content} />
            {rules.versions.length > 0 && (
              <div className="space-y-1">
                <h3 className="text-sm font-medium">{t("versions")}</h3>
                <ul className="space-y-0.5 text-xs text-muted-foreground" data-rules-versions>
                  {rules.versions.map((v) => (
                    <li key={v.id}>
                      {t("versionLine", {
                        version: v.version,
                        when: format.dateTime(new Date(v.created_at), { dateStyle: "medium", timeStyle: "short" }),
                      })}{" "}
                      <span className="font-mono" dir="ltr">
                        {v.content_hash.slice(0, 12)}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </Panel>
        );
      })}
      <p className="text-sm text-muted-foreground">{t("publishHelp")}</p>
    </div>
  );
}
