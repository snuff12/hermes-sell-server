"""Official x402 v2 boundary with an order-bound EIP-3009 nonce."""

import asyncio
import re
import time

from .contracts import ASSET, NETWORK, Problem, digest


class X402Payments:
    mode = "testnet"

    def __init__(self, facilitator, *, chain=None):
        self.facilitator = facilitator
        self.chain = chain

    async def preflight(self):
        # The Python SDK exposes discovery synchronously even on its async client.
        supported = await asyncio.to_thread(self.facilitator.get_supported)
        if not any(kind.x402_version == 2 and kind.scheme == "exact" and kind.network == NETWORK
                   for kind in supported.kinds):
            raise Problem("unsupported_facilitator", "The facilitator does not support exact payments on Base Sepolia.", 503)
        if self.chain is None:
            raise Problem("chain_required", "Testnet hosting requires chain receipt verification.", 503)
        await self.chain.check_network()
        return {"network": NETWORK, "scheme": "exact", "chain_verified": True}

    @staticmethod
    def requirements(order):
        from x402.schemas import PaymentRequirements
        return PaymentRequirements(scheme="exact", network=NETWORK, asset=ASSET,
            amount=order["amount"], pay_to=order["pay_to"], max_timeout_seconds=180,
            extra={"name": "USDC", "version": "2", "assetTransferMethod": "eip3009"})

    @classmethod
    def bound_payload(cls, raw, order):
        from x402.schemas import PaymentPayload
        try:
            payload = PaymentPayload.model_validate(raw)
            requirement = cls.requirements(order)
            if payload.x402_version != 2 or payload.accepted != requirement:
                raise ValueError("payment requirements mismatch")
            auth = payload.payload["authorization"]
            if auth["from"].lower() != order["owner"].lower() or auth["to"].lower() != order["pay_to"].lower() or auth["value"] != order["amount"]:
                raise ValueError("authorization mismatch")
            if not re.fullmatch(r"0x[0-9a-fA-F]{64}", auth["nonce"]):
                raise ValueError("invalid nonce")
            if auth["nonce"].lower() != ("0x" + order["order_digest"]).lower():
                raise ValueError("nonce is not bound to this order")
            if payload.resource is None or payload.resource.url != f"{order['origin']}/v1/orders/{order['id']}":
                raise ValueError("resource mismatch")
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise Problem("invalid_authorization", "The payment authorization does not match the order.") from exc
        return payload, requirement, auth

    async def verify(self, raw, order):
        payload, requirement, auth = self.bound_payload(raw, order)
        try:
            valid_before, valid_after = int(auth["validBefore"]), int(auth["validAfter"])
        except (ValueError, TypeError, KeyError) as exc:
            raise Problem("invalid_authorization", "Check the authorization timestamps.") from exc
        now = int(time.time())
        if valid_after != 0 or not 120 <= valid_before - now <= requirement.max_timeout_seconds:
            raise Problem("authorization_expiry", "The authorization validity period is too short or too long.")
        verified = await self.facilitator.verify(payload, requirement)
        if not verified.is_valid or not verified.payer or verified.payer.lower() != order["owner"].lower():
            raise Problem("payment_invalid", "The facilitator could not verify the payment authorization.")
        result = {"identity": digest([NETWORK, ASSET.lower(), order["owner"].lower(), auth["nonce"].lower()]), "valid_before": valid_before}
        if self.chain is not None:
            result["chain_start_block"] = await self.chain.block_number()
        return result

    async def settle(self, raw, order):
        payload, requirement, auth = self.bound_payload(raw, order)
        if int(auth["validBefore"]) <= int(time.time()):
            raise Problem("authorization_expiry", "Settlement cannot start with an expired authorization.")
        result = await self.facilitator.settle(payload, requirement)
        if result.success and (result.network != NETWORK
                or not re.fullmatch(r"0x[0-9a-fA-F]{64}", result.transaction)
                or not result.payer or result.payer.lower() != order["owner"].lower()
                or (result.amount is not None and result.amount != order["amount"])):
            # Ambiguous success must remain unknown in the order engine, not become failed.
            raise RuntimeError("Settlement response does not identify the expected transaction")
        return result.model_dump(by_alias=True, exclude_none=True)
