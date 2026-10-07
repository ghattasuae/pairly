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

``inbound rotate`` generates a new secret token for an organisation's inbound email
address in one workflow and prints the full address once (only the token's hash is
stored). Mail to the previous address is dropped from then on:

    uv run pairly-admin inbound rotate --org <uuid> --workflow pay

``seed`` runs a workflow plugin's demo seed (``demo_seeds`` on the plugin, see
``pairly_core.plugins``) in one organisation, found by its slug, in one transaction.
Seeds are idempotent; invitations they create are printed once as accept links:

    uv run pairly-admin seed expense-demo --org acme

``crypto rotate`` re-wraps every encrypted field's data key with the current KEK
(``PAIRLY_KEK_CURRENT``), in batches, one audited transaction per batch. It is
resumable: rows already on the current KEK are skipped. ``--dry-run`` only counts what
is left. See ``pairly_core.crypto.rotation`` for the full procedure:

    uv run pairly-admin crypto rotate [--org <uuid>] [--batch-size 500] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import pairly_core.models  # noqa: F401  (registers every core table, encrypted ones included)
from pairly_core.audit import ActorType
from pairly_core.config import to_asyncpg_url
from pairly_core.crypto.keys import CryptoConfigError, init_crypto
from pairly_core.crypto.rotation import DEFAULT_BATCH, RotationReport, rotate_keys
from pairly_core.db import Base, create_sessionmaker, set_tenant
from pairly_core.errors import PairlyError
from pairly_core.ingestion.addresses import WORKFLOW_RE, inbound_address, rotate_inbound_token
from pairly_core.invitations import INVITATION_TTL, BootstrappedOrg, bootstrap_organisation
from pairly_core.llm.bundle import (
    BundleError,
    ImportReport,
    dumps_bundle,
    export_recordings,
    import_recordings,
    parse_bundle,
)
from pairly_core.models import Organisation
from pairly_core.plugins import SeedContext, SeedResult, demo_seeds, discover_plugins
from pairly_core.rbac import build_registry

OWNER_URL_ENV = "PAIRLY_DATABASE_URL_OWNER"
INBOUND_DOMAIN_ENV = "PAIRLY_INBOUND_EMAIL_DOMAIN"
ADMIN_ACTOR = "pairly.admin"
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


@dataclass(frozen=True, slots=True)
class RotatedInbound:
    org_slug: str
    workflow: str
    token: str = field(repr=False)


async def rotate_inbound(owner_url: str, org_id: uuid.UUID, workflow: str) -> RotatedInbound:
    """New inbound token for ``org_id`` and ``workflow``; raises ``LookupError`` if no org."""
    engine = create_async_engine(to_asyncpg_url(owner_url), pool_size=1, max_overflow=0)
    try:
        session = create_sessionmaker(engine)()
        set_tenant(session, org_id, None)
        async with session, session.begin():
            slug = await session.scalar(select(Organisation.slug).where(Organisation.id == org_id))
            if slug is None:
                raise LookupError("organisation not found")
            token = await rotate_inbound_token(
                session, workflow=workflow, actor_type=ActorType.SYSTEM, actor_id=ADMIN_ACTOR
            )
            return RotatedInbound(slug, workflow, token)
    finally:
        await engine.dispose()


async def rotate_crypto(
    owner_url: str,
    *,
    org_ids: Sequence[uuid.UUID] | None,
    batch_size: int,
    dry_run: bool,
) -> RotationReport:
    engine = create_async_engine(to_asyncpg_url(owner_url), pool_size=1, max_overflow=0)
    try:
        return await rotate_keys(
            create_sessionmaker(engine),
            Base.metadata,
            org_ids=org_ids,
            batch_size=batch_size,
            dry_run=dry_run,
            actor_id=ADMIN_ACTOR,
        )
    finally:
        await engine.dispose()


async def org_id_by_slug(session: AsyncSession, slug: str) -> uuid.UUID | None:
    """The organisation with ``slug``, through ``admin_org_id_by_slug`` (migration 0016).

    ``organisations`` forces Row Level Security, so even the owner sees no row without a
    tenant. The function is owner-only (EXECUTE revoked from everyone else) and returns
    the id alone, or NULL.
    """
    found = await session.scalar(
        text("SELECT admin_org_id_by_slug(:slug)"), {"slug": slug.strip().lower()}
    )
    return found if isinstance(found, uuid.UUID) else None


