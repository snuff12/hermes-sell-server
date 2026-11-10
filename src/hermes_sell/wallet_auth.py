"""EOA-only SIWX authentication with durable, single-use challenges."""

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import urlparse

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import to_checksum_address
from x402.extensions.sign_in_with_x import (
    SIWxPayload,
    SIWxValidationOptions,
    format_siwe_message,
    normalize_configured_origin,
    validate_siwx_message,
    verify_siwx_signature,
)

from .contracts import NETWORK, Problem, canonical


STATEMENT = "Sign in to Hermes Sell to view your orders. This does not authorize a payment."
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_NONCE = re.compile(r"[0-9a-f]{64}\Z")
_SIGNATURE = re.compile(r"0x[0-9a-fA-F]{130}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


def _now() -> int:
    return int(time.time())


def _timestamp(value: int) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class WalletAuth:
    """Authenticate wallets separately from order and payment authorization.

    The database stores challenge fields and token hashes, never private keys or
    submitted signatures. Crypto checks happen outside SQLite transactions.
    """

    def __init__(
        self,
        path: Path,
        origin: str,
        *,
        network: str = NETWORK,
        challenge_ttl: int = 300,
        session_ttl: int = 900,
    ):
        if not isinstance(origin, str) or any(ord(char) <= 32 or ord(char) >= 127 for char in origin):
            raise ValueError("The authentication origin must be an ASCII HTTP or HTTPS origin.")
        self.origin = normalize_configured_origin(origin)
        parsed = urlparse(self.origin)
        original = urlparse(origin)
        if not parsed.hostname or original.params or "?" in origin or "#" in origin:
            raise ValueError("The authentication origin cannot include a path, query, or fragment.")
        # Accessing port validates its syntax and range before creating a database.
        _ = parsed.port
        if "\\" in parsed.netloc or "%" in parsed.netloc or parsed.netloc.endswith(":"):
            raise ValueError("The authentication origin has an invalid host or port.")
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError:
            labels = parsed.hostname.rstrip(".").split(".")
            if len(parsed.hostname) > 253 or any(
                not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
                for label in labels
            ):
                raise ValueError("The authentication origin must have a valid host.") from None
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Public wallet authentication requires HTTPS.")
        if network != NETWORK:
            raise ValueError("Wallet authentication supports Base Sepolia only.")
        if type(challenge_ttl) is not int or not 1 <= challenge_ttl <= 300:
            raise ValueError("Challenge lifetime must be between 1 and 300 seconds.")
        if type(session_ttl) is not int or not 1 <= session_ttl <= 900:
            raise ValueError("Session lifetime must be between 1 and 900 seconds.")
        self.domain = parsed.netloc
        self.uri = self.origin + "/auth/verify"
        self.network = network
        self.challenge_ttl = challenge_ttl
        self.session_ttl = session_ttl
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with closing(self._connect()) as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS auth_challenges (
                    nonce TEXT PRIMARY KEY,
                    origin TEXT NOT NULL,
                    document TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires INTEGER NOT NULL,
                    consumed_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS auth_challenges_expiry ON auth_challenges(expires);
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    address TEXT NOT NULL,
                    network TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS auth_sessions_expiry ON auth_sessions(expires);
            """)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def _tx(self):
        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def challenge(self, address: str) -> dict:
        """Issue an address-bound message for a wallet's personal_sign prompt."""
        if not isinstance(address, str) or not _ADDRESS.fullmatch(address):
            raise Problem("invalid_wallet_address", "Enter a valid EVM wallet address.", 422)
        address = to_checksum_address(address)
        issued = _now()
        expires = issued + self.challenge_ttl
        nonce = secrets.token_hex(32)
        payload = {
            "domain": self.domain,
            "address": address,
            "uri": self.uri,
            "version": "1",
            "chainId": self.network,
            "type": "eip191",
            "nonce": nonce,
            "issuedAt": _timestamp(issued),
            "expirationTime": _timestamp(expires),
            "statement": STATEMENT,
            "signatureScheme": "eip191",
        }
        message = format_siwe_message(payload, address)
        with self._tx() as db:
            db.execute("DELETE FROM auth_challenges WHERE expires<=?", (issued,))
            db.execute("DELETE FROM auth_sessions WHERE expires<=?", (issued,))
            db.execute(
                "INSERT INTO auth_challenges(nonce,origin,document,created_at,expires) VALUES(?,?,?,?,?)",
                (nonce, self.origin, canonical(payload), issued, expires),
            )
        return {"nonce": nonce, "message": message, "payload": payload, "expires_at": expires}

    async def verify(self, payload: dict) -> dict:
        """Verify the exact issued challenge and atomically exchange it for a session."""
        if not isinstance(payload, dict):
            raise Problem("invalid_wallet_proof", "The wallet proof must be a JSON object.", 422)
        nonce, signature = payload.get("nonce"), payload.get("signature")
        if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
            raise Problem("invalid_wallet_proof", "The wallet challenge is invalid.", 401)
        if not isinstance(signature, str) or not _SIGNATURE.fullmatch(signature):
            raise Problem("invalid_wallet_proof", "A standard EOA wallet signature is required.", 401)
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM auth_challenges WHERE nonce=? AND origin=?",
                (nonce, self.origin),
            ).fetchone()
        current = _now()
        if not row or row["consumed_at"] is not None:
            raise Problem("wallet_challenge_used", "Request a new wallet sign-in challenge.", 401)
        if row["expires"] <= current or row["created_at"] > current:
            raise Problem("wallet_challenge_expired", "The wallet challenge has expired. Sign in again.", 401)
        expected = json.loads(row["document"])
        # Exact comparison also binds statement, URI path, expiry, address, chain,
        # and optional fields that the SDK's origin validation does not constrain.
        unsigned = {key: value for key, value in payload.items() if key != "signature"}
        if unsigned != expected or expected["chainId"] != self.network:
            raise Problem("wallet_challenge_mismatch", "The wallet proof does not match the issued challenge.", 401)
        try:
            proof = SIWxPayload.model_validate(payload)
            validation = await validate_siwx_message(
                proof, self.origin, SIWxValidationOptions(max_age=self.challenge_ttl * 1000)
            )
            if not validation.is_valid:
                raise ValueError("The SIWX message is invalid.")
            message = format_siwe_message(proof, proof.address)
            recovered = Account.recover_message(encode_defunct(text=message), signature=signature)
            if recovered.lower() != proof.address.lower():
                raise ValueError("The EOA signature does not match the wallet.")
            # EOA recovery above rejects smart-wallet proofs before the SDK call.
            # No provider or custom verifier is supplied, so no RPC is permitted.
            verification = await verify_siwx_signature(proof)
            if not verification.is_valid or not verification.payer:
                raise ValueError("The SIWX signature is invalid.")
            if verification.payer.lower() != recovered.lower():
                raise ValueError("The verified wallet does not match the signer.")
        except Exception:
            raise Problem("invalid_wallet_proof", "The wallet signature could not be verified.", 401) from None
        token = secrets.token_urlsafe(32)
        owner = recovered.lower()
        current = _now()
        expires = current + self.session_ttl
        with self._tx() as db:
            consumed = db.execute(
                "UPDATE auth_challenges SET consumed_at=? "
                "WHERE nonce=? AND origin=? AND document=? AND consumed_at IS NULL "
                "AND created_at<=? AND expires>?",
                (current, nonce, self.origin, row["document"], current, current),
            )
            if consumed.rowcount != 1:
                raise Problem("wallet_challenge_used", "Request a new wallet sign-in challenge.", 401)
            db.execute(
                "INSERT INTO auth_sessions VALUES(?,?,?,?,?,?,?)",
                (_token_hash(token), owner, recovered, self.network, self.origin, current, expires),
            )
        return {"token": token, "owner": owner, "address": recovered,
                "network": self.network, "expires_at": expires}

    def owner(self, token: str) -> str:
        """Resolve an unexpired bearer token to a stable wallet owner."""
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise Problem("authentication_required", "Reconnect your wallet session.", 401)
        current = _now()
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT owner FROM auth_sessions WHERE token_hash=? AND origin=? AND network=? "
                "AND created_at<=? AND expires>?",
                (_token_hash(token), self.origin, self.network, current, current),
            ).fetchone()
        if not row:
            raise Problem("authentication_required", "Reconnect your wallet session.", 401)
        return row["owner"]

    def revoke(self, token: str) -> None:
        """Revoke a session without affecting other sessions for the same wallet."""
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            return
        with self._tx() as db:
            db.execute("DELETE FROM auth_sessions WHERE token_hash=? AND origin=? AND network=?",
                       (_token_hash(token), self.origin, self.network))
