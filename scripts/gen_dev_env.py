#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Create a local `.env` from `.env.example` with strong random secrets.

Usage:
    uv run python scripts/gen_dev_env.py            # writes .env at the repo root if missing
    uv run python scripts/gen_dev_env.py --env PATH # custom target (used by tests)

Rules:
- An existing `.env` is never overwritten: no line in it is changed or removed. The only
  change ever made to one is appending a secret added after it was created
  (APPEND_IF_MISSING) when the file does not name that key at all.
- Every secret listed in SECRET_GENERATORS gets a fresh value from `secrets`.
- Every other key keeps the value from `.env.example` (optional keys stay empty).
- The file is created with mode 0600 and with O_EXCL, so a race cannot clobber a file.

Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _hex(nbytes: int) -> Callable[[], str]:
    return lambda: secrets.token_hex(nbytes)


def _b64key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def _prefixed(prefix: str) -> Callable[[], str]:
    return lambda: f"{prefix}{secrets.token_hex(16)}"


# Hex values are URL safe, so they can go straight into connection strings.
SECRET_GENERATORS: dict[str, Callable[[], str]] = {
    # First key encryption key (WP1.14). PAIRLY_KEK_2 stays empty until a rotation.
    "PAIRLY_KEK_1": _b64key,
    "PAIRLY_BLIND_INDEX_KEY": _b64key,
    # HMAC pepper for API key hashes (WP1.11).
    "PAIRLY_API_KEY_PEPPER": _b64key,
    "PAIRLY_SIGNED_LINK_SECRET": _hex(32),
    "POSTGRES_PASSWORD": _hex(24),
    "PAIRLY_OWNER_DB_PASSWORD": _hex(24),
    "PAIRLY_APP_DB_PASSWORD": _hex(24),
    "PAIRLY_AUTH_DB_PASSWORD": _hex(24),
    "REDIS_PASSWORD": _hex(24),
    "MINIO_ROOT_PASSWORD": _hex(24),
    "PAIRLY_S3_SECRET_KEY": _hex(24),
    "BETTER_AUTH_SECRET": _hex(32),
    "LANGFUSE_DB_PASSWORD": _hex(24),
    "LANGFUSE_NEXTAUTH_SECRET": _hex(32),
    "LANGFUSE_SALT": _hex(32),
    "LANGFUSE_ENCRYPTION_KEY": _hex(32),
    "CLICKHOUSE_PASSWORD": _hex(24),
    "LANGFUSE_S3_SECRET_KEY": _hex(24),
    "LANGFUSE_PUBLIC_KEY": _prefixed("pk-lf-"),
    "LANGFUSE_SECRET_KEY": _prefixed("sk-lf-"),
    "LANGFUSE_INIT_USER_PASSWORD": _hex(16),
}

# Secrets introduced after the first release of this script. An existing .env that does
# not name one gets a fresh value appended (never a changed line), so `make env` brings
# an older checkout up to date without touching the secrets it already has.
APPEND_IF_MISSING: tuple[str, ...] = ("PAIRLY_API_KEY_PEPPER",)


def env_keys(text: str) -> set[str]:
    """The keys a .env text assigns (commented out lines do not count)."""
    keys: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            keys.add(stripped.partition("=")[0].strip())
    return keys


def append_missing(path: Path) -> list[str]:
    """Append each APPEND_IF_MISSING secret that ``path`` does not name. Returns them.

    Opens with O_APPEND (and O_NOFOLLOW), so existing bytes are never rewritten.
    """
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW)
    with os.fdopen(fd, "r+", encoding="utf-8") as handle:
        current = handle.read()
        missing = [key for key in APPEND_IF_MISSING if key not in env_keys(current)]
        if not missing:
            return []
        lines = [] if current == "" or current.endswith("\n") else [""]
        lines.append("# Added by scripts/gen_dev_env.py (new secret, existing lines unchanged).")
        lines.extend(f"{key}={SECRET_GENERATORS[key]()}" for key in missing)
        handle.write("\n".join(lines) + "\n")
    return missing


def render(template: str) -> str:
    """Return the .env text for a .env.example template, with secrets filled in.

    Raises ValueError when a known secret is missing from the template or already
    carries a value there (the example must never hold a secret).
    """
    seen: set[str] = set()
    out: list[str] = []
    for line in template.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key in SECRET_GENERATORS:
            if value.strip():
                raise ValueError(f".env.example must leave {key} empty")
            seen.add(key)
            out.append(f"{key}={SECRET_GENERATORS[key]()}")
        else:
            out.append(line)
    missing = sorted(set(SECRET_GENERATORS) - seen)
    if missing:
        raise ValueError(f".env.example is missing secret keys: {', '.join(missing)}")
    header = [
        "# Generated by scripts/gen_dev_env.py for LOCAL DEVELOPMENT ONLY.",
        "# Never commit this file. Delete it and rerun the script to rotate every secret",
        "# (and run `make down` with volumes removed, since Postgres keeps old passwords).",
    ]
    return "\n".join([*header, *out]) + "\n"


def write_new(path: Path, text: str) -> bool:
    """Create path with text and mode 0600. Returns False if it already exists."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return True


def _update_existing(path: Path) -> int:
    try:
        added = append_missing(path)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if added:
        print(f"{path} exists; appended new secrets: {', '.join(added)} (other lines unchanged).")
    else:
        print(f"{path} exists, leaving it unchanged.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--example", type=Path, default=ROOT / ".env.example")
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    args = parser.parse_args(argv)

    if args.env.exists():
        return _update_existing(args.env)
    try:
        text = render(args.example.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not write_new(args.env, text):
        return _update_existing(args.env)
    print(f"created {args.env} with {len(SECRET_GENERATORS)} generated secrets (mode 0600).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
