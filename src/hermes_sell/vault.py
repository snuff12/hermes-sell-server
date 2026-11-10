"""Authenticated encryption for payment authorizations stored in the order ledger."""

import base64
import json
import os
from pathlib import Path
import stat

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .contracts import Problem, canonical


class AuthorizationVault:
    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("The authorization key must contain exactly 32 bytes.")
        self.cipher = AESGCM(key)

    @classmethod
    def load(cls, path: Path):
        path = Path(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                raise ValueError("The authorization key must be a private file owned by the current user.")
            key = os.read(descriptor, 33)
        finally:
            os.close(descriptor)
        return cls(key)

    @classmethod
    def create(cls, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            key = os.urandom(32)
            os.write(descriptor, key)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return cls(key)

    @staticmethod
    def context(order):
        return canonical(["hermes-sell-authorization-v1", order["id"], order["order_digest"]]).encode()

    def seal(self, payload, order):
        nonce = os.urandom(12)
        encrypted = self.cipher.encrypt(nonce, canonical(payload).encode(), self.context(order))
        return {"version": 1, "nonce": base64.b64encode(nonce).decode(),
                "ciphertext": base64.b64encode(encrypted).decode()}

    def open(self, envelope, order):
        try:
            if envelope["version"] != 1:
                raise ValueError("Unsupported authorization envelope")
            raw = self.cipher.decrypt(base64.b64decode(envelope["nonce"], validate=True),
                                      base64.b64decode(envelope["ciphertext"], validate=True), self.context(order))
            return json.loads(raw)
        except Exception as exc:
            raise Problem("authorization_unavailable", "The saved authorization could not be decrypted. Restore its original key; do not sign again.", 503) from exc
