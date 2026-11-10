import { useEffect, useRef, useState } from "react";
import {
  ApiError,
  money,
  pretty,
  request,
  type Order,
  type Schema,
  type Service,
  type UiConfig,
} from "./api";
import type { ConnectedWallet } from "./wallet";
import {
  intentKey,
  mayHaveSignature,
  rememberUnsignedOrder,
  withSigningIntent,
} from "./paymentIntent";
import { SchemaForm } from "./SchemaForm";
import { ReviewForm } from "./ReviewForm";
import { Badge, Button, EmptyState, Message, Toolbar, Workspace } from "./ui";

const LAST_ORDER = "hermes-sell:last-order";
function compatibleInput(value: unknown, schema: Schema): unknown {
  if (
    value === undefined ||
    (schema.enum && !schema.enum.includes(value as never))
  )
    return undefined;
  if (schema.type === "object") {
    if (!value || typeof value !== "object" || Array.isArray(value))
      return undefined;
    return Object.fromEntries(
      Object.entries(schema.properties || {}).flatMap(([key, child]) => {
        const next = compatibleInput(
          (value as Record<string, unknown>)[key],
          child,
        );
        return next === undefined ? [] : [[key, next]];
      }),
    );
  }
  if (schema.type === "array")
    return Array.isArray(value) && schema.items
      ? value
          .map((item) => compatibleInput(item, schema.items!))
          .filter((item) => item !== undefined)
      : undefined;
  const type = schema.type === "integer" ? "number" : schema.type;
  return typeof value === type ? value : undefined;
}
const LABELS: Record<string, string> = {
  created: "Quote created",
  queued: "Queued",
  dispatching: "Dispatching",
  running: "Running",
  result_ready: "Result ready",
  failed: "Failed",
  unknown: "Needs review",
  required: "Awaiting approval",
  verified: "Approval verified",
  settling: "Settling",
  settled: "Settled",
};

