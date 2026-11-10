import { useEffect, useState } from "react";
import { pretty, request, type UiConfig } from "./api";
import { Button, Message, Toolbar, Workspace } from "./ui";

export function SellerRegistration({ config }: { config: UiConfig }) {
  const [origins, setOrigins] = useState<string[]>([]);
  const [origin, setOrigin] = useState("");
  const [owner, setOwner] = useState(
    config.owner_origin || "http://127.0.0.1:8786",
  );
  const [checked, setChecked] = useState<{ ready: boolean; mode: string }>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    void request<{ seller_origins: string[] }>("/v1/sellers", {
      signal: controller.signal,
    })
      .then((result) => {
        setOrigins(result.seller_origins);
        setOrigin(result.seller_origins[0] || "");
      })
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      });
    return () => controller.abort();
  }, []);
  let ownerLink: string | undefined;
  try {
    const url = new URL(owner);
    if (
      url.protocol === "http:" &&
      url.hostname === "127.0.0.1" &&
      Number(url.port) >= 1024 &&
      !url.username &&
      !url.password &&
      !url.search &&
      !url.hash &&
      url.pathname === "/"
    )
      ownerLink = url.origin;
  } catch {
    /* The form remains editable until it contains a valid local URL. */
  }
  async function check() {
    if (busy) return;
    setBusy(true);
    setError("");
    setChecked(undefined);
    try {
      setChecked(
        await request("/v1/sellers/check", {
          method: "POST",
          body: pretty({ origin }),
        }),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not check the seller.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Workspace
      navigation={
        <>
          <div className="sidebar-heading">Service marketplace</div>
          <nav>
            <a className="nav-item" href="/">
              Browse
            </a>
            <a className="nav-item" href="/sell" aria-current="page">
              Register a seller
            </a>
          </nav>
        </>
      }
    >
      <Toolbar title="Seller registration" detail="Local preview" />
      <div className="list-pane">
        <header className="list-heading">
          <div>
            <h1>Start selling your Hermes skills</h1>
            <p className="muted">
              Run your seller, verify its address, then publish a tested
              service.
            </p>
          </div>
        </header>
        {error && <Message error>{error}</Message>}
        <ol className="setup-steps">
          <li>
            <h2>Start your seller</h2>
            <p>
              Install the prepared Hermes Sell package and start a local seller
              API with its separate owner workspace. Start with simulation to
              try the flow.
            </p>
            <pre>hermes-sell dev</pre>
            <p className="hint">
              Real execution and payment use the isolated testnet setup.
              Simulation makes no real payments.
            </p>
          </li>
          <li>
            <h2>Check your seller address</h2>
            <label>
              Seller API URL
              <select
                aria-label="Seller API URL"
                disabled={busy}
                value={origin}
                onChange={(e) => {
                  setOrigin(e.target.value);
                  setChecked(undefined);
                }}
              >
                {origins.map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </label>
            <p className="hint">
              Only configured local seller addresses are accepted in this
              preview.
            </p>
            <Button busy={busy} disabled={!origin} onClick={() => void check()}>
              Check seller
            </Button>
            {checked && (
              <Message>
                {checked.mode === "simulation"
                  ? "Simulation seller"
                  : "Base Sepolia testnet seller"}
                :{" "}
                {checked.ready
                  ? "connected and ready"
                  : "connected; dependencies need attention"}
                .
              </Message>
            )}
          </li>
          <li>
            <h2>Open your owner workspace</h2>
            <label>
              Owner workspace URL
              <input
                value={owner}
                onChange={(e) => setOwner(e.target.value)}
                placeholder="http://127.0.0.1:8786"
              />
            </label>
            <p className="hint">
              Use the owner URL printed by your seller. Your private admin token
              is entered there.
            </p>
            {checked && ownerLink && (
              <a className="button" data-variant="primary" href={ownerLink}>
                Open owner workspace
              </a>
            )}
          </li>
          <li>
            <h2>Create, test and list</h2>
            <p>
              Choose a skill, set the price and payee, run the test, review the
              result, and publish. Then select List in directory from Services.
            </p>
          </li>
        </ol>
        <section className="section-block">
          <h2>What buyers see</h2>
          <p>
            Your public contract, price, seller address, availability, and
            purchase-backed reviews. Usage and settled volume appear only when
            you choose to share them.
          </p>
          <p className="hint">
            Open Internet seller enrollment and public hosting remain a separate
            release step.
          </p>
        </section>
      </div>
    </Workspace>
  );
}
