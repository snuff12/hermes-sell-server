"""Separate public storefront and private owner applications."""

import asyncio
from contextlib import asynccontextmanager, suppress
import json
from pathlib import Path
import secrets
from urllib.parse import urlparse

import httpx

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .contracts import PROTOCOL, Problem, canonical, make_revision
from .engine import Engine


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QuoteBody(StrictBody):
    revision: str
    input: dict


class ApprovalBody(StrictBody):
    authorization: dict


class DemoSessionBody(StrictBody):
    actor: str


class WalletChallengeBody(StrictBody):
    address: str = Field(min_length=42, max_length=42)


class WalletVerifyBody(StrictBody):
    payload: dict


class ImportDraftBody(StrictBody):
    draft_id: str = Field(min_length=64, max_length=64)
    slug: str
    amount: str
    pay_to: str


class DraftBody(StrictBody):
    spec: dict
    skill_text: str = Field(min_length=1, max_length=80000)


class PublishBody(StrictBody):
    report_id: str
    owner_reviewed: bool


class ReconcileBody(StrictBody):
    transaction: str = Field(pattern=r"^0x[0-9a-fA-F]{64}$")


class ListingBody(StrictBody):
    share_usage: bool = Field(default=False, strict=True)


class ReviewChallengeBody(StrictBody):
    score: int = Field(ge=1, le=5, strict=True)
    comment: str = Field(max_length=1000)


class ReviewSubmitBody(StrictBody):
    challenge_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature: str | None = None


def base_app(engine: Engine, origin: str, *, worker=False, private=False):
    mode = engine.payments.mode
    if mode not in {"simulation", "testnet"}:
        raise RuntimeError("Only simulation and Base Sepolia testnet modes are supported.")
    if mode == "testnet" and (engine.runtime.mode != "hermes" or engine.vault is None
                              or getattr(engine.payments, "chain", None) is None):
        raise RuntimeError("Testnet hosting requires Hermes execution, encrypted authorizations and chain reconciliation.")
    parsed = urlparse(origin)
    if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or not parsed.netloc:
        raise ValueError("Configure an exact origin without a path or credentials.")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "testserver"}):
        raise ValueError("Public origins require HTTPS.")

    @asynccontextmanager
    async def lifespan(app):
        task = None
        if worker:
            engine.recover_interrupted()

            async def work():
                while True:
                    await engine.tick()
                    await asyncio.sleep(.25)

            task = asyncio.create_task(work())
            app.state.worker = task
        yield
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Hermes Sell", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(Problem)
    async def problem_handler(request, exc):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({"error": {"code": "invalid_request", "message": "Check the required request fields."}}, status_code=422)

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        # A demo session is intentionally not a wallet authentication mechanism.
        # Never allow it through a reverse proxy or a public/tunnel hostname.
        local_only = private or mode == "simulation"
        proxied = any(name.startswith("x-forwarded-") or name in {"cf-connecting-ip", "forwarded", "x-real-ip"}
                      for name in request.headers)
        if local_only and (request.url.hostname not in {"localhost", "127.0.0.1", "testserver"} or proxied):
            return JSONResponse({"error": {"code": "local_only", "message": "This preview is available locally only."}}, status_code=403)
        if not local_only and request.headers.get("host", "").lower() != parsed.netloc.lower():
            return JSONResponse({"error": {"code": "host_denied", "message": "Use the configured seller origin."}}, status_code=403)
        if request.headers.get("origin") not in {None, origin}:
            return JSONResponse({"error": {"code": "origin_denied", "message": "Requests from other origins are not allowed."}}, status_code=403)
        if request.method in {"POST", "PUT", "PATCH"}:
            body = await request.body()
            if len(body) > 180_000:
                return JSONResponse({"error": {"code": "body_too_large", "message": "The request is too large."}}, status_code=413)
            if body:
                try:
                    # Reject non-finite numbers and invalid Unicode before model
                    # validation can include those values in an error response.
                    canonical(json.loads(body))
                except (ValueError, UnicodeError, RecursionError, Problem):
                    return JSONResponse({"error": {"code": "invalid_json", "message": "JSON must contain only finite numbers and valid UTF-8 strings."}}, status_code=422)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    return app


