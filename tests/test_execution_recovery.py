"""Recovery uses existing run evidence without replaying model or payment requests."""

import asyncio
import time
import uuid

import httpx
import pytest

from conftest import authorization, quote
from hermes_sell.api import admin_app
from hermes_sell.adapters import SimulationRuntime, HermesRunsRuntime
from hermes_sell.contracts import Problem
from hermes_sell.engine import Engine
from hermes_sell.storage import Store
from hermes_sell.vault import AuthorizationVault
from hermes_sell.x402_adapter import X402Payments


async def uncertain_run(seller, *, expired=False):
    order = quote(seller)
    await seller.admit(order["id"], "demo-browser", authorization(order))
    return seller.transition(order["id"], "fixture_lost_poll", execution="unknown", run_id="run-original",
                             execution_deadline=int(time.time()) + (-10 if expired else 50))


async def test_known_completed_run_recovers_and_settles_without_execution_replay(seller):
    order = await uncertain_run(seller)
    recovered = await seller.reconcile_order(order["id"])
    assert recovered["execution"] == "result_ready" and recovered["payment"] == "verified"
    assert "result" not in recovered
    assert seller.runtime.starts == seller.payments.settlements == 0
    await seller.tick()
    assert seller.store.order(order["id"])["payment"] == "settled"
    assert seller.runtime.starts == 0 and seller.payments.settlements == 1


async def test_late_completed_run_releases_capacity_without_charging(seller):
    order = await uncertain_run(seller, expired=True)
    recovered = await seller.reconcile_order(order["id"])
    assert recovered["execution"] == recovered["payment"] == "failed"
    assert recovered["error"] == "execution_deadline_expired"
    assert "result" not in recovered
    assert seller.store.order(order["id"])["authorization"] is None
    second = quote(seller)
    await seller.admit(second["id"], "demo-browser", authorization(second))
    assert seller.runtime.starts == seller.payments.settlements == 0


@pytest.mark.parametrize("status", ["running", "queued", "unknown"])
async def test_nonterminal_run_stays_unknown_without_new_side_effects(seller, status):
    order = await uncertain_run(seller, expired=True)

    async def poll(*args):
        return {"status": status}

    seller.runtime.poll = poll
    await seller.tick()
    assert seller.store.order(order["id"])["execution"] == "unknown"
    second = quote(seller)
    with pytest.raises(Problem, match="Another order"):
        await seller.admit(second["id"], "demo-browser", authorization(second))
    assert seller.runtime.starts == seller.payments.settlements == 0


@pytest.mark.parametrize("status", ["failed", "interrupted", "cancelled"])
async def test_terminal_runtime_evidence_releases_unknown_execution(seller, status):
    order = await uncertain_run(seller)

    async def poll(*args):
        return {"status": status}

    seller.runtime.poll = poll
    recovered = await seller.reconcile_execution(order["id"])
    assert recovered["execution"] == recovered["payment"] == "failed"
    assert recovered["error"] == status
    assert seller.runtime.starts == seller.payments.settlements == 0


async def test_missing_run_identity_and_read_failure_stay_unknown(seller):
    order = await uncertain_run(seller)
    polls = []

    async def poll(*args):
        polls.append(args)
        raise RuntimeError("Runtime binding changed or read failed")

    seller.runtime.poll = poll
    assert (await seller.reconcile_execution(order["id"]))["execution"] == "unknown"
    seller.transition(order["id"], "fixture_lost_admission", run_id=None)
    assert (await seller.reconcile_execution(order["id"]))["execution"] == "unknown"
    assert len(polls) == 1
    assert seller.runtime.starts == seller.payments.settlements == 0


async def test_invalid_recovered_output_is_not_charged(seller):
    order = await uncertain_run(seller)

    async def poll(*args):
        return {"status": "completed", "output": {"wrong": True}}

    seller.runtime.poll = poll
    recovered = await seller.reconcile_execution(order["id"])
    assert recovered["execution"] == recovered["payment"] == "failed"
    assert recovered["error"] == "invalid_output"
    assert seller.payments.settlements == 0


async def test_locked_owner_reconciliation_is_read_only_and_keeps_restore_lock(seller):
    order = await uncertain_run(seller)
    seller.store.flag_recovery()
    app = admin_app(seller, "owner-token")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8766") as client:
        path = f"/admin/orders/{order['id']}/reconcile"
        assert (await client.post(path)).status_code == 401
        response = await client.post(path, headers={"Authorization": "Bearer owner-token"})
        assert response.status_code == 200
        assert response.json()["execution"] == "result_ready"
    await seller.tick()
    assert seller.store.recovery_required
    assert seller.runtime.starts == seller.payments.settlements == 0
    assert seller.store.order(order["id"])["payment"] == "unknown"


