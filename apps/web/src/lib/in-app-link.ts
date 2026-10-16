// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { isLocale } from "@/i18n/routing";

const SEGMENT = /^[A-Za-z0-9_-]{1,128}$/;
const QUERY = /^\?[A-Za-z0-9_=&%.:-]{0,512}$/;

const BASE = "http://placeholder.invalid";

/**
 * A link from a notification, made safe to follow inside the app: only a same-origin
 * relative path of this app (`/inbox?item=...`, `/<locale>/pay/bank-changes/...`) with
 * plain segments and an optional simple query. The API stores every notification link
 * as such a path without a locale; the viewer's `locale` goes in front.
 *
 * Anything else is null: a full URL (even to this app's own host), a protocol-relative
 * one (`//host/...`, `/\host/...`), a path the URL parser had to rewrite (dot segments,
 * tabs, backslashes), or a path without a locale when no valid locale is given.
 */
export function inAppPath(link: string | null | undefined, locale?: string): string | null {
  if (!link || link.length > 1024) return null;
  if (!link.startsWith("/") || /^\/[/\\]/.test(link)) return null;
  let url: URL;
  try {
    url = new URL(link, BASE);
  } catch {
    return null;
  }
  if (url.origin !== BASE || url.username || url.password) return null;
  if (link.split(/[?#]/, 1)[0] !== url.pathname) return null;
  const [first, head, ...rest] = url.pathname.split("/");
  if (first !== "" || head === undefined) return null;
  let segments: string[];
  if (isLocale(head)) {
    segments = [head, ...rest];
  } else if (locale !== undefined && isLocale(locale)) {
    segments = [locale, head, ...rest];
  } else {
    return null;
  }
  if (!segments.slice(1).every((segment) => SEGMENT.test(segment))) return null;
  const search = url.search && QUERY.test(url.search) ? url.search : "";
  return `/${segments.join("/")}${search}`;
}
