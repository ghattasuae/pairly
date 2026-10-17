// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type * as React from "react";

import { loadNotifications, markAllNotificationsRead, markNotificationRead } from "@/lib/actions/notifications";
import type { Notification } from "@/lib/api/types";

type Listener = (notification: Notification) => void;

export interface NotificationsState {
  items: Notification[];
  unread: number;
  /** False until the first list arrives. */
  loaded: boolean;
  /** Whether the live stream is connected right now. */
  live: boolean;
  markRead: (id: string) => Promise<void>;
  markAllRead: () => Promise<void>;
  refresh: () => Promise<void>;
  /** Called for every new notification from the live stream (the inbox refreshes on approvals). */
  subscribe: (listener: Listener) => () => void;
}

const EMPTY: NotificationsState = {
  items: [],
  unread: 0,
  loaded: false,
  live: false,
  markRead: async () => {},
  markAllRead: async () => {},
  refresh: async () => {},
  subscribe: () => () => {},
};

const NotificationsContext = createContext<NotificationsState>(EMPTY);

export function useNotifications(): NotificationsState {
  return useContext(NotificationsContext);
}

export const STREAM_PATH = "/api/notifications/stream";
const defaultOpenStream = (url: string) => new EventSource(url);
const MAX_BACKOFF_MS = 60_000;

function isNotification(value: unknown): value is Notification {
  const v = value as Partial<Notification> | null;
  return !!v && typeof v.id === "string" && typeof v.title === "string" && typeof v.created_at === "string";
}

/**
 * In-app notifications for the signed-in user: the newest items, the unread count, and
 * a live connection to the event stream (`/api/notifications/stream`, same origin). The
 * stream closes every few minutes when its token expires; the browser reconnects with
 * `Last-Event-ID`, and after a failure this retries with a growing pause.
 */
export function NotificationsProvider({
  enabled,
  children,
  load = loadNotifications,
  openStream = defaultOpenStream,
}: {
  /** Off for people the API does not know yet (nothing to stream). */
  enabled: boolean;
  children: React.ReactNode;
  /** Overrides for tests and stories. */
  load?: typeof loadNotifications;
  openStream?: (url: string) => EventSource;
}) {
  const [items, setItems] = useState<Notification[]>([]);
  const [unread, setUnread] = useState(0);
  const [loaded, setLoaded] = useState(false);
  const [live, setLive] = useState(false);
  const listeners = useRef(new Set<Listener>());

  const refresh = useCallback(async () => {
    try {
      const result = await load();
      if (result.ok) {
        setItems(result.items);
        setUnread(result.unread);
      }
    } catch {
      /* the bell keeps what it has */
    }
    setLoaded(true);
  }, [load]);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    load()
      .then((result) => {
        if (cancelled || !result.ok) return;
        setItems(result.items);
        setUnread(result.unread);
      })
      .catch(() => {
        /* the bell stays empty; the stream still adds new items */
      })
      .finally(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, load]);

  useEffect(() => {
    if (!enabled || typeof window === "undefined") return;
    let source: EventSource | null = null;
    let timer: number | null = null;
    let backoff = 2000;
    let stopped = false;

    const connect = () => {
      if (stopped) return;
      source = openStream(STREAM_PATH);
      source.onopen = () => {
        backoff = 2000;
        setLive(true);
      };
      source.addEventListener("notification", (event) => {
        let data: unknown;
        try {
          data = JSON.parse((event as MessageEvent<string>).data);
        } catch {
          return;
        }
        if (!isNotification(data)) return;
        setItems((all) => (all.some((n) => n.id === data.id) ? all : [data, ...all].slice(0, 50)));
        if (!data.read_at) setUnread((n) => n + 1);
        for (const listener of listeners.current) listener(data);
      });
      source.addEventListener("end", (event) => {
        let reason = "reconnect";
        try {
          reason = (JSON.parse((event as MessageEvent<string>).data) as { reason?: string }).reason ?? reason;
        } catch {
          /* keep the default */
        }
        // "reconnect": the server is about to close the stream (its token expires). The
        // browser reconnects by itself and sends Last-Event-ID, so nothing is missed.
        if (reason === "access_revoked") {
          stopped = true;
          source?.close();
          setLive(false);
        }
      });
      source.onerror = () => {
        setLive(false);
        // CONNECTING: the browser retries by itself. CLOSED: an error status; retry later.
        if (source?.readyState === EventSource.CLOSED) {
          source.close();
          timer = window.setTimeout(connect, backoff);
          backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
        }
      };
    };

    connect();
    return () => {
      stopped = true;
      if (timer !== null) window.clearTimeout(timer);
      source?.close();
      setLive(false);
    };
  }, [enabled, openStream]);

  const markRead = useCallback(
    async (id: string) => {
      const target = items.find((n) => n.id === id);
      if (!target || target.read_at) return;
      const now = new Date().toISOString();
      setItems((all) => all.map((n) => (n.id === id ? { ...n, read_at: now } : n)));
      setUnread((n) => Math.max(0, n - 1));
      try {
        await markNotificationRead(id);
      } catch {
        /* the next refresh corrects the count */
      }
    },
    [items],
  );

  const markAllRead = useCallback(async () => {
    const now = new Date().toISOString();
    setItems((all) => all.map((n) => (n.read_at ? n : { ...n, read_at: now })));
    setUnread(0);
    try {
      await markAllNotificationsRead();
    } catch {
      /* the next refresh corrects the count */
    }
  }, []);

  const subscribe = useCallback((listener: Listener) => {
    listeners.current.add(listener);
    return () => {
      listeners.current.delete(listener);
    };
  }, []);

  const value = useMemo(
    () => ({ items, unread, loaded, live, markRead, markAllRead, refresh, subscribe }),
    [items, unread, loaded, live, markRead, markAllRead, refresh, subscribe],
  );
  return <NotificationsContext.Provider value={value}>{children}</NotificationsContext.Provider>;
}
