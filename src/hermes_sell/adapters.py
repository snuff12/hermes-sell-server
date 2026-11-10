"""Explicit simulation adapters and a real Hermes Runs transport.

Simulation never claims to execute an LLM or make a chain transaction.
"""

import asyncio
import json
import re
import time
from typing import Protocol
from urllib.parse import urlparse

import httpx

from .contracts import Problem, canonical, digest


class Runtime(Protocol):
    async def start(self, order: dict, revision: dict) -> str: ...
    async def poll(self, run_id: str, order: dict, revision: dict) -> dict: ...


class SimulationRuntime:
    mode = "simulation"

    def __init__(self):
        self.starts = 0

    async def start(self, order, revision):
        self.starts += 1
        return "sim_" + order["id"]

    async def poll(self, run_id, order, revision):
        return {"status": "completed", "output": revision["public"]["examples"][0]["output"],
                "provenance": {"mode": "simulation", "model": None, "note": "Fixed example output; no model called."}}


class SimulationPayments:
    mode = "simulation"

    def __init__(self):
        self.settlements = 0

    async def verify(self, payload, order):
        expected = {"mode": "simulation", "order_digest": order["order_digest"], "payer": order["owner"]}
        if any(payload.get(k) != v for k, v in expected.items()):
            raise Problem("invalid_authorization", "The authorization does not match the order.")
        nonce, valid_before = payload.get("nonce"), payload.get("valid_before")
        if not isinstance(nonce, str) or not 16 <= len(nonce) <= 100 or type(valid_before) is not int:
            raise Problem("invalid_authorization", "Check the authorization format.")
        if not 120 <= valid_before - int(time.time()) <= 300:
            raise Problem("authorization_expiry", "The authorization must remain valid for 120-300 seconds.")
        return {"identity": digest([order["network"], order["asset"], order["owner"], nonce]),
                "valid_before": valid_before}

    async def settle(self, payload, order):
        self.settlements += 1
        return {"success": True, "transaction": "simulation:" + order["id"], "network": order["network"]}