export function Storefront({ config }: { config: UiConfig }) {
  const testnet = config.mode === "testnet";
  const [wallet, setWallet] = useState<ConnectedWallet>();
  const walletGeneration = useRef(0);
  const [services, setServices] = useState<Service[]>([]);
  const [selected, setSelected] = useState<Service>();
  const [value, setValue] = useState<Record<string, unknown>>({});
  const [session, setSession] = useState<string>();
  const [order, setOrder] = useState<Order>();
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [needsRefresh, setNeedsRefresh] = useState(false);
  const [hasSaved, setHasSaved] = useState(() =>
    Boolean(localStorage.getItem(LAST_ORDER)),
  );
  const [admissionAttempted, setAdmissionAttempted] = useState(false);
  const [unadmittedConfirmed, setUnadmittedConfirmed] = useState(false);
  const [clock, setClock] = useState(Date.now());
  const operation = useRef(false);
  const form = useRef<HTMLFormElement>(null);
  const authorization = useRef<object | null>(null);
  const authorizationOwner = useRef<string | undefined>(undefined);
  const authorizationExpiry = useRef<number | undefined>(undefined);
  const quoteKey = useRef<{ key: string; input: string; slug: string } | null>(
    null,
  );
  const service = order?.service_contract || selected;
  const expired = Boolean(order && clock >= order.quote_expires_at * 1000);
  const uncertain =
    order?.execution === "unknown" || order?.payment === "unknown";
  const replacementAfter = order
    ? Math.max(
        order.quote_expires_at,
        authorizationExpiry.current ?? order.quote_expires_at + 180,
      )
    : 0;
  useEffect(() => {
    if (!wallet) return;
    const changed = () => {
      walletGeneration.current++;
      setSession(undefined);
      setWallet(undefined);
      setError(
        "The wallet account or network changed. Reconnect the original wallet to continue.",
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
  function fail(e: unknown) {
    if (e instanceof ApiError && e.status === 401) setSession(undefined);
    if (e instanceof ApiError && e.code === "revision_changed")
      setNeedsRefresh(true);
    setError(e instanceof Error ? e.message : "Request failed.");
  }
  function adopt(next: Order) {
    setOrder(next);
    setValue(next.input);
    localStorage.setItem(LAST_ORDER, next.id);
    setHasSaved(true);
    if (next.service_contract) setSelected(next.service_contract);
  }
  useEffect(() => {
    const controller = new AbortController();
    request<{ services: Service[] }>("/v1/services", {
      signal: controller.signal,
    })
      .then((data) => {
        setServices(data.services);
        const slug = decodeURIComponent(location.pathname.split("/")[2] || "");
        const found =
          data.services.find((item) => item.slug === slug) ||
          (!slug ? data.services[0] : undefined);
        if (found) {
          setSelected(found);
          setValue(found.examples[0].input);
        } else if (slug) setError("Service not found.");
        setLoaded(true);
      })
      .catch((e) => {
        if (e.name !== "AbortError") {
          fail(e);
          setLoaded(true);
        }
      });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    if (!order || order.payment !== "required") return;
    const timer = setInterval(() => setClock(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [order?.id, order?.payment]);
  useEffect(() => {
    if (
      !session ||
      !order ||
      ["required", "settled", "failed", "unknown"].includes(order.payment) ||
      order.execution === "unknown"
    )
      return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const next = await request<Order>(
          "/v1/orders/" + order!.id,
          { signal: controller.signal },
          session,
        );
        if (!controller.signal.aborted) setOrder(next);
      } catch (e) {
        if (!controller.signal.aborted) fail(e);
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, 1000);
    }
    timer = setTimeout(poll, 500);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [session, order?.id, order?.payment, order?.execution]);

  async function act(action: () => Promise<void>) {
    if (operation.current) return;
    operation.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
    } catch (e) {
      fail(e);
    } finally {
      operation.current = false;
      setBusy(false);
    }
  }
  async function connect() {
    await act(async () => {
      const generation = walletGeneration.current;
      let result: { token: string };
      if (testnet) {
        const signing = await import("./wallet");
        const connected = await signing.connectWallet(config);
        setWallet(connected);
        const signedIn = await signing.signIn(connected, config);
        if (!signedIn.token)
          throw new Error("The seller did not return a session token.");
        result = { token: signedIn.token };
        await signing.assertWallet(connected);
        if (generation !== walletGeneration.current)
          throw new Error(
            "The wallet changed during sign-in. Reconnect to continue.",
          );
      } else {
        result = await request<{ token: string }>("/dev/session", {
          method: "POST",
          body: pretty({ actor: "demo-browser" }),
        });
      }
      setSession(result.token);
      const saved = localStorage.getItem(LAST_ORDER);
      if (saved) {
        try {
          adopt(await request<Order>("/v1/orders/" + saved, {}, result.token));
          setAdmissionAttempted(testnet ? mayHaveSignature(saved) : true);
        } catch (e) {
          if (testnet) setSession(undefined);
          if (e instanceof ApiError && e.status === 404) {
            if (testnet)
              throw new Error(
                "The saved order is not available to this wallet. Reconnect the wallet that created it.",
              );
            localStorage.removeItem(LAST_ORDER);
            setHasSaved(false);
            setOrder(undefined);
            authorization.current = null;
            quoteKey.current = null;
            setAdmissionAttempted(false);
            setNotice(
              "The saved order is not in the current ledger. You can create a new quote.",
            );
          } else throw e;
        }
      }
    });
  }
  async function quote() {
    if (!service || !session || !form.current?.reportValidity()) return;
    await act(async () => {
      if (testnet) {
        if (!wallet) throw new Error("Connect your wallet first.");
        await (await import("./wallet")).assertWallet(wallet);
      }
      const body = pretty({ revision: service.revision, input: value });
      if (
        quoteKey.current?.input !== body ||
        quoteKey.current?.slug !== service.slug
      )
        quoteKey.current = {
          key: crypto.randomUUID(),
          input: body,
          slug: service.slug,
        };
      const next = await request<Order>(
        "/v1/services/" + service.slug + "/orders",
        {
          method: "POST",
          body,
          headers: { "Idempotency-Key": quoteKey.current.key },
        },
        session,
      );
      if (testnet) rememberUnsignedOrder(next.id);
      adopt(next);
    });
  }
  async function purchase() {
    if (!session || !order || expired || uncertain) return;
    await act(async () => {
      const generation = walletGeneration.current;
      if (testnet) {
        if (!wallet) throw new Error("Connect your wallet first.");
        const signing = await import("./wallet");
        await signing.assertWallet(wallet);
        if (!authorization.current) {
          if (admissionAttempted)
            throw new Error(
              "This order may already have a signature. Refresh the original order instead.",
            );
          signing.paymentRequirements(order, config);
          try {
            authorization.current = await withSigningIntent(
              intentKey(order.id),
              order.order_digest,
              () => signing.signPayment(wallet, order, config),
              signing.wasRejected,
              () => setAdmissionAttempted(true),
            );
            authorizationOwner.current = wallet.address.toLowerCase();
            authorizationExpiry.current = Number(
              (
                authorization.current as {
                  payload: { authorization: { validBefore: string } };
                }
              ).payload.authorization.validBefore,
            );
          } catch (e) {
            if (signing.wasRejected(e)) {
              setAdmissionAttempted(false);
              throw new Error(
                "Payment approval was rejected. No payment was submitted.",
              );
            }
            throw e;
          }
        }
        if (authorizationOwner.current !== wallet.address.toLowerCase())
          throw new Error("Reconnect the wallet that approved this order.");
        await signing.assertWallet(wallet);
        if (generation !== walletGeneration.current)
          throw new Error(
            "The wallet changed during approval. The original signature is kept; reconnect to check the order.",
          );
      } else if (!authorization.current) {
        authorization.current = {
          mode: "simulation",
          order_digest: order.order_digest,
          payer: "demo-browser",
          nonce: crypto.randomUUID(),
          valid_before: Math.floor(Date.now() / 1000) + 180,
        };
      }
      setAdmissionAttempted(true);
      adopt(
        await request<Order>(
          "/v1/orders/" + order.id,
          {
            method: "POST",
            body: pretty({ authorization: authorization.current }),
          },
          session,
        ),
      );
    });
  }
  function reset() {
    localStorage.removeItem(LAST_ORDER);
    setHasSaved(false);
    setOrder(undefined);
    authorization.current = null;
    authorizationOwner.current = undefined;
    authorizationExpiry.current = undefined;
    quoteKey.current = null;
    setAdmissionAttempted(false);
    setUnadmittedConfirmed(false);
    setError("");
    const latest = services.find((item) => item.slug === service?.slug);
    if (latest) {
      setSelected(latest);
      setValue(latest.examples[0].input);
    }
  }
  function choose(item: Service) {
    if (operation.current || order) return;
    setSelected(item);
    setValue(item.examples[0].input);
    setError("");
    setNeedsRefresh(false);
    quoteKey.current = null;
    history.replaceState(null, "", "/s/" + item.slug);
  }
  return (
    <Workspace
      navigation={
        <>
          <div className="sidebar-heading">Services</div>
          <nav aria-label="Choose a service">
            {services.map((item) => (
              <Button
                key={item.slug}
                variant="ghost"
                className="nav-item"
                aria-current={service?.slug === item.slug ? "page" : undefined}
                disabled={Boolean(order) || busy}
                onClick={() => choose(item)}
              >
                <span className="nav-glyph" aria-hidden="true">
                  ◇
                </span>
                <span>{item.name}</span>
              </Button>
            ))}
          </nav>
          <div className="sidebar-note">
            Choose a service.
            <br />
            Review the input and price.
          </div>
        </>
      }
    >
      <Toolbar
        title={service?.name || "Services"}
        detail={order ? "Order " + order.id.slice(0, 8) : "New request"}
      >
        <Badge>{testnet ? "Testnet payments" : "Local preview"}</Badge>
      </Toolbar>
      {error && (
        <div className="message-inset">
          <Message error>{error}</Message>
          {needsRefresh && !order && (
            <Button
              disabled={busy}
              onClick={() =>
                void act(async () => {
                  const latest = await request<Service>(
                    "/v1/services/" + selected!.slug,
                  );
                  setSelected(latest);
                  setServices((current) =>
                    current.map((item) =>
                      item.slug === latest.slug ? latest : item,
                    ),
                  );
                  setValue(
                    compatibleInput(value, latest.input_schema) as Record<
                      string,
                      unknown
                    >,
                  );
                  quoteKey.current = null;
                  setNeedsRefresh(false);
                  setNotice(
                    "Loaded the latest contract and kept compatible input. Review the updated input and price, then request a new quote.",
                  );
                })
              }
            >
              Review latest contract
            </Button>
          )}
        </div>
      )}
      {notice && (
        <div className="message-inset">
          <Message>{notice}</Message>
        </div>
      )}
      {!loaded ? (
        <EmptyState title="Loading services…" />
      ) : !service ? (
        <EmptyState title="No published services">
          <p>Publish a service from the seller workspace to see it here.</p>
        </EmptyState>
      ) : (
        <div className="split-view">
          <div className="document-pane">
            <header className="document-heading">
              <span className="eyebrow">Service</span>
              <h1>{service.name}</h1>
              <p>{service.description}</p>
              <div className="meta-row">
                <span>JSON output</span>
                <span>Text input</span>
                <span className="mono">v {service.revision.slice(0, 8)}</span>
              </div>
            </header>
            <section className="section-block">
              <div className="section-heading">
                <h2>Your request</h2>
                <Button
                  variant="text"
                  size="sm"
                  disabled={Boolean(order) || busy}
                  onClick={() => setValue(service.examples[0].input)}
                >
                  Use example
                </Button>
              </div>
              <form
                ref={form}
                onSubmit={(e) => {
                  e.preventDefault();
                  void quote();
                }}
              >
                <fieldset disabled={Boolean(order) || busy}>
                  <SchemaForm
                    schema={service.input_schema}
                    value={value}
                    onChange={(next) =>
                      setValue(next as Record<string, unknown>)
                    }
                  />
                </fieldset>
              </form>
            </section>
            <details className="section-block">
              <summary>Example output and API</summary>
              <p className="hint">
                {config.execution_mode === "hermes"
                  ? "Example output format. Each request runs the published skill."
                  : "The local simulation returns the fixed example below."}
              </p>
              <pre>{pretty(service.examples[0].output)}</pre>
              <a href={"/v1/services/" + service.slug + "/openapi.json"}>
                Current service OpenAPI specification ↗
              </a>
              {order && (
                <p className="hint">
                  This order uses the pinned version shown above. The current
                  API specification may describe a different version.
                </p>
              )}
            </details>
            {order?.result !== undefined && (
              <section className="section-block result">
                <div className="section-heading">
                  <h2>Result</h2>
                  <Badge tone="success">
                    {testnet
                      ? "Testnet settlement confirmed"
                      : "Simulated settlement complete"}
                  </Badge>
                </div>
                <p className="hint">
                  {testnet
                    ? "Base Sepolia testnet payment · " +
                      (config.execution_mode === "hermes"
                        ? "Hermes result"
                        : "Simulated result")
                    : "Simulated result · No model execution or real payment"}
                </p>
                <pre>{pretty(order.result)}</pre>
                <details>
                  <summary>
                    {testnet ? "Testnet receipt" : "Simulated receipt"}
                  </summary>
                  <pre>{pretty(order.receipt)}</pre>
                </details>
                <ReviewForm
                  config={config}
                  order={order}
                  token={session}
                  wallet={wallet}
                  endpoint={"/v1/orders/" + order.id}
                />
              </section>
            )}
          </div>
          <aside className="inspector">
            <div className="inspector-heading">Order</div>
            <div className="inspector-body">
              <div className="price-label">Price per request</div>
              <div className="price">
                {money(order?.amount || service.amount)}
              </div>
              <p className="hint">
                {testnet
                  ? "Testnet USDC only · Base Sepolia"
                  : "No real charges in this preview"}
              </p>
              <ol className="steps">
                <li className={session ? "done" : "active"}>
                  {testnet
                    ? "Connect wallet and sign in"
                    : "Connect test session"}
                </li>
                <li className={order ? "done" : session ? "active" : ""}>
                  Review quote
                </li>
                <li
                  className={
                    order?.payment === "settled"
                      ? "done"
                      : order
                        ? "active"
                        : ""
                  }
                >
                  Approve purchase and get result
                </li>
              </ol>
              {!session ? (
                <>
                  <Button
                    variant="primary"
                    className="full"
                    busy={busy}
                    onClick={connect}
                  >
                    {hasSaved
                      ? "Reconnect and restore order"
                      : testnet
                        ? "Connect wallet"
                        : "Connect test session"}
                  </Button>
                  <p className="hint">
                    {testnet
                      ? "Use an EOA browser wallet on Base Sepolia. Signing in proves ownership and does not approve a payment."
                      : "This development account stands in for a connected wallet."}
                  </p>
                </>
              ) : !order ? (
                <Button
                  variant="primary"
                  className="full"
                  busy={busy}
                  onClick={quote}
                >
                  Review quote
                </Button>
              ) : (
                <>
                  <dl className="quote">
                    <dt>Order</dt>
                    <dd className="mono">{order.id.slice(0, 8)}</dd>
                    <dt>Network</dt>
                    <dd>
                      {testnet
                        ? "Base Sepolia testnet"
                        : "Base Sepolia · Simulated"}
                    </dd>
                    {testnet && (
                      <>
                        <dt>Storefront</dt>
                        <dd className="mono">{order.origin}</dd>
                        <dt>Wallet</dt>
                        <dd className="mono address">
                          {wallet?.address || "Reconnect to verify"}
                        </dd>
                        <dt>Order digest</dt>
                        <dd className="mono address">{order.order_digest}</dd>
                      </>
                    )}
                    <dt>Payee address</dt>
                    <dd className="mono address">{order.pay_to}</dd>
                    <dt>Quote expires</dt>
                    <dd>
                      {new Date(
                        order.quote_expires_at * 1000,
                      ).toLocaleTimeString("en-US")}
                      {expired ? " · Expired" : ""}
                    </dd>
                  </dl>
                  <div className="status-row" aria-live="polite">
                    <span>{LABELS[order.execution] || order.execution}</span>
                    <Badge
                      tone={
                        uncertain
                          ? "warning"
                          : order.payment === "settled"
                            ? "success"
                            : "muted"
                      }
                    >
                      {LABELS[order.payment] || order.payment}
                    </Badge>
                  </div>
                  {order.payment === "required" &&
                    (!admissionAttempted || authorization.current) && (
                      <Button
                        variant="primary"
                        className="full"
                        disabled={expired}
                        busy={busy}
                        onClick={purchase}
                      >
                        {admissionAttempted
                          ? "Retry same approval"
                          : "Approve " +
                            money(order.amount) +
                            (testnet ? " on testnet" : " simulation")}
                      </Button>
                    )}
                  {order.payment === "required" &&
                    admissionAttempted &&
                    !authorization.current && (
                      <p className="hint">
                        This order will not be signed again automatically.
                        Refresh the original order. If the quote was never
                        accepted, wait for it to expire before starting a new
                        order.
                      </p>
                    )}
                  {order.error && (
                    <Message error>
                      {uncertain
                        ? "The outcome needs confirmation. Do not retry with a new approval."
                        : order.error}
                    </Message>
                  )}
                  {(admissionAttempted || uncertain) && (
                    <Button
                      className="full"
                      busy={busy}
                      onClick={() =>
                        void act(async () => {
                          const requestedAt = Date.now();
                          const next = await request<Order>(
                            "/v1/orders/" + order.id,
                            {},
                            session,
                          );
                          adopt(next);
                          if (
                            testnet &&
                            next.payment === "required" &&
                            next.execution === "created" &&
                            requestedAt >= replacementAfter * 1000
                          )
                            setUnadmittedConfirmed(true);
                        })
                      }
                    >
                      Refresh original order
                    </Button>
                  )}
                  {["required", "settled", "failed"].includes(
                    order.payment,
                  ) && (
                    <Button
                      variant="text"
                      className="full"
                      disabled={
                        busy ||
                        (order.payment === "required" &&
                          admissionAttempted &&
                          (testnet ? !unadmittedConfirmed : !expired))
                      }
                      onClick={reset}
                    >
                      Start new order
                    </Button>
                  )}
                  {testnet &&
                    admissionAttempted &&
                    order.payment === "required" &&
                    expired &&
                    !unadmittedConfirmed && (
                      <p className="hint">
                        Before starting a replacement order, refresh this
                        original order after{" "}
                        {new Date(replacementAfter * 1000).toLocaleTimeString(
                          "en-US",
                        )}{" "}
                        to confirm the previous approval can no longer be used.
                      </p>
                    )}
                </>
              )}
              <div className="inspector-note">
                <strong>After approval</strong>
                <p>
                  {testnet
                    ? "Approve the amount, recipient, and storefront shown above. Your wallet signs one order-bound USDC authorization. The result is released after settlement is confirmed. Checking an order does not add a charge."
                    : "The result is saved before simulated settlement is confirmed. Checking an order does not add a charge."}
                </p>
              </div>
            </div>
          </aside>
        </div>
      )}
    </Workspace>
  );
}
