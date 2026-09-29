#!/usr/bin/env python3
"""Assert Raven's hosted-mode invariants in $RAVEN_HOME/config.json (stdlib only).

Runs on EVERY boot from hosted-entrypoint.sh, before the upstream entrypoint. It is not a
first-boot seed: the invariants below are the platform's, and a settings edit made in the
WebUI (or a stale PVC from an older image) must not be able to undo them. Everything else
in config.json (persona, cron, skills, UI preferences) is preserved untouched.

The pure function `hosted_overlay(env)` returns the enforced fragment; the tests render it
and assert on it, and also run this script for real against a config file.

Enforced (agents-raven.md, Contract E, "Hosted-mode configuration"):

  * providers.custom is the gateway: apiBase = AGENT_GATEWAY_BASE, apiKey = the Raven's own
    integrated key (OPENAI_API_KEY). agents.defaults selects it (the upstream default model,
    anthropic/claude-opus-4-5, would bill a key we do not hold).
  * subagents: no local third-party ACP/CLI rows. Presets that would spawn hermes, openclaw,
    codex or claude-code INSIDE the Raven pod on Raven's key bypass per-agent instances and
    per-agent billing; remote agents attach only through Contract G. Every non-builtin row
    is forced `enabled: false`.
  * a2a inbound server off with an empty token, no peers.
  * skill hub: no endpoint (skillForge.router.hub) so discovery never queries a hub.
  * EverOS memory on for real: memory.backend=everos, root under RAVEN_HOME (the PVC),
    loopback :18791.

EverOS embeddings and LLM are NOT written here: they are pinned by the pod environment
(EVEROS_LLM__* / EVEROS_EMBEDDING__*), which upstream treats as operator-managed and
refuses to edit from the WebUI.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

EVEROS_PORT = 18791


def hosted_overlay(env: dict[str, str]) -> dict:
    base = env.get("AGENT_GATEWAY_BASE", "").strip()
    key = env.get("OPENAI_API_KEY", "").strip()
    model = env.get("RAVEN_MODEL", "").strip()
    missing = [n for n, v in (("AGENT_GATEWAY_BASE", base), ("OPENAI_API_KEY", key), ("RAVEN_MODEL", model)) if not v]
    if missing:
        raise SystemExit(f"hosted-mode Raven refuses to start without: {', '.join(missing)}")
    everos_root = str(Path(env.get("RAVEN_HOME", "/data/.raven")) / "everos")
    return {
        "agents": {"defaults": {"model": model, "provider": "custom"}},
        "providers": {"custom": {"apiKey": key, "apiBase": base}},
        "a2a": {"server": {"enabled": False, "token": ""}, "peers": []},
        "skillForge": {"router": {"hub": {"endpoint": None, "apiKey": None}}},
        # EverOS memory: raven spawns and owns the server, on loopback :18791, with its
        # store under RAVEN_HOME (the PVC). Its LLM and embedding endpoints are pinned by
        # the pod env (EVEROS_LLM__* / EVEROS_EMBEDDING__*), which upstream treats as
        # operator-managed: the WebUI refuses to edit a role that came from the environment.
        "memory": {"backend": "everos"},
        "plugins": {"config": {"everos-memory": {
            "root": everos_root, "owned": True, "port": EVEROS_PORT,
            "base_url": f"http://127.0.0.1:{EVEROS_PORT}",
        }}},
    }


def _merge(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def enforce(config: dict, env: dict[str, str]) -> dict:
    """Return `config` with the hosted invariants applied."""
    _merge(config, hosted_overlay(env))
    # Local sub-agent launchers: keep only in-process (builtin) rows enabled.
    subs = config.setdefault("subagents", {})
    rows = subs.get("agents", subs.get("thirdParty", []))
    for row in rows:
        if row.get("kind", "builtin") != "builtin":
            row["enabled"] = False
    subs["agents"] = rows
    subs.pop("thirdParty", None)
    return config


def main() -> int:
    home = Path(os.environ.get("RAVEN_HOME", "/data/.raven"))
    path = home / "config.json"
    home.mkdir(parents=True, exist_ok=True)
    try:
        config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError as exc:
        raise SystemExit(f"{path} is not valid JSON; refusing to overwrite it: {exc}")
    enforce(config, dict(os.environ))
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    print(f"raven-hosted: config.json enforced at {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
