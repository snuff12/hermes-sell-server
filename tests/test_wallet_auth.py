"""Real EOA signing checks using temporary, unfunded accounts."""

import asyncio
from contextlib import closing
from copy import deepcopy
import secrets
import socket
import sqlite3

from eth_account import Account
from eth_account.messages import encode_defunct
import pytest
from x402.extensions.sign_in_with_x import create_siwx_payload, format_siwe_message

from hermes_sell import wallet_auth
from hermes_sell.contracts import NETWORK, Problem
from hermes_sell.wallet_auth import WalletAuth


@pytest.fixture
def account():
    return Account.create()


@pytest.fixture
def auth(tmp_path):
    return WalletAuth(tmp_path / "auth.sqlite3", "http://127.0.0.1:8765")


def signed(account, challenge):
    payload = deepcopy(challenge["payload"])
    message = format_siwe_message(payload, payload["address"])
    signature = account.sign_message(encode_defunct(text=message)).signature
    return {**payload, "signature": "0x" + signature.hex()}


def assert_auth_error(code, action):
    with pytest.raises(Problem) as caught:
        action()
    assert caught.value.code == code
    assert caught.value.status == 401


async def test_official_client_proof_creates_real_wallet_session_without_network(auth, account, monkeypatch):
    def reject_network(*args, **kwargs):
        raise AssertionError("Wallet authentication must not use a network connection.")

    monkeypatch.setattr(socket.socket, "connect", reject_network)
    challenge = auth.challenge(account.address.lower())
    assert challenge["payload"]["address"] == account.address
    assert challenge["message"] == format_siwe_message(challenge["payload"], account.address)
    assert challenge["payload"]["chainId"] == NETWORK
    assert challenge["payload"]["domain"] == "127.0.0.1:8765"
    assert challenge["payload"]["uri"] == "http://127.0.0.1:8765/auth/verify"
    assert "does not authorize a payment" in challenge["message"]
    proof = await create_siwx_payload(challenge["payload"], account, auth.uri)
    session = await auth.verify(proof.model_dump(by_alias=True, exclude_none=True))
    assert session["owner"] == account.address.lower()
    assert session["address"] == account.address
    assert session["network"] == NETWORK
    assert auth.owner(session["token"]) == account.address.lower()
    assert session["expires_at"] > wallet_auth._now()
    assert auth.path.stat().st_mode & 0o777 == 0o600
    with closing(auth._connect()) as db:
        row = db.execute("SELECT * FROM auth_sessions").fetchone()
        assert row["token_hash"] != session["token"]
        assert session["token"] not in str(tuple(row))
        assert proof.signature not in str(tuple(row))
        stored = db.execute("SELECT document FROM auth_challenges").fetchone()[0]
        assert proof.signature not in stored


async def test_invalid_signature_does_not_consume_challenge(auth, account):
    challenge = auth.challenge(account.address)
    wrong_account = Account.create()
    with pytest.raises(Problem, match="signature could not be verified"):
        await auth.verify(signed(wrong_account, challenge))
    session = await auth.verify(signed(account, challenge))
    assert auth.owner(session["token"]) == account.address.lower()


async def test_signature_cannot_be_replayed_after_restart(auth, account):
    proof = signed(account, auth.challenge(account.address))
    session = await auth.verify(proof)
    reopened = WalletAuth(auth.path, auth.origin)
    assert reopened.owner(session["token"]) == account.address.lower()
    with pytest.raises(Problem) as caught:
        await reopened.verify(proof)
    assert caught.value.code == "wallet_challenge_used"
    with closing(auth._connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0] == 1


async def test_concurrent_verification_consumes_nonce_and_creates_session_once(auth, account, monkeypatch):
    second = WalletAuth(auth.path, auth.origin)
    proof = signed(account, auth.challenge(account.address))
    real_verify = wallet_auth.verify_siwx_signature
    verified = 0
    both_verified = asyncio.Event()

    async def verify_then_wait(payload):
        nonlocal verified
        result = await real_verify(payload)
        assert result.is_valid
        verified += 1
        if verified == 2:
            both_verified.set()
        await both_verified.wait()
        return result

    monkeypatch.setattr(wallet_auth, "verify_siwx_signature", verify_then_wait)
    outcomes = await asyncio.gather(auth.verify(proof), second.verify(proof), return_exceptions=True)
    assert len([value for value in outcomes if isinstance(value, dict)]) == 1
    failures = [value for value in outcomes if isinstance(value, Problem)]
    assert len(failures) == 1
    assert failures[0].code == "wallet_challenge_used"
    with closing(auth._connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0] == 1


@pytest.mark.parametrize("field,value", [
    ("domain", "attacker.example"),
    ("uri", "http://127.0.0.1:8765/other"),
    ("uri", "https://127.0.0.1:8765/auth/verify"),
    ("chainId", "eip155:8453"),
    ("statement", "Authorize something else."),
    ("expirationTime", "2099-01-01T00:00:00Z"),
])
async def test_validly_signed_modified_challenge_is_rejected(auth, account, field, value):
    challenge = auth.challenge(account.address)
    altered = deepcopy(challenge)
    altered["payload"][field] = value
    with pytest.raises(Problem) as caught:
        await auth.verify(signed(account, altered))
    assert caught.value.code == "wallet_challenge_mismatch"
    assert (await auth.verify(signed(account, challenge)))["owner"] == account.address.lower()


@pytest.mark.parametrize("change", [
    {"type": "ed25519"},
    {"signatureScheme": "eip1271"},
    {"signatureScheme": "eip6492"},
    {"resources": ["https://attacker.example"]},
    {"notBefore": "2099-01-01T00:00:00Z"},
    {"version": "2"},
    {"unexpected": "field"},
])
async def test_unsupported_or_extra_proof_fields_are_rejected(auth, account, change):
    proof = signed(account, auth.challenge(account.address))
    proof.update(change)
    with pytest.raises(Problem) as caught:
        await auth.verify(proof)
    assert caught.value.code == "wallet_challenge_mismatch"


