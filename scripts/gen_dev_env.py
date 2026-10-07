#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Create a local `.env` from `.env.example` with strong random secrets.

Usage:
    uv run python scripts/gen_dev_env.py            # writes .env at the repo root if missing
    uv run python scripts/gen_dev_env.py --env PATH # custom target (used by tests)

Rules:
- An existing `.env` is never overwritten or changed. The script exits 0 and says so.
- Every secret listed in SECRET_GENERATORS gets a fresh value from `secrets`.
- Every key listed in DERIVED is computed from one of those secrets (the audit anchor
  public key is derived from the audit signing key, so the pair always matches).
- Every other key keeps the value from `.env.example` (optional keys stay empty).
- The file is created with mode 0600 and with O_EXCL, so a race cannot clobber a file.

Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
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


# ---------------------------------------------------------------- Ed25519 public key
# RFC 8032 section 5.1.5 (public key from a 32-byte seed), standard library only. Used
# for the audit anchor key pair; tested against `cryptography` in scripts/tests.
_P = 2**255 - 19
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _recover_x(y: int) -> int:
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    return _P - x if x % 2 else x


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _recover_x(_GY)
_BASE = (_GX, _GY, 1, _GX * _GY % _P)


def _add(p: tuple[int, int, int, int], q: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def ed25519_public_key(seed: bytes) -> bytes:
    """The raw 32-byte Ed25519 public key of a 32-byte private seed."""
    if len(seed) != 32:
        raise ValueError("an Ed25519 seed is 32 bytes")
    digest = hashlib.sha512(seed).digest()
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    point, addend = (0, 1, 1, 0), _BASE
    while scalar:
        if scalar & 1:
            point = _add(point, addend)
        addend = _add(addend, addend)
        scalar >>= 1
    z_inv = pow(point[2], _P - 2, _P)
    x, y = point[0] * z_inv % _P, point[1] * z_inv % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _public_from_seed(seed_b64: str) -> str:
    seed = base64.urlsafe_b64decode(seed_b64)
    return base64.urlsafe_b64encode(ed25519_public_key(seed)).decode("ascii")


# Hex values are URL safe, so they can go straight into connection strings.
SECRET_GENERATORS: dict[str, Callable[[], str]] = {
    # First key encryption key (WP1.14). PAIRLY_KEK_2 stays empty until a rotation.
    "PAIRLY_KEK_1": _b64key,
    "PAIRLY_BLIND_INDEX_KEY": _b64key,
    "PAIRLY_SIGNED_LINK_SECRET": _hex(32),
    # Ed25519 seed that signs the nightly audit chain anchors (worker only, WP1.10b).
    "PAIRLY_AUDIT_SIGNING_KEY": _b64key,
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


# Keys computed from a generated secret: name -> (source secret, derivation).
DERIVED: dict[str, tuple[str, Callable[[str], str]]] = {
    "PAIRLY_AUDIT_ANCHOR_PUBLIC_KEY": ("PAIRLY_AUDIT_SIGNING_KEY", _public_from_seed),
}


def render(template: str) -> str:
    """Return the .env text for a .env.example template, with secrets filled in.

    Raises ValueError when a known secret is missing from the template or already
    carries a value there (the example must never hold a secret).
    """
    seen: set[str] = set()
    out: list[str] = []
    values = {key: make() for key, make in SECRET_GENERATORS.items()}
    for line in template.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key in SECRET_GENERATORS or key in DERIVED:
            if value.strip():
                raise ValueError(f".env.example must leave {key} empty")
            if key in DERIVED:
                source, derive = DERIVED[key]
                out.append(f"{key}={derive(values[source])}")
                continue
            seen.add(key)
            out.append(f"{key}={values[key]}")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--example", type=Path, default=ROOT / ".env.example")
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    args = parser.parse_args(argv)

    if args.env.exists():
        print(f"{args.env} exists, leaving it unchanged.")
        return 0
    try:
        text = render(args.example.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not write_new(args.env, text):
        print(f"{args.env} exists, leaving it unchanged.")
        return 0
    print(f"created {args.env} with {len(SECRET_GENERATORS)} generated secrets (mode 0600).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
