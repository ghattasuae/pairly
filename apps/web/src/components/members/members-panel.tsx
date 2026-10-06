// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
"use client";

import { Check, Copy, MailPlus, UserRound } from "lucide-react";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useId, useState, useTransition } from "react";
import type * as React from "react";
import { z } from "zod";

import { FormError } from "@/components/auth/auth-card";
import { EmptyState } from "@/components/states/empty-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { locales } from "@/i18n/routing";
import { inviteMember, revokeInvitation, type InviteInput } from "@/lib/actions/invitations";
import { isolate } from "@/lib/bidi";
import { formatRole, type Lookup } from "@/lib/role-label";
import { cn } from "@/lib/utils";

/** Roles people can be invited to. The API still decides who may grant which. */
export const INVITABLE_ROLES = [
  "read_only",
  "recruiter",
  "hiring_manager",
  "interviewer",
  "ap_clerk",
  "approver",
  "finance_controller",
  "auditor",
  "admin",
] as const;

export interface MemberRow {
  user_id: string;
  email: string;
  display_name: string | null;
  roles: { role: string; workflow: string | null }[];
}

export interface InvitationRow {
  id: string;
  email: string;
  role: string;
  workflow: string | null;
  status: "pending" | "accepted" | "revoked" | "expired";
  expires_at: string;
}

type Result<T extends object = object> = ({ ok: true } & T) | { ok: false; code: string };

export interface MembersPanelProps {
  currentUserId: string;
  members: readonly MemberRow[];
  invitations: readonly InvitationRow[];
  /** Holds core.users.manage. */
  canManage: boolean;
  /** Holds it but has no TOTP sign-in yet: the form is shown disabled. */
  blockedBySecondFactor: boolean;
  /** Overrides for stories and tests. Default to the server actions. */
  invite?: (input: InviteInput) => Promise<Result<{ emailed: boolean; link?: string }>>;
  revoke?: (id: string) => Promise<Result>;
}

const KNOWN_ERRORS = new Set([
  "invalid_input",
  "invitation_pending",
  "already_member",
  "cannot_invite_self",
  "permission_denied",
  "second_factor_required",
  "invitation_closed",
  "not_found",
]);

const emailSchema = z.email().max(254);

function useRoleLabel() {
  const roles = useTranslations("Roles") as unknown as Lookup;
  const workflows = useTranslations("Workflows") as unknown as Lookup;
  return (role: string, workflow: string | null) => formatRole(roles, workflows, role, workflow);
}

function Section({ title, children, id }: { title: string; children: React.ReactNode; id: string }) {
  return (
    <section aria-labelledby={id} className="space-y-3">
      <h2 id={id} className="eyebrow">
        {title}
      </h2>
      {children}
    </section>
  );
}

