"""Canonical x402 EIP-3009 payloads with a nonce fixed by the approved order."""

import time

from eth_account.messages import encode_typed_data
from x402.mechanisms.evm.eip712 import build_typed_data_for_signing
from x402.mechanisms.evm.types import ExactEIP3009Authorization, ExactEIP3009Payload
from x402.schemas import PaymentPayload, ResourceInfo

from .contracts import Problem
from .x402_adapter import X402Payments


def payment_message(order, address, *, valid_before=None):
    if address.lower() != order["owner"].lower():
        raise Problem("wallet_mismatch", "Use the wallet that owns this order.")
    if order.get("payment_nonce") != "0x" + order["order_digest"]:
        raise Problem("invalid_quote", "The quote is missing its order-bound payment nonce.")
    requirement = X402Payments.requirements(order)
    authorization = ExactEIP3009Authorization(from_address=address, to=requirement.pay_to,
        value=requirement.amount, valid_after="0", valid_before=str(valid_before or int(time.time()) + 180),
        nonce=order["payment_nonce"])
    domain, types, primary_type, message = build_typed_data_for_signing(
        authorization, 84532, requirement.asset, "USDC", "2")
    signable = encode_typed_data(domain_data={"name": domain.name, "version": domain.version,
        "chainId": domain.chain_id, "verifyingContract": domain.verifying_contract},
        message_types=types, message_data=message)
    return authorization, requirement, signable


def sign_order(account, order):
    """Call only after recording explicit user approval of this exact quote."""
    authorization, requirement, signable = payment_message(order, account.address)
    signature = "0x" + account.sign_message(signable).signature.hex()
    inner = ExactEIP3009Payload(authorization=authorization, signature=signature).to_dict()
    payload = PaymentPayload(payload=inner, accepted=requirement,
        resource=ResourceInfo(url=f"{order['origin']}/v1/orders/{order['id']}"))
    return payload.model_dump(by_alias=True, exclude_none=True)
