// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { isLocale } from "@/i18n/routing";

const SEGMENT = /^[A-Za-z0-9_-]{1,128}$/;
const QUERY = /^\?[A-Za-z0-9_=&%.:-]{0,512}$/;

/**
 * A link from a notification, made safe to follow inside the app: only a path of this
 * app (`/<locale>/...` with plain segments, an optional simple query), whether it came
 * as a path or as a full URL (the API builds links from its own idea of the web
 * address, which can differ from the one in the address bar). Anything else is null.
 */
export function inAppPath(link: string | null | undefined): string | null {
  if (!link || link.length > 1024) return null;
  let url: URL;
  try {
    url = new URL(link, "http://placeholder.invalid");
  } catch {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  if (url.username || url.password) return null;
  const [first, locale, ...rest] = url.pathname.split("/");
  if (first !== "" || !isLocale(locale)) return null;
  if (!rest.every((segment) => SEGMENT.test(segment))) return null;
  const search = url.search && QUERY.test(url.search) ? url.search : "";
  return `/${[locale, ...rest].join("/")}${search}`;
}
