import { useEffect, useState } from "react";
import { money, request } from "./api";
import { Badge, Button, Message } from "./ui";

type Summary = {
  quotes: number;
  executions: number;
  paid_requests: number;
  failed_orders: number;
  unknown_orders: number;
  settled_amount: string;
  pending_amount: string;
  pending_approvals: number;
  unique_paying_customers: number;
  average_execution_seconds: number | null;
  unpaid_results: number;
  untimed_payments: number;
};
type AnalyticsData = {
  period: string;
  mode: string;
  recovery_required: boolean;
  summary: Summary;
  services: {
    slug: string;
    name: string;
    quotes: number;
    executions: number;
    paid_requests: number;
    failed_orders: number;
    settled_amount: string;
  }[];
  daily: {
    date: string;
    executions: number;
    paid_requests: number;
    settled_amount: string;
  }[];
  payments: {
    order_id: string;
    service_name: string;
    amount: string;
    payer: string;
    pay_to: string;
    confirmed_at: number | null;
    transaction: string;
    mode: string;
  }[];
};

export function Metric({
  label,
  value,
  detail,
}: {
  label: string;
  value: string | number;
  detail?: string;
}) {
  return (
    <div className="metric-card">
      <div className="muted">{label}</div>
      <strong>{value}</strong>
      {detail && <span className="hint">{detail}</span>}
    </div>
  );
}

export function Analytics({ token }: { token: string }) {
  const [period, setPeriod] = useState("30d");
  const [report, setReport] = useState<AnalyticsData>();
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    setBusy(true);
    setError("");
    void request<AnalyticsData>(
      "/admin/analytics?period=" + period,
      { signal: controller.signal },
      token,
    )
      .then(setReport)
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => controller.abort();
  }, [token, period, refresh]);
  const summary = report?.summary;
  const max = Math.max(
    1,
    ...(report?.daily.map((day) => day.executions) || []),
  );
  return (
    <div className="list-pane">
      <header className="list-heading">
        <div>
          <h1>Usage and payments</h1>
          <p className="muted">Durable order and settlement records · UTC</p>
        </div>
        <div className="directory-actions">
          <select
            aria-label="Analytics period"
            value={period}
            onChange={(e) => setPeriod(e.target.value)}
          >
            <option value="7d">Last 7 days</option>
            <option value="30d">Last 30 days</option>
            <option value="all">All time</option>
          </select>
          <Button busy={busy} onClick={() => setRefresh((v) => v + 1)}>
            Refresh analytics
          </Button>
        </div>
      </header>
      {error && <Message error>{error}</Message>}
      {report?.recovery_required && (
        <Message error>
          Recovery lock is active. This restored ledger may be incomplete;
          reconcile records before relying on totals.
        </Message>
      )}
      {summary && (
        <>
          <div className="metrics-grid">
            <Metric
              label={
                report.mode === "simulation"
                  ? "Simulated settled volume"
                  : "Settled testnet volume"
              }
              value={money(summary.settled_amount)}
              detail="Confirmed payments only"
            />
            <Metric
              label="Paid requests"
              value={summary.paid_requests}
              detail={`${summary.unique_paying_customers} paying ${summary.unique_paying_customers === 1 ? "customer" : "customers"}`}
            />
            <Metric
              label="Executions started"
              value={summary.executions}
              detail={`${summary.quotes} quotes created`}
            />
            <Metric
              label="Pending confirmation"
              value={money(summary.pending_amount)}
              detail={`${summary.pending_approvals} approvals · All time`}
            />
            <Metric
              label="Failed orders"
              value={summary.failed_orders}
              detail={`${summary.unpaid_results} completed results without payment`}
            />
            <Metric
              label="Unknown outcomes"
              value={summary.unknown_orders}
              detail="All time · Requires reconciliation"
            />
          </div>
          <section className="section-block">
            <h2>Daily executions</h2>
            <p className="hint">
              {period === "all"
                ? "Last 30 days shown; cards include all time."
                : "Selected period."}{" "}
              Retries and status polls do not add executions.
            </p>
            <div
              className="usage-bars"
              role="img"
              aria-label="Daily executions in UTC"
            >
              {report.daily.map((day, index) => (
                <div
                  className="usage-bar-slot"
                  key={day.date}
                  title={`${day.date}: ${day.executions} executions`}
                >
                  <div
                    className="usage-bar"
                    style={{ height: `${(day.executions / max) * 100}%` }}
                  />
                  {(report.daily.length <= 7 ||
                    index === report.daily.length - 1 ||
                    (index % 7 === 0 && index < report.daily.length - 4)) && (
                    <span>{day.date.slice(5)}</span>
                  )}
                </div>
              ))}
            </div>
            <p className="hint">
              Average observed execution time:{" "}
              {summary.average_execution_seconds === null
                ? "No completed executions"
                : `${summary.average_execution_seconds} seconds`}
              .
            </p>
          </section>
          <section className="section-block">
            <h2>By service</h2>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Service</th>
                    <th>Quotes</th>
                    <th>Executions</th>
                    <th>Paid</th>
                    <th>Failed</th>
                    <th>Settled volume</th>
                  </tr>
                </thead>
                <tbody>
                  {report.services.map((service) => (
                    <tr key={service.slug}>
                      <td>{service.name}</td>
                      <td>{service.quotes}</td>
                      <td>{service.executions}</td>
                      <td>{service.paid_requests}</td>
                      <td>{service.failed_orders}</td>
                      <td>{money(service.settled_amount)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
          <section className="section-block">
            <h2>Confirmed payments</h2>
            {!report.payments.length ? (
              <p className="muted">No confirmed payments in this period.</p>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Service</th>
                      <th>Amount</th>
                      <th>Confirmed (UTC)</th>
                      <th>Receipt</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.payments.map((payment) => (
                      <tr key={payment.order_id}>
                        <td>
                          {payment.service_name}
                          <div className="hint">
                            {payment.order_id.slice(0, 8)}
                          </div>
                        </td>
                        <td>{money(payment.amount)}</td>
                        <td>
                          {payment.confirmed_at
                            ? new Date(payment.confirmed_at * 1000)
                                .toISOString()
                                .replace("T", " ")
                                .slice(0, 19)
                            : "Time unavailable"}
                        </td>
                        <td>
                          {payment.mode === "testnet" &&
                          /^0x[0-9a-fA-F]{64}$/.test(payment.transaction) ? (
                            <a
                              href={`https://sepolia.basescan.org/tx/${payment.transaction}`}
                              target="_blank"
                              rel="noreferrer"
                            >
                              View transaction
                            </a>
                          ) : (
                            <Badge>Simulation</Badge>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <p className="hint">
              Amounts are gross confirmed USDC transfers, not model costs or
              profit. Confirmation time records when this ledger observed
              settlement. Pending and unknown amounts are not included in
              settled volume.
            </p>
            {summary.untimed_payments > 0 && (
              <Message>
                {summary.untimed_payments} payments have no recorded
                confirmation time. Select All time to include them.
              </Message>
            )}
          </section>
        </>
      )}
    </div>
  );
}
