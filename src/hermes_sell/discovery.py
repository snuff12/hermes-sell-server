"""Public service discovery. Listings never grant payment or execution authority."""

import re
from urllib.parse import urlparse

import httpx

from .contracts import Problem, canonical, digest, make_revision


def exact_origin(value, *, local_only=False):
    if not isinstance(value, str):
        raise Problem("invalid_origin", "Use an exact seller origin.", 422)
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError:
        raise Problem("invalid_origin", "Use a valid seller port.", 422) from None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path or parsed.params or parsed.query or parsed.fragment
            or any(ord(c) <= 32 or ord(c) >= 127 for c in value)
            or any(c in value for c in "\\%?#") or parsed.netloc.endswith(":")):
        raise Problem("invalid_origin", "Use an exact origin without paths, credentials or query parameters.", 422)
    local = parsed.hostname == "127.0.0.1" and parsed.scheme == "http" and port is not None and 1024 <= port <= 65535
    if (local_only and not local) or (not local and parsed.scheme != "https"):
        raise Problem("invalid_origin", "Use an explicit loopback HTTP port or an HTTPS origin.", 422)
    return value


def listing_id(origin, slug):
    return digest(["hermes-sell-listing-v1", exact_origin(origin), slug])


def public_contract(document):
    """Validate and select the same public contract fields used by the seller."""
    if not isinstance(document, dict) or len(canonical(document).encode()) > 150_000:
        raise Problem("invalid_listing", "The public service contract is invalid.", 422)
    keys = ("slug", "name", "description", "amount", "pay_to", "input_schema", "output_schema", "examples")
    try:
        checked = make_revision({key: document[key] for key in keys}, {})["public"]
        if (set(document) != set(checked) or any(document[k] != checked[k] for k in checked if k != "revision")
                or not re.fullmatch(r"[0-9a-f]{64}", document["revision"])):
            raise ValueError("Contract mismatch")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise Problem("invalid_listing", "The public service contract is invalid.", 422) from exc
    checked["revision"] = document["revision"]
    return checked


class DirectoryClient:
    def __init__(self, origin, *, transport=None):
        self.origin = exact_origin(origin)
        self.client = httpx.AsyncClient(base_url=self.origin, transport=transport, timeout=8,
                                       follow_redirects=False, trust_env=False)

    @staticmethod
    def document(response):
        response.raise_for_status()
        if len(response.content) > 200_000:
            raise Problem("directory_response_invalid", "The service directory response is too large.", 502)
        try:
            value = response.json()
            canonical(value)
        except (ValueError, Problem) as exc:
            raise Problem("directory_response_invalid", "The service directory returned invalid JSON.", 502) from exc
        return value

    def validate(self, value, *, summary=False):
        try:
            origin = exact_origin(value["origin"])
            contract = value["service"] if summary else public_contract(value["service"])
            if summary and (set(contract) != {"slug", "name", "description", "revision", "amount", "pay_to", "network", "asset"}
                    or not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,63}", contract["slug"])
                    or not isinstance(contract["name"], str) or not 1 <= len(contract["name"]) <= 100
                    or not isinstance(contract["description"], str) or not 1 <= len(contract["description"]) <= 1500
                    or not re.fullmatch(r"[0-9a-f]{64}", contract["revision"])
                    or not re.fullmatch(r"[0-9]{1,8}", contract["amount"]) or not 1 <= int(contract["amount"]) <= 10_000_000):
                raise ValueError("Invalid listing summary")
            if (value["id"] != listing_id(origin, contract["slug"]) or value["mode"] not in {"simulation", "testnet"}
                    or value["storefront_url"] != origin + "/s/" + contract["slug"]
                    or value["state"] not in {"online", "offline", "stale", "withdrawn"}
                    or type(value["checked_at"]) is not int):
                raise ValueError("Listing identity mismatch")
            selected = {key: value[key] for key in ("id", "origin", "service", "mode", "storefront_url", "state", "checked_at")}
            for optional in ("ratings", "usage"):
                if optional in value:
                    selected[optional] = value[optional]
            return selected
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise Problem("directory_response_invalid", "The directory returned an invalid service listing.", 502) from exc

    async def search(self, query="", *, mode=None):
        document = self.document(await self.client.get("/v1/listings", params={"q": query, **({"mode": mode} if mode else {})}))
        if not isinstance(document, dict) or not isinstance(document.get("listings"), list) or len(document["listings"]) > 100:
            raise Problem("directory_response_invalid", "The directory returned an invalid service list.", 502)
        return {"listings": [self.validate(row, summary=True) for row in document["listings"]]}

    async def get(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise Problem("invalid_listing_id", "Use the listing ID returned by service_search.", 422)
        return self.validate(self.document(await self.client.get("/v1/listings/" + identity)))

    async def register(self, seller_origin, slug):
        return self.validate(self.document(await self.client.post("/v1/listings", json={"origin": seller_origin, "slug": slug},
                                                        headers={"Origin": self.origin})))

    async def review_challenge(self, identity, proof, score, comment):
        return self.document(await self.client.post(f"/v1/listings/{identity}/review-challenges",
            json={"proof": proof, "score": score, "comment": comment}, headers={"Origin": self.origin}))

    async def publish_review(self, identity, challenge_id, signature):
        return self.document(await self.client.post(f"/v1/listings/{identity}/reviews",
            json={"challenge_id": challenge_id, "signature": signature}, headers={"Origin": self.origin}))

    async def close(self):
        await self.client.aclose()
