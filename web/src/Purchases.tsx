import { useEffect, useState } from "react";
import { money, pretty, request, type Order, type UiConfig } from "./api";
import { Badge, Button, EmptyState, Message, Toolbar, Workspace } from "./ui";
import { Metric } from "./Analytics";
import { ReviewForm } from "./ReviewForm";

type Purchase = {
  purchase_id: string;
  origin: string;
  status: string;
  order?: Order;
  approval_url: string;
};

export function Purchases({ config }: { config: UiConfig }) {
  const [purchases, setPurchases] = useState<Purchase[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [connection, setConnection] = useState<string>();
  const [analytics, setAnalytics] = useState<{
    summary: {
      settled_amount: string;
      pending_amount: string;
      paid_requests: number;
      budget_committed: string;
      budget_available: string;
    };
    sellers: {
      origin: string;
      paid_requests: number;
      settled_amount: string;
    }[];
  }>();
  async function load(signal?: AbortSignal) {
    const result = await request<{ purchases: Purchase[] }>(
      "/buyer/purchases",
      { signal },
    );
    setPurchases(result.purchases);
    if (config.features?.includes("analytics"))
      setAnalytics(await request("/buyer/analytics", { signal }));
  }
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal).catch((e) => {
      if (e.name !== "AbortError") setError(e.message);
    });
    return () => controller.abort();
  }, []);
  async function refresh(identity: string) {
    if (busy) return;
    setBusy(identity);
    setError("");
    setConnection(undefined);
    try {
      const result = await request<{ status: string; approval_url?: string }>(
        "/buyer/purchases/" + encodeURIComponent(identity) + "/status",
      );
      if (result.status === "needs_wallet_connection")
        setConnection(result.approval_url);
      await load();
    } catch (e) {
      setError(
        e instanceof Error ? e.message : "Could not refresh this purchase.",
      );
    } finally {
      setBusy("");
    }
  }
  return (
    <Workspace
      navigation={
        <>
          <div className="sidebar-heading">Buyer workspace</div>
          <nav>
            <a className="nav-item" href="/purchases" aria-current="page">
              Purchases
            </a>
            {config.directory_origin && (
              <a className="nav-item" href={config.directory_origin}>
                Browse services
              </a>
            )}
          </nav>
          <div className="sidebar-note">
            Purchase IDs keep the original seller, wallet and payment approval
            together.
          </div>
        </>
      }
    >
      <Toolbar title="Purchases" detail="Across sellers" />
      <div className="list-pane">
        <header className="list-heading">
          <div>
            <h1>Your purchases</h1>
            <p className="muted">Stored in this buyer's local ledger.</p>
          </div>
        </header>
        {error && <Message error>{error}</Message>}
        {analytics && (
          <>
            <div className="metrics-grid">
              <Metric
                label="Settled testnet spending"
                value={money(analytics.summary.settled_amount)}
                detail={`${analytics.summary.paid_requests} paid requests · All time`}
              />
              <Metric
                label="Pending approvals"
                value={money(analytics.summary.pending_amount)}
                detail="Not included in settled spending"
              />
              <Metric
                label="Budget committed"
                value={money(analytics.summary.budget_committed)}
                detail="Includes paid and unresolved purchases"
              />
              <Metric
                label="Budget available"
                value={money(analytics.summary.budget_available)}
                detail="Shared across sellers"
              />
            </div>
            <details className="section-block">
              <summary>Spending by seller</summary>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Seller</th>
                      <th>Paid requests</th>
                      <th>Settled spending</th>
                    </tr>
                  </thead>
                  <tbody>
                    {analytics.sellers.map((seller) => (
                      <tr key={seller.origin}>
                        <td>
                          <code>{seller.origin}</code>
                        </td>
                        <td>{seller.paid_requests}</td>
                        <td>{money(seller.settled_amount)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          </>
        )}
        {connection && (
          <Message>
            <a href={connection}>Reconnect the original wallet</a> to check this
            seller's order.
          </Message>
        )}
        {!purchases.length && (
          <EmptyState title="No purchases yet">
            <p>Find a service in Browse, then ask Hermes to prepare a quote.</p>
          </EmptyState>
        )}
        {purchases.map((purchase) => (
          <article className="purchase-card" key={purchase.purchase_id}>
            <div className="service-title">
              <h2>
                {purchase.order?.service_contract?.name || "Pending quote"}
              </h2>
              <Badge tone={purchase.status === "settled" ? "success" : "muted"}>
                {purchase.status}
              </Badge>
            </div>
            <p>
              <code>{purchase.origin}</code>
            </p>
            <div className="meta-row">
              <code>{purchase.purchase_id}</code>
              {purchase.order && (
                <strong>{money(purchase.order.amount)}</strong>
              )}
            </div>
            <div className="directory-actions">
              <a href={purchase.approval_url}>Review purchase</a>
              <Button
                busy={busy === purchase.purchase_id}
                disabled={Boolean(busy)}
                onClick={() => void refresh(purchase.purchase_id)}
              >
                Refresh status
              </Button>
            </div>
            {purchase.order?.payment === "settled" &&
              purchase.order.result !== undefined && (
                <>
                  <h3>Result</h3>
                  <pre>{pretty(purchase.order.result)}</pre>
                  <details>
                    <summary>Receipt</summary>
                    <pre>{pretty(purchase.order.receipt)}</pre>
                  </details>
                  <ReviewForm
                    config={{ ...config, seller_origin: purchase.origin }}
                    order={purchase.order}
                    endpoint={"/buyer/purchases/" + purchase.purchase_id}
                    reconnect={purchase.approval_url}
                  />
                </>
              )}
          </article>
        ))}
      </div>
    </Workspace>
  );
}
