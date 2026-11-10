"""Run the plugin in a clean Hermes host without loading the personal installation.

Use an exported official source tree and installed dependency directory. Python
starts with -I -S so editable-install .pth files are never executed. Only one
access token is copied into the temporary home, and it has no refresh authority.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def child(arguments):
    from hermes_cli.plugins import get_plugin_manager
    from tools.registry import registry
    manager = get_plugin_manager()
    manager.discover_and_load()
    entries = {name: registry.get_entry(name, scope=manager.scope_key) for name in (
        "service_search", "service_draft", "service_inspect", "service_quote", "service_purchase", "service_status")}
    if any(entry is None or not entry.is_async for entry in entries.values()):
        raise RuntimeError("The real Hermes registry did not load all asynchronous plugin tools")
    if arguments["action"] == "doctor":
        from hermes_cli.plugin_dev import doctor_plugin
        report = doctor_plugin(arguments["staged_plugin"])
        return {"ok": report.ok, "tools": list(report.registered_tools),
                "findings": [{"level": item.level, "message": item.message} for item in report.findings]}
    if arguments["action"] == "draft":
        import asyncio
        import traceback
        from agent.plugin_llm import PluginLlm
        failures = []
        original = PluginLlm.acomplete_structured
        async def observed(self, **kwargs):
            try:
                return await original(self, **kwargs)
            except Exception as exc:
                frames = traceback.extract_tb(exc.__traceback__)
                failures.append({"exception": type(exc).__name__,
                    "location": f"{Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else None,
                    "missing_module": exc.name if isinstance(exc, ModuleNotFoundError) else None})
                raise
        PluginLlm.acomplete_structured = observed
        example = json.loads(Path(arguments["example_file"]).read_text())
        response = asyncio.run(entries["service_draft"].handler({
            "skill_path": arguments["skill_path"], "input_example": example["input"], "output_example": example["output"],
        }))
        result = json.loads(response)
        return {"host": "Hermes", "model_call_succeeded": "draft" in result, "tool": "service_draft",
                "response": result, "diagnostics": failures}
    from gateway.run import _resolve_runtime_agent_kwargs
    from run_agent import AIAgent
    observed_tools = []
    def tool_started(*args, **kwargs):
        name = next((value for value in args if isinstance(value, str) and value.startswith("service_")), None)
        if name:
            observed_tools.append(name)
    runtime = _resolve_runtime_agent_kwargs()
    runtime.pop("_fallback_notice", None)
    model = runtime.pop("model", "gpt-6.1-sol")
    agent = AIAgent(model=model, **runtime, max_iterations=6, max_tokens=1200, run_budget_seconds=120,
                    enabled_toolsets=["hermes_sell"], quiet_mode=True, verbose_logging=False,
                    skip_context_files=True, skip_memory=True, skip_background_review=True,
                    tool_start_callback=tool_started, platform="cli")
    prompt = (
        "The user has approved purchase " + arguments["purchase_id"] + " through the browser wallet. "
        "Use service_purchase to submit that existing purchase, then service_status to retrieve its result. "
        "The tool must confirm stored approval; never grant or invent approval. Do not quote or create another purchase. "
        "If it has settled, use its actual purchased result "
        "to write a concise, useful follow-up answer in English. If it is not settled, report the status accurately."
    )
    result = agent.run_conversation(prompt)
    return {"host": "Hermes", "role": "buyer", "tools_called": observed_tools,
            **{key: result.get(key) for key in ("final_response", "completed", "failed", "partial", "interrupted")}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("doctor", "draft", "buyer-followup"))
    parser.add_argument("--hermes-source", type=Path, required=True)
    parser.add_argument("--hermes-site-packages", type=Path, required=True)
    parser.add_argument("--codex-auth-store", type=Path)
    parser.add_argument("--draft-dir", type=Path, default=ROOT / ".state/testnet/drafts")
    parser.add_argument("--skill-path", type=Path)
    parser.add_argument("--example-file", type=Path)
    parser.add_argument("--buyer-state-dir", type=Path)
    parser.add_argument("--buyer-vault-key-file", type=Path)
    parser.add_argument("--buyer-origin", default="http://127.0.0.1:8785")
    parser.add_argument("--purchase-id")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    for name, value in vars(args).items():
        if isinstance(value, Path):
            setattr(args, name, value.expanduser().resolve())
    if args.action != "doctor" and not args.codex_auth_store:
        parser.error("Real model checks require an explicitly selected --codex-auth-store")
    if args.action == "draft" and (not args.skill_path or not args.example_file):
        parser.error("draft requires --skill-path and --example-file")
    if args.action == "buyer-followup" and (not args.purchase_id or not args.buyer_state_dir or not args.buyer_vault_key_file):
        parser.error("buyer-followup requires a purchase ID and the shared buyer state and vault key paths")
    from hermes_runtime import access_only_auth
    with tempfile.TemporaryDirectory(prefix="hermes-sell-plugin-host-") as directory:
        root = Path(directory)
        home = root / "home"
        host_home = home / ".hermes"
        plugin = host_home / "plugins/hermes-sell"
        plugin.mkdir(mode=0o700, parents=True)
        for name in ("__init__.py", "plugin.yaml"):
            shutil.copyfile(ROOT / name, plugin / name)
        for name in ("src", "skills"):
            shutil.copytree(ROOT / name, plugin / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
        settings = {"role": "buyer" if args.action == "buyer-followup" else "owner", "draft_dir": str(args.draft_dir),
                    "buyer_mode": "testnet", "buyer_origin": args.buyer_origin,
                    "buyer_state_dir": str(args.buyer_state_dir or ""),
                    "buyer_vault_key_file": str(args.buyer_vault_key_file or ""),
                    "buyer_approval_origin": "http://127.0.0.1:8767"}
        config = {"_config_version": 52, "model": {"provider": "openai-codex", "default": "gpt-6.1-sol"},
                  "plugins": {"enabled": ["hermes-sell"], "entries": {"hermes-sell": {"settings": settings}}},
                  "platform_toolsets": {"cli": ["hermes_sell"]}, "mcp_servers": {},
                  "memory": {"memory_enabled": False, "user_profile_enabled": False}}
        (host_home / "config.yaml").write_text(json.dumps(config))
        if args.codex_auth_store:
            auth, _ = access_only_auth(args.codex_auth_store)
            path = host_home / "auth.json"
            path.write_text(json.dumps(auth))
            path.chmod(0o600)
        arguments = {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()}
        arguments["staged_plugin"] = str(plugin)
        env = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(home), "HERMES_REAL_HOME": str(home),
               "HERMES_HOME": str(host_home), "HERMES_ENABLE_PROJECT_PLUGINS": "0", "PYTHONDONTWRITEBYTECODE": "1",
               "HERMES_BUNDLED_PLUGINS": str(args.hermes_source / "plugins/model-providers")}
        package_dirs = [str(args.hermes_source), str(args.hermes_site_packages),
                        str(ROOT / ".venv/lib/python3.14/site-packages"), str(ROOT / "scripts")]
        code = ("import sys,json; sys.path[:0]=" + repr(package_dirs) + "; "
                "from check_hermes_host import child; print(json.dumps(child(json.load(sys.stdin))))")
        result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", code], input=json.dumps(arguments),
                                cwd=root, env=env, text=True, capture_output=True, timeout=150)
        if result.returncode:
            # Host exception messages do not include the imported credential document.
            sys.stderr.write(result.stderr[-6000:])
            raise SystemExit(result.returncode)
        # Hermes may render tool progress even in quiet mode. Keep the evidence
        # file machine-readable and exclude the host's terminal decorations.
        payload = None
        for line in reversed(result.stdout.splitlines()):
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and (candidate.get("host") == "Hermes" or "findings" in candidate):
                payload = candidate
                break
        if payload is None:
            raise RuntimeError("The clean Hermes host did not return a structured check result")
        output = json.dumps(payload)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w") as handle:
                handle.write(output + "\n")
        print(output)


if __name__ == "__main__":
    main()