async def run_seed(owner_url: str, name: str, slug: str, web_url: str) -> SeedResult:
    """Run the demo seed ``name`` in the organisation ``slug`` (one transaction)."""
    plugins = list(discover_plugins().values())
    seeds = demo_seeds(plugins)
    seed = seeds.get(name)
    if seed is None:
        known = ", ".join(sorted(seeds)) or "none"
        raise LookupError(f"unknown seed {name!r} (known: {known})")
    registry = build_registry(plugins)
    engine = create_async_engine(to_asyncpg_url(owner_url), pool_size=1, max_overflow=0)
    try:
        session = create_sessionmaker(engine)()
        async with session:
            async with session.begin():
                org_id = await org_id_by_slug(session, slug)
            if org_id is None:
                raise LookupError(f"no organisation with slug {slug!r}")
            set_tenant(session, org_id, None)
            async with session.begin():
                return await seed(
                    session,
                    SeedContext(org_id=org_id, org_slug=slug, registry=registry, web_url=web_url),
                )
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

    inbound = commands.add_parser("inbound", help="manage inbound email addresses")
    inbound_commands = inbound.add_subparsers(dest="inbound_command", required=True)
    rotate = inbound_commands.add_parser(
        "rotate", help="new secret token for a workflow's inbound address (prints it once)"
    )
    rotate.add_argument("--org", required=True, type=uuid.UUID, help="organisation id")
    rotate.add_argument("--workflow", required=True, help="workflow id, for example 'pay'")

    seed = commands.add_parser("seed", help="run a workflow's demo seed in an organisation")
    seed.add_argument("seed_name", metavar="name", help="seed name, for example expense-demo")
    seed.add_argument("--org", dest="org_slug", required=True, help="organisation slug")
    seed.add_argument(
        "--web-url",
        type=_web_url,
        default=os.environ.get(WEB_URL_ENV) or "http://localhost:3100",
        help=f"public web app URL for invite links (default: ${WEB_URL_ENV} or localhost:3100)",
    )
    seed.add_argument("--locale", choices=LOCALES, default="en", help="language of the links")

    crypto = commands.add_parser("crypto", help="application-level encryption keys")
    crypto_commands = crypto.add_subparsers(dest="crypto_command", required=True)
    crypto_rotate = crypto_commands.add_parser(
        "rotate", help="re-wrap data keys with the current KEK (resumable, audited)"
    )
    crypto_rotate.add_argument(
        "--org", action="append", type=uuid.UUID, help="only this organisation (repeatable)"
    )
    crypto_rotate.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    crypto_rotate.add_argument(
        "--dry-run", action="store_true", help="count rows still on an old KEK, change nothing"
    )
    return parser


def _inbound(args: argparse.Namespace, owner_url: str) -> int:
    workflow = args.workflow.strip().lower()
    known = set(discover_plugins())
    if not WORKFLOW_RE.fullmatch(workflow) or workflow not in known:
        names = ", ".join(sorted(known)) or "none"
        print(f"pairly-admin: unknown workflow {workflow!r} (known: {names})", file=sys.stderr)
        return 2
    try:
        rotated = asyncio.run(rotate_inbound(owner_url, args.org, workflow))
    except LookupError as exc:
        print(f"pairly-admin: {exc}", file=sys.stderr)
        return 1
    domain = os.environ.get(INBOUND_DOMAIN_ENV, "").strip()
    print(f"New inbound address for {rotated.org_slug} / {workflow}.")
    print("Shown once, keep it private. The previous address no longer receives mail:")
    if domain:
        print(f"  {inbound_address(workflow, rotated.org_slug, rotated.token, domain)}")
    else:
        local = f"{workflow}+{rotated.org_slug}.{rotated.token}"
        print(f"  {local}@<inbound domain>   (${INBOUND_DOMAIN_ENV} is not set)")
    return 0


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


def _seed(args: argparse.Namespace, owner_url: str) -> int:
    try:
        init_crypto()
        result = asyncio.run(run_seed(owner_url, args.seed_name, args.org_slug, args.web_url))
    except (LookupError, CryptoConfigError, BundleError, OSError) as exc:
        print(f"pairly-admin: {exc}", file=sys.stderr)
        return 2
    except PairlyError as exc:
        print(f"pairly-admin: {exc.detail}", file=sys.stderr)
        return 2
    print(f"Seed {args.seed_name} ran in {args.org_slug}.")
    for line in result.lines:
        print(f"  {line}")
    if result.invitations:
        print("Invitations (each link is shown once, keep them private):")
    for invite in result.invitations:
        scope = f" ({invite.workflow} only)" if invite.workflow else ""
        print(f"  {invite.email}, {invite.role}{scope}:")
        print(f"    {accept_link(args.web_url, args.locale, invite.token)}")
    return 0


def _crypto(args: argparse.Namespace, owner_url: str) -> int:
    try:
        ring = init_crypto()
    except CryptoConfigError as exc:
        print(f"pairly-admin: {exc}", file=sys.stderr)
        return 2
    if not 1 <= args.batch_size <= 10_000:
        print("pairly-admin: --batch-size must be 1..10000", file=sys.stderr)
        return 2
    report = asyncio.run(
        rotate_crypto(owner_url, org_ids=args.org, batch_size=args.batch_size, dry_run=args.dry_run)
    )
    print(f"Current KEK version: {ring.current}. Organisations: {report.orgs}.")
    if args.dry_run:
        if not report.pending:
            print("Nothing to re-wrap: every encrypted field uses the current KEK.")
        for key, count in sorted(report.pending.items()):
            print(f"  {key}: {count} rows on an old KEK")
        return 0
    for key, count in sorted(report.rewrapped.items()):
        print(f"  {key}: {count} rows re-wrapped")
    for key, count in sorted(report.failed.items()):
        print(f"  {key}: {count} rows could not be decrypted (left unchanged)", file=sys.stderr)
    print(f"Re-wrapped {report.total_rewrapped} rows, {report.total_failed} failed.")
    return 1 if report.total_failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    owner_url = os.environ.get(OWNER_URL_ENV)
    if not owner_url:
        print(f"pairly-admin: {OWNER_URL_ENV} is not set (owner role URL)", file=sys.stderr)
        return 2
    if args.command == "recordings":
        return _recordings(args, owner_url)
    if args.command == "inbound":
        return _inbound(args, owner_url)
    if args.command == "crypto":
        return _crypto(args, owner_url)
    if args.command == "seed":
        return _seed(args, owner_url)
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
