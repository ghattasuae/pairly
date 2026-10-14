# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Pairly Pay vendors (WP4.2) on Postgres, through the API.

* Validation: IBAN (ISO 13616) and TRN (15 digits) refuse bad input with stable codes.
* Encryption: IBAN, account name, contacts and the phone number on file are stored
  encrypted; the IBAN and phone have blind indexes, and a lookup by IBAN finds the
  vendor without decrypting anything, however the IBAN was typed.
* Dedupe: the same TRN or IBAN is refused; a similar name needs the person to say it
  is a different company.
* Permissions, the second factor on Pay settings, and tenant isolation.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncEngine

from pairly_core.crypto.blind_index import blind_index
from pairly_pay.vendors import find_duplicates

from .pay_support import (
    PayApi,
    PayWorld,
    audit_actions,
    other,
    owner_rows,
    pay_api,
    synth_vendors,
    vendor_body,
    world,
)

__all__ = ["other", "pay_api", "world"]  # fixtures


async def test_create_validate_encrypt_and_find_by_blind_index(
    pay_api: PayApi, world: PayWorld, owner_engine: AsyncEngine
) -> None:
    api = pay_api
    source = synth_vendors()[0]
    created = await api.ok(
        "POST", "/v1/pay/vendors", world, "clerk", status=201, json=vendor_body(0)
    )
    assert created["status"] == "active"
    assert created["source"] == "staff"
    assert created["reference"].startswith("VEN-")
    assert created["payment_hold"] is False
    iban = source["bank"]["iban"]
    assert created["iban_masked"] == f"AE** **** {iban[-4:]}"
    (account,) = created["accounts"]
    assert account["status"] == "active"
    assert account["bank_code"] == iban[4:7]
    assert account["account_name"] == source["bank"]["account_name"]
    assert created["trn_status"] == "not_verified"  # format only: no official source yet
    assert [c["name"] for c in created["contacts"]] == [c["name"] for c in source["contacts"]]
    assert created["phone_on_file"] == "+97128611629"
    assert set(created["risk"]) >= {"record_age_days", "payment_history", "signals"}
    assert created["risk"]["payment_history"] == {"status": "not_available"}
    assert "new_vendor" in created["risk"]["signals"]
    # Nothing readable at rest; the blind indexes are the HMACs of the normalised values.
    (raw,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT iban, iban_bidx, account_name FROM pay_vendor_bank_accounts WHERE id = :i",
        {"i": account["id"]},
    )
    assert iban not in raw.iban
    assert iban[-8:] not in raw.iban
    assert source["bank"]["account_name"] not in raw.account_name
    assert raw.iban_bidx == blind_index("iban", iban, world.org_id)
    (vraw,) = await owner_rows(
        owner_engine,
        world.org_id,
        "SELECT contacts, phone_on_file, phone_bidx FROM pay_vendors WHERE id = :i",
        {"i": created["id"]},
    )
    assert source["contacts"][0]["email"] not in vraw.contacts
    assert "8611629" not in vraw.phone_on_file
    assert vraw.phone_bidx == blind_index("phone", source["phone_on_file"], world.org_id)
    # Lookup by IBAN, typed with spaces and in lower case: found through the index.
    typed = " ".join(iban[i : i + 4] for i in range(0, len(iban), 4)).lower()
    async with await api.in_org(world) as session, session.begin():
        hints = await find_duplicates(session, world.org_id, legal_name="Unrelated", iban=typed)
    assert [(str(h.vendor_id), h.reasons) for h in hints] == [(created["id"], ("iban",))]
    assert "pay.vendor.created" in await audit_actions(owner_engine, world.org_id, created["id"])


async def test_validation_codes(pay_api: PayApi, world: PayWorld) -> None:
    api = pay_api
    bad_iban = vendor_body(1)
    bad_iban["bank"]["iban"] = bad_iban["bank"]["iban"][:-1] + (
        "0" if bad_iban["bank"]["iban"][-1] != "0" else "1"
    )
    response = await api.call("POST", "/v1/pay/vendors", world, "clerk", json=bad_iban)
    assert response.status_code == 400
    assert response.json()["code"] == "bank_details_invalid"
    assert "iban_checksum" in response.json()["detail"]
    short = vendor_body(1)
    short["bank"]["iban"] = short["bank"]["iban"][:-2]
    response = await api.call("POST", "/v1/pay/vendors", world, "clerk", json=short)
    assert "iban_length" in response.json()["detail"]
    response = await api.call(
        "POST", "/v1/pay/vendors", world, "clerk", json=vendor_body(1, trn="12345")
    )
    assert response.status_code == 400
    assert response.json()["code"] == "trn_invalid"
    response = await api.call(
        "POST", "/v1/pay/vendors", world, "clerk", json=vendor_body(1, phone_on_file="call me")
    )
    assert response.status_code == 400
    assert response.json()["code"] == "phone_invalid"
    checked = await api.ok(
        "POST",
        "/v1/pay/vendors/check",
        world,
        "clerk",
        json={
            "legal_name": "x",
            "trn": "198-4274-0184-9981",
            "iban": "AE07 0331 2345 6789 0123 456",
            "swift": "XQAMGB2L",
        },
    )
    assert checked["trn"] == {
        "valid": True,
        "error": None,
        "country": None,
        "bank_code": None,
        "formatted": "198427401849981",
    }
    assert checked["iban"]["valid"]
    assert checked["iban"]["bank_code"] == "033"
    assert checked["iban"]["formatted"] == "AE07 0331 2345 6789 0123 456"
    assert checked["warnings"] == ["swift_country_differs"]


