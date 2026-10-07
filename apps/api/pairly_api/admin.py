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

``recordings export`` and ``recordings import`` move stored model responses between
databases as a bundle file (see ``pairly_core.llm.bundle``), so seed data can ship the
responses ``make demo`` replays offline in ``recorded`` mode:

    uv run pairly-admin recordings export --org <uuid> --out recordings.json [--task t]
    uv run pairly-admin recordings import --org <uuid> --in recordings.json
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from pairly_core.config import to_asyncpg_url
from pairly_core.db import create_sessionmaker, set_tenant
from pairly_core.errors import PairlyError
from pairly_core.invitations import INVITATION_TTL, BootstrappedOrg, bootstrap_organisation
from pairly_core.llm.bundle import (
    BundleError,
    ImportReport,
    dumps_bundle,
    export_recordings,
    import_recordings,
    parse_bundle,
)

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


async def export_bundle(owner_url: str, org_id: uuid.UUID, task: str | None) -> str:
    engine = create_async_engine(to_asyncpg_url(owner_url), pool_size=1, max_overflow=0)
    try:
        session = create_sessionmaker(engine)()
        set_tenant(session, org_id, None)
        async with session, session.begin():
            return dumps_bundle(await export_recordings(session, task=task))
    finally:
        await engine.dispose()


async def import_bundle(owner_url: str, org_id: uuid.UUID, text: str) -> ImportReport:
    entries = parse_bundle(text)
    engine = create_async_engine(to_asyncpg_url(owner_url), pool_size=1, max_overflow=0)
    try:
        session = create_sessionmaker(engine)()
        set_tenant(session, org_id, None)
        async with session, session.begin():
            return await import_recordings(session, org_id, entries)
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

    rec = commands.add_parser("recordings", help="export or import recorded model responses")
    rec_commands = rec.add_subparsers(dest="recordings_command", required=True)
    export = rec_commands.add_parser("export", help="write an org's recordings to a bundle file")
    export.add_argument("--org", required=True, type=uuid.UUID, help="organisation id")
    export.add_argument("--out", required=True, type=Path, help="bundle file to write")
    export.add_argument("--task", help="only this task")
    imp = rec_commands.add_parser("import", help="load a bundle file into an org")
    imp.add_argument("--org", required=True, type=uuid.UUID, help="organisation id")
    imp.add_argument("--in", dest="source", required=True, type=Path, help="bundle file to read")
    return parser


def _recordings(args: argparse.Namespace, owner_url: str) -> int:
    if args.recordings_command == "export":
        text = asyncio.run(export_bundle(owner_url, args.org, args.task))
        args.out.write_text(text, encoding="utf-8")
        print(f"Wrote {args.out}")
        return 0
    try:
        text = args.source.read_text(encoding="utf-8")
        report = asyncio.run(import_bundle(owner_url, args.org, text))
    except (OSError, BundleError) as exc:
        print(f"pairly-admin: {exc}", file=sys.stderr)
        return 2
    print(f"Imported {report.imported} recordings, skipped {report.skipped} already present.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    owner_url = os.environ.get(OWNER_URL_ENV)
    if not owner_url:
        print(f"pairly-admin: {OWNER_URL_ENV} is not set (owner role URL)", file=sys.stderr)
        return 2
    if args.command == "recordings":
        return _recordings(args, owner_url)
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
