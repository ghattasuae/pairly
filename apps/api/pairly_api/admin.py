# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""``pairly-admin``: operator commands that need the owner database role.

    uv run pairly-admin bootstrap-org --name "Acme" --slug acme --admin-email you@acme.test

``bootstrap-org`` creates an organisation and an admin invitation for it, and prints the
accept link (shown once; only a hash of its token is stored). It is meant for
self-hosters and seed scripts. The application role cannot create organisations, so this
connects as the owner role from ``PAIRLY_DATABASE_URL_OWNER``.

The Better Auth organization that mirrors it (same UUID) is created by the web app when
the admin accepts the invitation, through Better Auth's own adapter, so this command
never writes to the ``auth`` schema. See ``apps/web/README.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from urllib.parse import urlencode, urlsplit

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from pairly_core.config import to_asyncpg_url
from pairly_core.db import create_sessionmaker
from pairly_core.errors import PairlyError
from pairly_core.invitations import INVITATION_TTL, BootstrappedOrg, bootstrap_organisation

OWNER_URL_ENV = "PAIRLY_DATABASE_URL_OWNER"
WEB_URL_ENV = "PAIRLY_PUBLIC_WEB_URL"
LOCALES = ("en", "ar")


def accept_link(web_url: str, locale: str, token: str) -> str:
    """The web app's invite page, with the token in the URL fragment.

    Browsers never send the fragment to a server, so the token stays out of access logs
    and Referer headers (the same pattern as the magic link).
    """
    return f"{web_url.rstrip('/')}/{locale}/invite#{urlencode({'token': token})}"


async def bootstrap(owner_url: str, *, name: str, slug: str, admin_email: str) -> BootstrappedOrg:
    engine = create_async_engine(to_asyncpg_url(owner_url), pool_size=1, max_overflow=0)
    try:
        session = create_sessionmaker(engine)()
        async with session:
            result = await bootstrap_organisation(
                session, name=name, slug=slug, admin_email=admin_email
            )
            await session.commit()
            return result
    finally:
        await engine.dispose()


def _web_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise argparse.ArgumentTypeError("must be an absolute http(s) URL")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pairly-admin", description="Pairly operator commands (owner database role)."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    boot = commands.add_parser(
        "bootstrap-org", help="create an organisation and print its admin invite link"
    )
    boot.add_argument("--name", required=True, help="display name, for example 'Acme Ltd'")
    boot.add_argument("--slug", required=True, help="2 to 63 characters: a-z, 0-9 and '-'")
    boot.add_argument("--admin-email", required=True, help="who receives the admin invitation")
    boot.add_argument(
        "--web-url",
        type=_web_url,
        default=os.environ.get(WEB_URL_ENV) or "http://localhost:3100",
        help=f"public web app URL for the link (default: ${WEB_URL_ENV} or localhost:3100)",
    )
    boot.add_argument("--locale", choices=LOCALES, default="en", help="language of the link")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    owner_url = os.environ.get(OWNER_URL_ENV)
    if not owner_url:
        print(f"pairly-admin: {OWNER_URL_ENV} is not set (owner role URL)", file=sys.stderr)
        return 2
    try:
        result = asyncio.run(
            bootstrap(owner_url, name=args.name, slug=args.slug, admin_email=args.admin_email)
        )
    except PairlyError as exc:
        print(f"pairly-admin: {exc.detail}", file=sys.stderr)
        return 2
    except IntegrityError:
        print("pairly-admin: an organisation with this slug already exists", file=sys.stderr)
        return 1
    days = INVITATION_TTL.days
    print(f"Organisation created: {result.org.name} ({result.org.slug})")
    print(f"  id: {result.org.id}")
    print(f"Admin invitation for {args.admin_email.strip().lower()}, valid for {days} days.")
    print("Open this link to accept it (shown once, keep it private):")
    print(f"  {accept_link(args.web_url, args.locale, result.token)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