async def test_stale_failed_reconciliation_cannot_replace_a_recovered_result(seller):
    order = await uncertain_run(seller)
    entered, release = asyncio.Event(), asyncio.Event()
    original_poll = seller.runtime.poll
    count = 0

    async def poll(*args):
        nonlocal count
        count += 1
        if count == 1:
            entered.set()
            await release.wait()
            return {"status": "failed"}
        return await original_poll(*args)

    seller.runtime.poll = poll
    stale = asyncio.create_task(seller.reconcile_execution(order["id"]))
    await entered.wait()
    assert (await seller.reconcile_execution(order["id"]))["execution"] == "result_ready"
    release.set()
    assert (await stale)["execution"] == "result_ready"
    assert seller.runtime.starts == seller.payments.settlements == 0


@pytest.mark.parametrize("execution,payment", [("created", "required"), ("queued", "verified"),
                                              ("failed", "failed"), ("result_ready", "unknown")])
async def test_old_backup_never_claims_an_unconfirmed_order_was_unpaid(seller, execution, payment):
    order = quote(seller)
    seller.transition(order["id"], "fixture_snapshot", execution=execution, payment=payment)
    seller.store.flag_recovery()
    public = seller.view(seller.store.order(order["id"]))
    assert public["payment"] == "unknown" and public["recovery_required"] is True
    assert public["execution"] in {"unknown", "result_ready"}
    assert "result" not in public
    # Keep the original snapshot for evidence; only its public certainty changes.
    assert seller.store.order(order["id"])["payment"] == payment


@pytest.mark.parametrize("chain_status", ["settled", "unknown", "expired_unspent"])
async def test_restored_late_run_checks_actual_payment_before_deciding_delivery(tmp_path, revision, chain_status):
    class Chain:
        async def reconcile(self, order):
            return {"status": chain_status, "receipt": {"success": True, "transaction": "original-transfer"}}

    seller = Engine(Store(tmp_path / "restored.sqlite3"), SimulationRuntime(), X402Payments(None, chain=Chain()),
                    vault=AuthorizationVault(b"r" * 32))
    seller.put_revision(revision)
    report = await seller.test_revision(revision["public"]["revision"])
    seller.publish(revision["public"]["revision"], report["id"], True)
    seller.runtime.starts = 0
    service = revision["public"]
    order = seller.create_order("0x" + "1" * 40, service["slug"], service["revision"],
                                service["examples"][0]["input"], str(uuid.uuid4()), "http://127.0.0.1:8785")
    seller.transition(order["id"], "fixture_old_backup", execution="running", payment="verified", run_id="run-original",
                      authorization_digest="a" * 64, authorization_expires_at=int(time.time()) - 20,
                      execution_deadline=int(time.time()) - 30)
    seller.store.flag_recovery()
    public = await seller.reconcile_order(order["id"])
    stored = seller.store.order(order["id"])
    if chain_status == "settled":
        assert public["payment"] == "settled" and public["result"] == service["examples"][0]["output"]
        assert stored["receipt"]["transaction"] == "original-transfer"
    elif chain_status == "unknown":
        assert stored["execution"] == "result_ready" and stored["payment"] == "unknown"
        assert "result" not in public
    else:
        assert stored["execution"] == stored["payment"] == "failed"
        assert "result" not in public
    await seller.tick()
    assert seller.runtime.starts == 0 and seller.store.recovery_required


async def test_lost_admission_response_recovers_same_run_without_second_post(seller):
    requests, lookups = [], []

    def handle(request):
        requests.append((request.method, request.url.path))
        if request.url.path == "/v1/capabilities":
            from test_runtime import capabilities
            return httpx.Response(200, json=capabilities())
        if request.url.path == "/v1/toolsets":
            return httpx.Response(200, json={"data": []})
        if request.method == "POST":
            raise httpx.ReadError("Admission response was lost", request=request)
        return httpx.Response(200, json={"run_id": "run-original", "status": "completed", "completed": True,
                                       "partial": False, "interrupted": False,
                                       "output": seller.service("diff-review")["examples"][0]["output"]})

    def lookup(*args):
        lookups.append(args)
        return "run-original"

    runtime = HermesRunsRuntime("http://127.0.0.1:8642", "secret", "generation-1",
                               transport=httpx.MockTransport(handle), admission_lookup=lookup)
    seller.runtime = runtime
    try:
        order = quote(seller)
        await seller.admit(order["id"], "demo-browser", authorization(order))
        await seller.tick()
        assert seller.store.order(order["id"])["execution"] == "unknown"
        assert "run_id" not in seller.store.order(order["id"])
        assert (await seller.reconcile_order(order["id"]))["execution"] == "result_ready"
        await seller.tick()
        assert seller.store.order(order["id"])["run_id"] == "run-original"
        assert seller.store.order(order["id"])["payment"] == "settled"
        assert requests.count(("POST", "/v1/runs")) == 1
        assert len(lookups) == seller.payments.settlements == 1
    finally:
        await runtime.close()


