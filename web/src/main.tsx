import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { request, type UiConfig } from "./api";
import { Storefront } from "./Storefront";
import { Studio } from "./Studio";
import { BuyerApproval } from "./BuyerApproval";
import { Directory } from "./Directory";
import { Purchases } from "./Purchases";
import { SellerRegistration } from "./SellerRegistration";
import { Badge, Button, Message } from "./ui";
import "./style.css";

function preferredTheme() {
  const saved = localStorage.getItem("hermes-sell:theme");
  return saved === "dark" || saved === "light"
    ? saved
    : matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
}
document.documentElement.dataset.theme = preferredTheme();
function App() {
  const [config, setConfig] = useState<UiConfig>();
  const [error, setError] = useState("");
  const [theme, setTheme] = useState(preferredTheme);
  useEffect(() => {
    const controller = new AbortController();
    const purchase = location.pathname.startsWith("/approve/")
      ? location.pathname.split("/")[2]
      : undefined;
    const listing = new URLSearchParams(location.search).get("listing_id");
    const headers: Record<string, string> = purchase
      ? { "X-Purchase-ID": purchase }
      : listing
        ? { "X-Listing-ID": listing }
        : {};
    request<UiConfig>("/ui-config", { signal: controller.signal, headers })
      .then((data) => {
        if (!["simulation", "testnet", "directory"].includes(data.mode))
          throw new Error("This server uses an unsupported payment mode.");
        if (!["storefront", "studio", "buyer", "directory"].includes(data.role))
          throw new Error("This server uses an unsupported workspace role.");
        setConfig(data);
      })
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      });
    return () => controller.abort();
  }, []);
  function toggleTheme() {
    const next = theme === "light" ? "dark" : "light";
    setTheme(next);
    localStorage.setItem("hermes-sell:theme", next);
    document.documentElement.dataset.theme = next;
  }
  return (
    <div className="app-frame">
      <a className="skip-link" href="#main-content">
        Skip to content
      </a>
      <header className="titlebar">
        <a className="brand" href="/">
          hermes<span className="brand-separator">/</span>
          <span className="brand-product">Sell</span>
        </a>
        <span className="window-title">
          {config?.role === "studio"
            ? "Seller workspace"
            : config?.role === "buyer"
              ? "Buyer approval"
              : "Services"}
        </span>
        <div className="titlebar-actions">
          <Badge tone="warning">
            {config?.mode === "directory"
              ? "Service directory"
              : config?.mode === "testnet"
                ? "Base Sepolia testnet"
                : "Simulation"}
          </Badge>
          <Button
            variant="ghost"
            size="sm"
            onClick={toggleTheme}
            aria-label={
              theme === "light"
                ? "Switch to dark theme"
                : "Switch to light theme"
            }
          >
            {theme === "light" ? "◐" : "◑"}
          </Button>
        </div>
      </header>
      {error ? (
        <main id="main-content" className="startup">
          <Message error>{error}</Message>
        </main>
      ) : config?.role === "studio" ? (
        <Studio config={config} />
      ) : config?.role === "storefront" ? (
        <Storefront config={config} />
      ) : config?.role === "directory" ? (
        location.pathname === "/sell" ? (
          <SellerRegistration config={config} />
        ) : (
          <Directory config={config} />
        )
      ) : config?.role === "buyer" ? (
        location.pathname === "/purchases" ? (
          <Purchases config={config} />
        ) : (
          <BuyerApproval config={config} />
        )
      ) : (
        <main id="main-content" className="startup" role="status">
          Loading workspace…
        </main>
      )}
      <footer className="statusbar">
        <span>
          <span className="status-dot" />
          Hermes Sell <span className="muted">0.1</span>
        </span>
        <span>
          {config?.role === "directory"
            ? "Discover services · Execution and settlement stay with the seller"
            : config?.role === "buyer"
              ? "Local buyer approval · Base Sepolia testnet"
              : config?.mode === "testnet"
                ? "Base Sepolia testnet · " +
                  (config.execution_mode === "hermes"
                    ? "Hermes execution"
                    : "Simulated execution")
                : "Local simulation · No real payments or model execution"}
        </span>
      </footer>
    </div>
  );
}
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
