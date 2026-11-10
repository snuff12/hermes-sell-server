"""Build, start, and inspect the isolated Hermes seller runtime.

The optional Codex import copies one currently valid access token. It never copies
refresh tokens, updates the original store, or mounts host directories.
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from hermes_sell.runtime_container import RUNTIME_IMAGE, inspect_runtime


def run(context, *arguments, data=None, capture=False):
    result = subprocess.run(["docker", "--context", context, *arguments], input=data,
                            capture_output=capture, check=False)
    if result.returncode:
        raise RuntimeError("The Docker runtime operation failed")
    return result.stdout


def access_only_auth(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r") as handle:
        info = os.fstat(handle.fileno())
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("The source auth store must be a private file owned by this user")
        original = json.load(handle)
    candidates = []
    for entry in original.get("credential_pool", {}).get("openai-codex", []):
        if entry.get("base_url", "https://chatgpt.com/backend-api/codex").rstrip("/") == "https://chatgpt.com/backend-api/codex":
            candidates.append(entry.get("access_token"))
    candidates.append(original.get("providers", {}).get("openai-codex", {}).get("tokens", {}).get("access_token"))
    for token in candidates:
        if not isinstance(token, str):
            continue
        try:
            payload = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            expiry = float(claims["exp"])
        except (IndexError, ValueError, KeyError, TypeError):
            continue
        if expiry < time.time() + 600:
            continue
        # Expiry parsing does not validate the token; the model provider still authenticates it.
        return {"version": original.get("version", 1), "active_provider": "openai-codex", "providers": {},
                "credential_pool": {"openai-codex": [{
                    "id": "seller-access-only", "label": "Seller runtime access token", "auth_type": "oauth",
                    "source": "manual:hermes-sell-access-only", "priority": 0, "access_token": token,
                    "base_url": "https://chatgpt.com/backend-api/codex",
                }]}}, int(expiry)
    raise ValueError("No Codex access token remains valid for at least ten minutes; refresh in the original Hermes host first")


def runtime_token(path):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "r") as handle:
            info = os.fstat(handle.fileno())
            if info.st_uid != os.getuid() or info.st_mode & 0o077 or not stat.S_ISREG(info.st_mode):
                raise ValueError("The runtime token file must be private and owned by this user")
            token = handle.read().strip()
    else:
        token = secrets.token_urlsafe(48)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    if len(token) < 32:
        raise ValueError("The runtime token is too short")
    return token


def start(context, auth_store, token_file):
    existing = subprocess.run(["docker", "--context", context, "container", "inspect", "hermes-sell-runtime"],
                              capture_output=True, timeout=30)
    if existing.returncode == 0:
        raise RuntimeError("The runtime container already exists. Stop and remove that container before starting; retain its named volumes.")
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", 18790))
        except OSError as exc:
            raise RuntimeError("Port 18790 is already in use; do not replace an unrelated local service") from exc
    auth, expiry = access_only_auth(auth_store)
    token = runtime_token(token_file)
    for volume in ("hermes-sell-runtime-state", "hermes-sell-runtime-secrets"):
        run(context, "volume", "create", "--label", "org.hermes-sell.runtime=true", volume, capture=True)
    populate = (
        "import json,os,sys; from pathlib import Path; os.umask(0o077); data=json.load(sys.stdin); "
        "root=Path('/run/secrets'); root.mkdir(exist_ok=True); "
        "[(root/name).write_text(value) for name,value in data.items()]; "
        "[os.chmod(root/name,0o400) for name in data]; [os.chown(root/name,10000,10000) for name in data]; "
        "os.chown(root,10000,10000); os.chmod(root,0o700); os.chown('/state',10000,10000); os.chmod('/state',0o700)"
    )
    payload = json.dumps({"auth.json": json.dumps(auth), "runtime-token": token})
    run(context, "run", "--rm", "-i", "--user", "0:0", "--network", "none", "--entrypoint", "/opt/hermes/.venv/bin/python",
        "--mount", "type=volume,source=hermes-sell-runtime-state,target=/state",
        "--mount", "type=volume,source=hermes-sell-runtime-secrets,target=/run/secrets",
        "--tmpfs", "/opt/data:rw,nosuid,noexec,size=16m", RUNTIME_IMAGE, "-I", "-c", populate,
        data=payload.encode(), capture=True)
    run(context, "run", "-d", "--name", "hermes-sell-runtime", "--user", "10000:10000", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--pids-limit", "256", "--memory", "2g",
        "--cpus", "2", "--tmpfs", "/tmp:rw,nosuid,noexec,size=256m,uid=10000,gid=10000",
        "--tmpfs", "/opt/data:rw,nosuid,noexec,size=16m,uid=10000,gid=10000",
        "--mount", "type=volume,source=hermes-sell-runtime-state,target=/state",
        "--mount", "type=volume,source=hermes-sell-runtime-secrets,target=/run/secrets,readonly",
        "-p", "127.0.0.1:18790:8642", RUNTIME_IMAGE, capture=True)
    return {"container": "hermes-sell-runtime", "origin": "http://127.0.0.1:18790",
            "credential_generation": hashlib.sha256(token.encode()).hexdigest(), "model_access_expires_at": expiry,
            "token_file": str(token_file)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "start", "inspect"))
    parser.add_argument("--context", default="colima-hermes-sell")
    parser.add_argument("--codex-auth-store", type=Path)
    parser.add_argument("--token-file", type=Path, default=ROOT / ".state/testnet/runtime-token")
    args = parser.parse_args()
    if args.action == "build":
        run(args.context, "build", "-f", str(ROOT / "deployment/hermes/Dockerfile"), "-t", RUNTIME_IMAGE, str(ROOT))
    elif args.action == "start":
        if args.codex_auth_store is None:
            parser.error("start requires an explicitly selected --codex-auth-store")
        print(json.dumps(start(args.context, args.codex_auth_store, args.token_file)))
    else:
        print(json.dumps(inspect_runtime(args.context)))


if __name__ == "__main__":
    main()
