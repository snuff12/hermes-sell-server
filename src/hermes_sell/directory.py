"""A small directory of owner-published services, separate from plugin installs."""

import asyncio
from contextlib import asynccontextmanager, closing, suppress
import json
from pathlib import Path
import re
import sqlite3
import time

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .contracts import PROTOCOL, Problem, canonical
from .discovery import exact_origin, listing_id, public_contract
from .local_http import local_app, static_workspace


def publication_key(slug):
    return "directory-publication:" + slug


def public_publication(engine, slug, origin):
    row = engine.store.one("SELECT value FROM meta WHERE key=?", (publication_key(slug),))
    if not row:
        raise Problem("not_listed", "This service is not listed in a directory.", 404)
    record = json.loads(row[0])
    service = engine.service(slug)
    if not record["enabled"] or record["revision"] != service["revision"]:
        raise Problem("not_listed", "This service listing has been withdrawn or replaced.", 404)
    publication = {"protocol": PROTOCOL, "directory_origin": record["directory_origin"], "origin": origin,
                   "mode": engine.payments.mode, "service": service}
    if record.get("share_usage"):
        from .analytics import public_service_usage
        publication["usage"] = public_service_usage(engine, slug)
    return publication


class ServiceDirectory:
    def __init__(self, path, origin, sellers, *, transport=None, chain=None):
        self.origin = exact_origin(origin, local_only=True)
        # Explicit loopback peers prevent directory registration from becoming
        # an arbitrary URL fetcher. Public federation requires a separate policy.
        self.sellers = frozenset(exact_origin(value, local_only=True) for value in sellers)
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(self.db()) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS listings(id TEXT PRIMARY KEY, origin TEXT NOT NULL, slug TEXT NOT NULL, version INTEGER NOT NULL, document TEXT NOT NULL)")
            connection.commit()
        self.path.chmod(0o600)
        self.client = httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False, transport=transport)
        from .chain import ChainReader
        from .reviews import ReviewStore
        self.chain = chain or ChainReader("https://sepolia.base.org")
        self.reviews = ReviewStore(self, self.chain)

    def db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def get(self, identity):
        with closing(self.db()) as connection:
            row = connection.execute("SELECT document FROM listings WHERE id=?", (identity,)).fetchone()
        if not row:
            raise Problem("listing_not_found", "Service listing not found.", 404)
        record = json.loads(row[0])
        if record["state"] == "online" and int(time.time()) - record["checked_at"] > 90:
            record["state"] = "stale"
        record["ratings"] = self.reviews.summary(identity)
        return record

    def search(self, query="", mode=None):
        query = query.casefold().strip()[:150]
        with closing(self.db()) as connection:
            identities = [row[0] for row in connection.execute("SELECT id FROM listings ORDER BY rowid DESC LIMIT 1000")]
        records = [self.get(identity) for identity in identities]
        matches = [record for record in records if record["state"] != "withdrawn"
            and (not mode or record["mode"] == mode)
            and query in (record["service"]["name"] + " " + record["service"]["description"] + " " + record["origin"]).casefold()][:100]
        return {"listings": [{**record, "service": {key: record["service"][key] for key in
            ("slug", "name", "description", "revision", "amount", "pay_to", "network", "asset")}} for record in matches]}

    async def refresh(self, origin, slug):
        if origin not in self.sellers:
            raise Problem("seller_not_allowed", "This local directory has not been configured for that seller origin.", 403)
        if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,63}", slug):
            raise Problem("invalid_slug", "Use a valid service slug.", 422)
        identity = listing_id(origin, slug)
        with closing(self.db()) as connection:
            old = connection.execute("SELECT version,document FROM listings WHERE id=?", (identity,)).fetchone()
        previous_version = old[0] if old else 0
        try:
            response = await self.client.get(origin + "/.well-known/hermes-sell/listings/" + slug)
            response.raise_for_status()
            if len(response.content) > 180_000:
                raise ValueError("Oversized publication")
            manifest = response.json()
            if (set(manifest) - {"protocol", "directory_origin", "origin", "mode", "service", "usage"}
                    or manifest["protocol"] != PROTOCOL or manifest["directory_origin"] != self.origin
                    or manifest["origin"] != origin or manifest["mode"] not in {"simulation", "testnet"}):
                raise ValueError("Publication scope mismatch")
            contract = public_contract(manifest["service"])
            if contract["slug"] != slug:
                raise ValueError("Publication slug mismatch")
            health = await self.client.get(origin + "/health/ready")
            online = health.status_code == 200 and health.json().get("ready") is True
            record = {"id": identity, "origin": origin, "service": contract, "mode": manifest["mode"],
                      "state": "online" if online else "offline", "checked_at": int(time.time()),
                      "storefront_url": origin + "/s/" + slug}
            usage = manifest.get("usage")
            if usage is not None:
                if (not isinstance(usage, dict) or set(usage) != {"executions", "paid_requests", "settled_amount", "source", "as_of"}
                        or usage["source"] != "seller_reported" or type(usage["as_of"]) is not int
                        or any(type(usage[key]) is not int or not 0 <= usage[key] <= 2**63 - 1 for key in ("executions", "paid_requests"))
                        or not isinstance(usage["settled_amount"], str) or not re.fullmatch(r"[0-9]{1,30}", usage["settled_amount"])):
                    raise ValueError("Invalid shared usage")
                record["usage"] = usage
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, Problem):
            if old is None:
                raise Problem("publication_unavailable", "The seller has not published this service to this directory.", 409) from None
            record = json.loads(old[1])
            record.update(state="withdrawn" if 'response' in locals() and response.status_code == 404 else "offline",
                          checked_at=int(time.time()))
        with closing(self.db()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            latest = connection.execute("SELECT version FROM listings WHERE id=?", (identity,)).fetchone()
            if (latest[0] if latest else 0) == previous_version:
                connection.execute("INSERT OR REPLACE INTO listings VALUES(?,?,?,?,?)",
                    (identity, origin, slug, previous_version + 1, canonical(record)))
            connection.commit()
        return self.get(identity)

    async def monitor(self):
        while True:
            with closing(self.db()) as connection:
                peers = connection.execute("SELECT origin,slug FROM listings LIMIT 1000").fetchall()
            for origin, slug in peers:
                try:
                    await self.refresh(origin, slug)
                except Problem:
                    pass
            await asyncio.sleep(30)

    async def close(self):
        await self.client.aclose()
        if hasattr(self.chain, "close"):
            await self.chain.close()


def directory_app(directory, web_dist=None, *, buyer_origin="http://127.0.0.1:8767", owner_origin="http://127.0.0.1:8786"):
    exact_origin(buyer_origin, local_only=True)
    exact_origin(owner_origin, local_only=True)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(directory.monitor())
        yield
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await directory.close()

    app = local_app("Hermes Sell · Service directory", directory.origin, lifespan=lifespan)

    class RegisterBody(BaseModel):
        model_config = ConfigDict(extra="forbid")
        origin: str
        slug: str

    class ReviewChallengeBody(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        proof: dict
        score: int = Field(ge=1, le=5)
        comment: str = Field(max_length=1000)

    class ReviewBody(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        challenge_id: str = Field(pattern=r"^[0-9a-f]{64}$")
        signature: str | None = None

    class SellerCheckBody(BaseModel):
        model_config = ConfigDict(extra="forbid")
        origin: str

    @app.get("/ui-config")
    async def config():
        return {"role": "directory", "mode": "directory", "origin": directory.origin,
                "buyer_origin": buyer_origin, "owner_origin": owner_origin}

    @app.get("/health/ready")
    async def health():
        return {"ready": True, "role": "directory"}

    @app.get("/v1/listings")
    async def listings(q: str = "", mode: str | None = None):
        return directory.search(q, mode)

    @app.get("/v1/sellers")
    async def sellers():
        return {"seller_origins": sorted(directory.sellers), "registration_mode": "configured_local_sellers"}

    @app.post("/v1/sellers/check")
    async def check_seller(body: SellerCheckBody):
        if body.origin not in directory.sellers:
            raise Problem("seller_not_allowed", "This directory must allow the exact local seller origin before registration.", 403)
        try:
            response = await directory.client.get(body.origin + "/ui-config")
            response.raise_for_status()
            config = response.json()
            if config.get("role") != "storefront" or config.get("mode") not in {"simulation", "testnet"}:
                raise ValueError("Not a seller")
            health = await directory.client.get(body.origin + "/health/ready")
            return {"origin": body.origin, "reachable": True, "ready": health.status_code == 200 and health.json().get("ready") is True,
                    "mode": config["mode"]}
        except (httpx.HTTPError, ValueError, AttributeError):
            raise Problem("seller_unavailable", "Start the seller API at this exact origin, then check again.", 409) from None

    @app.get("/v1/listings/{identity}/reviews")
    async def reviews(identity: str):
        return directory.reviews.list(identity)

    @app.post("/v1/listings/{identity}/review-challenges")
    async def review_challenge(identity: str, body: ReviewChallengeBody):
        return await directory.reviews.challenge(identity, body.proof, body.score, body.comment)

    @app.post("/v1/listings/{identity}/reviews")
    async def publish_review(identity: str, body: ReviewBody):
        return await directory.reviews.submit(identity, body.challenge_id, body.signature)

    @app.get("/v1/listings/{identity}")
    async def listing(identity: str):
        return directory.get(identity)

    @app.post("/v1/listings")
    async def register(body: RegisterBody):
        return await directory.refresh(body.origin, body.slug)

    static_workspace(app, web_dist, ["/", "/services/{identity}", "/sell"])
    return app
