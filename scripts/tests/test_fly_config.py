# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""The hosted demo's Fly.io files (infra/fly, docs/hosted-demo.md), checked without an
account or Docker: TOML syntax, Dockerfile paths, targets and build args, what the build
context holds, and that each app's environment plus its secrets passes the real settings
validation in ``staging``, the environment the demo runs as."""

from __future__ import annotations

import fnmatch
import importlib.util
import re
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
FLY = ROOT / "infra" / "fly"
APPS = ("db", "clamav", "api", "worker", "web", "ops")


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gen = _load("gen_dev_env")


def config(app: str) -> dict[str, Any]:
    with (FLY / app / "fly.toml").open("rb") as handle:
        return tomllib.load(handle)


@pytest.mark.parametrize("app", APPS)
def test_every_app_parses_and_is_named_for_the_demo(app: str) -> None:
    data = config(app)
    assert data["app"] == f"pairly-demo-{app}"
    assert re.fullmatch(r"[a-z]{3}", data["primary_region"])
    text = (FLY / app / "fly.toml").read_text(encoding="utf-8")
    assert text.startswith(
        "# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial\n"
    )
    assert "—" not in text


def _stages(dockerfile: Path) -> list[str]:
    return re.findall(r"^FROM \S+ AS (\S+)$", dockerfile.read_text(encoding="utf-8"), re.M)


def _args(dockerfile: Path) -> set[str]:
    return set(re.findall(r"^ARG (\w+)", dockerfile.read_text(encoding="utf-8"), re.M))


@pytest.mark.parametrize("app", [a for a in APPS if a != "clamav"])
def test_dockerfile_target_and_build_args_exist(app: str) -> None:
    build = config(app)["build"]
    dockerfile = ROOT / build["dockerfile"]
    assert dockerfile.is_file(), dockerfile
    target = build.get("build-target")
    if target is not None:
        assert target in _stages(dockerfile)
    assert set(build.get("args", {})) <= _args(dockerfile)
    # The deploy command in the header names the same Dockerfile.
    header = (FLY / app / "fly.toml").read_text(encoding="utf-8")
    assert f"--dockerfile {build['dockerfile']}" in header


def test_python_image_default_target_is_still_the_runtime_image() -> None:
    dockerfile = ROOT / "infra" / "docker" / "python.Dockerfile"
    stages = _stages(dockerfile)
    assert stages[-1] == "service"
    assert "FROM runtime AS service" in dockerfile.read_text(encoding="utf-8")
    assert "demo" in stages
    assert {"PYTHON_IMAGE", "UV_IMAGE"} <= _args(dockerfile)
    # Every ${ARG} the file uses is declared.
    used = set(re.findall(r"\$\{(\w+)\}", dockerfile.read_text(encoding="utf-8")))
    assert used <= _args(dockerfile)


def _ignored(path: str) -> bool:
    patterns = [
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#") and not line.startswith("!")
    ]
    return any(fnmatch.fnmatch(path, p) or path.startswith(f"{p}/") for p in patterns)


@pytest.mark.parametrize(
    "dockerfile", ["infra/docker/python.Dockerfile", "infra/fly/db/Dockerfile"]
)
def test_copied_paths_are_in_the_build_context(dockerfile: str) -> None:
    text = (ROOT / dockerfile).read_text(encoding="utf-8")
    sources = [
        part
        for line in re.findall(r"^COPY (?!--from)(.+)$", text, re.M)
        for part in line.split()[:-1]
        if not part.startswith("--")
    ]
    assert sources
    for source in sources:
        assert (ROOT / source).exists(), source
        assert not _ignored(source), f"{source} is excluded by .dockerignore"


def test_postgres_init_creates_the_extension_and_the_three_roles() -> None:
    init = (ROOT / "infra" / "postgres" / "init" / "01-pairly-roles.sql").read_text()
    assert "CREATE EXTENSION IF NOT EXISTS vector" in init
    for role in ("pairly_owner", "pairly_app", "pairly_auth"):
        assert f"CREATE ROLE {role}" in init
    assert "01-pairly-roles.sql" in (FLY / "db" / "Dockerfile").read_text()


def test_private_apps_have_no_public_http_service() -> None:
    for app in ("db", "worker", "ops"):
        data = config(app)
        assert "http_service" not in data, app
        assert "services" not in data, app
    assert "http_service" not in config("clamav")
    assert "--no-public-ips" in (FLY / "clamav" / "fly.toml").read_text()


def test_api_worker_and_ops_share_one_environment() -> None:
    api, worker, ops = (config(a)["env"] for a in ("api", "worker", "ops"))
    assert api == worker
    assert {k: v for k, v in ops.items() if k in api} == api
    assert ops["PAIRLY_HOSTED_DEMO"] == "1"
    assert api["PAIRLY_ENV"] == config("web")["env"]["PAIRLY_ENV"] == "staging"
    assert api["PAIRLY_MODEL_MODE"] == "recorded"
    assert api["PAIRLY_DEMO_PUBLIC_SIGNIN"] == config("web")["env"]["PAIRLY_DEMO_PUBLIC_SIGNIN"]
    # The documents bucket is never the anchor bucket (the reset empties the first).
    assert api["PAIRLY_S3_BUCKET"] != api["PAIRLY_AUDIT_ANCHOR_BUCKET"]


def test_web_and_api_point_at_each_other() -> None:
    api, web = config("api")["env"], config("web")["env"]
    assert web["BETTER_AUTH_URL"] == api["PAIRLY_PUBLIC_WEB_URL"] == api["PAIRLY_AUTH_ISSUER"]
    assert api["PAIRLY_AUTH_JWKS_URL"] == f"{web['BETTER_AUTH_URL']}/api/auth/jwks"
    assert web["PAIRLY_API_INTERNAL_URL"] == api["PAIRLY_PUBLIC_API_URL"]
    assert web["PAIRLY_EMAIL_TRANSPORT"] == "fake"
    assert web["PAIRLY_DOCUMENT_ORIGIN"] == api["PAIRLY_S3_PUBLIC_ENDPOINT_URL"]


# ------------------------------------------------------------------ secrets


def test_fly_secrets_match_across_apps() -> None:
    secrets = gen.fly_secrets()
    assert set(secrets) == set(gen.FLY_APPS)
    db = secrets["db"]
    assert db["PAIRLY_APP_DB_PASSWORD"] in secrets["api"]["PAIRLY_DATABASE_URL"]
    assert db["PAIRLY_OWNER_DB_PASSWORD"] in secrets["ops"]["PAIRLY_DATABASE_URL_OWNER"]
    assert db["PAIRLY_AUTH_DB_PASSWORD"] in secrets["web"]["PAIRLY_AUTH_DATABASE_URL"]
    assert secrets["web"]["PAIRLY_AUTH_DATABASE_URL"] == secrets["ops"]["PAIRLY_AUTH_DATABASE_URL"]
    # Least privilege: the signing key only on the worker, the owner only on ops.
    assert "PAIRLY_AUDIT_SIGNING_KEY" not in secrets["api"]
    assert "PAIRLY_AUDIT_SIGNING_KEY" in secrets["worker"]
    assert all("PAIRLY_DATABASE_URL_OWNER" not in secrets[a] for a in ("api", "worker", "web"))
    assert all(db["PAIRLY_OWNER_DB_PASSWORD"] not in str(secrets[a]) for a in ("api", "web"))
    public = gen._public_from_seed(secrets["worker"]["PAIRLY_AUDIT_SIGNING_KEY"])
    assert secrets["api"]["PAIRLY_AUDIT_ANCHOR_PUBLIC_KEY"] == public
    # Two runs never share a value.
    again = gen.fly_secrets()
    assert again["db"]["POSTGRES_PASSWORD"] != db["POSTGRES_PASSWORD"]
    with pytest.raises(ValueError, match="prefix"):
        gen.fly_secrets("Bad_Prefix")


def test_write_fly_never_prints_or_overwrites(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "fly"
    assert gen.main(["--fly", str(out)]) == 0
    printed = capsys.readouterr().out
    for app in gen.FLY_APPS:
        path = out / f"{app}.env"
        assert path.stat().st_mode & 0o777 == 0o600
        for line in path.read_text().splitlines():
            key, _, value = line.partition("=")
            assert re.fullmatch(r"[A-Z][A-Z0-9_]+", key)
            assert value
            assert value not in printed
        assert f"fly secrets import --stage -a pairly-demo-{app} < {path}" in printed
    before = (out / "db.env").read_text()
    assert gen.main(["--fly", str(out)]) == 1
    assert (out / "db.env").read_text() == before


SUPABASE = gen.SupabaseTarget("abcdefghijklmnopqrst", "aws-0-eu-central-1.pooler.supabase.com")


def test_supabase_secrets_use_the_session_pooler() -> None:
    secrets = gen.fly_secrets(supabase=SUPABASE)
    # No db app: Supabase replaces it. The passwords are for the bootstrap only.
    assert set(secrets) == {"supabase", "api", "worker", "web", "ops"}
    pw = secrets["supabase"]
    urls = {
        "app": secrets["api"]["PAIRLY_DATABASE_URL"],
        "owner": secrets["ops"]["PAIRLY_DATABASE_URL_OWNER"],
        "auth": secrets["web"]["PAIRLY_AUTH_DATABASE_URL"],
    }
    for role, url in urls.items():
        assert url.split("@")[0].endswith(pw[f"PAIRLY_{role.upper()}_DB_PASSWORD"])
        assert f"pairly_{role}.abcdefghijklmnopqrst:" in url
        assert "@aws-0-eu-central-1.pooler.supabase.com:5432/postgres?sslmode=require" in url
        assert ":6543" not in url
    assert urls["auth"].endswith("&uselibpqcompat=true")
    assert secrets["api"]["PAIRLY_DATABASE_POOLER"] == "session"
    assert secrets["worker"]["PAIRLY_DATABASE_POOLER"] == "session"
    assert secrets["web"]["PAIRLY_AUTH_DB_SCHEMA"] == "pairly_auth"
    assert secrets["ops"]["PAIRLY_AUTH_DB_SCHEMA"] == "pairly_auth"
    assert secrets["ops"]["PAIRLY_HOSTED_DEMO_DATABASE"] == "postgres"
    assert all("PAIRLY_DATABASE_URL_OWNER" not in secrets[a] for a in ("api", "worker", "web"))
    for bad in (("Bad", SUPABASE.pooler_host), ("abc", "db.abc.supabase.co")):
        with pytest.raises(ValueError, match=r"Supabase|pooler"):
            gen.SupabaseTarget(*bad)


def test_write_fly_for_supabase(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "fly"
    args = ["--fly", str(out), "--supabase", SUPABASE.ref]
    args += ["--supabase-pooler", SUPABASE.pooler_host]
    assert gen.main(args) == 0
    printed = capsys.readouterr().out
    assert not (out / "db.env").exists()
    assert (out / "supabase.env").stat().st_mode & 0o777 == 0o600
    assert "infra/supabase/bootstrap.sql" in printed
    assert "pairly-demo-supabase" not in printed
    assert gen.main(["--fly", str(out), "--supabase", SUPABASE.ref]) == 1


def _environment(app: str, supabase: bool = False) -> dict[str, str]:
    env = dict(config(app)["env"])
    env.update(gen.fly_secrets(supabase=SUPABASE if supabase else None)[app])
    env.update(
        {
            "PAIRLY_REDIS_URL": "rediss://default:pw@fly-pairly-demo-redis.upstash.io:6379",
            "PAIRLY_S3_ACCESS_KEY": "tid_example",
            "PAIRLY_S3_SECRET_KEY": "tsec_example",
        }
    )
    return env


@pytest.mark.parametrize("supabase", [False, True], ids=["fly-db", "supabase"])
@pytest.mark.parametrize("app", ["api", "worker", "ops"])
def test_settings_validate_as_staging(
    app: str, supabase: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every settings class the services load accepts the demo environment in staging,
    with all the production checks on (https URLs, TLS, no fake scanner), with the
    database on the Fly Postgres app or on Supabase."""
    from pairly_core.audit_anchor.store import load_anchor_settings
    from pairly_core.config import Environment, load_settings
    from pairly_core.demo_signin import load_demo_signin_settings
    from pairly_core.ingestion.settings import load_ingestion_settings
    from pairly_core.llm.settings import load_llm_settings
    from pairly_core.notifications.settings import load_notification_settings
    from pairly_hire.antispam import CloudflareTurnstileVerifier, turnstile_from_env

    for key, value in _environment(app, supabase).items():
        monkeypatch.setenv(key, value)
    settings = load_settings()
    assert settings.database_pooler == ("session" if supabase else "none")
    assert settings.env is Environment.STAGING
    assert settings.is_production
    notify = load_notification_settings()
    assert notify.email_transport == "fake"
    ingestion = load_ingestion_settings()
    assert ingestion.virus_scanner == "clamd"
    assert ingestion.clamav_host
    assert load_anchor_settings().audit_anchor_lock_mode == "none"
    assert load_llm_settings().model_mode.value == "recorded"
    assert load_demo_signin_settings().enabled is True
    assert isinstance(turnstile_from_env(Environment.STAGING), CloudflareTurnstileVerifier)
    if app == "worker":
        from pairly_worker.config import load_worker_config

        assert load_worker_config().env is Environment.STAGING