@pytest.mark.parametrize("valid_hint", [False, True])
async def test_old_quote_snapshot_requires_exact_receipt_and_admission_before_delivery(tmp_path, revision, valid_hint):
    transaction = "0x" + "a" * 64
    calls = []

    class Chain:
        async def confirm(self, order, candidate):
            calls.append(("confirm", candidate))
            if not valid_hint:
                return None
            return {"success": True, "transaction": candidate, "verified_by": "receipt-transfer-authorization"}

    seller = Engine(Store(tmp_path / "old-quote.sqlite3"), SimulationRuntime(), X402Payments(None, chain=Chain()),
                    vault=AuthorizationVault(b"r" * 32))
    seller.put_revision(revision)
    report = await seller.test_revision(revision["public"]["revision"])
    seller.publish(revision["public"]["revision"], report["id"], True)
    service = revision["public"]
    order = seller.create_order("0x" + "1" * 40, service["slug"], service["revision"],
                                service["examples"][0]["input"], str(uuid.uuid4()), "http://127.0.0.1:8785")

    def lookup(*args):
        calls.append(("lookup", args))
        return "run-original"

    def handle(request):
        calls.append((request.method, request.url.path))
        assert request.method == "GET"
        return httpx.Response(200, json={"run_id": "run-original", "status": "completed", "completed": True,
                                       "partial": False, "interrupted": False,
                                       "output": service["examples"][0]["output"]})

    runtime = HermesRunsRuntime("http://127.0.0.1:8642", "secret", "generation-1",
                               transport=httpx.MockTransport(handle), admission_lookup=lookup)
    seller.runtime = runtime
    seller.store.flag_recovery()
    try:
        if valid_hint:
            result = await seller.reconcile_order(order["id"], transaction=transaction)
            assert result["payment"] == "settled" and result["result"] == service["examples"][0]["output"]
            assert seller.store.order(order["id"])["run_id"] == "run-original"
            assert seller.store.order(order["id"])["runtime_binding"] == runtime.execution_identity(order, revision)
            assert [call[0] for call in calls] == ["confirm", "lookup", "GET"]
        else:
            with pytest.raises(Problem) as rejected:
                await seller.reconcile_order(order["id"], transaction=transaction)
            assert rejected.value.code == "settlement_unconfirmed"
            assert seller.store.order(order["id"])["payment"] == "required"
            assert calls == [("confirm", transaction)]
        assert seller.store.recovery_required
    finally:
        await runtime.close()


async def test_owner_reconciliation_rejects_malformed_hint_before_chain_access(seller):
    order = await uncertain_run(seller)
    app = admin_app(seller, "owner-token")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8766") as client:
        response = await client.post(f"/admin/orders/{order['id']}/reconcile", json={"transaction": "not-a-hash"},
                                     headers={"Authorization": "Bearer owner-token"})
        assert response.status_code == 422
    assert seller.store.order(order["id"])["execution"] == "unknown"


@pytest.mark.parametrize("restore_locked", [False, True])
async def test_dependency_outage_keeps_local_read_and_owner_recovery_routes_available(tmp_path, monkeypatch, restore_locked):
    import hermes_sell.testnet as module
    from hermes_sell.settings import initialize_testnet

    initialize_testnet(tmp_path)
    token = tmp_path / "runtime-token"
    token.write_text("fixture-runtime-token")
    token.chmod(0o600)
    if restore_locked:
        Store(tmp_path / "seller.sqlite3").flag_recovery()
    seen = []

    async def unavailable(self):
        return {"ready": False, "message": "Fixture dependency outage"}

    async def monitor(self):
        await asyncio.Event().wait()

    class LocalServer:
        def __init__(self, config):
            self.config = config

        async def serve(self):
            assert self.config.host == "127.0.0.1" and self.config.proxy_headers is False
            app = self.config.app
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                             base_url=f"http://127.0.0.1:{self.config.port}") as client:
                    role = (await client.get("/ui-config")).json()["role"]
                    seen.append(role)
                    if role == "storefront":
                        response = await client.get("/health/ready")
                        assert response.status_code == 503 and response.json()["ready"] is False
                        assert response.json()["recovery_required"] is restore_locked
                        assert (await client.get("/v1/services")).status_code == 200
                    else:
                        client.headers["Authorization"] = "Bearer " + (tmp_path / "owner-token").read_text()
                        assert (await client.get("/admin/orders")).status_code == 200
                        # The authenticated operator route is reachable even
                        # though it cannot resolve nonexistent evidence.
                        response = await client.post("/admin/orders/missing/reconcile")
                        assert response.status_code == 404

    monkeypatch.setattr(module.Readiness, "check", unavailable)
    monkeypatch.setattr(module.Readiness, "monitor", monitor)
    monkeypatch.setattr(module.uvicorn, "Server", LocalServer)
    await module.run_testnet(tmp_path / "config.json", None)
    assert set(seen) == {"storefront", "studio"}
