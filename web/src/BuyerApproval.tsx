import { useEffect, useRef, useState } from "react";
import type { PaymentPayload } from "@x402/core/types";
import {
  money,
  pretty,
  request as apiRequest,
  type Order,
  type UiConfig,
} from "./api";
import type { ConnectedWallet } from "./wallet";
import { withSigningIntent } from "./paymentIntent";
import { Badge, Button, Message, Toolbar, Workspace } from "./ui";

type Purchase = {
  purchase_id: string;
  status: string;
  order: Order;
  approved_digest?: string;
  error?: { message?: string };
};
type Session = {
  connected: boolean;
  owner?: string;
  address?: string;
  expires_at?: number;
};

export function BuyerApproval({ config }: { config: UiConfig }) {
  const purchaseId = decodeURIComponent(location.pathname.split("/")[2] || "");
  function request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    if (purchaseId) headers.set("X-Purchase-ID", purchaseId);
    else if (config.listing_id) headers.set("X-Listing-ID", config.listing_id);
    return apiRequest<T>(path, {
      ...init,
      headers: Object.fromEntries(headers.entries()),
    });
  }
  const attemptKey = "hermes-sell:buyer-signature:" + purchaseId;
  const [purchase, setPurchase] = useState<Purchase>();
  const [session, setSession] = useState<Session>({ connected: false });
  const [wallet, setWallet] = useState<ConnectedWallet>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [attempted, setAttempted] = useState(() =>
    Boolean(
      purchaseId &&
        localStorage.getItem(attemptKey) &&
        localStorage.getItem(attemptKey) !== "unsigned",
    ),
  );
  const [clock, setClock] = useState(Date.now());
  const operation = useRef(false);
  const generation = useRef(0);
  const approval = useRef<
    { payload: PaymentPayload; payer: string; digest: string } | undefined
  >(undefined);
  const order = purchase?.order;
  const approved = Boolean(
    purchase &&
      ((Boolean(purchase.approved_digest) &&
        purchase.approved_digest === order?.order_digest) ||
        ["approved", "submitted", "settled"].includes(purchase.status)),
  );
  const expired = Boolean(order && clock >= order.quote_expires_at * 1000);
  const canApprove =
    purchase?.status === "awaiting_approval" &&
    order?.payment === "required" &&
    !expired;
  const connected =
    session.connected &&
    Boolean(session.expires_at && clock < session.expires_at * 1000);
  const statusLabel =
    purchase?.status === "unknown"
      ? "Outcome unknown · Check original purchase"
      : purchase?.status === "settled"
        ? "Testnet payment settled"
        : purchase?.status === "failed"
          ? "Purchase failed"
          : purchase?.status === "expired"
            ? "Quote expired"
            : approved
              ? "Approved for agent submission"
              : "Awaiting your approval";

  async function refresh() {
    const [nextSession, nextPurchase] = await Promise.all([
      request<Session>("/buyer/session"),
      purchaseId
        ? request<Purchase>(
            "/buyer/purchases/" + encodeURIComponent(purchaseId),
          )
        : undefined,
    ]);
    setSession(nextSession);
    if (nextPurchase) setPurchase(nextPurchase);
  }
  async function act(action: () => Promise<void>) {
    if (operation.current) return;
    operation.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed.");
    } finally {
      operation.current = false;
      setBusy(false);
    }
  }
  useEffect(() => {
    void act(refresh);
    const timer = setInterval(() => setClock(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    if (!wallet) return;
    const changed = () => {
      generation.current++;
      setWallet(undefined);
      setSession({ connected: false });
      setError(
        "The wallet changed. Reconnect the original wallet before approving this purchase.",
      );
      void request("/buyer/auth/disconnect", { method: "POST" }).catch(
        () => undefined,
      );
    };
    wallet.provider.on?.("accountsChanged", changed);
    wallet.provider.on?.("chainChanged", changed);
    wallet.provider.on?.("disconnect", changed);
    return () => {
      wallet.provider.removeListener?.("accountsChanged", changed);
      wallet.provider.removeListener?.("chainChanged", changed);
      wallet.provider.removeListener?.("disconnect", changed);
    };
  }, [wallet]);

  async function connect() {
    await act(async () => {
      const signing = await import("./wallet");
      const version = generation.current;
      const nextWallet = await signing.connectWallet(config);
      setWallet(nextWallet);
      await signing.signIn(nextWallet, config);
      await signing.assertWallet(nextWallet);
      if (version !== generation.current)
        throw new Error(
          "The wallet changed during sign-in. Reconnect to continue.",
        );
      await refresh();
      setNotice(
        "Wallet connected to the seller. Signing in did not approve a payment.",
      );
    });
  }

  async function approve() {
    if (!wallet || !order || !connected || approved || !canApprove) return;
    await act(async () => {
      const signing = await import("./wallet");
      const version = generation.current;
      await signing.assertWallet(wallet);
      if (session.owner?.toLowerCase() !== wallet.address.toLowerCase())
        throw new Error("Reconnect the wallet that created this purchase.");
      if (!approval.current) {
        if (attempted)
          throw new Error(
            "A signature may already exist. Refresh the purchase; do not sign it again.",
          );
        signing.paymentRequirements(order, config);
        try {
          const payload = await withSigningIntent(
            attemptKey,
            order.order_digest,
            () => signing.signPayment(wallet, order, config),
            signing.wasRejected,
            () => setAttempted(true),
          );
          approval.current = {
            payload,
            payer: wallet.address.toLowerCase(),
            digest: order.order_digest,
          };
        } catch (e) {
          if (signing.wasRejected(e)) {
            setAttempted(false);
            throw new Error(
              "Payment approval was rejected. No approval was saved.",
            );
          }
          throw e;
        }
      }
      if (
        approval.current.payer !== wallet.address.toLowerCase() ||
        approval.current.digest !== order.order_digest
      )
        throw new Error(
          "The saved signature belongs to a different wallet or quote.",
        );
      await signing.assertWallet(wallet);
      if (version !== generation.current)
        throw new Error(
          "The wallet changed during approval. Reconnect and refresh the purchase.",
        );
      const next = await request<Purchase>(
        "/buyer/purchases/" + encodeURIComponent(purchaseId) + "/approve",
        {
          method: "POST",
          body: pretty({
            order_digest: order.order_digest,
            authorization: approval.current.payload,
          }),
        },
      );
      setPurchase(next);
      setNotice("Approved. Ask your agent to submit this purchase.");
    });
  }

  return (
    <Workspace
      navigation={
        <>
          <div className="sidebar-heading">Independent buyer</div>
          <nav>
            <a className="nav-item" href="/purchases">
              Purchases
            </a>
            {config.directory_origin && (
              <a className="nav-item" href={config.directory_origin}>
                Browse services
              </a>
            )}
          </nav>
          <div className="sidebar-note">
            Your agent prepares the request. You review the quote and sign with
            your browser wallet.
          </div>
        </>
      }
    >
      <Toolbar
        title={purchaseId ? "Approve agent purchase" : "Connect buyer wallet"}
        detail="Local buyer"
      >
        <Badge>Wallet approval</Badge>
      </Toolbar>
      <div className="split-view">
        <div className="document-pane">
          <header className="document-heading">
            <span className="eyebrow">Independent Hermes buyer</span>
            <h1>{order?.service_contract?.name || "Connect your wallet"}</h1>
            <p>
              The local buyer keeps the seller session. Your browser wallet
              signs authentication and payment approvals separately.
            </p>
          </header>
          <div className="section-block">
            <h2>Seller origin</h2>
            <p className="mono origin-value">{config.seller_origin}</p>
            <p className="hint">
              Confirm this is the seller you intend to use. This page runs on
              the separate local buyer gateway.
            </p>
          </div>
          {error && <Message error>{error}</Message>}
          {notice && <Message>{notice}</Message>}
          {purchase?.error?.message && (
            <Message error>{purchase.error.message}</Message>
          )}
          {purchase && !order && (
            <Message>
              The quote is not confirmed. Ask your agent to recover this
              original purchase before approving it.
            </Message>
          )}
          {order && (
            <>
              <section className="section-block">
                <h2>Agent request</h2>
                <pre>{pretty(order.input)}</pre>
                <p className="hint">
                  Service version <code>{order.revision}</code>
                </p>
              </section>
              <section className="section-block">
                <h2>Order digest</h2>
                <pre>{order.order_digest}</pre>
                <p className="hint">
                  The payment nonce binds this exact order, its input, and the
                  seller origin.
                </p>
              </section>
            </>
          )}
        </div>
        <aside className="inspector">
          <div className="inspector-heading">Your approval</div>
          <div className="inspector-body">
            {order ? (
              <>
                <div className="price-label">Exact payment</div>
                <div className="price">{money(order.amount)}</div>
                <dl className="quote">
                  <dt>Network</dt>
                  <dd>Base Sepolia testnet</dd>
                  <dt>Payee address</dt>
                  <dd className="mono address">{order.pay_to}</dd>
                  <dt>Wallet</dt>
                  <dd className="mono address">
                    {wallet?.address || session.address || "Not connected"}
                  </dd>
                  <dt>Quote expires</dt>
                  <dd>
                    {new Date(order.quote_expires_at * 1000).toLocaleTimeString(
                      "en-US",
                    )}
                    {expired ? " · Expired" : ""}
                  </dd>
                </dl>
                <Badge
                  tone={
                    purchase?.status === "unknown"
                      ? "warning"
                      : approved
                        ? "success"
                        : "muted"
                  }
                >
                  {statusLabel}
                </Badge>
              </>
            ) : (
              <p className="hint">
                Connect once, then ask your Hermes agent to create a quote. No
                payment is authorized by signing in.
              </p>
            )}
            {!wallet || !connected ? (
              <Button
                variant="primary"
                className="full"
                busy={busy}
                onClick={connect}
              >
                Connect wallet
              </Button>
            ) : (
              <p className="hint">Wallet connected</p>
            )}
            {order && !approved && canApprove && (
              <>
                {(!attempted || approval.current) && (
                  <Button
                    variant="primary"
                    className="full"
                    disabled={!wallet || !connected}
                    busy={busy}
                    onClick={approve}
                  >
                    {attempted
                      ? "Retry same approval"
                      : "Approve " + money(order.amount) + " for agent"}
                  </Button>
                )}
                {attempted && !approval.current && (
                  <p className="hint">
                    A payment signature may already exist. Refresh this purchase
                    and ask your agent to check its status. This page will not
                    create another signature.
                  </p>
                )}
              </>
            )}
            <Button
              className="full"
              busy={busy}
              onClick={() => void act(refresh)}
            >
              Refresh purchase
            </Button>
            {config.buyer_budget && (
              <p className="hint">
                Buyer spending limit: {money(config.buyer_budget)}
              </p>
            )}
            <p className="hint inspector-note">
              Approval saves the signed authorization for the agent. It does not
              submit a purchase or claim that an onchain payment succeeded.
            </p>
          </div>
        </aside>
      </div>
    </Workspace>
  );
}
