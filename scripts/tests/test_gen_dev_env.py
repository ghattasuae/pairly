# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Tests for scripts/gen_dev_env.py."""

from __future__ import annotations

import base64
import importlib.util
import secrets
import stat
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "gen_dev_env.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_dev_env", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_dev_env"] = module
    spec.loader.exec_module(module)
    return module


gen = _load()


def _parse(text: str) -> dict[str, str]:
    pairs = {}
    for line in text.splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            pairs[key] = value
    return pairs


def test_real_example_fills_every_secret_with_unique_values(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    assert gen.main(["--example", str(ROOT / ".env.example"), "--env", str(target)]) == 0
    values = _parse(target.read_text())
    secrets_ = [values[key] for key in gen.SECRET_GENERATORS]
    assert all(len(v) >= 32 for v in secrets_)
    assert len(set(secrets_)) == len(secrets_)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    # Non-secret keys keep their example values.
    assert values["POSTGRES_DB"] == "pairly"


def test_never_overwrites_existing_env(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("KEEP=me\nPAIRLY_API_KEY_PEPPER=\n")
    assert gen.main(["--example", str(ROOT / ".env.example"), "--env", str(target)]) == 0
    # The file names every appendable secret already (even empty): nothing changes.
    assert target.read_text() == "KEEP=me\nPAIRLY_API_KEY_PEPPER=\n"


def test_existing_env_gets_new_secrets_appended(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    original = "KEEP=me\n# PAIRLY_API_KEY_PEPPER=commented-out\nNO_NEWLINE=x"
    target.write_text(original)
    target.chmod(0o600)
    assert gen.main(["--example", str(ROOT / ".env.example"), "--env", str(target)]) == 0
    text = target.read_text()
    assert text.startswith(original + "\n")
    values = _parse(text)
    assert values["KEEP"] == "me"
    assert values["NO_NEWLINE"] == "x"
    assert len(values["PAIRLY_API_KEY_PEPPER"]) >= 43
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    # A second run appends nothing more.
    assert gen.main(["--example", str(ROOT / ".env.example"), "--env", str(target)]) == 0
    assert target.read_text() == text


def test_appendable_secrets_are_known_secrets() -> None:
    assert set(gen.APPEND_IF_MISSING) <= set(gen.SECRET_GENERATORS)


def test_example_with_a_secret_value_is_rejected() -> None:
    template = "\n".join(f"{key}=" for key in gen.SECRET_GENERATORS)
    template = template.replace("REDIS_PASSWORD=", "REDIS_PASSWORD=hunter2")
    with pytest.raises(ValueError, match="REDIS_PASSWORD"):
        gen.render(template)


def test_example_missing_a_secret_is_rejected() -> None:
    with pytest.raises(ValueError, match="missing secret keys"):
        gen.render("POSTGRES_PASSWORD=\n")


def test_audit_anchor_public_key_matches_the_signing_key(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    target = tmp_path / ".env"
    assert gen.main(["--example", str(ROOT / ".env.example"), "--env", str(target)]) == 0
    values = _parse(target.read_text())
    seed = base64.urlsafe_b64decode(values["PAIRLY_AUDIT_SIGNING_KEY"])
    public = Ed25519PrivateKey.from_private_bytes(seed).public_key()
    expected = public.public_bytes(Encoding.Raw, PublicFormat.Raw)
    assert base64.urlsafe_b64decode(values["PAIRLY_AUDIT_ANCHOR_PUBLIC_KEY"]) == expected


def test_pure_python_ed25519_matches_cryptography() -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    for seed in (bytes(32), bytes(range(32)), secrets.token_bytes(32)):
        key = Ed25519PrivateKey.from_private_bytes(seed).public_key()
        assert gen.ed25519_public_key(seed) == key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    with pytest.raises(ValueError, match="32 bytes"):
        gen.ed25519_public_key(b"short")


def test_derived_key_must_be_empty_in_the_example() -> None:
    template = "\n".join(f"{key}=" for key in gen.SECRET_GENERATORS)
    template += "\nPAIRLY_AUDIT_ANCHOR_PUBLIC_KEY=abc"
    with pytest.raises(ValueError, match="PAIRLY_AUDIT_ANCHOR_PUBLIC_KEY"):
        gen.render(template)
