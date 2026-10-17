# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
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


def _run(target: Path, *flags: str) -> int:
    return int(gen.main(["--example", str(ROOT / ".env.example"), "--env", str(target), *flags]))


def _public_of(seed_b64: str) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = Ed25519PrivateKey.from_private_bytes(base64.urlsafe_b64decode(seed_b64))
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _full_env() -> str:
    """An existing .env holding every required key already."""
    seed = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    lines = [f"{key}=existing-{key.lower()}" for key in gen.SECRET_GENERATORS]
    lines = [line for line in lines if not line.startswith("PAIRLY_AUDIT_SIGNING_KEY=")]
    lines.append(f"PAIRLY_AUDIT_SIGNING_KEY={seed}")
    lines.append(f"PAIRLY_AUDIT_ANCHOR_PUBLIC_KEY={gen._public_from_seed(seed)}")
    return "\n".join(lines) + "\n"


def test_never_overwrites_existing_env_without_the_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / ".env"
    target.write_text("KEEP=me\n")
    assert _run(target) == 0
    assert target.read_text() == "KEEP=me\n"
    # It names the keys the file lacks (never their values).
    out = capsys.readouterr().out
    assert "PAIRLY_API_KEY_PEPPER" in out
    assert "--append-missing" in out


def test_append_missing_adds_every_missing_key_and_changes_no_line(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    # An older .env: everything but the Wave C keys, one commented out, no final newline.
    lines = [
        line
        for line in _full_env().splitlines()
        if not line.startswith(
            ("PAIRLY_API_KEY_PEPPER=", "PAIRLY_AUDIT_SIGNING_KEY=", "PAIRLY_AUDIT_ANCHOR_")
        )
    ]
    original = "\n".join(["KEEP=me", "# PAIRLY_API_KEY_PEPPER=commented-out", *lines])
    target.write_text(original)
    target.chmod(0o600)
    assert _run(target, "--append-missing") == 0
    text = target.read_text()
    assert text.startswith(original + "\n")
    added = _parse(text[len(original) :])
    assert set(added) == {
        "PAIRLY_API_KEY_PEPPER",
        "PAIRLY_AUDIT_SIGNING_KEY",
        "PAIRLY_AUDIT_ANCHOR_PUBLIC_KEY",
    }
    assert len(added["PAIRLY_API_KEY_PEPPER"]) >= 43
    # Appended together, the pair matches.
    public = base64.urlsafe_b64decode(added["PAIRLY_AUDIT_ANCHOR_PUBLIC_KEY"])
    assert public == _public_of(added["PAIRLY_AUDIT_SIGNING_KEY"])
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    # A second run appends nothing more.
    assert _run(target, "--append-missing") == 0
    assert target.read_text() == text


def test_append_missing_derives_the_public_key_from_an_existing_signing_key(
    tmp_path: Path,
) -> None:
    target = tmp_path / ".env"
    original = "\n".join(
        line
        for line in _full_env().splitlines()
        if not line.startswith("PAIRLY_AUDIT_ANCHOR_PUBLIC_KEY=")
    )
    target.write_text(original + "\n")
    assert _run(target, "--append-missing") == 0
    values = _parse(target.read_text())
    assert target.read_text().startswith(original + "\n")
    public = base64.urlsafe_b64decode(values["PAIRLY_AUDIT_ANCHOR_PUBLIC_KEY"])
    assert public == _public_of(values["PAIRLY_AUDIT_SIGNING_KEY"])


def test_append_missing_never_pairs_a_new_signing_key_with_an_old_public_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / ".env"
    original = "\n".join(
        line
        for line in _full_env().splitlines()
        if not line.startswith(("PAIRLY_AUDIT_SIGNING_KEY=", "PAIRLY_API_KEY_PEPPER="))
    )
    target.write_text(original + "\n")
    assert _run(target, "--append-missing") == 1
    text = target.read_text()
    # The safe key is still appended; the signing key is reported, not generated.
    assert set(_parse(text[len(original) + 1 :])) == {"PAIRLY_API_KEY_PEPPER"}
    assert "PAIRLY_AUDIT_SIGNING_KEY is missing" in capsys.readouterr().err


def test_append_missing_leaves_empty_keys_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / ".env"
    original = _full_env().replace(
        "PAIRLY_API_KEY_PEPPER=existing-pairly_api_key_pepper", "PAIRLY_API_KEY_PEPPER="
    )
    target.write_text(original)
    assert _run(target, "--append-missing") == 0
    assert target.read_text() == original
    assert "PAIRLY_API_KEY_PEPPER is set but empty" in capsys.readouterr().err


def test_append_missing_on_a_complete_env_changes_nothing(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    original = _full_env()
    target.write_text(original)
    assert _run(target, "--append-missing") == 0
    assert target.read_text() == original


def test_append_missing_creates_a_missing_env(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    assert _run(target, "--append-missing") == 0
    assert set(gen.SECRET_GENERATORS) <= set(_parse(target.read_text()))


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
