"""Read-only Base Sepolia receipt and authorization reconciliation."""

import re
from urllib.parse import urlparse

from eth_utils import keccak
import httpx

from .contracts import ASSET, NETWORK, Problem


def topic(signature):
    return "0x" + keccak(text=signature).hex()


TRANSFER = topic("Transfer(address,address,uint256)")
AUTHORIZATION_USED = topic("AuthorizationUsed(address,bytes32)")
AUTHORIZATION_CANCELED = topic("AuthorizationCanceled(address,bytes32)")


def address_topic(address):
    return "0x" + address.lower().removeprefix("0x").zfill(64)


class ChainReader:
    def __init__(self, url, *, transport=None, confirmations=2):
        parsed = urlparse(url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "testserver"}):
            raise ValueError("Use an HTTPS RPC endpoint or a loopback development endpoint.")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("RPC URLs cannot contain credentials or fragments.")
        self.client = httpx.AsyncClient(base_url=url, timeout=10, follow_redirects=False, transport=transport)
        self.confirmations = max(2, confirmations)

    async def rpc(self, method, params):
        response = await self.client.post("", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        response.raise_for_status()
        body = response.json()
        if body.get("id") != 1 or body.get("jsonrpc") != "2.0" or "error" in body or "result" not in body:
            raise Problem("rpc_unavailable", "The chain response could not be verified.", 503)
        return body["result"]

    async def check_network(self):
        if int(await self.rpc("eth_chainId", []), 16) != 84532:
            raise Problem("wrong_chain", "Only Base Sepolia is supported.", 503)

    async def block_number(self):
        await self.check_network()
        return int(await self.rpc("eth_blockNumber", []), 16)

    async def _confirmed_receipt(self, order, transaction):
        if order.get("network") != NETWORK:
            raise Problem("wrong_chain", "Only Base Sepolia orders are supported.", 503)
        if not isinstance(transaction, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", transaction):
            return None
        head = await self.block_number()
        receipt = await self.rpc("eth_getTransactionReceipt", [transaction])
        if not receipt or receipt.get("transactionHash", "").lower() != transaction.lower() or receipt.get("status") != "0x1":
            return None
        block = int(receipt["blockNumber"], 16)
        if head - block + 1 < self.confirmations:
            return None
        canonical = await self.rpc("eth_getBlockByNumber", [hex(block), False])
        if not canonical or canonical.get("hash") != receipt.get("blockHash"):
            return None
        return receipt, block, head

    async def confirm(self, order, transaction):
        evidence = await self._confirmed_receipt(order, transaction)
        if evidence is None:
            return None
        receipt, block, head = evidence
        logs = receipt.get("logs", [])
        payer, recipient = address_topic(order["owner"]), address_topic(order["pay_to"])
        nonce = order["payment_nonce"].lower()
        # Circle USDC emits AuthorizationUsed immediately before its Transfer.
        # Independent matches in a batched transaction could credit two nonces
        # for one transfer; require the exact adjacent pair in the full receipt.
        paired = any(
            used.get("address", "").lower() == ASSET.lower()
            and transfer.get("address", "").lower() == ASSET.lower()
            and not used.get("removed", False) and not transfer.get("removed", False)
            and [t.lower() for t in used.get("topics", [])] == [AUTHORIZATION_USED, payer, nonce]
            and [t.lower() for t in transfer.get("topics", [])] == [TRANSFER, payer, recipient]
            and int(transfer.get("data", "0x0"), 16) == int(order["amount"])
            for used, transfer in zip(logs, logs[1:]))
        if not paired:
            return None
        return {"success": True, "transaction": transaction, "network": order["network"],
                "payer": order["owner"], "amount": order["amount"], "block_number": block,
                "block_hash": receipt["blockHash"], "confirmations": head - block + 1,
                "confirmation_policy": f"testnet-l2-depth-{self.confirmations}",
                "verified_by": "receipt-transfer-authorization"}

    async def confirm_cancellation(self, order, transaction):
        evidence = await self._confirmed_receipt(order, transaction)
        if evidence is None:
            return None
        receipt, block, head = evidence
        payer, nonce = address_topic(order["owner"]), order["payment_nonce"].lower()
        logs = [entry for entry in receipt.get("logs", [])
                if entry.get("address", "").lower() == ASSET.lower() and not entry.get("removed", False)]
        topics = [[value.lower() for value in entry.get("topics", [])] for entry in logs]
        if [AUTHORIZATION_CANCELED, payer, nonce] not in topics or [AUTHORIZATION_USED, payer, nonce] in topics:
            return None
        return {"transaction": transaction, "network": NETWORK, "payer": order["owner"],
                "nonce": order["payment_nonce"], "block_number": block, "block_hash": receipt["blockHash"],
                "confirmations": head - block + 1, "confirmation_policy": f"testnet-l2-depth-{self.confirmations}",
                "verified_by": "receipt-authorization-cancelled"}

    async def reconcile(self, order):
        if order.get("network") != NETWORK:
            raise Problem("wrong_chain", "Only Base Sepolia orders are supported.", 503)
        candidate = order.get("settlement_candidate", {}).get("transaction")
        if candidate:
            confirmed = await self.confirm(order, candidate)
            if confirmed:
                return {"status": "settled", "receipt": confirmed}
        start = order.get("chain_start_block")
        if type(start) is not int:
            return {"status": "unknown"}
        head = await self.block_number()
        # Bound each query. Do not interpret an incomplete scan as unpaid.
        if head - start > 5000:
            return {"status": "unknown", "reason": "manual_block_range_required"}
        for first in range(max(0, start - 2), head + 1, 500):
            logs = await self.rpc("eth_getLogs", [{"address": ASSET,
                "fromBlock": hex(first), "toBlock": hex(min(first + 499, head)),
                "topics": [[AUTHORIZATION_USED, AUTHORIZATION_CANCELED], address_topic(order["owner"]), order["payment_nonce"]]}])
            for entry in logs:
                if entry.get("removed", False):
                    continue
                confirmed = await self.confirm(order, entry.get("transactionHash"))
                if confirmed:
                    return {"status": "settled", "receipt": confirmed}
                cancelled = await self.confirm_cancellation(order, entry.get("transactionHash"))
                if cancelled:
                    return {"status": "expired_unspent", "reason": "authorization_cancelled", "receipt": cancelled}
        confirmed_block = max(0, head - self.confirmations + 1)
        block = await self.rpc("eth_getBlockByNumber", [hex(confirmed_block), False])
        if block and int(block["timestamp"], 16) > order["authorization_expires_at"]:
            selector = keccak(text="authorizationState(address,bytes32)")[:4].hex()
            data = "0x" + selector + address_topic(order["owner"])[2:] + order["payment_nonce"][2:]
            used = await self.rpc("eth_call", [{"to": ASSET, "data": data}, hex(confirmed_block)])
            if int(used, 16) == 0:
                return {"status": "expired_unspent"}
        return {"status": "unknown"}

    async def close(self):
        await self.client.aclose()
