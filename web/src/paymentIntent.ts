// Persist only signing intent. Payment signatures remain in memory.
export const intentKey = (orderId: string) =>
  "hermes-sell:payment-intent:" + orderId;

export function rememberUnsignedOrder(orderId: string) {
  const key = intentKey(orderId);
  if (localStorage.getItem(key) === null) localStorage.setItem(key, "unsigned");
}

export function mayHaveSignature(orderId: string) {
  // Missing metadata includes orders created before intent tracking existed.
  return localStorage.getItem(intentKey(orderId)) !== "unsigned";
}

export async function withSigningIntent<T>(
  key: string,
  digest: string,
  sign: () => Promise<T>,
  rejected: (error: unknown) => boolean,
  onAttempt: () => void,
) {
  if (!navigator.locks)
    throw new Error(
      "Your browser cannot safely coordinate payment approval across tabs. Update your browser and try again.",
    );
  return navigator.locks.request(key, { ifAvailable: true }, async (lock) => {
    if (!lock) {
      onAttempt();
      throw new Error(
        "This payment is being approved in another tab. Finish there, then refresh the original order.",
      );
    }
    const prior = localStorage.getItem(key);
    if (prior !== null && prior !== "unsigned") {
      onAttempt();
      throw new Error(
        "A signature may already exist. Refresh the original order instead of signing again.",
      );
    }
    localStorage.setItem(key, digest);
    onAttempt();
    try {
      return await sign();
    } catch (error) {
      if (rejected(error)) localStorage.setItem(key, "unsigned");
      throw error;
    }
  });
}
