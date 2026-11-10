"""Authenticated seller-side review bridge; no buyer sessions leave the seller."""

import httpx
import json

from .contracts import Problem, canonical
from .discovery import DirectoryClient, listing_id
from .reviews import purchase_proof


async def forward_review(engine, order, origin, directory_origin, *, score=None, comment=None, challenge_id=None, signature=None):
    client = DirectoryClient(directory_origin)
    identity = listing_id(origin, order["slug"])
    try:
        if challenge_id is None:
            result = await client.review_challenge(identity, purchase_proof(engine, order, origin), score, comment)
            payload = result.get("payload", {}) if isinstance(result, dict) else {}
            if (not isinstance(payload, dict) or payload.get("order_id") != order["id"] or payload.get("listing_id") != identity
                    or payload.get("directory_origin") != directory_origin or payload.get("payer") != order["owner"]
                    or payload.get("score") != score or payload.get("comment") != comment
                    or result.get("challenge_id") != payload.get("nonce")):
                raise Problem("invalid_review_challenge", "The review challenge does not match this purchase.", 502)
            with engine.store.tx() as db:
                db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("review-challenge:" + result["challenge_id"],
                    canonical({"order_id": order["id"], "directory_origin": directory_origin})))
            return result
        row = engine.store.one("SELECT value FROM meta WHERE key=?", ("review-challenge:" + challenge_id,))
        if not row or json.loads(row[0]) != {"order_id": order["id"], "directory_origin": directory_origin}:
            raise Problem("review_order_mismatch", "Prepare the review from this original purchase first.", 409)
        return await client.publish_review(identity, challenge_id, signature)
    except httpx.HTTPStatusError as exc:
        messages = {
            "review_payment_unconfirmed": "The directory cannot confirm this payment yet. Retry the same review.",
            "review_wallet_mismatch": "Sign with the wallet that bought this service.",
            "review_challenge_expired": "The review expired. Prepare it again.",
            "review_changed": "A newer review has been published. Refresh before editing it.",
            "listing_not_found": "List this service in the directory before reviewing it.",
            "invalid_purchase_proof": "The directory could not verify this completed purchase.",
        }
        code = None
        try:
            error = exc.response.json()
            if isinstance(error, dict) and isinstance(error.get("error"), dict):
                code = error["error"].get("code")
                if not isinstance(code, str):
                    code = None
        except (ValueError, UnicodeError):
            pass
        raise Problem(code if code in messages else "review_unavailable", messages.get(code, "The review service is unavailable. Retry the original review."), 409) from None
    except httpx.HTTPError:
        raise Problem("review_unavailable", "The review service is unavailable. Retry the original review.", 502) from None
    finally:
        await client.close()
