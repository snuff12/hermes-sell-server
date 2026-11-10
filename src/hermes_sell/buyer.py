"""A separately invoked buyer with its own ledger and explicit human approval.

No signing key lives here. Buyer supports simulation; TestnetBuyer accepts only
wallet signatures supplied through the separate local approval gateway.
"""

import json
from contextlib import contextmanager
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlparse
import uuid

import httpx

from .contracts import Problem, canonical

TERMINAL = {"settled", "failed", "expired"}
PAYMENT_PROGRESS = {"required": 0, "verified": 1, "settling": 2, "unknown": 3, "settled": 4, "failed": 4}


def purchase_route(path, purchase_id):
    """Route recovery by its persisted origin, never the currently selected seller."""
    from contextlib import closing
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute("SELECT origin,document FROM purchases WHERE id=?", (purchase_id,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    if not row:
        raise Problem("not_found", "Purchase record not found.", 404)
    document = json.loads(row[1])
    if document.get("origin") != row[0]:
        raise Problem("purchase_origin_mismatch", "The saved purchase origin is inconsistent.", 409)
    return row[0], document.get("mode", "simulation")


class Buyer:
    def __init__(self, origin: str, path: Path, *, transport=None):
        parsed = urlparse(origin)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "testserver"} or parsed.username or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise Problem("simulation_only", "The buyer client currently supports local simulation only.")
        self.origin = origin.rstrip("/")
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS purchases(id TEXT PRIMARY KEY, origin TEXT NOT NULL, document TEXT NOT NULL)")
        self.path.chmod(0o600)
        self.client = httpx.AsyncClient(base_url=self.origin, transport=transport, follow_redirects=False, timeout=10)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    async def connect(self):
        response = await self.client.post("/dev/session", json={"actor": "demo-hermes"})
        response.raise_for_status()
        self.client.headers["Authorization"] = "Bearer " + response.json()["token"]

    async def inspect(self, slug):
        response = await self.client.get(f"/v1/services/{slug}")
        response.raise_for_status()
        return response.json()

    def save(self, purchase):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO purchases VALUES (?,?,?)", (purchase["purchase_id"], self.origin, canonical(purchase)))

    def get(self, purchase_id):
        with self.db() as db:
            row = db.execute("SELECT document FROM purchases WHERE id=? AND origin=?", (purchase_id, self.origin)).fetchone()
        if not row:
            raise Problem("not_found", "Purchase record not found.", 404)
        return json.loads(row[0])

    def update(self, purchase_id, change):
        """Merge into the latest record under a transaction, never a pre-HTTP snapshot."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT document FROM purchases WHERE id=? AND origin=?", (purchase_id, self.origin)).fetchone()
            if not row:
                raise Problem("not_found", "Purchase record not found.", 404)
            latest = json.loads(row[0])
            change(latest)
            db.execute("UPDATE purchases SET document=? WHERE id=? AND origin=?",
                       (canonical(latest), purchase_id, self.origin))
        return latest

    @staticmethod
    def request_error(exc):
        if isinstance(exc, httpx.HTTPStatusError):
            try:
                error = exc.response.json().get("error", {})
                if isinstance(error, dict) and isinstance(error.get("code"), str) and isinstance(error.get("message"), str):
                    return {"code": error["code"][:100], "message": error["message"][:500]}
            except (ValueError, AttributeError):
                pass
        return {"code": "response_unknown", "message": "The response could not be confirmed. Check the status using the original purchase ID."}

    def apply_order(self, purchase_id, order, *, observed_after=None):
        if not isinstance(order, dict) or not isinstance(order.get("id"), str) or not order["id"]:
            raise ValueError("Missing order identity")
        if not isinstance(order.get("order_digest"), str) or not re.fullmatch(r"[a-f0-9]{64}", order["order_digest"]):
            raise ValueError("Invalid order digest")
        if order.get("payment") not in PAYMENT_PROGRESS or type(order.get("quote_expires_at")) is not int:
            raise ValueError("Invalid order state")
        if not isinstance(order.get("amount"), str) or not re.fullmatch(r"[0-9]+", order["amount"]) or not 1 <= int(order["amount"]) <= 10_000_000:
            raise ValueError("Invalid order amount")
        if order.get("origin") != self.origin:
            raise ValueError("Order origin mismatch")
        def merge(latest):
            previous = latest.get("order")
            if previous:
                for key in ("id", "order_digest", "revision", "origin", "amount", "pay_to", "network", "asset", "input", "quote_expires_at"):
                    if order.get(key) != previous.get(key):
                        raise Problem("order_changed", "The original quote has changed. Approval is blocked.", 409)
                # An older GET/POST response must not roll back a newer observation.
                if latest["status"] in TERMINAL or PAYMENT_PROGRESS.get(order["payment"], -1) < PAYMENT_PROGRESS.get(previous["payment"], -1):
                    return
            latest["order"] = order
            latest.pop("error", None)
            if order["payment"] in {"settled", "failed"}:
                latest["status"] = order["payment"]
            elif order["payment"] == "unknown" or order.get("execution") == "unknown":
                latest["status"] = "unknown"
            elif order["payment"] != "required":
                latest["status"] = "submitted"
            elif (latest.get("authorization") or latest.get("authorization_encrypted")) and observed_after is not None and (
                latest.get("authorization_expires_at", latest.get("authorization", {}).get("valid_before", 0)) <= observed_after
                or order["quote_expires_at"] <= observed_after
            ):
                # Only a fresh seller observation of required, after expiry, proves
                # this authorization was never admitted. Time alone is insufficient.
                latest["status"] = "expired"
                latest["error"] = {"code": "expired_unsubmitted", "message": "The seller confirmed that this expired order was never admitted. You were not charged."}
            elif not previous:
                latest["status"] = "awaiting_approval"
        return self.update(purchase_id, merge)

    async def quote(self, slug, value, *, expected_revision=None):
        service = await self.inspect(slug)
        if expected_revision is not None and service["revision"] != expected_revision:
            raise Problem("listing_changed", "This service version changed. Search and review the current listing again.", 409)
        purchase = {"purchase_id": str(uuid.uuid4()), "slug": slug, "origin": self.origin,
                    "revision": service["revision"], "input": value, "status": "quoting", "mode": "simulation"}
        # Durable idempotency key exists even if quote response is lost.
        self.save(purchase)
        return await self.recover_quote(purchase["purchase_id"])

    async def recover_quote(self, purchase_id):
        purchase = self.get(purchase_id)
        if "order" in purchase:
            return purchase
        try:
            response = await self.client.post(f"/v1/services/{purchase['slug']}/orders",
                headers={"Idempotency-Key": purchase_id}, json={"revision": purchase["revision"], "input": purchase["input"]})
            response.raise_for_status()
            return self.apply_order(purchase_id, response.json())
        except (httpx.HTTPError, ValueError):
            # The caller must receive the already-persisted ID even if the server
            # accepted the quote but its response was lost.
            def unknown(latest):
                if "order" not in latest:
                    latest.update(status="quote_unknown", error={"code": "quote_response_unknown",
                        "message": "The quote response is unknown. Check the status using this purchase ID to recover with the same request key."})
            return self.update(purchase_id, unknown)

    def approve_from_terminal(self, purchase_id, entered_digest):
        # Only the CLI calls this after reading from the controlling TTY. No tool or HTTP endpoint exposes it.
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT document FROM purchases WHERE id=? AND origin=?", (purchase_id, self.origin)).fetchone()
            if not row:
                raise Problem("not_found", "Purchase record not found.", 404)
            purchase = json.loads(row[0])
            if purchase["status"] != "awaiting_approval" or entered_digest != purchase["order"]["order_digest"]:
                raise Problem("approval_mismatch", "The entered confirmation does not match the order awaiting approval.", 409)
            order = purchase["order"]
            if order["quote_expires_at"] <= int(time.time()):
                raise Problem("quote_expired", "The quote has expired.", 409)
            # Local budget reservation includes unknown and active purchases.
            reserved = sum(int(p["order"]["amount"]) for (data,) in db.execute("SELECT document FROM purchases").fetchall()
                           if (p := json.loads(data)).get("status") in {"approved", "submitted", "unknown", "settled"})
            if reserved + int(order["amount"]) > 1_000_000:
                raise Problem("budget_exceeded", "The local simulation spending limit of 1 USDC has been exceeded.", 409)
            purchase.update(status="approved", approved_digest=entered_digest, authorization={
                "mode": "simulation", "order_digest": entered_digest, "payer": "demo-hermes",
                "nonce": uuid.uuid4().hex, "valid_before": int(time.time()) + 180})
            db.execute("UPDATE purchases SET document=? WHERE id=?", (canonical(purchase), purchase_id))
        return purchase

    async def purchase(self, purchase_id):
        def intent(latest):
            if latest["status"] in TERMINAL:
                return
            if latest["status"] not in {"approved", "submitted", "unknown"}:
                raise Problem("human_approval_required", "The user must approve this quote in the terminal.", 409)
            if latest["approved_digest"] != latest["order"]["order_digest"]:
                raise Problem("approval_mismatch", "The approved order does not match the current order.", 409)
            latest.update(status="unknown", admission_attempted=True)
        purchase = self.update(purchase_id, intent)
        if purchase["status"] in TERMINAL:
            return purchase
        try:
            response = await self.client.post(f"/v1/orders/{purchase['order']['id']}", json={"authorization": purchase["authorization"]})
            response.raise_for_status()
            return self.apply_order(purchase_id, response.json())
        except (httpx.HTTPError, ValueError) as exc:
            error = self.request_error(exc)
            def failed_response(latest):
                if latest["status"] not in TERMINAL:
                    latest["error"] = error
            self.update(purchase_id, failed_response)
            # Query-only reconciliation may prove a terminal outcome or an
            # expired, unsubmitted approval. Never regenerate a signature here.
            try:
                return await self.status(purchase_id)
            except (httpx.HTTPError, ValueError):
                return self.get(purchase_id)

    async def status(self, purchase_id):
        purchase = self.get(purchase_id)
        if "order" not in purchase:
            return await self.recover_quote(purchase_id)
        requested_at = int(time.time())
        response = await self.client.get(f"/v1/orders/{purchase['order']['id']}")
        response.raise_for_status()
        return self.apply_order(purchase_id, response.json(), observed_after=requested_at)

    async def close(self):
        await self.client.aclose()


class TestnetBuyer(Buyer):
    """A signer-free buyer whose wallet sessions and approvals come from a browser."""

    __test__ = False

    def __init__(self, origin: str, path: Path, *, vault,
                 approval_origin="http://127.0.0.1:8767", transport=None, budget=1_000_000):
        from .contracts import NETWORK
        # Seller and gateway origins come from explicit local configuration.
        parsed = urlparse(origin)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in {"", "/"} or parsed.params
                or "?" in origin or "#" in origin or "\\" in origin or "%" in parsed.netloc or parsed.netloc.endswith(":")
                or any(ord(char) <= 32 or ord(char) >= 127 for char in origin)):
            raise Problem("invalid_origin", "Configure an HTTP loopback or HTTPS seller origin.")
        try:
            _ = parsed.port
        except ValueError as exc:
            raise Problem("invalid_origin", "The seller origin has an invalid port.") from exc
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise Problem("invalid_origin", "Remote sellers require HTTPS.")
        gateway = urlparse(approval_origin)
        if (gateway.scheme != "http" or gateway.hostname not in {"localhost", "127.0.0.1", "::1"}
                or gateway.username is not None or gateway.password is not None
                or gateway.path not in {"", "/"} or gateway.params or "?" in approval_origin or "#" in approval_origin
                or "\\" in approval_origin or gateway.netloc.endswith(":")
                or any(ord(char) <= 32 or ord(char) >= 127 for char in approval_origin)):
            raise Problem("invalid_gateway", "The approval gateway must use an explicit loopback HTTP origin.")
        try:
            _ = gateway.port
        except ValueError as exc:
            raise Problem("invalid_gateway", "The approval gateway has an invalid port.") from exc
        if type(budget) is not int or not 1 <= budget <= 10_000_000:
            raise Problem("invalid_budget", "The buyer budget must be 1-10,000,000 USDC base units.")
        self.origin = origin.rstrip("/")
        self.approval_origin = approval_origin.rstrip("/")
        self.network = NETWORK
        self.vault, self.budget = vault, budget
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        import os
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        self.path.chmod(0o600)
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS purchases(id TEXT PRIMARY KEY, origin TEXT NOT NULL, document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS buyer_wallet_sessions(
                    origin TEXT PRIMARY KEY, token_hash TEXT NOT NULL, encrypted TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS buyer_wallet_challenges(
                    nonce TEXT PRIMARY KEY, origin TEXT NOT NULL, document TEXT NOT NULL, expires INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS buyer_wallet_generation(
                    origin TEXT PRIMARY KEY, generation INTEGER NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO buyer_wallet_generation VALUES(?,0)", (self.origin,))
        self.client = httpx.AsyncClient(base_url=self.origin, transport=transport, follow_redirects=False, timeout=10, trust_env=False)

    def _session_context(self):
        from .contracts import digest
        return {"id": "buyer-wallet-session:" + self.origin,
                "order_digest": digest(["buyer-wallet-session-v1", self.origin])}

    def _session(self):
        with self.db() as db:
            row = db.execute("SELECT encrypted FROM buyer_wallet_sessions WHERE origin=?", (self.origin,)).fetchone()
        if not row:
            return None
        session = self.vault.open(json.loads(row[0]), self._session_context())
        if session["expires_at"] <= int(time.time()):
            return None
        return session

    def session_info(self):
        session = self._session()
        if session is None:
            return {"connected": False}
        return {"connected": True, **{key: session[key] for key in ("owner", "address", "network", "expires_at")}}

    def _connection_needed(self, purchase_id=None):
        result = {"mode": "testnet", "status": "needs_wallet_connection", "origin": self.origin,
                  "approval_url": self.approval_origin + ("/approve/" + purchase_id if purchase_id else "/connect"),
                  "message": "Open the local approval page and connect your wallet, then retry this request."}
        if purchase_id:
            result["purchase_id"] = purchase_id
        return result

    def public_purchase(self, purchase):
        result = {key: value for key, value in purchase.items()
                  if key not in {"authorization", "authorization_encrypted"}}
        result["mode"] = "testnet"
        result["approval_url"] = self.approval_origin + "/approve/" + purchase["purchase_id"]
        return result

    @staticmethod
    def request_error(exc):
        # Seller error text can echo a submitted proof or session header.
        # Return a fixed message rather than copying untrusted response details.
        return {"code": "response_unknown",
                "message": "The seller response could not be confirmed. Refresh the original purchase; do not sign again."}

    def _session_for(self, purchase=None):
        session = self._session()
        if session and purchase and session["owner"] != purchase.get("owner"):
            raise Problem("wallet_mismatch", "Reconnect the wallet that owns this purchase.", 409)
        return session

    def disconnect(self, token=None):
        from .contracts import digest
        with self.db() as db:
            if token is None:
                # Explicit disconnect also invalidates sign-ins already in flight.
                # A delayed seller response must not reconnect the wallet afterward.
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE buyer_wallet_generation SET generation=generation+1 WHERE origin=?", (self.origin,))
                db.execute("DELETE FROM buyer_wallet_sessions WHERE origin=?", (self.origin,))
                db.execute("DELETE FROM buyer_wallet_challenges WHERE origin=?", (self.origin,))
            else:
                db.execute("DELETE FROM buyer_wallet_sessions WHERE origin=? AND token_hash=?", (self.origin, digest(token)))
        return {"connected": False}

    async def connect(self):
        return self.session_info() if self._session() else self._connection_needed()

    async def wallet_challenge(self, address):
        from eth_utils import to_checksum_address
        from x402.extensions.sign_in_with_x import format_siwe_message
        from .wallet_auth import STATEMENT

        if not isinstance(address, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
            raise Problem("invalid_wallet_address", "Enter a valid EVM wallet address.", 422)
        address = to_checksum_address(address)
        with self.db() as db:
            generation = db.execute("SELECT generation FROM buyer_wallet_generation WHERE origin=?", (self.origin,)).fetchone()[0]
        response = await self.client.post("/auth/challenge", json={"address": address})
        response.raise_for_status()
        challenge = response.json()
        try:
            payload = challenge["payload"]
            expected = {"domain", "address", "uri", "version", "chainId", "type", "nonce", "issuedAt",
                        "expirationTime", "statement", "signatureScheme"}
            from datetime import datetime
            issued = datetime.fromisoformat(payload["issuedAt"].replace("Z", "+00:00")).timestamp()
            expires = datetime.fromisoformat(payload["expirationTime"].replace("Z", "+00:00")).timestamp()
            current = int(time.time())
            if (set(payload) != expected or not payload["issuedAt"].endswith("Z") or not payload["expirationTime"].endswith("Z")
                    or payload["domain"] != urlparse(self.origin).netloc
                    or payload["uri"] != self.origin + "/auth/verify" or payload["address"] != address
                    or payload["version"] != "1" or payload["chainId"] != self.network
                    or payload["type"] != "eip191" or payload["signatureScheme"] != "eip191"
                    or payload["statement"] != STATEMENT or payload["nonce"] != challenge["nonce"]
                    or not re.fullmatch(r"[0-9a-f]{64}", payload["nonce"])
                    or not current - 300 <= issued <= current + 30 or not current < expires <= issued + 300
                    or type(challenge["expires_at"]) is not int or expires != challenge["expires_at"]
                    or format_siwe_message(payload, address) != challenge["message"]):
                raise ValueError("Challenge binding mismatch")
        except (KeyError, TypeError, ValueError, AttributeError):
            raise Problem("invalid_wallet_challenge", "The seller returned a wallet challenge that does not match its origin.", 502) from None
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current_generation = db.execute("SELECT generation FROM buyer_wallet_generation WHERE origin=?", (self.origin,)).fetchone()[0]
            if current_generation != generation:
                raise Problem("wallet_disconnected", "The wallet was disconnected. Start a new sign-in request.", 409)
            db.execute("DELETE FROM buyer_wallet_challenges WHERE expires<=?", (current,))
            db.execute("INSERT OR REPLACE INTO buyer_wallet_challenges VALUES(?,?,?,?)",
                       (payload["nonce"], self.origin, canonical(payload), int(expires)))
        return challenge

    async def wallet_verify(self, payload):
        from eth_account import Account
        from eth_account.messages import encode_defunct
        from x402.extensions.sign_in_with_x import format_siwe_message
        from .contracts import digest

        if not isinstance(payload, dict) or not isinstance(payload.get("nonce"), str):
            raise Problem("invalid_wallet_proof", "The wallet proof is invalid.", 401)
        with self.db() as db:
            row = db.execute("SELECT document,expires FROM buyer_wallet_challenges WHERE nonce=? AND origin=?",
                             (payload["nonce"], self.origin)).fetchone()
        if not row or row[1] <= int(time.time()):
            raise Problem("wallet_challenge_expired", "Request a new wallet challenge.", 401)
        expected = json.loads(row[0])
        if {key: value for key, value in payload.items() if key != "signature"} != expected:
            raise Problem("wallet_challenge_mismatch", "The wallet proof does not match the requested challenge.", 401)
        try:
            signature = payload["signature"]
            if not isinstance(signature, str) or not re.fullmatch(r"0x[0-9a-fA-F]{130}", signature):
                raise ValueError("Invalid EOA signature")
            recovered = Account.recover_message(encode_defunct(text=format_siwe_message(expected, expected["address"])),
                                                signature=signature)
            if recovered.lower() != expected["address"].lower():
                raise ValueError("Different signer")
        except Exception:
            raise Problem("invalid_wallet_proof", "The wallet signature could not be verified.", 401) from None
        response = await self.client.post("/auth/verify", json={"payload": payload})
        response.raise_for_status()
        session = response.json()
        current = int(time.time())
        if (not isinstance(session, dict) or not isinstance(session.get("token"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{43}", session["token"])
                or session.get("owner") != recovered.lower() or not isinstance(session.get("address"), str)
                or session["address"].lower() != recovered.lower()
                or session.get("network") != self.network or type(session.get("expires_at")) is not int
                or not current < session["expires_at"] <= current + 930):
            raise Problem("invalid_wallet_session", "The seller session does not match the connected wallet.", 502)
        encrypted = canonical(self.vault.seal(session, self._session_context()))
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            consumed = db.execute("DELETE FROM buyer_wallet_challenges WHERE nonce=? AND origin=? AND document=? AND expires>?",
                                  (payload["nonce"], self.origin, row[0], current))
            if consumed.rowcount != 1:
                raise Problem("wallet_challenge_used", "Request a new wallet challenge.", 401)
            db.execute("INSERT OR REPLACE INTO buyer_wallet_sessions VALUES(?,?,?)",
                       (self.origin, digest(session["token"]), encrypted))
        return self.session_info()

    def _validate_order(self, order, purchase):
        from .contracts import ASSET, PROTOCOL, digest
        from .x402_adapter import X402Payments

        if not isinstance(order, dict):
            raise ValueError("Missing order")
        allowed = {"id", "owner", "slug", "revision", "input", "origin", "amount", "pay_to", "network", "asset",
                   "created_at", "quote_expires_at", "order_digest", "payment_nonce", "execution", "payment", "protocol", "mode",
                   "error", "service_contract", "payment_requirements", "result", "receipt", "recovery_required"}
        if (set(order) - allowed or not isinstance(order.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", order["id"])):
            raise Problem("invalid_quote", "The seller returned unexpected order fields.", 409)
        if "recovery_required" in order and type(order["recovery_required"]) is not bool:
            raise Problem("invalid_quote", "The seller returned an invalid recovery state.", 409)
        if (order.get("mode") != "testnet" or order.get("network") != self.network or order.get("asset") != ASSET
                or order.get("owner") != purchase.get("owner") or order.get("origin") != self.origin
                or order.get("slug") != purchase["slug"] or order.get("revision") != purchase["revision"]
                or canonical(order.get("input")) != canonical(purchase["input"]) or order.get("protocol") != PROTOCOL
                or order.get("payment_nonce") != "0x" + str(order.get("order_digest"))):
            raise Problem("invalid_quote", "The quote does not match this wallet, input, service, or network.", 409)
        if order.get("payment_requirements") != X402Payments.requirements(order).model_dump(by_alias=True, exclude_none=True):
            raise Problem("invalid_quote", "The payment requirements do not match the quote.", 409)
        initial = {key: order[key] for key in (
            "id", "owner", "slug", "revision", "input", "origin", "amount", "pay_to", "network", "asset",
            "created_at", "quote_expires_at", "protocol", "mode")}
        initial.update(execution="created", payment="required", run_key="sell-" + order["id"])
        if digest(initial) != order["order_digest"]:
            raise Problem("invalid_quote", "The order digest does not bind the displayed quote.", 409)

    def apply_order(self, purchase_id, order, *, observed_after=None):
        purchase = self.get(purchase_id)
        self._validate_order(order, purchase)
        previous = purchase.get("order")
        if previous:
            for key in ("owner", "payment_nonce", "payment_requirements", "protocol", "mode", "service_contract"):
                if previous.get(key) != order.get(key):
                    raise Problem("order_changed", "The original quote has changed. Approval is blocked.", 409)
        result = super().apply_order(purchase_id, order, observed_after=observed_after)
        if result["status"] == "expired":
            result = self.update(purchase_id, lambda latest: latest.update(budget_reserved=False)
                                 if latest["status"] == "expired" else None)
        return result

    async def quote(self, slug, value, *, expected_revision=None):
        session = self._session_for()
        if session is None:
            return self._connection_needed()
        service = await self.inspect(slug)
        if expected_revision is not None and service["revision"] != expected_revision:
            raise Problem("listing_changed", "This service version changed. Search and review the current listing again.", 409)
        purchase = {"purchase_id": str(uuid.uuid4()), "slug": slug, "origin": self.origin, "owner": session["owner"],
                    "revision": service["revision"], "input": value, "status": "quoting", "mode": "testnet"}
        self.save(purchase)
        return await self.recover_quote(purchase["purchase_id"])

    async def recover_quote(self, purchase_id):
        purchase = self.get(purchase_id)
        if "order" in purchase:
            return self.public_purchase(purchase)
        session = self._session_for(purchase)
        if session is None:
            return self._connection_needed(purchase_id)
        try:
            response = await self.client.post(f"/v1/services/{purchase['slug']}/orders",
                headers={"Idempotency-Key": purchase_id, "Authorization": "Bearer " + session["token"]},
                json={"revision": purchase["revision"], "input": purchase["input"]})
            if response.status_code == 401:
                self.disconnect(session["token"])
                return self._connection_needed(purchase_id)
            response.raise_for_status()
            return self.public_purchase(self.apply_order(purchase_id, response.json()))
        except (httpx.HTTPError, ValueError, KeyError, Problem):
            def unknown(latest):
                if "order" not in latest:
                    latest.update(status="quote_unknown", error={"code": "quote_response_unknown",
                        "message": "The quote response is unknown. Recover using this original purchase ID."})
            return self.public_purchase(self.update(purchase_id, unknown))

    def approve_from_terminal(self, purchase_id, entered_digest):
        raise Problem("wallet_approval_required", "Approve testnet purchases with your wallet in the local browser gateway.", 409)

    def approve_from_browser(self, purchase_id, entered_digest, authorization):
        """The gateway calls this only with an actual browser wallet signature."""
        from eth_account import Account
        from .contracts import digest
        from .payment_signing import payment_message
        from .x402_adapter import X402Payments

        purchase = self.get(purchase_id)
        session = self._session_for(purchase)
        if session is None:
            raise Problem("authentication_required", "Connect the wallet that owns this purchase.", 401)
        order = purchase.get("order")
        if not order or entered_digest != order["order_digest"]:
            raise Problem("approval_mismatch", "The confirmation does not match the stored quote.", 409)
        self._validate_order(order, purchase)
        authorization_digest = digest(authorization)
        # A lost response retries the same proof without generating another one,
        # including after its signing window closes.
        if purchase.get("authorization_digest") == authorization_digest and purchase.get("approved_digest") == entered_digest:
            return self.public_purchase(purchase)
        try:
            if set(authorization) != {"x402Version", "payload", "accepted", "resource"}:
                raise ValueError("Unexpected payment fields")
            if set(authorization["payload"]) != {"authorization", "signature"}:
                raise ValueError("Unexpected signature fields")
            _, _, fields = X402Payments.bound_payload(authorization, order)
            if set(fields) != {"from", "to", "value", "validAfter", "validBefore", "nonce"}:
                raise ValueError("Unexpected authorization fields")
            if fields["validAfter"] != "0" or not isinstance(fields["validBefore"], str) or not re.fullmatch(r"[0-9]{1,12}", fields["validBefore"]):
                raise ValueError("Invalid authorization times")
            valid_before = int(fields["validBefore"])
            signature = authorization["payload"]["signature"]
            if not isinstance(signature, str) or not re.fullmatch(r"0x[0-9a-fA-F]{130}", signature):
                raise ValueError("Invalid EOA signature")
            _, _, signable = payment_message(order, fields["from"], valid_before=valid_before)
            recovered = Account.recover_message(signable, signature=signature)
            if recovered.lower() != order["owner"]:
                raise ValueError("Different signer")
        except Exception:
            raise Problem("invalid_authorization", "The wallet signature does not authorize this exact quote.", 401) from None
        encrypted = self.vault.seal(authorization, order)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            latest = json.loads(db.execute("SELECT document FROM purchases WHERE id=? AND origin=?",
                                           (purchase_id, self.origin)).fetchone()[0])
            if latest.get("authorization_digest") == authorization_digest and latest.get("approved_digest") == entered_digest:
                return self.public_purchase(latest)
            if (latest["status"] != "awaiting_approval" or latest.get("authorization_encrypted")
                    or latest["order"] != order or latest["order"]["payment"] != "required"):
                raise Problem("approval_mismatch", "This purchase cannot accept a new authorization. Refresh its existing state.", 409)
            current = int(time.time())
            active_session = db.execute("SELECT token_hash FROM buyer_wallet_sessions WHERE origin=?", (self.origin,)).fetchone()
            if not active_session or active_session[0] != digest(session["token"]) or session["expires_at"] <= current:
                raise Problem("authentication_required", "Reconnect the wallet that owns this purchase.", 401)
            if order["quote_expires_at"] <= current or not 120 <= valid_before - current <= 180:
                raise Problem("authorization_expiry", "The quote or wallet authorization has too little time remaining.", 409)
            reserved = sum(int(record["order"]["amount"]) for (document,) in db.execute("SELECT document FROM purchases")
                           if (record := json.loads(document)).get("budget_reserved"))
            if reserved + int(order["amount"]) > self.budget:
                raise Problem("budget_exceeded", "This approval would exceed the local buyer spending limit.", 409)
            latest.update(status="approved", approved_digest=entered_digest, approved_at=current,
                          authorization_digest=authorization_digest, authorization_encrypted=encrypted,
                          authorization_expires_at=valid_before, budget_reserved=True)
            db.execute("UPDATE purchases SET document=? WHERE id=? AND origin=?",
                       (canonical(latest), purchase_id, self.origin))
        return self.public_purchase(latest)

    async def purchase(self, purchase_id):
        purchase = self.get(purchase_id)
        session = self._session_for(purchase)
        if session is None:
            return self._connection_needed(purchase_id)
        def intent(latest):
            if latest["status"] in TERMINAL:
                return
            if latest["status"] not in {"approved", "submitted", "unknown"} or not latest.get("authorization_encrypted"):
                raise Problem("human_approval_required", "Approve this quote with your wallet in the local browser gateway.", 409)
            if latest["approved_digest"] != latest["order"]["order_digest"]:
                raise Problem("approval_mismatch", "The approved order does not match the current order.", 409)
            latest.update(status="unknown", admission_attempted=True)
        purchase = self.update(purchase_id, intent)
        if purchase["status"] in TERMINAL:
            return self.public_purchase(purchase)
        payload = self.vault.open(purchase["authorization_encrypted"], purchase["order"])
        try:
            response = await self.client.post(f"/v1/orders/{purchase['order']['id']}",
                headers={"Authorization": "Bearer " + session["token"]}, json={"authorization": payload})
            if response.status_code == 401:
                self.disconnect(session["token"])
                return self._connection_needed(purchase_id)
            response.raise_for_status()
            return self.public_purchase(self.apply_order(purchase_id, response.json()))
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            error = self.request_error(exc)
            self.update(purchase_id, lambda latest: latest.update(error=error) if latest["status"] not in TERMINAL else None)
            try:
                return await self.status(purchase_id)
            except (httpx.HTTPError, ValueError, KeyError):
                return self.public_purchase(self.get(purchase_id))

    async def status(self, purchase_id):
        purchase = self.get(purchase_id)
        if "order" not in purchase:
            return await self.recover_quote(purchase_id)
        session = self._session_for(purchase)
        if session is None:
            return self._connection_needed(purchase_id)
        requested_at = int(time.time())
        response = await self.client.get(f"/v1/orders/{purchase['order']['id']}",
                                         headers={"Authorization": "Bearer " + session["token"]})
        if response.status_code == 401:
            self.disconnect(session["token"])
            return self._connection_needed(purchase_id)
        response.raise_for_status()
        return self.public_purchase(self.apply_order(purchase_id, response.json(), observed_after=requested_at))
