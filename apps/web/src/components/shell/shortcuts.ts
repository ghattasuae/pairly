// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
// Copyright (c) 2026 Proofly Technology
"use client";

import { useEffect, useRef } from "react";

import type { NavItem } from "@/lib/nav";

/** Whether the platform shows the Command key (macOS, iOS) or Ctrl. */
export function isApplePlatform(): boolean {
  if (typeof navigator === "undefined") return false;
  return /mac|iphone|ipad|ipod/i.test(navigator.platform || navigator.userAgent);
}

/** True when the key event is typing into a field, where single-key shortcuts must not fire. */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

/**
 * Global keyboard shortcuts: Cmd/Ctrl+K toggles the command palette, and G then a letter
 * jumps to a section (G H home, G I inbox, ...). Only visible sections get a shortcut.
 */
export function useGlobalShortcuts({
  onTogglePalette,
  items,
  onNavigate,
}: {
  onTogglePalette: () => void;
  items: readonly NavItem[];
  onNavigate: (href: string) => void;
}) {
  const pendingG = useRef<number | null>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        onTogglePalette();
        return;
      }
      if (event.metaKey || event.ctrlKey || event.altKey || isTypingTarget(event.target)) return;
      const key = event.key.toUpperCase();
      if (pendingG.current !== null) {
        window.clearTimeout(pendingG.current);
        pendingG.current = null;
        const item = items.find((candidate) => candidate.shortcut === key);
        if (item) {
          event.preventDefault();
          onNavigate(item.href);
        }
        return;
      }
      if (key === "G") {
        pendingG.current = window.setTimeout(() => {
          pendingG.current = null;
        }, 1200);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      if (pendingG.current !== null) window.clearTimeout(pendingG.current);
    };
  }, [items, onNavigate, onTogglePalette]);
}
