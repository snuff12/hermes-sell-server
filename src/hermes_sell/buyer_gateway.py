"""Loopback browser approval for a signer-free testnet buyer."""

from contextlib import asynccontextmanager
import json
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import BaseModel, ConfigDict, Field

from .buyer import TestnetBuyer
from .contracts import ASSET, NETWORK, Problem, canonical


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WalletChallengeBody(StrictBody):
    address: str


class WalletProofBody(StrictBody):
    payload: dict


class BrowserApprovalBody(StrictBody):
    order_digest: str
    authorization: dict


class ReviewChallengeBody(StrictBody):
    score: int = Field(strict=True, ge=1, le=5)
    comment: str = Field(max_length=1000)


class ReviewSubmitBody(StrictBody):
    challenge_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature: str | None = Field(default=None, max_length=132)


def buyer_gateway_app(buyer: TestnetBuyer, web_dist: Path | None = None, *, directory_origin=None):
    """Serve only on the configured loopback address; never expose via a tunnel."""
    if not isinstance(buyer, TestnetBuyer):
        raise ValueError("The approval gateway requires a testnet buyer.")
    origin = buyer.approval_origin
    host = urlparse(origin).netloc
    from .buyer_hub import BuyerHub
    hub = BuyerHub(buyer, directory_origin) if directory_origin else None

    async def selected(request, purchase_id=None, listing_id=None):
        purchase_header = request.headers.get("x-purchase-id")
        listing_header = request.headers.get("x-listing-id")
        if purchase_id and purchase_header and purchase_id != purchase_header:
            raise Problem("purchase_context_mismatch", "The approval context does not match this purchase.", 409)
        identity = purchase_id or purchase_header
        listing_id = listing_id or listing_header
        if identity and listing_id:
            raise Problem("ambiguous_seller", "Use one purchase or listing context.", 422)
        if hub:
            if identity:
                return hub.for_purchase(identity)
            if listing_id:
                return await hub.for_listing(listing_id)
        elif listing_id:
            raise Problem("directory_unavailable", "This gateway has no service directory configured.", 409)
        return buyer

    @asynccontextmanager
    async def lifespan(app):
        yield
        await hub.close() if hub else await buyer.close()

    app = FastAPI(title="Hermes Sell · Buyer approval", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    def failure(code, message, status):
        return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)

    @app.exception_handler(Problem)
    async def problem_handler(request, exc):
        return failure(exc.code, exc.message, exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Validation diagnostics can include submitted signatures; never echo them.
        return failure("invalid_request", "Check the required request fields.", 422)

    @app.exception_handler(httpx.HTTPError)
    async def upstream_error(request, exc):
        return failure("seller_unavailable", "The seller response could not be confirmed. Refresh the original purchase or reconnect your wallet.", 502)

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        headers = request.headers
        forwarded = any(name.startswith("x-forwarded-") or name in {"forwarded", "cf-connecting-ip", "x-real-ip"}
                        for name in headers)
        if (len(headers.getlist("host")) != 1 or headers.get("host") != host or forwarded
                or request.client is None or request.client.host not in {"127.0.0.1", "::1"}):
            return failure("local_only", "The buyer approval gateway is available on its configured loopback origin only.", 403)
        supplied_origins = headers.getlist("origin")
        mutating = request.method not in {"GET", "HEAD", "OPTIONS"}
        if len(supplied_origins) > 1 or (supplied_origins and supplied_origins != [origin]) or (mutating and supplied_origins != [origin]):
            return failure("origin_denied", "Open the local approval page to perform this action.", 403)
        if (headers.get("sec-fetch-site") in {"cross-site", "same-site"}
                and not (headers.get("sec-fetch-mode") == "navigate" and request.method == "GET"
                         and not request.url.path.startswith(("/buyer/", "/ui-config")))):
            return failure("origin_denied", "Requests from other sites are not allowed.", 403)
        if mutating:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 180_000:
                    return failure("body_too_large", "The request is too large.", 413)
            request._body = bytes(body)
            if body:
                if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                    return failure("invalid_content_type", "Send a JSON request.", 415)
                try:
                    canonical(json.loads(body))
                except (ValueError, UnicodeError, RecursionError, Problem):
                    return failure("invalid_json", "Send valid JSON with finite numbers and UTF-8 strings.", 422)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.get("/ui-config")
    async def config(request: Request, purchase_id: str | None = None, listing_id: str | None = None):
        purchase_id = purchase_id or request.headers.get("x-purchase-id")
        listing_id = listing_id or request.headers.get("x-listing-id")
        target = await selected(request, purchase_id, listing_id)
        return {"role": "buyer", "mode": "testnet", "auth": "siwx", "origin": origin,
                "seller_origin": target.origin, "network": NETWORK, "asset": ASSET, "buyer_budget": str(target.budget),
                "listing_id": listing_id, "purchase_id": purchase_id, "directory_origin": directory_origin,
                "features": ["analytics", "reviews"] if directory_origin else ["analytics"]}

    @app.get("/buyer/session")
    async def session(request: Request):
        return (await selected(request)).session_info()

    @app.post("/buyer/auth/challenge")
    async def challenge(body: WalletChallengeBody, request: Request):
        return await (await selected(request)).wallet_challenge(body.address)

    @app.post("/buyer/auth/verify")
    async def verify(body: WalletProofBody, request: Request):
        return await (await selected(request)).wallet_verify(body.payload)

    @app.post("/buyer/auth/disconnect")
    async def disconnect(request: Request):
        return (await selected(request)).disconnect()

    @app.get("/buyer/purchases")
    async def history():
        if hub:
            return {"purchases": hub.purchases()}
        with buyer.db() as db:
            records = [json.loads(row[0]) for row in db.execute("SELECT document FROM purchases WHERE origin=? ORDER BY rowid DESC LIMIT 100", (buyer.origin,))]
        return {"purchases": [buyer.public_purchase(record) for record in records]}

    @app.get("/buyer/analytics")
    async def analytics():
        from .analytics import buyer_analytics
        return buyer_analytics(buyer)

    async def forward_purchase_review(request, purchase_id, action, body):
        target = await selected(request, purchase_id)
        purchase = target.get(purchase_id)
        if purchase["status"] != "settled" or purchase.get("order", {}).get("payment") != "settled":
            raise Problem("purchase_required", "Only settled purchases can be reviewed.", 409)
        session = target._session_for(purchase)
        if session is None:
            raise Problem("authentication_required", "Reconnect the original wallet to review this purchase.", 401)
        response = await target.client.post("/v1/orders/" + purchase["order"]["id"] + "/" + action,
            json=body.model_dump(), headers={"Authorization": "Bearer " + session["token"]})
        if not response.is_success:
            raise Problem("review_unavailable", "The review could not be confirmed. Reconnect the original wallet or retry the same review.", 409)
        try:
            if len(response.content) > 100_000:
                raise ValueError("Oversized review response")
            document = response.json()
            if not isinstance(document, dict):
                raise ValueError("Invalid review response")
        except (ValueError, UnicodeError):
            raise Problem("review_unavailable", "The review response could not be confirmed. Retry the same review.", 502) from None
        if action == "review-challenge":
            from .discovery import listing_id
            payload = document.get("payload", {})
            fields = {"directory_origin", "listing_id", "order_id", "payer", "score", "comment", "nonce", "expires_at"}
            if (set(document) != {"challenge_id", "payload", "message", "mode"} or not isinstance(payload, dict) or set(payload) != fields
                    or payload["order_id"] != purchase["order"]["id"] or payload["payer"] != session["owner"]
                    or payload["listing_id"] != listing_id(target.origin, purchase["slug"])
                    or payload["directory_origin"] != directory_origin or payload["score"] != body.score
                    or payload["comment"] != body.comment or document["challenge_id"] != payload["nonce"]
                    or document["mode"] != "testnet"):
                raise Problem("invalid_review_challenge", "The seller returned a mismatched review challenge.", 502)
            return document
        if not isinstance(document, dict) or set(document) != {"summary", "reviews"}:
            raise Problem("review_unavailable", "The review response could not be confirmed. Retry the same review.", 502)
        return {"published": True}

    @app.post("/buyer/purchases/{purchase_id}/review-challenge")
    async def prepare_review(purchase_id: str, body: ReviewChallengeBody, request: Request):
        return await forward_purchase_review(request, purchase_id, "review-challenge", body)

    @app.post("/buyer/purchases/{purchase_id}/review")
    async def submit_review(purchase_id: str, body: ReviewSubmitBody, request: Request):
        return await forward_purchase_review(request, purchase_id, "review", body)

    @app.get("/buyer/purchases/{purchase_id}")
    async def purchase(purchase_id: str, request: Request):
        target = await selected(request, purchase_id)
        return target.public_purchase(target.get(purchase_id))

    @app.get("/buyer/purchases/{purchase_id}/status")
    async def purchase_status(purchase_id: str, request: Request):
        return await (await selected(request, purchase_id)).status(purchase_id)

    @app.post("/buyer/purchases/{purchase_id}/approve")
    async def approve(purchase_id: str, body: BrowserApprovalBody, request: Request):
        return (await selected(request, purchase_id)).approve_from_browser(purchase_id, body.order_digest, body.authorization)

    if web_dist is not None:
        web_dist = Path(web_dist)
        if (web_dist / "index.html").exists():
            app.mount("/assets", StaticFiles(directory=web_dist / "assets"), name="assets")
            for route in ("/", "/connect", "/approve/{purchase_id}", "/purchases"):
                app.add_api_route(route, lambda: FileResponse(web_dist / "index.html"), methods=["GET"], include_in_schema=False)
    return app
