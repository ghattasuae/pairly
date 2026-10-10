// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 Proofly Technology
import { isLocale } from "@/i18n/routing";

const SEGMENT = /^[A-Za-z0-9_-]{1,128}$/;
const QUERY = /^\?[A-Za-z0-9_=&%.:-]{0,512}$/;

const BASE = "http://placeholder.invalid";

/**
 * A link from a notification, made safe to follow inside the app: only a path of this
 * app (`/<locale>/...` with plain segments, an optional simple query), whether it came
 * as a path or as a full URL (the API builds links from its own idea of the web
 * address, which can differ from the one in the address bar). Anything else is null.
 *
 * Paths the API stores without a locale (`/hire/data-requests?request=...`) get the
 * viewer's `locale` in front, but only when the link is a same-origin relative path:
 * an absolute URL (`https://host/hire/...`) or a protocol-relative one (`//host/...`,
 * `/\host/...`) without a locale is still refused.
 */
export function inAppPath(link: string | null | undefined, locale?: string): string | null {
  if (!link || link.length > 1024) return null;
  let url: URL;
  try {
    url = new URL(link, BASE);
  } catch {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  if (url.username || url.password) return null;
  // Same origin, a single leading slash, and nothing the URL parser had to rewrite (dot
  // segments, tabs, backslashes): only then may a locale be put in front.
  const relative =
    url.origin === BASE &&
    link.startsWith("/") &&
    !/^\/[/\\]/.test(link) &&
    link.split(/[?#]/, 1)[0] === url.pathname;
  const [first, head, ...rest] = url.pathname.split("/");
  if (first !== "" || head === undefined) return null;
  let segments: string[];
  if (isLocale(head)) {
    segments = [head, ...rest];
  } else if (relative && locale !== undefined && isLocale(locale)) {
    segments = [locale, head, ...rest];
  } else {
    return null;
  }
  if (!segments.slice(1).every((segment) => SEGMENT.test(segment))) return null;
  const search = url.search && QUERY.test(url.search) ? url.search : "";
  return `/${segments.join("/")}${search}`;
}
