// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import type { Metadata } from "next";
import { setRequestLocale } from "next-intl/server";

import { toLocale } from "@/i18n/routing";

import { SectionPlaceholder, sectionMetadata } from "../section-placeholder";

interface Props {
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  return sectionMetadata(toLocale((await params).locale), "hire");
}

export default async function Page({ params }: Props) {
  setRequestLocale(toLocale((await params).locale));
  return <SectionPlaceholder section="hire" />;
}
