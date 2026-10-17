// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
import { notFound } from "next/navigation";

import { redirect } from "@/i18n/navigation";
import { toLocale } from "@/i18n/routing";
import { isUuid } from "@/lib/inbox-filters";

interface Props {
  params: Promise<{ locale: string; id: string }>;
}

/**
 * `/<locale>/inbox/<approval id>`: a plain path to one inbox item, used as the place to
 * come back to after signing in again with a code (sign-in only returns to plain paths).
 * Opens the inbox with that item selected.
 */
export default async function InboxItemPage({ params }: Props) {
  const { locale, id } = await params;
  if (!isUuid(id)) notFound();
  redirect({ href: `/inbox?item=${id.toLowerCase()}`, locale: toLocale(locale) });
}
