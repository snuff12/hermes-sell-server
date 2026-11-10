import { useEffect, useState } from "react";
import { request, type UiConfig } from "./api";
import { Badge, Button, Message } from "./ui";

export function SellerSetup({
  config,
  token,
  create,
  services,
}: {
  config: UiConfig;
  token: string;
  create: () => void;
  services: () => void;
}) {
  const [setup, setSetup] = useState<{
    seller_origin: string;
    ready: boolean;
    published_services: number;
    directory_publications: number;
  }>();
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    void request<typeof setup>(
      "/admin/seller",
      { signal: controller.signal },
      token,
    )
      .then(setSetup)
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      });
    return () => controller.abort();
  }, [token]);
  return (
    <div className="list-pane">
      <header className="list-heading">
        <div>
          <h1>Register as a seller</h1>
          <p className="muted">
            Create a tested API and list it for other Hermes users.
          </p>
        </div>
        <Badge>
          {config.mode === "simulation" ? "Simulation" : "Base Sepolia testnet"}
        </Badge>
      </header>
      {error && <Message error>{error}</Message>}
      <section className="section-block">
        <h2>Your seller connection</h2>
        <code>{setup?.seller_origin || config.seller_origin}</code>
        <p>
          {setup?.ready
            ? "Ready to accept work"
            : "Check your runtime and payment connections before accepting work."}
        </p>
        <p>
          {setup?.published_services ?? 0} published services ·{" "}
          {setup?.directory_publications ?? 0} directory publications enabled
        </p>
      </section>
      <ol className="setup-steps">
        <li>
          <h2>Select your skill</h2>
          <p>
            Create a service from an original SKILL.md or import a draft from
            your owner Hermes.
          </p>
          <Button onClick={create}>Create a service</Button>
        </li>
        <li>
          <h2>Set the price and payee</h2>
          <p>
            Choose the USDC price per request and the wallet that receives
            payment.
          </p>
        </li>
        <li>
          <h2>Test and review</h2>
          <p>
            Run the execution test, read its result, then explicitly publish
            this version.
          </p>
        </li>
        <li>
          <h2>List in the directory</h2>
          <p>
            Open Services and choose List in directory. Choose whether to share
            usage and settled volume publicly.
          </p>
          <Button onClick={services}>Open my services</Button>
        </li>
        <li>
          <h2>Track your business</h2>
          <p>
            Use Analytics for executions, confirmed payments, pending amounts
            and per-service totals. Paid buyers can publish a rating and review.
          </p>
        </li>
      </ol>
      <section className="section-block">
        <a href={config.directory_origin}>Browse the service directory</a>
        <p className="hint">
          This release registers configured local sellers. Public addresses and
          open Internet enrollment remain deferred.
        </p>
      </section>
    </div>
  );
}