async def test_dedupe_on_create(pay_api: PayApi, world: PayWorld) -> None:
    api = pay_api
    first = await api.ok("POST", "/v1/pay/vendors", world, "clerk", status=201, json=vendor_body(2))
    # Same TRN, different everything else: refused, whatever the person says.
    same_trn = vendor_body(3, trn=first["trn"], acknowledge_similar=True)
    response = await api.call("POST", "/v1/pay/vendors", world, "clerk", json=same_trn)
    assert response.status_code == 409
    assert response.json()["code"] == "vendor_duplicate"
    # Same IBAN under another name: refused.
    same_iban = vendor_body(3, acknowledge_similar=True)
    same_iban["bank"] = vendor_body(2)["bank"]
    response = await api.call("POST", "/v1/pay/vendors", world, "clerk", json=same_iban)
    assert response.status_code == 409
    # A similar name (a legal form and a dot apart): refused until acknowledged.
    similar = vendor_body(
        3, legal_name=first["legal_name"].replace("LLC", "L.L.C."), trade_name=None
    )
    response = await api.call("POST", "/v1/pay/vendors", world, "clerk", json=similar)
    assert response.status_code == 409
    checked = await api.ok(
        "POST", "/v1/pay/vendors/check", world, "clerk", json={"legal_name": similar["legal_name"]}
    )
    assert [d["reference"] for d in checked["duplicates"]] == [first["reference"]]
    assert checked["duplicates"][0]["reasons"] == ["name"]
    acknowledged = await api.ok(
        "POST",
        "/v1/pay/vendors",
        world,
        "clerk",
        status=201,
        json={**similar, "acknowledge_similar": True},
    )
    assert acknowledged["review_reasons"] == [f"similar_name:{first['reference']}"]
    listed = await api.ok("GET", "/v1/pay/vendors?q=" + first["trn"], world, "auditor")
    assert [v["id"] for v in listed["items"]] == [first["id"]]


async def test_permissions_and_tenant_isolation(
    pay_api: PayApi, world: PayWorld, other: PayWorld
) -> None:
    api = pay_api
    created = await api.ok(
        "POST", "/v1/pay/vendors", world, "clerk", status=201, json=vendor_body(4)
    )
    path = f"/v1/pay/vendors/{created['id']}"
    # Auditors and approvers read; neither writes. Hire people see nothing of Pay.
    assert (await api.call("GET", path, world, "auditor")).status_code == 200
    assert (await api.call("GET", path, world, "approver")).status_code == 200
    for who in ("auditor", "approver"):
        denied = await api.call("POST", "/v1/pay/vendors", world, who, json=vendor_body(5))
        assert denied.status_code == 403
    assert (await api.call("GET", path, world, "recruiter")).status_code == 403
    assert (await api.call("GET", "/v1/pay/vendors", world, "recruiter")).status_code == 403
    # Another organisation: the vendor does not exist there.
    assert (await api.call("GET", path, other, "admin")).status_code == 404
    assert (
        await api.call("PATCH", path, other, "admin", json={"trade_name": "X"})
    ).status_code == 404
    assert (await api.ok("GET", "/v1/pay/vendors", other, "admin"))["items"] == []
    hold = await api.call("POST", f"{path}/hold", other, "finance", json={"reason": "test"})
    assert hold.status_code == 404
    # Settings: read by Pay readers, changed only with pay.settings.manage and a second factor.
    assert (await api.ok("GET", "/v1/pay/settings", world, "clerk"))["cooling_off_hours"] == 24
    body = {"cooling_off_hours": 48}
    assert (await api.call("PUT", "/v1/pay/settings", world, "clerk", json=body)).status_code == 403
    no_otp = await api.call("PUT", "/v1/pay/settings", world, "finance", json=body, otp=False)
    assert no_otp.status_code == 403
    assert no_otp.json()["code"] == "second_factor_required"
    saved = await api.ok("PUT", "/v1/pay/settings", world, "finance", json=body)
    assert saved["cooling_off_hours"] == 48
    assert (await api.ok("GET", "/v1/pay/settings", other, "admin"))["cooling_off_hours"] == 24


async def test_edit_records_a_phone_change(pay_api: PayApi, world: PayWorld) -> None:
    api = pay_api
    created = await api.ok(
        "POST", "/v1/pay/vendors", world, "clerk", status=201, json=vendor_body(6)
    )
    assert created["phone_changed_at"] is None
    edited = await api.ok(
        "PATCH",
        f"/v1/pay/vendors/{created['id']}",
        world,
        "clerk",
        json={"phone_on_file": "+971 4 555 0101", "trade_name": "New Trade Name"},
    )
    assert edited["phone_on_file"] == "+97145550101"
    assert edited["phone_changed_at"] is not None
    assert "phone_on_file_changed_recently" in edited["risk"]["signals"]
    assert edited["trade_name"] == "New Trade Name"
    # Bank details are not editable here: the field does not exist.
    response = await api.call(
        "PATCH", f"/v1/pay/vendors/{created['id']}", world, "clerk", json={"bank": {"iban": "x"}}
    )
    assert response.status_code == 422
    assert uuid.UUID(created["id"])
