"""Usage and payment totals derived from durable records, not HTTP retries."""

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import time

from .contracts import Problem

SETTLED_EVENTS = {"settled", "chain_settlement_confirmed", "hinted_chain_settlement_confirmed"}


def seller_analytics(engine, period="30d", *, clock=None):
    current = int(time.time()) if clock is None else int(clock)
    if period not in {"7d", "30d", "all"}:
        raise Problem("invalid_period", "Choose 7d, 30d or all.", 422)
    today = datetime.fromtimestamp(current, timezone.utc).date()
    days = 7 if period == "7d" else 30
    first_day = today - timedelta(days=days - 1)
    cutoff = 0 if period == "all" else int(datetime.combine(first_day, datetime.min.time(), timezone.utc).timestamp())
    with closing(engine.store.connect()) as db:
        db.execute("BEGIN")
        event_rows = db.execute("SELECT order_id,kind,MIN(at) AS at FROM events GROUP BY order_id,kind").fetchall()
        revisions = db.execute("SELECT id,document FROM revisions").fetchall()
        order_rows = db.execute("SELECT document FROM orders").fetchall()
        recovering = db.execute("SELECT 1 FROM meta WHERE key='recovery_required' AND value='true'").fetchone() is not None
        db.commit()
    events = defaultdict(dict)
    for row in event_rows:
        events[row["order_id"]][row["kind"]] = row["at"]
    names = {row["id"]: json.loads(row["document"])["public"]["name"]
             for row in revisions}
    summary = {"quotes": 0, "executions": 0, "paid_requests": 0, "failed_orders": 0,
               "unknown_orders": 0, "pending_approvals": 0, "settled_amount": 0,
               "pending_amount": 0, "unpaid_results": 0, "untimed_payments": 0}
    services = {}
    daily = {str(first_day + timedelta(days=i)): {"date": str(first_day + timedelta(days=i)), "executions": 0, "paid_requests": 0, "settled_amount": 0}
             for i in range(days)}
    payments, durations, customers = [], [], set()
    for row in order_rows:
        order = json.loads(row[0])
        slug = order["slug"]
        service = services.setdefault(slug, {"slug": slug, "name": names.get(order["revision"], slug),
            "quotes": 0, "executions": 0, "paid_requests": 0, "failed_orders": 0, "settled_amount": 0})
        history = events[order["id"]]
        if cutoff <= order["created_at"] <= current:
            summary["quotes"] += 1; service["quotes"] += 1
            if order["execution"] == "failed" or order["payment"] == "failed":
                summary["failed_orders"] += 1; service["failed_orders"] += 1
        started = history.get("dispatch_intent")
        if started is not None and cutoff <= started <= current:
            summary["executions"] += 1; service["executions"] += 1
            day = str(datetime.fromtimestamp(started, timezone.utc).date())
            if day in daily:
                daily[day]["executions"] += 1
            ended = history.get("result_saved", history.get("execution_reconciled"))
            if ended is not None and ended >= started:
                durations.append(ended - started)
        payment = "unknown" if recovering and order["payment"] != "settled" else order["payment"]
        if payment == "unknown" or order["execution"] == "unknown":
            summary["unknown_orders"] += 1
        if payment in {"verified", "settling", "unknown"}:
            summary["pending_approvals"] += 1
            summary["pending_amount"] += int(order["amount"])
        if order["execution"] == "result_ready" and payment == "failed":
            summary["unpaid_results"] += 1
        if payment != "settled" or not order.get("receipt"):
            continue
        times = [value for kind, value in history.items() if kind in SETTLED_EVENTS]
        settled_at = min(times) if times else None
        if settled_at is None:
            summary["untimed_payments"] += 1
        if (settled_at is None and period != "all") or (settled_at is not None and not cutoff <= settled_at <= current):
            continue
        amount = int(order["amount"])
        summary["settled_amount"] += amount; summary["paid_requests"] += 1
        service["settled_amount"] += amount; service["paid_requests"] += 1
        customers.add(order["owner"])
        if settled_at is not None:
            day = str(datetime.fromtimestamp(settled_at, timezone.utc).date())
            if day in daily:
                daily[day]["settled_amount"] += amount; daily[day]["paid_requests"] += 1
        payments.append({"order_id": order["id"], "slug": slug, "service_name": names.get(order["revision"], slug),
            "amount": order["amount"], "payer": order["owner"], "pay_to": order["pay_to"],
            "confirmed_at": settled_at, "transaction": order["receipt"].get("transaction"), "mode": order["mode"]})
    summary.update(unique_paying_customers=len(customers), average_execution_seconds=round(sum(durations) / len(durations), 2) if durations else None)
    for key in ("settled_amount", "pending_amount"):
        summary[key] = str(summary[key])
    for service in services.values():
        service["settled_amount"] = str(service["settled_amount"])
    for day in daily.values():
        day["settled_amount"] = str(day["settled_amount"])
    return {"mode": engine.payments.mode, "period": period, "timezone": "UTC", "as_of": current,
            "recovery_required": recovering, "summary": summary,
            "services": list(services.values()), "daily": list(daily.values()),
            "payments": sorted(payments, key=lambda p: p["confirmed_at"] or 0, reverse=True)[:100],
            "chart_days": days, "pending_scope": "all_time"}


def public_service_usage(engine, slug):
    if engine.store.recovery_required:
        return None
    report = seller_analytics(engine, "all")
    if report["recovery_required"]:
        return None
    service = next((row for row in report["services"] if row["slug"] == slug), None)
    return {"executions": service["executions"] if service else 0,
            "paid_requests": service["paid_requests"] if service else 0,
            "settled_amount": service["settled_amount"] if service else "0",
            "source": "seller_reported", "as_of": report["as_of"]}


def buyer_analytics(buyer):
    with buyer.db() as db:
        records = [json.loads(row[0]) for row in db.execute("SELECT document FROM purchases")]
    summary = {"purchases": 0, "paid_requests": 0, "unknown_purchases": 0, "settled_amount": 0,
               "pending_amount": 0, "budget_committed": 0}
    sellers = {}
    for purchase in records:
        if purchase.get("mode") != "testnet":
            continue
        summary["purchases"] += 1
        origin = purchase["origin"]
        seller = sellers.setdefault(origin, {"origin": origin, "purchases": 0, "paid_requests": 0, "settled_amount": 0})
        seller["purchases"] += 1
        order = purchase.get("order", {})
        amount = int(order.get("amount", "0"))
        settled = purchase["status"] == "settled" and order.get("payment") == "settled"
        if settled:
            summary["settled_amount"] += amount; summary["paid_requests"] += 1
            seller["settled_amount"] += amount; seller["paid_requests"] += 1
        if purchase.get("budget_reserved"):
            summary["budget_committed"] += amount
            if not settled:
                summary["pending_amount"] += amount
        if purchase["status"] in {"unknown", "quote_unknown"}:
            summary["unknown_purchases"] += 1
    summary["budget_limit"] = buyer.budget
    summary["budget_available"] = max(0, buyer.budget - summary["budget_committed"])
    for key in ("settled_amount", "pending_amount", "budget_committed", "budget_limit", "budget_available"):
        summary[key] = str(summary[key])
    for seller in sellers.values():
        seller["settled_amount"] = str(seller["settled_amount"])
    return {"mode": "testnet", "scope": "all_time_local_ledger", "summary": summary, "sellers": list(sellers.values())}
