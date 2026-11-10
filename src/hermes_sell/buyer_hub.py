"""Route local approvals to the seller bound to a listing or saved purchase."""

import json

from .buyer import TestnetBuyer, purchase_route
from .contracts import Problem
from .discovery import DirectoryClient


class BuyerHub:
    def __init__(self, default, directory_origin):
        self.default = default
        self.clients = {default.origin: default}
        self.directory = DirectoryClient(directory_origin)

    def for_origin(self, origin):
        if origin not in self.clients:
            self.clients[origin] = TestnetBuyer(origin, self.default.path, vault=self.default.vault,
                approval_origin=self.default.approval_origin, budget=self.default.budget)
        return self.clients[origin]

    def for_purchase(self, identity):
        origin, mode = purchase_route(self.default.path, identity)
        if mode != "testnet":
            raise Problem("buyer_mode_mismatch", "This wallet gateway only approves testnet purchases.", 409)
        return self.for_origin(origin)

    async def for_listing(self, identity):
        listing = await self.directory.get(identity)
        if listing["mode"] != "testnet":
            raise Problem("buyer_mode_mismatch", "This listing uses simulation. Open its storefront to try the simulation.", 409)
        if listing["state"] != "online":
            raise Problem("seller_unavailable", "The selected seller is unavailable. Refresh its listing.", 409)
        return self.for_origin(listing["origin"])

    def purchases(self):
        with self.default.db() as db:
            rows = db.execute("SELECT document FROM purchases ORDER BY rowid DESC LIMIT 100").fetchall()
        return [self.default.public_purchase(record) for (document,) in rows
                if (record := json.loads(document)).get("mode") == "testnet"]

    async def close(self):
        for client in self.clients.values():
            await client.close()
        await self.directory.close()
