import type { PaymentRequirements } from "@x402/core/types";

export type UiConfig = {
  role: "storefront" | "studio" | "buyer" | "directory";
  mode: "simulation" | "testnet" | "directory";
  origin?: string;
  seller_origin?: string;
  network?: string;
  asset?: string;
  auth?: "development" | "siwx";
  execution_mode?: "simulation" | "hermes";
  buyer_budget?: string;
  directory_origin?: string;
  buyer_origin?: string;
  owner_origin?: string;
  listing_id?: string;
  purchase_id?: string;
  features?: string[];
};

export type Schema = {
  type: string;
  title?: string;
  description?: string;
  properties?: Record<string, Schema>;
  required?: string[];
  enum?: (string | number | boolean)[];
  minLength?: number;
  maxLength?: number;
  minimum?: number;
  maximum?: number;
  items?: Schema;
};
export type Service = {
  slug: string;
  name: string;
  description: string;
  revision: string;
  input_schema: Schema;
  output_schema: Schema;
  examples: { input: Record<string, unknown>; output: unknown }[];
  amount: string;
  pay_to: string;
  network: string;
  asset: string;
};
export type Order = {
  id: string;
  slug: string;
  revision: string;
  input: Record<string, unknown>;
  amount: string;
  pay_to: string;
  network: string;
  asset: string;
  quote_expires_at: number;
  order_digest: string;
  origin?: string;
  owner?: string;
  mode?: "simulation" | "testnet";
  payment_nonce?: `0x${string}`;
  payment_requirements?: PaymentRequirements;
  execution: string;
  payment: string;
  result?: unknown;
  receipt?: unknown;
  error?: string;
  service_contract: Service;
};
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}
export async function request<T>(
  path: string,
  init: RequestInit = {},
  token?: string,
): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init.headers,
    },
  });
  const body = await response.json().catch(() => null);
  if (!response.ok)
    throw new ApiError(
      response.status,
      body?.error?.code || "request_failed",
      body?.error?.message ||
        (response.status === 422
          ? "Check the input format."
          : `Request failed (${response.status}).`),
    );
  if (body === null)
    throw new ApiError(
      response.status,
      "invalid_response",
      "Cannot read the server response. Refresh the original order.",
    );
  return body as T;
}
export const money = (amount: string) => {
  if (!/^-?[0-9]+$/.test(amount)) return "— USDC";
  const value = BigInt(amount);
  const absolute = value < 0n ? -value : value;
  const whole = new Intl.NumberFormat("en-US").format(absolute / 1_000_000n);
  const fraction = (absolute % 1_000_000n)
    .toString()
    .padStart(6, "0")
    .replace(/0+$/, "")
    .padEnd(2, "0");
  return `${value < 0n ? "-" : ""}${whole}.${fraction} USDC`;
};
export const pretty = (value: unknown) => JSON.stringify(value, null, 2);
