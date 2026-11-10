"""Start a dedicated Hermes gateway with a fixed no-tools policy."""

import hashlib
import json
import os
from pathlib import Path
import runpy
import sys

sys.path[:0] = ["/opt/hermes-sell", "/opt/hermes"]
from runtime_container import POLICY_BYTES, POLICY_SHA256


def check_policy():
    if os.getuid() != 10000 or os.environ.get("HERMES_HOME") != "/state/hermes":
        raise RuntimeError("The runtime must use its dedicated unprivileged identity and home")
    path = Path("/state/hermes/config.yaml")
    if path.resolve() != Path("/opt/hermes-sell/config.yaml") or path.read_bytes() != POLICY_BYTES:
        raise RuntimeError("The fixed seller policy has changed")
    auth = json.loads(Path("/run/secrets/auth.json").read_text())
    rows = auth.get("credential_pool", {}).get("openai-codex", [])
    if len(rows) != 1 or not rows[0].get("access_token") or rows[0].get("refresh_token"):
        raise RuntimeError("Only one access-token-only Codex credential is allowed")
    if set(auth.get("credential_pool", {})) != {"openai-codex"} or auth.get("providers"):
        raise RuntimeError("The runtime must not receive unrelated provider credentials")
    token = Path("/run/secrets/runtime-token").read_text().strip()
    if len(token) < 32:
        raise RuntimeError("The runtime bearer token is missing")
    return token


def initialize():
    os.umask(0o077)
    home = Path("/state/hermes")
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    Path("/state/home").mkdir(mode=0o700, parents=True, exist_ok=True)
    config = home / "config.yaml"
    if not config.exists():
        config.symlink_to("/opt/hermes-sell/config.yaml")
    token = check_policy()
    # The local copy may record quota state; it contains no rotating refresh secret.
    auth = home / "auth.json"
    auth.write_bytes(Path("/run/secrets/auth.json").read_bytes())
    auth.chmod(0o600)
    os.environ["API_SERVER_KEY"] = token


if __name__ == "__main__":
    if sys.argv[1:] == ["attest"]:
        token = check_policy()
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools
        config = load_config()
        print(json.dumps({"policy_sha256": POLICY_SHA256, "uid": os.getuid(),
            "effective_toolsets": sorted(_get_platform_tools(config, "api_server")),
            "credential_provider": "openai-codex", "has_refresh_token": False, "personal_mounts": False,
            "credential_generation": hashlib.sha256(token.encode()).hexdigest()}))
    else:
        initialize()
        sys.argv = ["hermes-gateway"]
        runpy.run_module("gateway.run", run_name="__main__")
