"""Order state machine. Persist intent, then execute, then persist result, then settle."""

import json
import secrets
import sqlite3
import time
import uuid

from .contracts import ASSET, NETWORK, PROTOCOL, Problem, canonical, digest, validate_value
from .storage import Store


def now():
    return int(time.time())


def save_order(db, order, event):
    db.execute("UPDATE orders SET document=?,execution=?,payment=? WHERE id=?",
               (canonical(order), order["execution"], order["payment"], order["id"]))
    db.execute("INSERT INTO events(order_id,kind,at) VALUES (?,?,?)", (order["id"], event, now()))


class Engine:
    def __init__(self, store: Store, runtime, payments, *, vault=None, readiness_check=None):
        self.store, self.runtime, self.payments = store, runtime, payments
        self.vault, self.readiness_check = vault, readiness_check
        self._next_reconciliation = 0.0
        if payments.mode == "testnet" and vault is None:
            raise ValueError("Testnet orders require encrypted authorization storage.")
        with store.tx() as db:
            stored_mode = db.execute("SELECT value FROM meta WHERE key='payment_mode'").fetchone()
            if stored_mode and stored_mode[0] != payments.mode:
                raise ValueError("Simulation and testnet must use separate ledgers.")
            for row in db.execute("SELECT document FROM orders"):
                if json.loads(row[0]).get("mode") != payments.mode:
                    raise ValueError("The ledger contains orders from a different payment mode.")
            db.execute("INSERT OR IGNORE INTO meta VALUES ('payment_mode', ?)", (payments.mode,))

    def ready(self):
        return not self.store.recovery_required and (self.readiness_check is None or self.readiness_check())

    def require_ready(self):
        if not self.ready():
            if self.store.recovery_required:
                raise Problem("recovery_required", "Reconcile executions and payments in the restored ledger before resuming.", 503)
            raise Problem("dependencies_unavailable", "The execution or payment connection is not ready. Existing orders remain available.", 503)

    def put_revision(self, revision):
        with self.store.tx() as db:
            db.execute("INSERT OR IGNORE INTO revisions(id,slug,document,created_at) VALUES(?,?,?,?)",
                       (revision["public"]["revision"], revision["public"]["slug"], canonical(revision), now()))
        return revision["public"]

    def revision(self, revision_id):
        row = self.store.one("SELECT document FROM revisions WHERE id=?", (revision_id,))
        if not row:
            raise Problem("not_found", "Service revision not found.", 404)
        return json.loads(row[0])

    def service(self, slug):
        row = self.store.one("SELECT document FROM revisions WHERE slug=? AND published=1", (slug,))
        if not row:
            raise Problem("not_found", "Published service not found.", 404)
        return json.loads(row[0])["public"]

    def services(self):
        return [json.loads(r[0])["public"] for r in self.store.all("SELECT document FROM revisions WHERE published=1 ORDER BY created_at DESC")]

    async def test_revision(self, revision_id):
        self.require_ready()
        revision = self.revision(revision_id)
        reports = []
        for example in revision["public"]["examples"]:
            self.require_ready()
            order = {"id": str(uuid.uuid4()), "run_key": "owner-test-" + str(uuid.uuid4()), "input": example["input"]}
            started = time.monotonic()
            try:
                import asyncio
                async with asyncio.timeout(60):
                    run_id = await self.runtime.start(order, revision)
                    while True:
                        result = await self.runtime.poll(run_id, order, revision)
                        if result.get("status") in {"completed", "failed", "interrupted", "cancelled", "waiting_for_approval", "unknown"}:
                            break
                        await asyncio.sleep(.2)
                if result.get("status") != "completed":
                    raise Problem("test_failed", "The execution did not complete successfully.")
                validate_value(result.get("output"), revision["public"]["output_schema"])
                reports.append({"passed": True, "output": result["output"], "seconds": round(time.monotonic() - started, 3)})
            except Exception:
                reports.append({"passed": False, "reason": "Execution did not finish with valid output."})
        report = {"id": str(uuid.uuid4()), "revision": revision_id, "mode": self.runtime.mode,
                  "passed": all(r["passed"] for r in reports), "cases": reports,
                  "semantic_review": "owner_required"}
        with self.store.tx() as db:
            db.execute("INSERT INTO tests VALUES (?,?,?,?,?,?)", (report["id"], revision_id, self.runtime.mode,
                       report["passed"], canonical(report), now()))
        return report

    def publish(self, revision_id, report_id, owner_reviewed):
        if owner_reviewed is not True:
            raise Problem("review_required", "Review the example results before publishing.", 409)
        with self.store.tx() as db:
            report = db.execute("SELECT * FROM tests WHERE id=? AND revision=? AND passed=1 AND mode=?",
                                (report_id, revision_id, self.runtime.mode)).fetchone()
            if not report:
                raise Problem("test_required", "This revision requires a successful test.", 409)
            revision = db.execute("SELECT slug FROM revisions WHERE id=?", (revision_id,)).fetchone()
            db.execute("UPDATE revisions SET published=0 WHERE slug=?", (revision[0],))
            db.execute("UPDATE revisions SET published=1 WHERE id=?", (revision_id,))
        return self.revision(revision_id)["public"]

    def session(self, owner):
        token = secrets.token_urlsafe(32)
        with self.store.tx() as db:
            db.execute("DELETE FROM sessions WHERE expires<?", (now(),))
            db.execute("INSERT INTO sessions VALUES(?,?,?)", (digest(token), owner, now() + 900))
        return {"token": token, "owner": owner, "expires_at": now() + 900}

    def owner(self, token):
        row = self.store.one("SELECT owner FROM sessions WHERE token_hash=? AND expires>?", (digest(token), now()))
        if not row:
            raise Problem("authentication_required", "Reconnect your session.", 401)
        return row[0]

    def create_order(self, owner, slug, expected_revision, value, idem, origin):
        self.require_ready()
        if not isinstance(idem, str) or not 8 <= len(idem) <= 128:
            raise Problem("invalid_idempotency_key", "Idempotency-Key must contain 8-128 characters.")
        request_digest = digest({"revision": expected_revision, "input": value})
        with self.store.tx() as db:
            old = db.execute("SELECT * FROM orders WHERE owner=? AND slug=? AND idem=?", (owner, slug, idem)).fetchone()
            if old:
                if old["request_digest"] != request_digest:
                    raise Problem("idempotency_conflict", "The same key cannot be used with a different input or revision.", 409)
                return self.view(json.loads(old["document"]))
            row = db.execute("SELECT document FROM revisions WHERE slug=? AND published=1", (slug,)).fetchone()
            if not row:
                raise Problem("not_found", "Published service not found.", 404)
            public = json.loads(row[0])["public"]
            if public["revision"] != expected_revision:
                raise Problem("revision_changed", "The service revision has changed. Review its price and details again.", 409)
            validate_value(value, public["input_schema"])
            order_id = str(uuid.uuid4())
            order = {"id": order_id, "owner": owner, "slug": slug, "revision": expected_revision,
                     "input": value, "origin": origin, "amount": public["amount"], "pay_to": public["pay_to"],
                     "network": NETWORK, "asset": ASSET, "created_at": now(), "quote_expires_at": now() + 300,
                     "execution": "created", "payment": "required", "run_key": "sell-" + order_id,
                     "protocol": PROTOCOL, "mode": self.payments.mode}
            order["order_digest"] = digest(order)
            if self.payments.mode == "testnet":
                order["payment_nonce"] = "0x" + order["order_digest"]
            db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?)", (order_id, owner, slug, idem, request_digest,
                       canonical(order), order["execution"], order["payment"], order["created_at"]))
            db.execute("INSERT INTO events(order_id,kind,at) VALUES(?,?,?)", (order_id, "quoted", now()))
        return self.view(order)

    def owned(self, order_id, owner):
        order = self.store.order(order_id)
        if not order or order["owner"] != owner:
            raise Problem("not_found", "Order not found.", 404)
        return order

    async def admit(self, order_id, owner, payload, *, admission_guard=None):
        self.require_ready()
        if admission_guard is not None:
            admission_guard()
        order = self.owned(order_id, owner)
        payload_digest = digest(payload)
        if order["payment"] != "required":
            if order.get("authorization_digest") != payload_digest:
                raise Problem("authorization_conflict", "Retries must use the original authorization.", 409)
            return self.view(order)
        if order["quote_expires_at"] <= now():
            raise Problem("quote_expired", "The quote has expired.", 409)
        verified = await self.payments.verify(payload, order)
        with self.store.tx() as db:
            self.require_ready()
            if admission_guard is not None:
                admission_guard()
            order = json.loads(db.execute("SELECT document FROM orders WHERE id=?", (order_id,)).fetchone()[0])
            if order["payment"] != "required":
                if order.get("authorization_digest") != payload_digest:
                    raise Problem("authorization_conflict", "This order was admitted with a different authorization.", 409)
                return self.view(order)
            if order["quote_expires_at"] <= now() or verified["valid_before"] - now() < 120:
                raise Problem("authorization_expiry", "Not enough authorization time remains to execute this order.", 409)
            active = db.execute("SELECT COUNT(*) FROM orders WHERE execution IN ('queued','dispatching','running','unknown') OR payment IN ('settling','unknown')").fetchone()[0]
            if active:
                raise Problem("capacity_busy", "Another order is running. Retry with the same authorization.", 409)
            try:
                db.execute("INSERT INTO authorizations VALUES(?,?)", (verified["identity"], order_id))
            except sqlite3.IntegrityError as exc:
                raise Problem("nonce_reused", "This authorization has already been used.", 409) from exc
            order.update(execution="queued", payment="verified", authorization_digest=payload_digest,
                         authorization_expires_at=verified["valid_before"])
            if self.payments.mode == "testnet":
                order["authorization_encrypted"] = self.vault.seal(payload, order)
                order["chain_start_block"] = verified.get("chain_start_block")
            else:
                order["authorization"] = payload
            save_order(db, order, "accepted")
        return self.view(order)

    def view(self, order):
        keys = ("id", "owner", "slug", "revision", "input", "origin", "amount", "pay_to", "network", "asset", "created_at",
                "quote_expires_at", "order_digest", "payment_nonce", "execution", "payment", "protocol", "mode", "error")
        public = {k: order[k] for k in keys if k in order}
        # Resolve the exact immutable revision, including for orders written before
        # this field existed. Never return its private execution bundle.
        public["service_contract"] = self.revision(order["revision"])["public"]
        if order["mode"] == "testnet":
            public["payment_requirements"] = self.payments.requirements(order).model_dump(by_alias=True, exclude_none=True)
        if order["execution"] == "result_ready" and order["payment"] == "settled":
            public.update(result=order["result"], receipt=order["receipt"])
        if self.store.recovery_required:
            public["recovery_required"] = True
            if order["payment"] != "settled":
                # A backup may predate even admission. Never let its old
                # required/failed state serve as proof that no payment happened.
                public.update(payment="unknown", error="recovery_required")
                if order["execution"] != "result_ready":
                    public["execution"] = "unknown"
        return public

    def transition(self, order_id, event, **changes):
        with self.store.tx() as db:
            order = json.loads(db.execute("SELECT document FROM orders WHERE id=?", (order_id,)).fetchone()[0])
            order.update(changes)
            save_order(db, order, event)
        return order

    def recover_interrupted(self):
        # Never infer that a network side effect failed just because this process died.
        with self.store.tx() as db:
            for row in db.execute("SELECT document FROM orders WHERE execution='dispatching' OR payment='settling'").fetchall():
                order = json.loads(row[0])
                if order["execution"] == "dispatching":
                    order["execution"] = "unknown"
                if order["payment"] == "settling":
                    order["payment"] = "unknown"
                order["error"] = "reconciliation_required"
                save_order(db, order, "recovery_required")

    async def tick(self):
        if self.store.recovery_required:
            return
        row = self.store.one("SELECT document FROM orders WHERE execution='unknown' ORDER BY created_at LIMIT 1")
        if row:
            if time.monotonic() >= self._next_reconciliation:
                self._next_reconciliation = time.monotonic() + 5
                await self.reconcile_execution(json.loads(row[0])["id"])
            return
        if self.payments.mode == "testnet":
            row = self.store.one("SELECT document FROM orders WHERE payment='unknown' ORDER BY created_at LIMIT 1")
            if row:
                if time.monotonic() >= self._next_reconciliation:
                    self._next_reconciliation = time.monotonic() + 5
                    await self.reconcile_payment(json.loads(row[0])["id"])
                return
        # One worker owns a seller database (CLI file lock enforces this).
        row = self.store.one("SELECT document FROM orders WHERE execution IN ('queued','running') OR (execution='result_ready' AND payment='verified') ORDER BY created_at LIMIT 1")
        if not row:
            return
        order = json.loads(row[0])
        revision = self.revision(order["revision"])
        if order["execution"] == "queued":
            if not self.ready():
                return
            if order["authorization_expires_at"] - now() < 120:
                self.transition(order["id"], "expired_before_execution", execution="failed", payment="failed", error="authorization_expired")
                return
            binding = (self.runtime.execution_identity(order, revision)
                       if hasattr(self.runtime, "execution_identity") else None)
            order = self.transition(order["id"], "dispatch_intent", execution="dispatching",
                                    execution_deadline=now() + 60, runtime_binding=binding)
            try:
                run_id = await self.runtime.start(order, revision)
            except Exception:
                self.transition(order["id"], "dispatch_unknown", execution="unknown", error="reconciliation_required")
                return
            order = self.transition(order["id"], "run_admitted", execution="running", run_id=run_id)
        if order["execution"] == "running":
            if order["execution_deadline"] <= now():
                self.transition(order["id"], "execution_timeout", execution="unknown", error="termination_unconfirmed")
                return
            try:
                result = await self.runtime.poll(order["run_id"], order, revision)
            except Problem:
                self.transition(order["id"], "invalid_output", execution="failed", payment="failed", error="invalid_output")
                return
            except Exception:
                return  # Read-only poll may be repeated until the execution deadline.
            if order["execution_deadline"] <= now():
                self.transition(order["id"], "execution_timeout", execution="unknown", error="termination_unconfirmed")
                return
            status = result.get("status")
            if status == "completed":
                try:
                    validate_value(result.get("output"), revision["public"]["output_schema"])
                except Problem:
                    self.transition(order["id"], "invalid_output", execution="failed", payment="failed", error="invalid_output")
                    return
                order = self.transition(order["id"], "result_saved", execution="result_ready", result=result["output"],
                                        provenance=result.get("provenance", {"mode": self.runtime.mode}))
            elif status in {"failed", "interrupted", "cancelled"}:
                self.transition(order["id"], "execution_failed", execution="failed", payment="failed", error=status)
                return
            elif status in {"waiting_for_approval", "unknown"}:
                self.transition(order["id"], "execution_unknown", execution="unknown", error="reconciliation_required")
                return
            else:
                return
        if order["execution"] == "result_ready" and order["payment"] == "verified":
            if not self.ready():
                return
            if order["authorization_expires_at"] <= now():
                self.transition(order["id"], "expired_before_settlement", payment="failed", error="authorization_expired")
                return
            order = self.transition(order["id"], "settlement_intent", payment="settling")
            try:
                authorization = (self.vault.open(order["authorization_encrypted"], order)
                                 if self.payments.mode == "testnet" else order["authorization"])
                receipt = await self.payments.settle(authorization, order)
            except Exception:
                self.transition(order["id"], "settlement_unknown", payment="unknown", error="reconciliation_required")
                return
            if receipt.get("success") is not True:
                self.transition(order["id"], "settlement_failed",
                                payment="unknown" if self.payments.mode == "testnet" else "failed",
                                error="settlement_failed")
                return
            if self.payments.mode == "testnet":
                order = self.transition(order["id"], "settlement_response", settlement_candidate=receipt, payment="unknown")
                await self.reconcile_payment(order["id"])
                return
            self.transition(order["id"], "settled", payment="settled", receipt=receipt,
                            authorization=None, authorization_encrypted=None)

    def prepare_restored_order(self, order_id):
        """An old snapshot cannot prove that a later execution or payment never happened."""
        with self.store.tx() as db:
            row = db.execute("SELECT document FROM orders WHERE id=?", (order_id,)).fetchone()
            if not row:
                raise Problem("not_found", "Order not found.", 404)
            order = json.loads(row[0])
            if (self.store.recovery_required and order.get("authorization_digest")
                    and order["payment"] in {"verified", "settling"}):
                order["payment"] = "unknown"
                if order["execution"] in {"queued", "dispatching", "running"}:
                    order["execution"] = "unknown"
                order["error"] = "reconciliation_required"
                save_order(db, order, "restored_order_uncertain")
            return order

    async def reconcile_execution(self, order_id):
        """Resolve a known run with GET only; never retry an uncertain admission."""
        order = self.prepare_restored_order(order_id)
        if self.store.recovery_required and order["payment"] == "unknown" and self.payments.mode == "testnet":
            await self.reconcile_payment(order_id)
            order = self.store.order(order_id)
        if order["execution"] != "unknown":
            return self.view(order)
        revision = self.revision(order["revision"])
        if not order.get("run_id"):
            if not hasattr(self.runtime, "find_run"):
                return self.view(order)
            try:
                run_id = await self.runtime.find_run(order, revision, allow_unbound=self.store.recovery_required)
            except Exception:
                return self.view(order)
            if run_id is None:
                return self.view(order)
            with self.store.tx() as db:
                row = db.execute("SELECT document FROM orders WHERE id=?", (order_id,)).fetchone()
                latest = json.loads(row[0])
                if (latest["execution"] != "unknown" or latest.get("run_id")
                        or latest.get("runtime_binding") != order.get("runtime_binding")
                        or latest.get("run_key") != order.get("run_key")
                        or latest.get("authorization_digest") != order.get("authorization_digest")):
                    return self.view(latest)
                latest["run_id"] = run_id
                if latest.get("runtime_binding") is None and hasattr(self.runtime, "execution_identity"):
                    latest["runtime_binding"] = self.runtime.execution_identity(latest, revision)
                save_order(db, latest, "run_identity_recovered")
                order = latest
        try:
            result = await self.runtime.poll(order["run_id"], order, revision)
            status = result.get("status")
            if status not in {"completed", "failed", "interrupted", "cancelled"}:
                return self.view(order)
            if status == "completed":
                validate_value(result.get("output"), revision["public"]["output_schema"])
        except Problem:
            status = "invalid_output"
        except Exception:
            return self.view(order)
        # RPC and runtime reads happen outside the transaction. A stale response
        # must never replace a newer result, run identity, or settlement decision.
        with self.store.tx() as db:
            row = db.execute("SELECT document FROM orders WHERE id=?", (order_id,)).fetchone()
            latest = json.loads(row[0])
            if (latest["execution"] == "unknown"
                    and latest["payment"] in {"verified", "unknown", "settled", "failed"}
                    and latest.get("run_id") == order.get("run_id")
                    and latest.get("runtime_binding") == order.get("runtime_binding")
                    and latest.get("authorization_digest") == order.get("authorization_digest")):
                if self.store.recovery_required and latest["payment"] == "verified":
                    # The restore lock may have been set while the poll awaited.
                    return self.view(latest)
                if status == "completed" and (latest["payment"] in {"unknown", "settled"}
                                               or (latest["payment"] == "verified" and latest.get("execution_deadline", 0) > now())):
                    latest.update(execution="result_ready", result=result["output"],
                                  error="reconciliation_required" if latest["payment"] == "unknown" else None,
                                  provenance=result.get("provenance", {"mode": self.runtime.mode}))
                    save_order(db, latest, "execution_reconciled")
                else:
                    # Terminal evidence releases capacity, but an output first
                    # observed after the original deadline never starts a charge.
                    # A restored payment may already have happened: retain its
                    # confirmed or uncertain financial state, even on run failure.
                    latest.update(execution="failed",
                                  error="execution_deadline_expired" if status == "completed" else status)
                    if latest["payment"] in {"verified", "failed"}:
                        latest.update(payment="failed", authorization=None, authorization_encrypted=None)
                    save_order(db, latest, "execution_terminal_confirmed")
            order = latest
        return self.view(order)

    async def reconcile_order(self, order_id, *, transaction=None):
        if transaction is not None:
            order = self.store.order(order_id)
            if not order:
                raise Problem("not_found", "Order not found.", 404)
            if self.payments.mode != "testnet" or (not self.store.recovery_required and order["payment"] != "unknown"):
                raise Problem("reconciliation_not_required", "Transaction hints are only accepted for uncertain orders.", 409)
            try:
                receipt = await self.payments.chain.confirm(order, transaction)
            except Exception:
                receipt = None
            if receipt is None:
                raise Problem("settlement_unconfirmed", "The transaction does not prove this order's confirmed payment.", 409)
            with self.store.tx() as db:
                latest = json.loads(db.execute("SELECT document FROM orders WHERE id=?", (order_id,)).fetchone()[0])
                if latest["payment"] != "settled":
                    latest.update(payment="settled", receipt=receipt, authorization=None,
                                  authorization_encrypted=None, error=None)
                    if self.store.recovery_required and latest["execution"] in {"created", "queued", "dispatching", "running"}:
                        latest["execution"] = "unknown"
                    save_order(db, latest, "hinted_chain_settlement_confirmed")
        await self.reconcile_execution(order_id)
        if self.store.recovery_required:
            return self.view(self.store.order(order_id))
        return await self.reconcile_payment(order_id)

    async def reconcile_payment(self, order_id):
        order = self.store.order(order_id)
        if not order:
            raise Problem("not_found", "Order not found.", 404)
        if order["payment"] != "unknown" or self.payments.mode != "testnet":
            return self.view(order)
        try:
            result = await self.payments.chain.reconcile(order)
        except Exception:
            return self.view(order)
        # An RPC result belongs to an older snapshot. A concurrent worker or
        # owner check may already have confirmed settlement while it was pending.
        with self.store.tx() as db:
            row = db.execute("SELECT document FROM orders WHERE id=?", (order_id,)).fetchone()
            latest = json.loads(row[0])
            if latest["payment"] == "unknown" and latest.get("authorization_digest") == order.get("authorization_digest"):
                if result["status"] == "settled":
                    latest.update(payment="settled", receipt=result["receipt"], authorization_encrypted=None, error=None)
                    save_order(db, latest, "chain_settlement_confirmed")
                elif result["status"] == "expired_unspent":
                    latest.update(payment="failed", authorization_encrypted=None,
                                  error=result.get("reason", "authorization_expired_unspent"))
                    save_order(db, latest, "chain_expired_unspent")
            order = latest
        return self.view(order)
