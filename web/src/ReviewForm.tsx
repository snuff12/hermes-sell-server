import { useId, useRef, useState } from "react";
import { pretty, request, type Order, type UiConfig } from "./api";
import type { ConnectedWallet, ReviewChallenge } from "./wallet";
import { Button, Message } from "./ui";

export function ReviewForm({
  config,
  order,
  endpoint,
  token,
  wallet,
  reconnect,
}: {
  config: UiConfig;
  order: Order;
  endpoint: string;
  token?: string;
  wallet?: ConnectedWallet;
  reconnect?: string;
}) {
  const formId = useId();
  const [open, setOpen] = useState(false);
  const [score, setScore] = useState(5);
  const [comment, setComment] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [hasPending, setHasPending] = useState(false);
  const pending = useRef<
    { challenge: ReviewChallenge; signature: string | null } | undefined
  >(undefined);
  const active = useRef(false);
  async function publish() {
    if (active.current) return;
    active.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      if (!pending.current) {
        const challenge = await request<ReviewChallenge>(
          endpoint + "/review-challenge",
          { method: "POST", body: pretty({ score, comment }) },
          token,
        );
        let signature: string | null = null;
        if (config.mode === "testnet") {
          const signing = await import("./wallet");
          const connected = wallet || (await signing.connectWallet(config));
          signature = await signing.signReview(
            connected,
            challenge,
            config,
            order,
            score,
            comment,
          );
        } else if (
          challenge.mode !== "simulation" ||
          challenge.payload.order_id !== order.id ||
          challenge.payload.score !== score ||
          challenge.payload.comment !== comment
        ) {
          throw new Error("This review does not match the simulated purchase.");
        }
        pending.current = { challenge, signature };
        setHasPending(true);
      }
      await request(
        endpoint + "/review",
        {
          method: "POST",
          body: pretty({
            challenge_id: pending.current.challenge.challenge_id,
            signature: pending.current.signature,
          }),
        },
        token,
      );
      pending.current = undefined;
      setHasPending(false);
      setNotice(
        "Your review is published. A later review from the same buyer updates the existing rating.",
      );
    } catch (e) {
      setError(
        e instanceof Error
          ? e.message
          : "The review outcome is unknown. Retry the same review.",
      );
    } finally {
      active.current = false;
      setBusy(false);
    }
  }
  if (!config.features?.includes("reviews")) return null;
  return (
    <section className="review-form">
      <Button onClick={() => setOpen((value) => !value)}>
        {open ? "Close review form" : "Rate this service"}
      </Button>
      {open && (
        <div>
          <h3>Review your purchase</h3>
          <p className="hint">
            Your rating and comment will be public. Testnet reviews show a
            shortened wallet address and require a separate review signature,
            with no payment authorization.
          </p>
          {config.mode === "simulation" && (
            <Message>
              Simulation review · No real purchase or wallet verification.
            </Message>
          )}
          <label>
            Rating
            <select
              aria-label="Rating"
              value={score}
              disabled={busy || hasPending}
              onChange={(e) => setScore(Number(e.target.value))}
            >
              {[5, 4, 3, 2, 1].map((value) => (
                <option key={value} value={value}>
                  {value} {value === 1 ? "star" : "stars"}
                </option>
              ))}
            </select>
          </label>
          <label htmlFor={formId + "-comment"}>Review</label>
          <textarea
            id={formId + "-comment"}
            value={comment}
            disabled={busy || hasPending}
            onChange={(e) => setComment(e.target.value)}
            maxLength={1000}
            placeholder="What worked well? What could improve?"
          />
          {error && <Message error>{error}</Message>}
          {notice && <Message>{notice}</Message>}
          {reconnect && (
            <p>
              <a href={reconnect}>Reconnect the original wallet</a> if your
              seller session expired.
            </p>
          )}
          <Button variant="primary" busy={busy} onClick={() => void publish()}>
            {hasPending
              ? "Retry same review"
              : config.mode === "testnet"
                ? "Sign and publish review"
                : "Publish simulation review"}
          </Button>
          {hasPending && (
            <Button
              disabled={busy}
              onClick={() => {
                pending.current = undefined;
                setHasPending(false);
                setError("");
              }}
            >
              Edit review
            </Button>
          )}
        </div>
      )}
    </section>
  );
}
