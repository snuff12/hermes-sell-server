"""Shared loopback HTTP boundary for local discovery workspaces."""

import json
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx

from .contracts import Problem, canonical
from .discovery import exact_origin


def local_app(title, origin, *, lifespan=None):
    exact_origin(origin, local_only=True)
    app = FastAPI(title=title, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def failure(code, message, status):
        return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)

    @app.exception_handler(Problem)
    async def problem(request, exc):
        return failure(exc.code, exc.message, exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        return failure("invalid_request", "Check the required request fields.", 422)

    @app.exception_handler(httpx.HTTPError)
    async def unavailable(request, exc):
        return failure("connection_unavailable", "The service connection is unavailable. Retry the original action.", 502)

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        headers = request.headers
        forwarded = any(name.startswith("x-forwarded-") or name in {"forwarded", "cf-connecting-ip", "x-real-ip"}
                        for name in headers)
        if (headers.getlist("host") != [urlparse(origin).netloc] or forwarded or request.client is None
                or request.client.host not in {"127.0.0.1", "::1"}):
            return failure("local_only", "Open the configured local workspace.", 403)
        mutating = request.method not in {"GET", "HEAD", "OPTIONS"}
        if headers.getlist("origin") not in ([], [origin]) or (mutating and headers.getlist("origin") != [origin]):
            return failure("origin_denied", "Use the local workspace for this action.", 403)
        if headers.get("sec-fetch-site") in {"cross-site", "same-site"} and not (
                request.method == "GET" and headers.get("sec-fetch-mode") == "navigate"
                and not request.url.path.startswith(("/v1/", "/ui-config"))):
            return failure("origin_denied", "Requests from other sites are not allowed.", 403)
        if mutating:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 180_000:
                    return failure("body_too_large", "The request is too large.", 413)
            request._body = bytes(body)
            if body:
                try:
                    canonical(json.loads(body))
                except (ValueError, UnicodeError, RecursionError, Problem):
                    return failure("invalid_json", "Send valid JSON.", 422)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response
    return app


def static_workspace(app, web_dist, routes):
    if web_dist is None:
        return
    path = Path(web_dist)
    if (path / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=path / "assets"), name="assets")
        for route in routes:
            app.add_api_route(route, lambda: FileResponse(path / "index.html"), methods=["GET"], include_in_schema=False)