class HermesRunsRuntime:
    mode = "hermes"

    def __init__(self, base_url: str, token: str, credential_generation: str, *, transport=None, admission_lookup=None):
        parsed = urlparse(base_url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("The seller runtime must use a dedicated loopback HTTP origin")
        if not isinstance(token, str) or not token or any(char.isspace() for char in token):
            raise ValueError("A nonempty runtime bearer token is required")
        if not isinstance(credential_generation, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", credential_generation):
            raise ValueError("A fixed credential generation is required")
        self.origin = base_url.rstrip("/")
        self.credential_generation = credential_generation
        self.admission_lookup = admission_lookup
        self.client = httpx.AsyncClient(base_url=self.origin,
            headers={"Authorization": f"Bearer {token}"}, timeout=10, follow_redirects=False, trust_env=False, transport=transport)

    @staticmethod
    def document(response):
        response.raise_for_status()
        if len(response.content) > 1_000_000:
            raise RuntimeError("The Hermes response exceeds the transport limit")
        try:
            document = response.json()
            canonical(document)
        except (ValueError, Problem) as exc:
            raise RuntimeError("Hermes returned invalid JSON") from exc
        if not isinstance(document, dict):
            raise RuntimeError("Hermes returned an invalid response object")
        return document

    async def preflight(self):
        """Check API policy. Container and filesystem isolation need separate verification."""
        capabilities = self.document(await self.client.get("/v1/capabilities"))
        features = capabilities.get("features", {})
        idempotency = features.get("runs_idempotency", {})
        if (capabilities.get("object") != "hermes.api_server.capabilities"
                or capabilities.get("auth", {}).get("required") is not True
                or features.get("run_submission") is not True
                or idempotency.get("supported") is not True
                or idempotency.get("durable") is not True
                or type(idempotency.get("retention_seconds")) is not int
                or idempotency["retention_seconds"] < 86400):
            raise RuntimeError("Hermes must require authentication and retain durable run keys for at least 24 hours")
        toolsets = self.document(await self.client.get("/v1/toolsets"))
        entries = toolsets.get("data")
        if not isinstance(entries, list) or any(not isinstance(item, dict) or item.get("enabled") is not False for item in entries):
            raise RuntimeError("The dedicated seller Hermes must have every toolset disabled")
        return {"origin": self.origin, "credential_generation": self.credential_generation,
                "idempotency_retention_seconds": idempotency["retention_seconds"], "tools_disabled": True}

    @staticmethod
    def body(order, revision):
        # Original frozen skill stays intact; public generated description is not executable.
        if (revision["bundle"].get("policy") != "text-json-no-tools-v1"
                or revision["bundle"].get("wrapper_version") != 1
                or revision["bundle"].get("model") != "runtime-default"):
            raise ValueError("The runtime does not support this frozen execution policy")
        files = revision["bundle"]["files"]
        instructions = "\n\n".join(f"--- {name} ---\n{text}" for name, text in sorted(files.items()))
        instructions += "\nReturn only a JSON value matching this output schema:\n" + canonical(revision["public"]["output_schema"])
        return {"input": canonical(order["input"]), "instructions": instructions}

    def execution_identity(self, order, revision):
        """Persist this binding with execution intent before calling start()."""
        return {"origin": self.origin, "credential_generation": self.credential_generation,
                "request_digest": digest(self.body(order, revision))}

    async def start(self, order, revision):
        expected = self.execution_identity(order, revision)
        if order.get("runtime_binding") is not None and order["runtime_binding"] != expected:
            raise RuntimeError("The runtime origin, credential generation, or frozen request has changed")
        if not isinstance(order.get("run_key"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", order["run_key"]):
            raise ValueError("A stable run key of 8-128 characters is required")
        await self.preflight()
        response = await self.client.post("/v1/runs", json=self.body(order, revision),
            headers={"Idempotency-Key": order["run_key"]})
        result = self.document(response)
        run_id = result.get("run_id")
        if response.status_code != 202 or not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
            raise RuntimeError("Hermes did not return a valid run admission")
        return run_id

    async def find_run(self, order, revision, *, allow_unbound=False):
        """Look up a lost admission in the verified container's retained ledger."""
        if self.admission_lookup is None:
            return None
        if order.get("runtime_binding") != self.execution_identity(order, revision) and not (
                allow_unbound and order.get("runtime_binding") is None):
            raise RuntimeError("The saved runtime binding does not match this runtime")
        if not isinstance(order.get("run_key"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", order["run_key"]):
            raise ValueError("A stable run key is required for admission lookup")
        # Match the pinned Hermes API's fingerprint, which wraps the body with
        # its optional session key. This integration never sends a session key.
        fingerprint = digest({"body": self.body(order, revision), "gateway_session_key": ""})
        run_id = await asyncio.to_thread(self.admission_lookup, order["run_key"], fingerprint, self.credential_generation)
        if run_id is not None and (not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id)):
            raise RuntimeError("The admission lookup returned an invalid run identity")
        return run_id

    async def poll(self, run_id, order, revision):
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
            raise ValueError("Invalid Hermes run ID")
        if order.get("runtime_binding") is not None and order["runtime_binding"] != self.execution_identity(order, revision):
            raise RuntimeError("The saved runtime binding does not match this runtime")
        response = await self.client.get(f"/v1/runs/{run_id}")
        if response.status_code == 404:
            return {"status": "unknown", "error": "run_not_found"}
        result = self.document(response)
        if result.get("run_id") != run_id:
            raise RuntimeError("Hermes returned a different run identity")
        if result.get("status") == "completed":
            if result.get("completed") is False or result.get("partial") is True or result.get("interrupted") is True or result.get("failed") is True:
                return {"status": "failed", "error": "incomplete_execution"}
            explicit_completion = (result.get("completed") is True and result.get("partial") is False
                                   and result.get("interrupted") is False)
            # Hermes v0.23 canonical polling omits completion flags. Its durable
            # admission projection applies terminal_run_status before emitting
            # completed; require that full projection rather than a bare status.
            canonical_completion = (
                result.get("object") == "hermes.run" and isinstance(result.get("admission_id"), str)
                and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", result["admission_id"]))
                and type(result.get("execution_generation")) is int and result["execution_generation"] > 0
                and isinstance(result.get("session_id"), str) and bool(result["session_id"])
                and result.get("pending_controls") == []
            )
            if not (explicit_completion or canonical_completion):
                return {"status": "unknown", "error": "completion_unconfirmed"}
            output = result.get("output")
            if isinstance(output, str):
                try:
                    output = json.loads(output)
                    canonical(output)
                except json.JSONDecodeError as exc:
                    raise Problem("invalid_output", "The Hermes output is not valid JSON.") from exc
            runtime = result.get("runtime") or {}
            return {"status": "completed", "output": output, "provenance": {
                "mode": "hermes", "run_id": run_id, "credential_generation": self.credential_generation,
                "model": runtime.get("model") or None, "provider": runtime.get("provider") or None,
            }}
        status = result.get("status")
        if status in {"queued", "running", "failed", "cancelled", "interrupted"}:
            return {"status": status}
        return {"status": "unknown", "error": "reconciliation_required"}

    async def close(self):
        await self.client.aclose()