async def test_challenge_cannot_be_transferred_to_another_wallet(auth, account):
    challenge = auth.challenge(account.address)
    other = Account.create()
    challenge["payload"]["address"] = other.address
    with pytest.raises(Problem) as caught:
        await auth.verify(signed(other, challenge))
    assert caught.value.code == "wallet_challenge_mismatch"


async def test_self_issued_nonce_is_rejected(auth, account):
    challenge = auth.challenge(account.address)
    challenge["payload"]["nonce"] = secrets.token_hex(32)
    with pytest.raises(Problem) as caught:
        await auth.verify(signed(account, challenge))
    assert caught.value.code == "wallet_challenge_used"


@pytest.mark.parametrize("signature", [None, "0x", "0x" + "00" * 64, "0x" + "00" * 66, "0x" + "zz" * 65])
async def test_malformed_signatures_are_rejected(auth, account, signature):
    proof = {**auth.challenge(account.address)["payload"], "signature": signature}
    with pytest.raises(Problem) as caught:
        await auth.verify(proof)
    assert caught.value.code == "invalid_wallet_proof"


async def test_challenge_expiry_is_enforced_at_exact_deadline(auth, account, monkeypatch):
    challenge = auth.challenge(account.address)
    proof = signed(account, challenge)
    monkeypatch.setattr(wallet_auth, "_now", lambda: challenge["expires_at"])
    with pytest.raises(Problem) as caught:
        await auth.verify(proof)
    assert caught.value.code == "wallet_challenge_expired"


async def test_challenge_expiring_during_verification_cannot_create_session(auth, account, monkeypatch):
    challenge = auth.challenge(account.address)
    proof = signed(account, challenge)
    real_verify = wallet_auth.verify_siwx_signature

    async def expire_after_verification(payload):
        result = await real_verify(payload)
        assert result.is_valid
        monkeypatch.setattr(wallet_auth, "_now", lambda: challenge["expires_at"])
        return result

    monkeypatch.setattr(wallet_auth, "verify_siwx_signature", expire_after_verification)
    with pytest.raises(Problem) as caught:
        await auth.verify(proof)
    assert caught.value.code == "wallet_challenge_used"
    with closing(auth._connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0] == 0


async def test_session_insert_failure_rolls_back_nonce_consumption(auth, account):
    proof = signed(account, auth.challenge(account.address))
    with auth._tx() as db:
        db.execute("CREATE TRIGGER reject_session BEFORE INSERT ON auth_sessions "
                   "BEGIN SELECT RAISE(ABORT, 'Session storage unavailable'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await auth.verify(proof)
    with auth._tx() as db:
        assert db.execute("SELECT consumed_at FROM auth_challenges").fetchone()[0] is None
        db.execute("DROP TRIGGER reject_session")
    assert (await auth.verify(proof))["owner"] == account.address.lower()


async def test_sessions_are_bound_to_origin_and_can_expire_or_be_revoked(auth, account, monkeypatch):
    first = await auth.verify(signed(account, auth.challenge(account.address)))
    second = await auth.verify(signed(account, auth.challenge(account.address.lower())))
    assert first["owner"] == second["owner"]
    assert first["token"] != second["token"]
    other_origin = WalletAuth(auth.path, "http://localhost:8765")
    assert_auth_error("authentication_required", lambda: other_origin.owner(first["token"]))
    auth.revoke(first["token"])
    assert_auth_error("authentication_required", lambda: auth.owner(first["token"]))
    assert auth.owner(second["token"]) == account.address.lower()
    monkeypatch.setattr(wallet_auth, "_now", lambda: second["expires_at"])
    assert_auth_error("authentication_required", lambda: auth.owner(second["token"]))


async def test_challenge_is_bound_to_configured_origin_even_with_shared_database(auth, account):
    proof = signed(account, auth.challenge(account.address))
    other_origin = WalletAuth(auth.path, "http://localhost:8765")
    with pytest.raises(Problem) as caught:
        await other_origin.verify(proof)
    assert caught.value.code == "wallet_challenge_used"


@pytest.mark.parametrize("origin", [
    "http://public.example", "ftp://public.example", "https://user:secret@example.com",
    "https://example.com/path", "https://example.com?query=yes", "https://example.com#fragment",
    "https://example.com:invalid", "https://example.com:70000", "https://example.com/;parameter",
    "https://example.com\n", "http://localhost.attacker.example", "https://",
    "https://example.com\\path", "https://%65xample.com", "https://example..com",
    "https://example.com:", "https://example.com?", "https://example.com#",
])
def test_unsafe_origins_are_rejected_before_database_creation(tmp_path, origin):
    path = tmp_path / "auth.sqlite3"
    with pytest.raises(ValueError):
        WalletAuth(path, origin)
    assert not path.exists()


@pytest.mark.parametrize("origin", ["http://localhost:8765", "http://[::1]:8765", "https://shop.example/"])
def test_https_and_explicit_localhost_origins_are_supported(tmp_path, origin):
    auth = WalletAuth(tmp_path / "auth.sqlite3", origin)
    assert auth.uri == origin.rstrip("/") + "/auth/verify"


@pytest.mark.parametrize("address", [None, "", "alice.eth", "0x123", "0x" + "gg" * 20])
def test_malformed_wallet_addresses_are_rejected(auth, address):
    with pytest.raises(Problem) as caught:
        auth.challenge(address)
    assert caught.value.code == "invalid_wallet_address"
