"""Private configuration and readiness checks for the local testnet service."""

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import stat
import time
from urllib.parse import urlparse

from .contracts import Problem
from .vault import AuthorizationVault


def private_text(path):
    descriptor = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise ValueError("Credential files must be private regular files owned by the current user.")
        value = os.read(descriptor, 16385)
        if not value or len(value) > 16384:
            raise ValueError("Credential file is empty or too large.")
        return value.decode().strip()
    finally:
        os.close(descriptor)


@dataclass
class TestnetSettings:
    state_dir: Path
    origin: str
    admin_port: int
    runtime_url: str
    runtime_token_file: Path
    owner_token_file: Path
    vault_key_file: Path
    draft_dir: Path
    docker_context: str
    runtime_container: str
    rpc_url: str
    facilitator_url: str
    directory_origin: str = "http://127.0.0.1:8795"

    @classmethod
    def load(cls, path):
        path = Path(path).resolve()
        raw = json.loads(path.read_text())
        for name in ("state_dir", "runtime_token_file", "owner_token_file", "vault_key_file", "draft_dir"):
            value = Path(raw[name]).expanduser()
            raw[name] = value if value.is_absolute() else (path.parent / value).resolve()
        result = cls(**raw)
        from .discovery import exact_origin
        exact_origin(result.directory_origin, local_only=True)
        parsed = urlparse(result.origin)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("This release serves testnet on an explicit http://127.0.0.1:PORT origin.")
        if type(result.admin_port) is not int or not 1024 <= result.admin_port <= 65535 or result.admin_port == parsed.port:
            raise ValueError("Use a separate owner port between 1024 and 65535.")
        if result.facilitator_url != "https://x402.org/facilitator":
            raise ValueError("This testnet release uses the official public testnet facilitator.")
        return result

    @property
    def port(self):
        return urlparse(self.origin).port


def initialize_testnet(directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = directory / "config.json"
    if config.exists():
        raise Problem("configuration_exists", "Testnet configuration already exists. It was not overwritten.", 409)
    key = directory / "authorization.key"
    if not key.exists():
        AuthorizationVault.create(key)
    owner = directory / "owner-token"
    if not owner.exists():
        descriptor = os.open(owner, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(secrets.token_urlsafe(32))
    values = {"state_dir": ".", "origin": "http://127.0.0.1:8785", "admin_port": 8786,
        "runtime_url": "http://127.0.0.1:18790", "runtime_token_file": "runtime-token",
        "owner_token_file": "owner-token", "vault_key_file": "authorization.key",
        "draft_dir": "drafts", "docker_context": "colima-hermes-sell",
        "runtime_container": "hermes-sell-runtime", "rpc_url": "https://sepolia.base.org",
        "facilitator_url": "https://x402.org/facilitator"}
    descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(values, handle, indent=2)
        handle.write("\n")
    return {"config": str(config), "origin": values["origin"], "mode": "testnet",
            "next": "Prepare the isolated Hermes runtime, then run testnet-doctor."}


class Readiness:
    def __init__(self, settings, runtime, payments):
        self.settings, self.runtime, self.payments = settings, runtime, payments
        self.last_success = 0.0
        self.report = {"ready": False, "error": "not_checked"}

    def ready(self):
        return self.last_success > 0 and time.monotonic() - self.last_success < 45

    async def check(self):
        try:
            from .runtime_container import inspect_runtime
            isolated = await asyncio.to_thread(inspect_runtime, self.settings.docker_context, self.settings.runtime_container)
            if not isolated:
                raise ValueError("Runtime isolation could not be verified.")
            if isolated.get("origin") != self.runtime.origin or isolated.get("credential_generation") != self.runtime.credential_generation:
                raise ValueError("The inspected container does not match the configured runtime endpoint and credential generation.")
            runtime = await self.runtime.preflight()
            payments = await self.payments.preflight()
            self.last_success = time.monotonic()
            self.report = {"ready": True, "mode": "testnet", "runtime": runtime, "payments": payments,
                           "isolation": isolated}
        except Exception as exc:
            self.last_success = 0.0
            self.report = {"ready": False, "mode": "testnet", "error": type(exc).__name__,
                           "message": "Runtime isolation, model authentication, facilitator, or RPC readiness could not be verified."}
        return self.report

    async def monitor(self):
        while True:
            await self.check()
            await asyncio.sleep(15)
