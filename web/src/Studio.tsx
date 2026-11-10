import { useEffect, useRef, useState } from "react";
import {
  ApiError,
  money,
  pretty,
  request,
  type Order,
  type Service,
  type UiConfig,
} from "./api";
import { Badge, Button, EmptyState, Message, Toolbar, Workspace } from "./ui";
import { Analytics } from "./Analytics";
import { SellerSetup } from "./SellerSetup";

type Template = { spec: Service; skill_text: string };
type Report = {
  id: string;
  passed: boolean;
  mode: string;
  cases: { output: unknown; passed: boolean }[];
};
type Tab = "services" | "builder" | "orders" | "analytics" | "setup";
const STATUS: Record<string, string> = {
  created: "Quote",
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

function atomicPrice(text: string) {
  if (!/^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?$/.test(text))
    throw new Error("Enter a USDC amount with up to 6 decimal places.");
  const [whole, fraction = ""] = text.split(".");
  const amount = BigInt(whole) * 1000000n + BigInt(fraction.padEnd(6, "0"));
  if (amount < 1n || amount > 10000000n)
    throw new Error("Enter a price between 0.000001 and 10 USDC.");
  return String(amount);
}

export function Studio({ config }: { config: UiConfig }) {
  const realExecution = config.execution_mode === "hermes";
  const testnet = config.mode === "testnet";
  const [token, setToken] = useState("");
  const [entry, setEntry] = useState("");
  const [tab, setTab] = useState<Tab>("services");
  const [revisions, setRevisions] = useState<
    { public: Service; published: boolean }[]
  >([]);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [orders, setOrders] = useState<Order[]>([]);
  const [publications, setPublications] = useState<
    { revision: string; enabled: boolean; share_usage?: boolean }[]
  >([]);
  const [sharing, setSharing] = useState<Record<string, boolean>>({});
  const [draft, setDraft] = useState<Template>();
  const [templateSlug, setTemplateSlug] = useState("");
  const [schemaText, setSchemaText] = useState("");
  const [price, setPrice] = useState("0.1");
  const [saved, setSaved] = useState<Service>();
  const [report, setReport] = useState<Report>();
  const [reviewed, setReviewed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [pollError, setPollError] = useState("");
  const [bundleLocked, setBundleLocked] = useState(false);
  const [importId, setImportId] = useState("");
  const [importSlug, setImportSlug] = useState("");
  const [importPrice, setImportPrice] = useState("0.1");
  const [importPayee, setImportPayee] = useState("");
  const operation = useRef(false);
  const draftVersion = useRef(0);
  const editorForm = useRef<HTMLFormElement>(null);
  function fail(e: unknown) {
    if (e instanceof ApiError && e.status === 401) setToken("");
    setError(e instanceof Error ? e.message : "Request failed.");
  }
  async function refresh(auth = token) {
    const [r, o, t, listings] = await Promise.all([
      request<{ revisions: typeof revisions }>("/admin/revisions", {}, auth),
      request<{ orders: Order[] }>("/admin/orders", {}, auth),
      request<{ templates: Template[] }>("/admin/templates", {}, auth),
      config.directory_origin
        ? request<{ listings: typeof publications }>(
            "/admin/listings",
            {},
            auth,
          )
        : undefined,
    ]);
    setRevisions(r.revisions);
    setOrders(o.orders);
    setTemplates(t.templates);
    if (listings) setPublications(listings.listings);
  }
  async function act(fn: () => Promise<void>) {
    if (operation.current) return;
    operation.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await fn();
    } catch (e) {
      fail(e);
    } finally {
      operation.current = false;
      setBusy(false);
    }
  }
  useEffect(() => {
    if (!token) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const result = await request<{ orders: Order[] }>(
          "/admin/orders",
          { signal: controller.signal },
          token,
        );
        if (!controller.signal.aborted) {
          setOrders(result.orders);
          setPollError("");
        }
      } catch (e) {
        if (!controller.signal.aborted) {
          if (e instanceof ApiError && e.status === 401) fail(e);
          else
            setPollError(
              "Order updates are delayed. Showing the last received data.",
            );
        }
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, 2500);
    }
    timer = setTimeout(poll, 2500);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [token]);
  function invalidate() {
    draftVersion.current++;
    setSaved(undefined);
    setReport(undefined);
    setReviewed(false);
  }
  function choose(template: Template) {
    if (operation.current) return;
    setDraft(structuredClone(template));
    setBundleLocked(false);
    setTemplateSlug(
      templates.some((t) => t.spec.slug === template.spec.slug)
        ? template.spec.slug
        : "",
    );
    setPrice(String(Number(template.spec.amount) / 1000000));
    setSchemaText(
      pretty({
        input_schema: template.spec.input_schema,
        output_schema: template.spec.output_schema,
        examples: template.spec.examples,
      }),
    );
    invalidate();
    setNotice("");
    setError("");
    setTab("builder");
  }
  function change(next: Template) {
    if (operation.current || bundleLocked) return;
    setDraft(next);
    invalidate();
  }
  async function readRevision(id: string) {
    const result = await request<{
      public: Service;
      bundle: { files: Record<string, string> };
    }>("/admin/revisions/" + id, {}, token);
    setBundleLocked(
      Object.keys(result.bundle.files).some((name) => name !== "SKILL.md"),
    );
    draftVersion.current++;
    setDraft({
      spec: result.public,
      skill_text: result.bundle.files["SKILL.md"],
    });
    setSchemaText(
      pretty({
        input_schema: result.public.input_schema,
        output_schema: result.public.output_schema,
        examples: result.public.examples,
      }),
    );
    setPrice(String(Number(result.public.amount) / 1000000));
    setTemplateSlug("");
    setSaved(result.public);
    setReport(undefined);
    setReviewed(false);
    setTab("builder");
  }
  async function loadRevision(id: string) {
    await act(() => readRevision(id));
  }
  async function save() {
    if (!draft || bundleLocked || !editorForm.current?.reportValidity()) return;
    const version = draftVersion.current;
    await act(async () => {
      const parsed = JSON.parse(schemaText);
      const keys = ["input_schema", "output_schema", "examples"];
      if (
        !parsed ||
        Array.isArray(parsed) ||
        Object.keys(parsed).some((key) => !keys.includes(key)) ||
        keys.some((key) => !(key in parsed))
      )
        throw new Error(
          "The contract must contain only input_schema, output_schema, and examples. Edit the price and payee address in the fields above.",
        );
      const spec = {
        ...draft.spec,
        input_schema: parsed.input_schema,
        output_schema: parsed.output_schema,
        examples: parsed.examples,
        amount: atomicPrice(price),
      };
      const result = await request<Service>(
        "/admin/drafts",
        { method: "POST", body: pretty({ ...draft, spec }) },
        token,
      );
      if (version !== draftVersion.current) return;
      setSaved(result);
      setNotice("Saved the original skill and contract as a new version.");
      await refresh();
    });
  }

  const navigation = (
    <>
      <div className="sidebar-heading">Workspace</div>
      <nav aria-label="Seller navigation">
        {config.features?.includes("seller_setup") && (
          <Button
            variant="ghost"
            className="nav-item"
            aria-current={tab === "setup" ? "page" : undefined}
            disabled={busy || !token}
            onClick={() => setTab("setup")}
          >
            Get started
          </Button>
        )}
        {(
          [
            ["services", "Services", "◇"],
            ["builder", "Create", "＋"],
            ["orders", "Orders", "≡"],
          ] as const
        ).map(([key, label, glyph]) => (
          <Button
            key={key}
            variant="ghost"
            className="nav-item"
            aria-current={tab === key ? "page" : undefined}
            disabled={busy || !token}
            onClick={() => setTab(key)}
          >
            <span className="nav-glyph" aria-hidden="true">
              {glyph}
            </span>
            <span>{label}</span>
            {key === "orders" && token && (
              <span className="nav-count">{orders.length}</span>
            )}
          </Button>
        ))}
        {config.features?.includes("analytics") && (
          <Button
            variant="ghost"
            className="nav-item"
            aria-current={tab === "analytics" ? "page" : undefined}
            disabled={busy || !token}
            onClick={() => setTab("analytics")}
          >
            Analytics
          </Button>
        )}
        {config.directory_origin && (
          <a className="nav-item" href={config.directory_origin}>
            Browse services
          </a>
        )}
      </nav>
      <div className="sidebar-note">
        Save the skill and contract.
        <br />
        Review the result, then publish.
      </div>
    </>
  );
  return (
    <Workspace navigation={navigation}>
      <Toolbar
        title={
          !token
            ? "Seller sign-in"
            : tab === "services"
              ? "Services"
              : tab === "builder"
                ? "Create service"
                : tab === "analytics"
                  ? "Usage and payments"
                  : tab === "setup"
                    ? "Seller registration"
                    : "Orders"
        }
        detail="Seller only"
      >
        {token && (
          <Button
            variant="primary"
            disabled={busy || !templates.length}
            onClick={() => choose(templates[0])}
          >
            New service
          </Button>
        )}
      </Toolbar>
      {!token ? (
        <div className="login">
          <span className="eyebrow">Seller only</span>
          <h1>Connect workspace</h1>
          <p className="muted">Create, test, and publish your services.</p>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void act(async () => {
                await refresh(entry);
                setToken(entry);
              });
            }}
          >
            <div className="field">
              <label htmlFor="owner-token">Admin token</label>
              <input
                required
                id="owner-token"
                type="password"
                value={entry}
                onChange={(e) => setEntry(e.target.value)}
                autoComplete="off"
              />
            </div>
            <p className="hint">
              {testnet ? (
                "Use the owner token configured for this seller."
              ) : (
                <>
                  Local preview token: <code>local-preview-only</code>
                </>
              )}
            </p>
            <Button type="submit" variant="primary" busy={busy}>
              Open workspace
            </Button>
          </form>
          {error && <Message error>{error}</Message>}
        </div>
      ) : (
        <>
          {error && (
            <div className="message-inset">
              <Message error>{error}</Message>
            </div>
          )}
          {notice && (
            <div className="message-inset">
              <Message>{notice}</Message>
            </div>
          )}
          {realExecution && (tab === "services" || tab === "builder") && (
            <details className="draft-import message-inset">
              <summary>Import Hermes draft</summary>
              <p className="hint">
                Use the draft ID returned by the Hermes service_draft tool. The
                original skill and saved references stay together.
              </p>
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void act(async () => {
                    const imported = await request<Service>(
                      "/admin/drafts/import",
                      {
                        method: "POST",
                        body: pretty({
                          draft_id: importId,
                          slug: importSlug,
                          amount: atomicPrice(importPrice),
                          pay_to: importPayee,
                        }),
                      },
                      token,
                    );
                    await refresh();
                    await readRevision(imported.revision);
                    setNotice(
                      "Imported the Hermes draft and its original execution bundle. Run a test and review the result before publishing.",
                    );
                  });
                }}
              >
                <fieldset className="fields" disabled={busy}>
                  <div className="field">
                    <label htmlFor="import-id">Hermes draft ID</label>
                    <input
                      id="import-id"
                      required
                      value={importId}
                      onChange={(e) => setImportId(e.target.value)}
                    />
                  </div>
                  <div className="field">
                    <label htmlFor="import-slug">
                      Imported service URL slug
                    </label>
                    <input
                      id="import-slug"
                      required
                      pattern="[a-z0-9][a-z0-9-]{2,63}"
                      value={importSlug}
                      onChange={(e) => setImportSlug(e.target.value)}
                    />
                  </div>
                  <div className="form-columns">
                    <div className="field">
                      <label htmlFor="import-price">
                        Imported price · USDC
                      </label>
                      <input
                        id="import-price"
                        required
                        inputMode="decimal"
                        value={importPrice}
                        onChange={(e) => setImportPrice(e.target.value)}
                      />
                    </div>
                    <div className="field">
                      <label htmlFor="import-payee">
                        Imported payee address
                      </label>
                      <input
                        id="import-payee"
                        required
                        pattern="0x[0-9a-fA-F]{40}"
                        value={importPayee}
                        onChange={(e) => setImportPayee(e.target.value)}
                      />
                    </div>
                  </div>
                  <div>
                    <Button type="submit" variant="primary" busy={busy}>
                      Import saved draft
                    </Button>
                  </div>
                </fieldset>
              </form>
            </details>
          )}
          {tab === "services" && (
            <div className="list-pane">
              <header className="list-heading">
                <div>
                  <h1>Services</h1>
                  <p className="muted">
                    Manage published services and saved versions.
                  </p>
                </div>
                <span className="hint">{revisions.length} saved</span>
              </header>
              <div className="service-list">
                {revisions.map((item) => (
                  <article className="service-row" key={item.public.revision}>
                    <span className="service-glyph" aria-hidden="true">
                      ◇
                    </span>
                    <div className="service-copy">
                      <div className="service-title">
                        <h2>{item.public.name}</h2>
                        <Badge tone={item.published ? "accent" : "muted"}>
                          {item.published
                            ? testnet
                              ? "Published"
                              : "Published locally"
                            : "Saved version"}
                        </Badge>
                      </div>
                      <p>{item.public.description}</p>
                      <div className="meta-row">
                        <code>/s/{item.public.slug}</code>
                        <code>{item.public.revision.slice(0, 8)}</code>
                      </div>
                    </div>
                    <div className="service-actions">
                      <strong>{money(item.public.amount)}</strong>
                      <Button
                        variant="text"
                        disabled={busy}
                        onClick={() => void loadRevision(item.public.revision)}
                        aria-label={"Load " + item.public.name + " version"}
                      >
                        Load
                      </Button>
                      {item.published && config.directory_origin && (
                        <>
                          <label className="sharing-control">
                            <input
                              type="checkbox"
                              checked={
                                sharing[item.public.revision] ??
                                publications.find(
                                  (p) => p.revision === item.public.revision,
                                )?.share_usage ??
                                false
                              }
                              disabled={busy}
                              onChange={(e) =>
                                setSharing((value) => ({
                                  ...value,
                                  [item.public.revision]: e.target.checked,
                                }))
                              }
                            />
                            Share usage and settled volume publicly
                          </label>
                          {publications.some(
                            (p) =>
                              p.revision === item.public.revision && p.enabled,
                          ) && <Badge>Directory publication enabled</Badge>}
                          <Button
                            disabled={busy}
                            onClick={() =>
                              void act(async () => {
                                const result = await request<{
                                  listed: boolean;
                                  message?: string;
                                }>(
                                  "/admin/listings/" + item.public.revision,
                                  {
                                    method: "POST",
                                    body: pretty({
                                      share_usage:
                                        sharing[item.public.revision] ??
                                        publications.find(
                                          (p) =>
                                            p.revision === item.public.revision,
                                        )?.share_usage ??
                                        false,
                                    }),
                                  },
                                  token,
                                );
                                await refresh();
                                setNotice(
                                  result.listed
                                    ? "Listed in the service directory. Buyers can discover this version."
                                    : result.message ||
                                        "Publication saved. Retry listing when the directory is available.",
                                );
                              })
                            }
                          >
                            List in directory
                          </Button>
                          {publications.some(
                            (p) =>
                              p.revision === item.public.revision && p.enabled,
                          ) && (
                            <Button
                              disabled={busy}
                              onClick={() =>
                                void act(async () => {
                                  await request(
                                    "/admin/listings/" +
                                      item.public.revision +
                                      "/withdraw",
                                    { method: "POST" },
                                    token,
                                  );
                                  await refresh();
                                  setNotice(
                                    "Directory publication withdrawn. The service remains available through its existing URL.",
                                  );
                                })
                              }
                            >
                              Unlist
                            </Button>
                          )}
                        </>
                      )}
                    </div>
                  </article>
                ))}
              </div>
            </div>
          )}
          {tab === "builder" &&
            (!draft ? (
              <EmptyState title="Create your first service">
                <p>
                  Start with an example skill, or edit the original skill and
                  contract.
                </p>
                <Button
                  variant="primary"
                  disabled={busy || !templates.length}
                  onClick={() => choose(templates[0])}
                >
                  Start with an example skill
                </Button>
              </EmptyState>
            ) : (
              <div className="split-view">
                <div className="document-pane">
                  <header className="document-heading">
                    <span className="eyebrow">Contract editor</span>
                    <h1>Service definition</h1>
                    <p>
                      Review the original skill and its public input and output.
                    </p>
                  </header>
                  <div className="template-row">
                    <label htmlFor="template">Example skill</label>
                    <select
                      id="template"
                      aria-label="Select example skill"
                      value={templateSlug}
                      disabled={busy}
                      onChange={(e) => {
                        const item = templates.find(
                          (t) => t.spec.slug === e.target.value,
                        );
                        if (item) choose(item);
                      }}
                    >
                      <option value="" disabled>
                        Load example skill
                      </option>
                      {templates.map((item) => (
                        <option key={item.spec.slug} value={item.spec.slug}>
                          {item.spec.name}
                        </option>
                      ))}
                    </select>
                  </div>
                  <p className="hint">
                    {bundleLocked
                      ? "This saved bundle contains reference files. Import a new Hermes draft to change it. Testing and publishing preserve the original bundle."
                      : realExecution
                        ? "Edit the service manually, or import a saved draft from the Hermes service_draft tool."
                        : "Edit the service manually. Hermes AI drafting requires a connected host."}
                  </p>
                  <form
                    ref={editorForm}
                    onSubmit={(e) => {
                      e.preventDefault();
                      void save();
                    }}
                  >
                    <fieldset
                      className="fields"
                      disabled={busy || bundleLocked}
                    >
                      <div className="field">
                        <label htmlFor="service-name">Service name</label>
                        <input
                          required
                          maxLength={100}
                          id="service-name"
                          value={draft.spec.name}
                          onChange={(e) =>
                            change({
                              ...draft,
                              spec: { ...draft.spec, name: e.target.value },
                            })
                          }
                        />
                      </div>
                      <div className="field">
                        <label htmlFor="service-slug">URL slug</label>
                        <input
                          required
                          pattern="[a-z0-9][a-z0-9-]{2,63}"
                          id="service-slug"
                          value={draft.spec.slug}
                          onChange={(e) =>
                            change({
                              ...draft,
                              spec: { ...draft.spec, slug: e.target.value },
                            })
                          }
                        />
                      </div>
                      <div className="field">
                        <label htmlFor="service-description">Description</label>
                        <textarea
                          required
                          maxLength={1500}
                          id="service-description"
                          value={draft.spec.description}
                          onChange={(e) =>
                            change({
                              ...draft,
                              spec: {
                                ...draft.spec,
                                description: e.target.value,
                              },
                            })
                          }
                        />
                      </div>
                      <div className="form-columns">
                        <div className="field">
                          <label htmlFor="service-price">Price · USDC</label>
                          <input
                            required
                            id="service-price"
                            inputMode="decimal"
                            value={price}
                            onChange={(e) => {
                              setPrice(e.target.value);
                              invalidate();
                            }}
                          />
                        </div>
                        <div className="field">
                          <label htmlFor="service-payee">Payee address</label>
                          <input
                            required
                            className="code"
                            id="service-payee"
                            pattern="0x[0-9a-fA-F]{40}"
                            value={draft.spec.pay_to}
                            onChange={(e) =>
                              change({
                                ...draft,
                                spec: { ...draft.spec, pay_to: e.target.value },
                              })
                            }
                          />
                        </div>
                      </div>
                      <div className="field">
                        <label htmlFor="skill-text">Original SKILL.md</label>
                        <textarea
                          required
                          className="code"
                          id="skill-text"
                          rows={8}
                          value={draft.skill_text}
                          onChange={(e) =>
                            change({ ...draft, skill_text: e.target.value })
                          }
                        />
                      </div>
                      <details>
                        <summary>
                          Edit input/output schemas and examples
                        </summary>
                        <textarea
                          aria-label="Input/output contract JSON"
                          className="code"
                          rows={18}
                          value={schemaText}
                          onChange={(e) => {
                            setSchemaText(e.target.value);
                            invalidate();
                          }}
                        />
                      </details>
                      <div>
                        <Button type="submit" variant="primary" busy={busy}>
                          Save version
                        </Button>
                      </div>
                    </fieldset>
                  </form>
                </div>
                <aside className="inspector">
                  <div className="inspector-heading">Test and publish</div>
                  <div className="inspector-body">
                    <h2>Before publishing</h2>
                    <p className="hint">
                      Test this version and review the result.
                    </p>
                    <ol className="steps">
                      <li className={saved ? "done" : "active"}>
                        Save skill and contract
                      </li>
                      <li
                        className={
                          report?.passed ? "done" : saved ? "active" : ""
                        }
                      >
                        Test examples
                      </li>
                      <li className={reviewed ? "done" : ""}>
                        Review and publish
                      </li>
                    </ol>
                    <Button
                      className="full"
                      disabled={!saved || busy}
                      onClick={() =>
                        void act(async () => {
                          const version = draftVersion.current;
                          const result = await request<Report>(
                            "/admin/revisions/" + saved!.revision + "/test",
                            { method: "POST" },
                            token,
                          );
                          if (version === draftVersion.current) {
                            setReport(result);
                            setReviewed(false);
                          }
                        })
                      }
                    >
                      {realExecution
                        ? "Run execution test"
                        : "Run simulation test"}
                    </Button>
                    {report && (
                      <>
                        <Message>
                          {report.passed
                            ? "Schema validation passed"
                            : "Test failed"}{" "}
                          ·{" "}
                          {report.mode === "simulation"
                            ? "Fixed example output"
                            : "Hermes execution output"}
                        </Message>
                        <pre>{pretty(report.cases)}</pre>
                        <label className="checkbox">
                          <input
                            type="checkbox"
                            checked={reviewed}
                            disabled={busy}
                            onChange={(e) => setReviewed(e.target.checked)}
                          />
                          {realExecution
                            ? "I reviewed the execution result and approve publishing this version."
                            : "I reviewed the example result and understand this is a simulation."}
                        </label>
                      </>
                    )}
                    <Button
                      variant="primary"
                      className="full"
                      disabled={!report?.passed || !reviewed || busy}
                      onClick={() =>
                        void act(async () => {
                          await request(
                            "/admin/revisions/" + saved!.revision + "/publish",
                            {
                              method: "POST",
                              body: pretty({
                                report_id: report!.id,
                                owner_reviewed: reviewed,
                              }),
                            },
                            token,
                          );
                          await refresh();
                          setNotice(
                            testnet
                              ? "Published to the testnet storefront."
                              : "Published to the local storefront. Live selling is still disabled.",
                          );
                          setTab("services");
                        })
                      }
                    >
                      {testnet ? "Publish service" : "Publish locally"}
                    </Button>
                    <p className="hint inspector-note">
                      {realExecution
                        ? "Tests run the original skill through the isolated Hermes adapter. Review output quality before publishing."
                        : "Tests use fixed examples. They do not verify real skill execution or content accuracy."}
                    </p>
                  </div>
                </aside>
              </div>
            ))}
          {tab === "orders" && (
            <div className="list-pane">
              <header className="list-heading">
                <div>
                  <h1>Orders</h1>
                  <p className="muted">
                    Track execution and payment status separately.
                  </p>
                </div>
                <Button
                  variant="text"
                  busy={busy}
                  onClick={() =>
                    void act(async () => {
                      await refresh();
                      setPollError("");
                    })
                  }
                >
                  Refresh
                </Button>
              </header>
              {pollError && <Message error>{pollError}</Message>}
              {orders.length ? (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Order</th>
                        <th>Service</th>
                        <th>Amount</th>
                        <th>Execution</th>
                        <th>Payment</th>
                      </tr>
                    </thead>
                    <tbody>
                      {orders.map((order) => (
                        <tr key={order.id}>
                          <td className="mono">{order.id.slice(0, 8)}</td>
                          <td>{order.service_contract?.name || order.slug}</td>
                          <td>{money(order.amount)}</td>
                          <td>{STATUS[order.execution] || order.execution}</td>
                          <td>
                            <Badge
                              tone={
                                order.payment === "settled"
                                  ? "success"
                                  : order.payment === "unknown"
                                    ? "warning"
                                    : "muted"
                              }
                            >
                              {STATUS[order.payment] || order.payment}
                            </Badge>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <EmptyState title="No orders yet">
                  <p>
                    {testnet
                      ? "Create your first testnet order from the storefront."
                      : "Create your first simulated order from the storefront."}
                  </p>
                </EmptyState>
              )}
            </div>
          )}
          {tab === "analytics" && <Analytics token={token} />}
          {tab === "setup" && (
            <SellerSetup
              config={config}
              token={token}
              create={() => {
                if (templates[0]) choose(templates[0]);
                else setTab("builder");
              }}
              services={() => setTab("services")}
            />
          )}
        </>
      )}
    </Workspace>
  );
}
