import { useEffect, useState } from "react";
import { money, pretty, request, type Service, type UiConfig } from "./api";
import { Badge, Button, EmptyState, Message, Toolbar, Workspace } from "./ui";

type Listing = {
  id: string;
  origin: string;
  service: Service;
  mode: "simulation" | "testnet";
  state: "online" | "offline" | "stale" | "withdrawn";
  storefront_url: string;
  checked_at: number;
  ratings?: { average: number | null; count: number; verified_count: number };
  usage?: {
    executions: number;
    paid_requests: number;
    settled_amount: string;
    as_of: number;
  };
};

export function Directory({ config }: { config: UiConfig }) {
  const identity = location.pathname.startsWith("/services/")
    ? location.pathname.split("/")[2]
    : undefined;
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState("");
  const [listings, setListings] = useState<Listing[]>([]);
  const [selected, setSelected] = useState<Listing>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [reviews, setReviews] = useState<
    {
      score: number;
      comment: string;
      reviewer: string;
      verification: string;
      revision: string;
      updated_at: number;
    }[]
  >([]);
  useEffect(() => {
    if (!identity) return;
    const controller = new AbortController();
    void request<{ reviews: typeof reviews }>(
      "/v1/listings/" + encodeURIComponent(identity) + "/reviews",
      { signal: controller.signal },
    )
      .then((result) => setReviews(result.reviews))
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      });
    return () => controller.abort();
  }, [identity]);
  useEffect(() => {
    const controller = new AbortController();
    setBusy(true);
    setError("");
    const path = identity
      ? "/v1/listings/" + encodeURIComponent(identity)
      : "/v1/listings?" + new URLSearchParams({ q: query, mode });
    const timer = setTimeout(() => {
      void request<Listing | { listings: Listing[] }>(path, {
        signal: controller.signal,
      })
        .then((value) => {
          if ("listings" in value) setListings(value.listings);
          else setSelected(value);
        })
        .catch((e) => {
          if (e.name !== "AbortError") setError(e.message);
        })
        .finally(() => {
          if (!controller.signal.aborted) setBusy(false);
        });
    }, 150);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [query, mode, identity]);
  async function refresh() {
    if (!selected || busy) return;
    setBusy(true);
    setError("");
    try {
      setSelected(
        await request<Listing>("/v1/listings", {
          method: "POST",
          body: pretty({
            origin: selected.origin,
            slug: selected.service.slug,
          }),
        }),
      );
    } catch (e) {
      setError(
        e instanceof Error ? e.message : "Could not refresh this listing.",
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <Workspace
      navigation={
        <>
          <div className="sidebar-heading">Service marketplace</div>
          <nav aria-label="Marketplace navigation">
            <a
              className="nav-item"
              href="/"
              aria-current={!identity ? "page" : undefined}
            >
              Browse
            </a>
            <a className="nav-item" href={config.owner_origin}>
              My services
            </a>
            <a className="nav-item" href="/sell">
              Register a seller
            </a>
            <a className="nav-item" href={config.buyer_origin + "/purchases"}>
              Purchases
            </a>
          </nav>
          <div className="sidebar-note">
            Find an API. Review its live contract and approve each purchase.
          </div>
        </>
      }
    >
      <Toolbar
        title={selected ? selected.service.name : "Browse services"}
        detail="Across sellers"
      />
      <div className="list-pane">
        {error && <Message error>{error}</Message>}
        {identity && selected ? (
          <>
            <header className="list-heading">
              <div>
                <h1>{selected.service.name}</h1>
                <p>{selected.service.description}</p>
              </div>
              <Badge tone={selected.state === "online" ? "success" : "warning"}>
                {selected.state}
              </Badge>
            </header>
            <div className="directory-detail">
              <div className="meta-row">
                <Badge>
                  {selected.mode === "testnet"
                    ? "Base Sepolia testnet"
                    : "Simulation"}
                </Badge>
                <strong>{money(selected.service.amount)}</strong>
                <Badge>
                  {selected.ratings?.count
                    ? `★ ${selected.ratings.average} · ${selected.ratings.count} ${selected.ratings.count === 1 ? "review" : "reviews"}`
                    : "No reviews yet"}
                </Badge>
              </div>
              {selected.usage && (
                <section className="public-usage">
                  <h2>Seller-reported usage</h2>
                  <p>
                    {selected.usage.executions} executions ·{" "}
                    {selected.usage.paid_requests} paid requests ·{" "}
                    {money(selected.usage.settled_amount)} settled
                  </p>
                  <p className="hint">
                    Shared by the seller · All time ·{" "}
                    {selected.mode === "simulation"
                      ? "Simulation totals"
                      : "Testnet USDC"}
                  </p>
                </section>
              )}
              <h2>Seller</h2>
              <code>{selected.origin}</code>
              <h2>Payee</h2>
              <code>{selected.service.pay_to}</code>
              <h2>Service version</h2>
              <code>{selected.service.revision}</code>
              <h2>Example input</h2>
              <pre>{pretty(selected.service.examples[0].input)}</pre>
              <h2>Example output</h2>
              <pre>{pretty(selected.service.examples[0].output)}</pre>
              <p className="muted">
                Examples describe the contract. Review the current version and
                exact quote on the seller's storefront before approving payment.
              </p>
              <div className="directory-actions">
                {selected.state === "online" && (
                  <a
                    className="button"
                    data-variant="primary"
                    href={selected.storefront_url}
                  >
                    Open storefront
                  </a>
                )}
                {selected.state === "online" && selected.mode === "testnet" && (
                  <a
                    className="button"
                    href={
                      config.buyer_origin + "/connect?listing_id=" + selected.id
                    }
                  >
                    Use with Hermes
                  </a>
                )}
                <Button onClick={() => void refresh()} busy={busy}>
                  Refresh availability
                </Button>
              </div>
              {selected.state !== "online" && (
                <Message>
                  This seller is unavailable or the listing was withdrawn.
                  Refresh before preparing a purchase.
                </Message>
              )}
              <section className="section-block">
                <h2>Buyer reviews</h2>
                <p className="hint">
                  One rating per buyer for this service. Testnet reviews require
                  a seller-issued purchase proof, a buyer wallet signature, and
                  a matching confirmed chain receipt.
                </p>
                {!reviews.length && (
                  <p className="muted">
                    No reviews yet. Paid buyers can rate the service from their
                    result or Purchases.
                  </p>
                )}
                {reviews.map((review, index) => (
                  <article className="buyer-review" key={index}>
                    <div className="service-title">
                      <strong>
                        {"★".repeat(review.score)}
                        {"☆".repeat(5 - review.score)}
                      </strong>
                      <Badge
                        tone={
                          review.verification === "chain_and_seller"
                            ? "success"
                            : "muted"
                        }
                      >
                        {review.verification === "chain_and_seller"
                          ? "Verified testnet purchase"
                          : "Simulation review"}
                      </Badge>
                    </div>
                    <p>{review.comment || "Rating only"}</p>
                    <p className="hint">
                      {review.reviewer} · Version {review.revision.slice(0, 8)}{" "}
                      ·{" "}
                      {new Date(review.updated_at * 1000).toLocaleDateString(
                        "en-US",
                      )}
                    </p>
                  </article>
                ))}
              </section>
            </div>
          </>
        ) : !identity ? (
          <>
            <header className="list-heading">
              <div>
                <h1>Services from the community</h1>
                <p className="muted">
                  Published by sellers running their own Hermes.
                </p>
              </div>
              <a className="button" href="/sell">
                Register a seller
              </a>
            </header>
            <div className="directory-search">
              <label>
                Search services
                <input
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Code review, research, summaries…"
                  maxLength={150}
                />
              </label>
              <label>
                Payment mode
                <select
                  aria-label="Payment mode"
                  value={mode}
                  onChange={(e) => setMode(e.target.value)}
                >
                  <option value="">All modes</option>
                  <option value="testnet">Base Sepolia testnet</option>
                  <option value="simulation">Simulation</option>
                </select>
              </label>
            </div>
            {busy && <p role="status">Loading services…</p>}
            {!busy && !listings.length && (
              <EmptyState title="No matching services">
                <p>
                  Publish a tested service from My services, then list it in
                  this directory.
                </p>
              </EmptyState>
            )}
            <div className="service-list">
              {listings.map((listing) => (
                <article className="service-row" key={listing.id}>
                  <span className="service-glyph" aria-hidden="true">
                    ◇
                  </span>
                  <div className="service-copy">
                    <div className="service-title">
                      <h2>
                        <a href={"/services/" + listing.id}>
                          {listing.service.name}
                        </a>
                      </h2>
                      <Badge
                        tone={
                          listing.state === "online" ? "success" : "warning"
                        }
                      >
                        {listing.state}
                      </Badge>
                    </div>
                    <p>{listing.service.description}</p>
                    <div className="meta-row">
                      <code>{listing.origin}</code>
                      <Badge>
                        {listing.mode === "testnet" ? "Testnet" : "Simulation"}
                      </Badge>
                      <span>
                        {listing.ratings?.count
                          ? `★ ${listing.ratings.average} (${listing.ratings.count})`
                          : "No reviews yet"}
                      </span>
                    </div>
                    {listing.usage && (
                      <p className="hint">
                        Seller reports {listing.usage.paid_requests} paid
                        requests · {money(listing.usage.settled_amount)} settled
                      </p>
                    )}
                  </div>
                  <div className="service-actions">
                    <strong>{money(listing.service.amount)}</strong>
                    <a href={"/services/" + listing.id}>View service</a>
                  </div>
                </article>
              ))}
            </div>
          </>
        ) : (
          <p role="status">Loading service…</p>
        )}
      </div>
    </Workspace>
  );
}