function InviteForm({
  disabled,
  invite,
  onDone,
}: {
  disabled: boolean;
  invite: NonNullable<MembersPanelProps["invite"]>;
  onDone: () => void;
}) {
  const t = useTranslations("Members");
  const localeNames = useTranslations("Locale");
  const workflows = useTranslations("Workflows");
  const roleLabel = useRoleLabel();
  const current = useLocale();
  const ids = { email: useId(), role: useId(), workflow: useId(), locale: useId(), error: useId() };
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<string>("read_only");
  const [workflow, setWorkflow] = useState<"" | "hire" | "pay">("");
  const [language, setLanguage] = useState<string>(current);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState<{ email: string; link?: string | undefined } | null>(null);
  const [copied, setCopied] = useState(false);

  const onSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const parsed = emailSchema.safeParse(email.trim());
    if (!parsed.success) {
      setError("invalid_input");
      return;
    }
    setPending(true);
    setError(null);
    setSent(null);
    try {
      const result = await invite({
        email: parsed.data,
        role,
        workflow: workflow || null,
        locale: language as InviteInput["locale"],
      });
      if (!result.ok) {
        setError(KNOWN_ERRORS.has(result.code) ? result.code : "generic");
      } else {
        setSent({ email: parsed.data, link: result.emailed ? undefined : result.link });
        setEmail("");
        onDone();
      }
    } catch {
      setError("generic");
    }
    setPending(false);
  };

  const copy = async (link: string) => {
    try {
      await navigator.clipboard.writeText(link);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  const select =
    "h-10 w-full rounded-md border border-input bg-surface px-3 text-sm shadow-xs outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/25 disabled:opacity-50";

  return (
    <form onSubmit={onSubmit} noValidate className="space-y-4 rounded-xl border bg-surface p-5 shadow-soft">
      <div className="space-y-1">
        <h3 className="font-display text-xl">{t("inviteHeading")}</h3>
        <p className="text-sm text-muted-foreground">{t("inviteBody")}</p>
      </div>
      <fieldset disabled={disabled || pending} className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-2 sm:col-span-2">
          <Label htmlFor={ids.email}>{t("emailLabel")}</Label>
          <Input
            id={ids.email}
            type="email"
            dir="ltr"
            autoComplete="off"
            inputMode="email"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            aria-invalid={error === "invalid_input" || undefined}
            aria-describedby={error ? ids.error : undefined}
            className="text-start"
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor={ids.role}>{t("roleLabel")}</Label>
          <select id={ids.role} className={select} value={role} onChange={(event) => setRole(event.target.value)}>
            {INVITABLE_ROLES.map((key) => (
              <option key={key} value={key}>
                {roleLabel(key, null)}
              </option>
            ))}
          </select>
        </div>
        <div className="space-y-2">
          <Label htmlFor={ids.workflow}>{t("workflowLabel")}</Label>
          <select
            id={ids.workflow}
            className={select}
            value={workflow}
            onChange={(event) => setWorkflow(event.target.value as "" | "hire" | "pay")}
          >
            <option value="">{workflows("all")}</option>
            <option value="hire">{workflows("hire")}</option>
            <option value="pay">{workflows("pay")}</option>
          </select>
        </div>
        <div className="space-y-2">
          <Label htmlFor={ids.locale}>{t("languageLabel")}</Label>
          <select id={ids.locale} className={select} value={language} onChange={(event) => setLanguage(event.target.value)}>
            {locales.map((key) => (
              <option key={key} value={key}>
                {localeNames(key)}
              </option>
            ))}
          </select>
        </div>
        <div className="flex items-end">
          <Button type="submit" className="w-full sm:w-auto">
            <MailPlus aria-hidden />
            {pending ? t("sending") : t("send")}
          </Button>
        </div>
      </fieldset>
      {error && <FormError id={ids.error}>{t(`errors.${KNOWN_ERRORS.has(error) ? error : "generic"}` as "errors.generic")}</FormError>}
      {sent && !sent.link && (
        <p role="status" className="text-sm text-primary">
          {t("sent", { email: isolate(sent.email) })}
        </p>
      )}
      {sent?.link && (
        <div role="status" className="space-y-2 rounded-md border border-signal/40 bg-signal-soft/50 p-3 text-sm">
          <p>{t("notEmailed", { email: isolate(sent.email) })}</p>
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
            <code dir="ltr" className="min-w-0 flex-1 truncate rounded bg-background px-2 py-1 font-mono text-xs">
              {sent.link}
            </code>
            <Button type="button" variant="outline" size="sm" onClick={() => copy(sent.link ?? "")}>
              {copied ? <Check aria-hidden /> : <Copy aria-hidden />}
              {copied ? t("copied") : t("copy")}
            </Button>
          </div>
        </div>
      )}
    </form>
  );
}

/** Settings, Members: people, their roles, invitations, and the invite form. */
export function MembersPanel({
  currentUserId,
  members,
  invitations,
  canManage,
  blockedBySecondFactor,
  invite = inviteMember,
  revoke = revokeInvitation,
}: MembersPanelProps) {
  const t = useTranslations("Members");
  const format = useFormatter();
  const roleLabel = useRoleLabel();
  const router = useRouter();
  const [, startTransition] = useTransition();
  const [revoking, setRevoking] = useState<string | null>(null);
  const [revokeError, setRevokeError] = useState<string | null>(null);
  const refresh = () => startTransition(() => router.refresh());

  const onRevoke = async (id: string) => {
    setRevoking(id);
    setRevokeError(null);
    try {
      const result = await revoke(id);
      if (!result.ok) setRevokeError(KNOWN_ERRORS.has(result.code) ? result.code : "generic");
      else refresh();
    } catch {
      setRevokeError("generic");
    }
    setRevoking(null);
  };

  const canRevoke = canManage && !blockedBySecondFactor;

  return (
    <div className="space-y-10">
      {canManage ? (
        <InviteForm disabled={blockedBySecondFactor} invite={invite} onDone={refresh} />
      ) : (
        <p className="text-sm text-muted-foreground">{t("readOnly")}</p>
      )}

      <Section title={t("membersHeading")} id="members-heading">
        {members.length === 0 ? (
          <EmptyState icon={<UserRound aria-hidden />} title={t("membersEmpty")} />
        ) : (
          <ul className="divide-y rounded-xl border bg-surface">
            {members.map((member) => (
              <li key={member.user_id} className="flex flex-col gap-2 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
                <div className="min-w-0">
                  {member.display_name && <p className="truncate text-sm font-medium">{member.display_name}</p>}
                  <p className="truncate text-sm">
                    <bdi>{member.email}</bdi>
                    {member.user_id === currentUserId && (
                      <span className="ms-2 text-xs text-muted-foreground">({t("you")})</span>
                    )}
                  </p>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {member.roles.map((r) => (
                    <Badge key={`${r.role}:${r.workflow ?? "all"}`} variant="outline">
                      {roleLabel(r.role, r.workflow)}
                    </Badge>
                  ))}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title={t("invitationsHeading")} id="invitations-heading">
        {revokeError && (
          <FormError id="revoke-error">{t(`errors.${revokeError}` as "errors.generic")}</FormError>
        )}
        {invitations.length === 0 ? (
          <EmptyState icon={<MailPlus aria-hidden />} title={t("invitationsEmpty")} />
        ) : (
          <ul className="divide-y rounded-xl border bg-surface">
            {invitations.map((inv) => (
              <li key={inv.id} className="flex flex-col gap-2 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
                <div className="min-w-0 space-y-1">
                  <p className="truncate text-sm">
                    <bdi>{inv.email}</bdi>
                  </p>
                  <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                    <Badge variant={inv.status === "pending" ? "signal" : "outline"}>{t(`status.${inv.status}`)}</Badge>
                    <span>{roleLabel(inv.role, inv.workflow)}</span>
                    {inv.status === "pending" && (
                      <span>· {t("expires", { date: format.dateTime(new Date(inv.expires_at), { dateStyle: "medium" }) })}</span>
                    )}
                  </div>
                </div>
                {inv.status === "pending" && canRevoke && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => onRevoke(inv.id)}
                    disabled={revoking !== null}
                    aria-label={t("revokeLabel", { email: inv.email })}
                    className={cn("self-start sm:self-auto")}
                  >
                    {revoking === inv.id ? t("revoking") : t("revoke")}
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </Section>
    </div>
  );
}