def attach_static(app: FastAPI, web_dist: Path | None, routes: list[str]):
    if not web_dist or not (web_dist / "index.html").exists():
        return
    app.mount("/assets", StaticFiles(directory=web_dist / "assets"), name="assets")
    for route in routes:
        app.add_api_route(route, lambda: FileResponse(web_dist / "index.html"), methods=["GET"], include_in_schema=False)


def public_app(engine: Engine, origin="http://127.0.0.1:8765", web_dist=None, *, worker=True, wallet_auth=None,
               directory_origin="http://127.0.0.1:8795"):
    app = base_app(engine, origin, worker=worker)
    mode = engine.payments.mode
    if mode == "testnet" and (wallet_auth is None or wallet_auth.origin != origin):
        raise RuntimeError("Testnet hosting requires wallet authentication bound to the seller origin.")

    def worker_ready():
        task = getattr(app.state, "worker", None)
        return not worker or (task is not None and not task.done())

    def require_worker():
        if not worker_ready():
            raise Problem("worker_unavailable", "The execution worker is not ready. Check existing orders and the server status.", 503)

    @app.get("/ui-config")
    async def ui_config():
        from .contracts import NETWORK, ASSET
        return {"role": "storefront", "mode": mode, "origin": origin, "network": NETWORK, "asset": ASSET,
                "auth": "development" if mode == "simulation" else "siwx", "execution_mode": engine.runtime.mode,
                "directory_origin": directory_origin, "features": ["reviews"]}

    def owner(request):
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise Problem("authentication_required", "Connect a session first.", 401)
        return wallet_auth.owner(header[7:]) if mode == "testnet" else engine.owner(header[7:])

    @app.get("/health/ready")
    async def health():
        ready = engine.ready() and worker_ready()
        return JSONResponse({"ready": ready, "mode": mode, "live_ready": ready and mode == "testnet", "protocol": PROTOCOL,
                             "limitations": (["No real wallet authentication", "No model execution", "No onchain payment"]
                                             if mode == "simulation" else ["Base Sepolia test tokens only"]),
                             "recovery_required": engine.store.recovery_required}, status_code=200 if ready else 503)

    @app.get("/v1/services")
    async def services():
        return {"services": engine.services(), "mode": mode}

    @app.get("/.well-known/hermes-sell/listings/{slug}")
    async def directory_publication(slug: str):
        from .directory import public_publication
        return public_publication(engine, slug, origin)

    @app.get("/.well-known/hermes-sell/review-key")
    async def review_key():
        from .reviews import issuer_document
        return issuer_document(engine, origin)

    @app.get("/v1/services/{slug}")
    async def service(slug: str):
        return engine.service(slug)

    @app.get("/v1/services/{slug}/openapi.json")
    async def service_openapi(slug: str):
        service = engine.service(slug)
        return {"openapi": "3.1.0", "info": {"title": service["name"], "version": service["revision"]},
                "servers": [{"url": origin}], "x-hermes-sell-protocol": PROTOCOL, "x-hermes-sell-mode": mode,
                "paths": {f"/v1/services/{slug}/orders": {"post": {"summary": "Create an authenticated quote; does not execute or charge",
                    "security": [{"bearerAuth": []}], "parameters": [{"in": "header", "name": "Idempotency-Key", "required": True,
                    "schema": {"type": "string"}}], "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object", "required": ["revision", "input"], "properties": {"revision": {"const": service["revision"]}, "input": service["input_schema"]}}}}},
                    "responses": {"201": {"description": "Immutable quote"}, "409": {"description": "Revision or idempotency conflict"}}}},
                    "/v1/orders/{order_id}": {"parameters": [{"in": "path", "name": "order_id", "required": True, "schema": {"type": "string"}}],
                    "get": {"summary": "Read original order; no payment", "security": [{"bearerAuth": []}], "responses": {"200": {"description": "Order status; result only after settlement"}}},
                    "post": {"summary": "Admit a previously approved authorization", "security": [{"bearerAuth": []}],
                    "responses": {"202": {"description": "Accepted, not settled"}, "402": {"description": "Authorization required"}}}}},
                "components": {"securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}}}

    @app.post("/dev/session")
    async def demo_session(body: DemoSessionBody):
        if mode != "simulation":
            raise Problem("not_found", "Route not found.", 404)
        if body.actor not in {"demo-browser", "demo-hermes", "demo-other"}:
            raise Problem("invalid_actor", "Only the designated simulation accounts are available.")
        return engine.session(body.actor)

    @app.post("/auth/challenge")
    async def wallet_challenge(body: WalletChallengeBody):
        if mode != "testnet":
            raise Problem("not_found", "Route not found.", 404)
        return wallet_auth.challenge(body.address)

    @app.post("/auth/verify")
    async def wallet_verify(body: WalletVerifyBody):
        if mode != "testnet":
            raise Problem("not_found", "Route not found.", 404)
        return await wallet_auth.verify(body.payload)

    @app.post("/v1/services/{slug}/orders", status_code=201)
    async def create_order(slug: str, body: QuoteBody, request: Request, idempotency_key: str = Header()):
        require_worker()
        return engine.create_order(owner(request), slug, body.revision, body.input, idempotency_key, origin)

    @app.get("/v1/orders/{order_id}")
    async def get_order(order_id: str, request: Request):
        return engine.view(engine.owned(order_id, owner(request)))

    @app.post("/v1/orders/{order_id}/review-challenge")
    async def review_challenge(order_id: str, body: ReviewChallengeBody, request: Request):
        from .review_proxy import forward_review
        return await forward_review(engine, engine.owned(order_id, owner(request)), origin, directory_origin,
                                    score=body.score, comment=body.comment)

    @app.post("/v1/orders/{order_id}/review")
    async def submit_review(order_id: str, body: ReviewSubmitBody, request: Request):
        from .review_proxy import forward_review
        return await forward_review(engine, engine.owned(order_id, owner(request)), origin, directory_origin,
                                    challenge_id=body.challenge_id, signature=body.signature)

    @app.post("/v1/orders/{order_id}", status_code=202)
    async def admit(order_id: str, request: Request, body: ApprovalBody | None = None):
        actor = owner(request)
        order = engine.owned(order_id, actor)
        if body is None:
            return JSONResponse({"mode": mode, "authorization_required": True, "order": engine.view(order)}, status_code=402)
        return await engine.admit(order_id, actor, body.authorization, admission_guard=require_worker)

    attach_static(app, web_dist, ["/", "/s/{slug}"])
    return app


def admin_app(engine: Engine, token: str, origin="http://127.0.0.1:8766", web_dist=None, *, draft_store=None,
              seller_origin=None, directory_origin="http://127.0.0.1:8795"):
    app = base_app(engine, origin, private=True)
    from .discovery import exact_origin
    exact_origin(directory_origin, local_only=True)

    @app.get("/ui-config")
    async def ui_config():
        return {"role": "studio", "mode": engine.payments.mode, "execution_mode": engine.runtime.mode,
                "directory_origin": directory_origin, "seller_origin": seller_origin,
                "features": ["analytics", "seller_setup"]}

    @app.get("/admin/listings")
    async def owner_listings():
        return {"listings": [json.loads(row[0]) for row in engine.store.all(
            "SELECT value FROM meta WHERE key LIKE 'directory-publication:%'")]}

    @app.post("/admin/listings/{revision}")
    async def list_service(revision: str, body: ListingBody | None = None):
        from .directory import publication_key
        from .discovery import DirectoryClient
        public = engine.revision(revision)["public"]
        if engine.service(public["slug"])["revision"] != revision:
            raise Problem("publication_required", "Publish and review this service version before listing it.", 409)
        if seller_origin is None:
            raise Problem("seller_origin_required", "Configure the seller origin before listing services.", 409)
        record = {"slug": public["slug"], "revision": revision, "directory_origin": directory_origin,
                  "enabled": True, "share_usage": body.share_usage if body else False}
        with engine.store.tx() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (publication_key(public["slug"]), canonical(record)))
        client = DirectoryClient(directory_origin)
        try:
            listing = await client.register(seller_origin, public["slug"])
            return {"listed": True, "listing": listing}
        except (httpx.HTTPError, Problem, ValueError):
            return {"listed": False, "publication_enabled": True,
                    "message": "Directory unavailable. The publication is saved; retry listing this version."}
        finally:
            await client.close()

    @app.post("/admin/listings/{revision}/withdraw")
    async def withdraw_service(revision: str):
        from .directory import publication_key
        from .discovery import DirectoryClient
        public = engine.revision(revision)["public"]
        with engine.store.tx() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (publication_key(public["slug"]),)).fetchone()
            if row:
                record = json.loads(row[0])
                if record["revision"] != revision:
                    raise Problem("revision_changed", "Select the currently listed service version.", 409)
                record["enabled"] = False
                db.execute("UPDATE meta SET value=? WHERE key=?", (canonical(record), publication_key(public["slug"])))
        client = DirectoryClient(directory_origin)
        try:
            if seller_origin:
                await client.register(seller_origin, public["slug"])
        except (httpx.HTTPError, Problem, ValueError):
            pass
        finally:
            await client.close()
        return {"withdrawn": True}

    @app.get("/admin/templates")
    async def templates():
        root = Path(__file__).resolve().parents[2] / "examples"
        return {"templates": [{"spec": json.loads((path / "service.json").read_text()),
                               "skill_text": (path / "SKILL.md").read_text()}
                              for path in sorted(root.iterdir()) if path.is_dir()]}

    @app.get("/admin/analytics")
    async def analytics(period: str = "30d"):
        from .analytics import seller_analytics
        return seller_analytics(engine, period)

    @app.get("/admin/seller")
    async def seller_setup():
        services = engine.services()
        return {"seller_origin": seller_origin, "owner_origin": origin, "directory_origin": directory_origin,
                "mode": engine.payments.mode, "ready": engine.ready(), "published_services": len(services),
                "directory_publications": sum(json.loads(row[0]).get("enabled", False) for row in engine.store.all(
                    "SELECT value FROM meta WHERE key LIKE 'directory-publication:%'"))}

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if request.url.path.startswith("/admin/"):
            if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
                return JSONResponse({"error": {"code": "owner_auth_required", "message": "Enter the owner token."}}, status_code=401)
        return await call_next(request)

    @app.get("/admin/revisions")
    async def revisions():
        return {"revisions": [{"public": json.loads(r["document"])["public"], "published": bool(r["published"])}
                              for r in engine.store.all("SELECT * FROM revisions ORDER BY created_at DESC")]}

    @app.get("/admin/revisions/{revision}")
    async def owner_revision(revision: str):
        return engine.revision(revision)

    @app.post("/admin/drafts", status_code=201)
    async def create_draft(body: DraftBody):
        bundle = {"files": {"SKILL.md": body.skill_text}, "wrapper_version": 1,
                  "policy": "text-json-no-tools-v1", "model": "runtime-default"}
        try:
            revision = make_revision(body.spec, bundle)
        except (KeyError, TypeError, ValueError) as exc:
            raise Problem("invalid_draft", "Check the required fields in the service definition.", 422) from exc
        return engine.put_revision(revision)

    @app.post("/admin/drafts/import", status_code=201)
    async def import_draft(body: ImportDraftBody):
        if draft_store is None:
            raise Problem("draft_store_unavailable", "Configure the shared private draft directory first.", 503)
        record = draft_store.get(body.draft_id)
        spec = {**record["draft"], "slug": body.slug, "amount": body.amount, "pay_to": body.pay_to}
        return engine.put_revision(make_revision(spec, record["bundle"]))

    @app.post("/admin/orders/{order_id}/reconcile")
    async def reconcile_order(order_id: str, body: ReconcileBody | None = None):
        return await engine.reconcile_order(order_id, transaction=body.transaction if body else None)

    @app.post("/admin/revisions/{revision}/test")
    async def test_revision(revision: str):
        return await engine.test_revision(revision)

    @app.post("/admin/revisions/{revision}/publish")
    async def publish(revision: str, body: PublishBody):
        return engine.publish(revision, body.report_id, body.owner_reviewed)

    @app.get("/admin/orders")
    async def orders():
        return {"orders": [engine.view(json.loads(r[0])) for r in engine.store.all("SELECT document FROM orders ORDER BY created_at DESC LIMIT 100")]}

    attach_static(app, web_dist, ["/"])
    return app
