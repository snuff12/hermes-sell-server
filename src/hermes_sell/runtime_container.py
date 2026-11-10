"""Inspect the dedicated seller container before enabling real model execution."""

import hashlib
import inspect
import json
import re
import subprocess

BASE_IMAGE = "nousresearch/hermes-agent@sha256:3eadce07c6cc35599ebacfa1f7d8ee06edce6374b9d07e379d6ec825ddaa7534"
RUNTIME_IMAGE = "hermes-sell-runtime:0.1"
POLICY = {
    "_config_version": 52,
    "model": {"provider": "openai-codex", "default": "gpt-6.1-sol"},
    "platform_toolsets": {"api_server": [], "cli": []},
    "mcp_servers": {}, "plugins": {"enabled": []},
    "memory": {"memory_enabled": False, "user_profile_enabled": False},
    "agent": {"max_turns": 2},
    "gateway": {"api_server": {"max_concurrent_runs": 1}},
}
POLICY_BYTES = (json.dumps(POLICY, sort_keys=True, separators=(",", ":")) + "\n").encode()
POLICY_SHA256 = hashlib.sha256(POLICY_BYTES).hexdigest()


def docker_json(context, *args):
    result = subprocess.run(["docker", "--context", context, *args], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("The dedicated Docker runtime could not be inspected")
    return json.loads(result.stdout)


def inspect_runtime(context="colima-hermes-sell", container="hermes-sell-runtime") -> dict:
    info = docker_json(context, "inspect", container)[0]
    host, config = info["HostConfig"], info["Config"]
    if (not info["State"]["Running"] or config.get("User") != "10000:10000"
            or host.get("Privileged") or not host.get("ReadonlyRootfs")
            or set(host.get("CapDrop") or []) != {"ALL"}
            or "no-new-privileges:true" not in (host.get("SecurityOpt") or [])
            or host.get("NetworkMode") not in {"default", "bridge"} or host.get("PidMode") not in {None, ""}
            or host.get("Devices") or host.get("Binds") or host.get("CapAdd")):
        raise RuntimeError("The seller container does not meet process isolation requirements")
    if config.get("Entrypoint") != ["/opt/hermes/.venv/bin/python", "-I", "-B", "/opt/hermes-sell/entrypoint.py"]:
        raise RuntimeError("The runtime container uses an unexpected entrypoint")
    mounts = {mount["Destination"]: mount for mount in info.get("Mounts", []) if mount["Type"] != "tmpfs"}
    if set(mounts) != {"/state", "/run/secrets"} or any(mount["Type"] != "volume" for mount in mounts.values()):
        raise RuntimeError("Only the dedicated state and credential volumes may be mounted")
    if not mounts["/state"]["RW"] or mounts["/run/secrets"]["RW"]:
        raise RuntimeError("The credential volume must be read-only")
    if mounts["/state"]["Name"] != "hermes-sell-runtime-state" or mounts["/run/secrets"]["Name"] != "hermes-sell-runtime-secrets":
        raise RuntimeError("The runtime uses unexpected data volumes")
    bindings = host.get("PortBindings") or {}
    if bindings != {"8642/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18790"}]}:
        raise RuntimeError("The runtime API must be bound only to 127.0.0.1:18790")
    labels = config.get("Labels") or {}
    if labels.get("org.hermes-sell.policy") != "text-json-no-tools-v1":
        raise RuntimeError("The runtime image has no seller policy declaration")
    proof = docker_json(context, "exec", container, "/opt/hermes/.venv/bin/python", "-I", "-B", "/opt/hermes-sell/entrypoint.py", "attest")
    if (proof.get("policy_sha256") != POLICY_SHA256 or proof.get("effective_toolsets") != []
            or proof.get("uid") != 10000 or proof.get("credential_provider") != "openai-codex"
            or proof.get("has_refresh_token") is not False or proof.get("personal_mounts") is not False):
        raise RuntimeError("The runtime configuration or credential boundary failed verification")
    return {"container": container, "image_id": info["Image"], "policy_sha256": POLICY_SHA256,
            "origin": "http://127.0.0.1:18790", "credential_generation": proof["credential_generation"],
            "model": POLICY["model"]["default"], "provider": "openai-codex"}


def read_admission(db_path, token_file, run_key, request_fingerprint, credential_generation):
    """Read the pinned Hermes v0.23 default-profile reservation without pruning it."""
    import hashlib
    import hmac
    from pathlib import Path
    import sqlite3

    token = Path(token_file).read_text().strip()
    if not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), credential_generation):
        raise RuntimeError("The runtime credential generation has changed")
    scope = hashlib.sha256(("default\0" + token).encode()).hexdigest()
    connection = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        connection.execute("PRAGMA query_only=ON")
        row = connection.execute(
            "SELECT fingerprint,run_id FROM run_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, run_key),
        ).fetchone()
        if row is None:
            return None
        if not hmac.compare_digest(row[0], request_fingerprint):
            raise RuntimeError("The runtime reservation belongs to a different request")
        return row[1]
    finally:
        connection.close()


def lookup_run_id(run_key, request_fingerprint, credential_generation, *,
                  context="colima-hermes-sell", container="hermes-sell-runtime"):
    """Recover an admission identity through read-only inspection, never POST /runs."""
    if (not isinstance(run_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_key)
            or not isinstance(request_fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", request_fingerprint)
            or not isinstance(credential_generation, str) or not re.fullmatch(r"[0-9a-f]{64}", credential_generation)):
        raise ValueError("Invalid runtime admission identity")
    proof = inspect_runtime(context, container)
    if proof["credential_generation"] != credential_generation:
        raise RuntimeError("The inspected runtime credential generation has changed")
    # Send only public identities as arguments. The bearer token remains inside
    # the isolated container, and SQLite is opened read-only without store setup.
    code = inspect.getsource(read_admission) + (
        "\nimport json,sys\nprint(json.dumps(read_admission("
        "'/state/hermes/runs_idempotency.db','/run/secrets/runtime-token',*sys.argv[1:])))"
    )
    result = docker_json(context, "exec", container, "/opt/hermes/.venv/bin/python", "-I", "-B", "-c", code,
                         run_key, request_fingerprint, credential_generation)
    if result is not None and (not isinstance(result, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", result)):
        raise RuntimeError("The runtime returned an invalid admission identity")
    return result
