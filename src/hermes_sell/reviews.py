"""Purchase-backed reviews. The directory never receives order inputs or sessions."""

import base64
from contextlib import closing
import json
import re
import secrets
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from eth_account import Account
from eth_account.messages import encode_defunct

from .contracts import ASSET, NETWORK, Problem, canonical, digest
from .discovery import listing_id


def issuer_key(store):
    with store.tx() as db:
        row = db.execute("SELECT value FROM meta WHERE key='review-issuer-key'").fetchone()
        if row:
            return Ed25519PrivateKey.from_private_bytes(base64.b64decode(row[0], validate=True))
        key = Ed25519PrivateKey.generate()
        encoded = base64.b64encode(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode()
        db.execute("INSERT INTO meta VALUES('review-issuer-key',?)", (encoded,))
        return key


def issuer_document(engine, origin):
    key = issuer_key(engine.store)
    return {"version": 1, "origin": origin,
            "public_key": base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()}


def purchase_proof(engine, order, origin):
    if order["payment"] != "settled" or order["execution"] != "result_ready" or not order.get("receipt"):
        raise Problem("purchase_required", "A completed, settled purchase is required to review this service.", 409)
    payload = {"version": 1, "mode": order["mode"], "origin": origin, "slug": order["slug"],
        "listing_id": listing_id(origin, order["slug"]), "order_id": order["id"], "revision": order["revision"],
        "payer": order["owner"], "amount": order["amount"], "pay_to": order["pay_to"], "network": order["network"],
        "asset": order["asset"], "payment_nonce": "0x" + order["order_digest"],
        "transaction": order["receipt"]["transaction"], "issued_at": int(time.time())}
    signature = base64.b64encode(issuer_key(engine.store).sign(canonical(payload).encode())).decode()
    return {"payload": payload, "signature": signature}


def review_message(payload):
    return ("Publish a Hermes Sell review\n"
        f"Directory: {payload['directory_origin']}\nListing: {payload['listing_id']}\nOrder: {payload['order_id']}\n"
        f"Rating: {payload['score']}/5\nComment: {canonical(payload['comment'])}\nWallet: {payload['payer']}\n"
        f"Nonce: {payload['nonce']}\nExpires: {payload['expires_at']}\n"
        "This signature publishes a review only. It does not authorize a payment.")


class ReviewStore:
    def __init__(self, directory, chain):
        self.directory, self.chain = directory, chain
        with closing(directory.db()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS review_challenges(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, listing_id TEXT NOT NULL,
                    document TEXT NOT NULL, expires INTEGER NOT NULL, signature_hash TEXT);
                CREATE TABLE IF NOT EXISTS reviews(
                    listing_id TEXT NOT NULL, payer TEXT NOT NULL, document TEXT NOT NULL, challenge_seq INTEGER NOT NULL,
                    PRIMARY KEY(listing_id,payer));
                CREATE TABLE IF NOT EXISTS review_receipts(
                    network TEXT, asset TEXT, payer TEXT, nonce TEXT, listing_id TEXT NOT NULL,
                    PRIMARY KEY(network,asset,payer,nonce));
            """)

    def summary(self, identity):
        with closing(self.directory.db()) as db:
            listing = db.execute("SELECT document FROM listings WHERE id=?", (identity,)).fetchone()
            mode = json.loads(listing[0])["mode"] if listing else None
            records = [json.loads(row[0]) for row in db.execute("SELECT document FROM reviews WHERE listing_id=?", (identity,))]
        records = [row for row in records if row.get("mode") == mode]
        return {"average": round(sum(row["score"] for row in records) / len(records), 2) if records else None,
                "count": len(records), "verified_count": sum(row["verification"] == "chain_and_seller" for row in records)}

    def list(self, identity):
        listing = self.directory.get(identity)
        with closing(self.directory.db()) as db:
            records = [json.loads(row[0]) for row in db.execute(
                "SELECT document FROM reviews WHERE listing_id=? ORDER BY challenge_seq DESC LIMIT 50", (identity,))]
        return {"summary": self.summary(identity), "reviews": [row for row in records if row.get("mode") == listing["mode"]]}

    async def challenge(self, identity, proof, score, comment):
        listing = self.directory.get(identity)
        if type(score) is not int or not 1 <= score <= 5 or not isinstance(comment, str) or len(comment) > 1000:
            raise Problem("invalid_review", "Choose 1-5 stars and a comment of at most 1,000 characters.", 422)
        try:
            payload = proof["payload"]
            if (set(payload) != {"version", "mode", "origin", "slug", "listing_id", "order_id", "revision", "payer", "amount", "pay_to", "network", "asset", "payment_nonce", "transaction", "issued_at"}
                    or payload["version"] != 1 or payload["origin"] != listing["origin"]
                    or payload["origin"] not in self.directory.sellers or payload["listing_id"] != identity
                    or payload["slug"] != listing["service"]["slug"] or payload["mode"] != listing["mode"]
                    or payload["network"] != NETWORK or payload["asset"] != ASSET
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", payload["order_id"])
                    or not re.fullmatch(r"[0-9a-f]{64}", payload["revision"])
                    or not re.fullmatch(r"0x[0-9a-f]{64}", payload["payment_nonce"])
                    or not re.fullmatch(r"[0-9]{1,8}", payload["amount"]) or not 1 <= int(payload["amount"]) <= 10_000_000
                    or not re.fullmatch(r"0x[0-9a-fA-F]{40}", payload["pay_to"])
                    or type(payload["issued_at"]) is not int or not int(time.time()) - 300 <= payload["issued_at"] <= int(time.time()) + 30):
                raise ValueError("Purchase proof scope mismatch")
            key_response = await self.directory.client.get(payload["origin"] + "/.well-known/hermes-sell/review-key")
            key_response.raise_for_status()
            if len(key_response.content) > 2000:
                raise ValueError("Invalid issuer")
            issuer = key_response.json()
            if issuer["version"] != 1 or issuer["origin"] != payload["origin"]:
                raise ValueError("Issuer origin mismatch")
            Ed25519PublicKey.from_public_bytes(base64.b64decode(issuer["public_key"], validate=True)).verify(
                base64.b64decode(proof["signature"], validate=True), canonical(payload).encode())
            if payload["mode"] == "testnet":
                if not re.fullmatch(r"0x[0-9a-f]{40}", payload["payer"]) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", payload["transaction"]):
                    raise ValueError("Invalid payment identity")
                if payload["payer"] == payload["pay_to"].lower():
                    raise ValueError("Seller self-review")
            elif payload["payer"] not in {"demo-browser", "demo-hermes", "demo-other"} or payload["transaction"] != "simulation:" + payload["order_id"]:
                raise ValueError("Invalid simulation proof")
        except Exception as exc:
            raise Problem("invalid_purchase_proof", "The review requires a valid settled purchase from this seller.", 401) from exc
        nonce = secrets.token_hex(32)
        expires = int(time.time()) + 300
        public = {"directory_origin": self.directory.origin, "listing_id": identity, "order_id": payload["order_id"],
                  "payer": payload["payer"], "score": score, "comment": comment, "nonce": nonce, "expires_at": expires}
        with closing(self.directory.db()) as db:
            db.execute("DELETE FROM review_challenges WHERE expires<? AND signature_hash IS NULL", (int(time.time()),))
            db.execute("INSERT INTO review_challenges(id,listing_id,document,expires) VALUES(?,?,?,?)",
                       (nonce, identity, canonical({"public": public, "proof": payload}), expires))
            db.commit()
        return {"challenge_id": nonce, "payload": public, "message": review_message(public), "mode": payload["mode"]}

    async def submit(self, identity, challenge_id, signature=None):
        with closing(self.directory.db()) as db:
            row = db.execute("SELECT seq,document,expires,signature_hash FROM review_challenges WHERE id=? AND listing_id=?",
                             (challenge_id, identity)).fetchone()
        if not row:
            raise Problem("review_challenge_missing", "Prepare this review again.", 409)
        document = json.loads(row[1]); public, proof = document["public"], document["proof"]
        if self.directory.get(identity)["mode"] != proof["mode"]:
            raise Problem("listing_changed", "The service payment mode changed. Refresh this listing.", 409)
        signature_hash = digest(signature)
        if row[3]:
            if row[3] != signature_hash:
                raise Problem("review_retry_mismatch", "Retry the original review signature.", 409)
            return self.list(identity)
        if row[2] <= int(time.time()):
            raise Problem("review_challenge_expired", "Prepare this review again; its signature window expired.", 409)
        if proof["mode"] == "testnet":
            try:
                if not isinstance(signature, str) or not re.fullmatch(r"0x[0-9a-fA-F]{130}", signature):
                    raise ValueError("Invalid signature")
                recovered = Account.recover_message(encode_defunct(text=review_message(public)), signature=signature)
                if recovered.lower() != proof["payer"]:
                    raise ValueError("Different reviewer")
            except Exception as exc:
                raise Problem("review_wallet_mismatch", "Sign this review with the wallet that bought the service.", 401) from exc
            receipt = await self.chain.confirm({**proof, "owner": proof["payer"]}, proof["transaction"])
            if not receipt:
                raise Problem("review_payment_unconfirmed", "The directory cannot confirm this purchase onchain yet. Retry the same review.", 409)
            verification = "chain_and_seller"
        else:
            if signature is not None:
                raise Problem("invalid_review", "Simulation reviews do not use wallet signatures.", 422)
            verification = "simulation"
        timestamp = int(time.time())
        result = {"score": public["score"], "comment": public["comment"], "revision": proof["revision"],
                  "reviewer": proof["payer"][:6] + "…" + proof["payer"][-4:] if proof["mode"] == "testnet" else proof["payer"],
                  "verification": verification, "mode": proof["mode"], "updated_at": timestamp}
        with closing(self.directory.db()) as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT expires,signature_hash FROM review_challenges WHERE id=?", (challenge_id,)).fetchone()
            if current[1]:
                if current[1] != signature_hash:
                    raise Problem("review_retry_mismatch", "Retry the original review signature.", 409)
                return self.list(identity)
            if current[0] <= timestamp:
                raise Problem("review_challenge_expired", "Prepare this review again.", 409)
            previous = db.execute("SELECT challenge_seq FROM reviews WHERE listing_id=? AND payer=?", (identity, proof["payer"])).fetchone()
            if previous and previous[0] > row[0]:
                raise Problem("review_changed", "A newer review has already been published. Refresh it.", 409)
            used = db.execute("SELECT listing_id FROM review_receipts WHERE network=? AND asset=? AND payer=? AND nonce=?",
                             (proof["network"], proof["asset"], proof["payer"], proof["payment_nonce"])).fetchone()
            if used and used[0] != identity:
                raise Problem("review_receipt_reused", "This payment has already been used for a different service review.", 409)
            db.execute("INSERT OR IGNORE INTO review_receipts VALUES(?,?,?,?,?)", (proof["network"], proof["asset"], proof["payer"], proof["payment_nonce"], identity))
            db.execute("INSERT OR REPLACE INTO reviews VALUES(?,?,?,?)", (identity, proof["payer"], canonical(result), row[0]))
            db.execute("UPDATE review_challenges SET signature_hash=? WHERE id=?", (signature_hash, challenge_id))
            db.commit()
        return self.list(identity)
