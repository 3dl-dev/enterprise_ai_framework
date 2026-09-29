"""Hosted-mode lock on Raven's WebUI provider and channel writes (item -250, Contract E).

Raven's WebUI is JSON-RPC 2.0 over one websocket. It can edit providers and channels, which
would make it a second console for settings the control plane owns. Raven v0.2.3 has no
upstream lock (verified in -39e), so the console proxy refuses those write RPCs. The rules
are DATA (`raven_write_lock.json`, or the file named by RAVEN_WRITE_LOCK_FILE), read on every
frame (cached by mtime), so a mutation of the data is a mutation of the fence.

Fail closed, three ways:
  * a method inside a locked namespace (`model.`, `channels.`, `gateway.channels.`) that is
    not on the read allow-list is refused, so a method a later Raven adds is refused too;
  * the method name is normalised (NFKC, case-folded, whitespace and zero-width characters
    removed) before matching, and the allow-list matches the RAW name only;
  * a frame that is not JSON, is not an object or array of objects, a method that is not a
    string, a batch containing any refused call, or an unreadable rules file: refused.
`settings.set` is the generic writer behind `channels.<n>.enabled` and the `*.provider`
pins, so it is refused by key (and by a provider key inside an object value).

This is a one-control-plane lock, not a sandbox: the agent's own tools can still edit its
config inside its pod.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import unicodedata
from pathlib import Path

_log = logging.getLogger(__name__)
_DEFAULT_FILE = Path(__file__).with_name("raven_write_lock.json")
REFUSED_CODE = -32001
_MESSAGE = ("Provider and channel settings are managed in the portal in hosted mode; "
            "this change was not applied.")

_lock = threading.Lock()
_cache: dict = {"key": None, "rules": None}


def _norm(name: str) -> str:
    """Case-fold and drop whitespace and format (zero-width) characters."""
    s = unicodedata.normalize("NFKC", name)
    return "".join(c for c in s if not c.isspace() and unicodedata.category(c) != "Cf").casefold()


class _Rules:
    def __init__(self, raw: dict):
        self.deny = {_norm(m) for m in raw["deny_methods"]}
        self.namespaces = tuple(_norm(n) for n in raw["locked_namespaces"])
        self.allow = frozenset(raw["allow_in_locked_namespaces"])
        ss = raw["settings_set"]
        self.settings_method = _norm(ss["method"])
        self.key_patterns = [re.compile(p, re.I) for p in ss["deny_key_patterns"]]
        self.value_key_patterns = [re.compile(p, re.I)
                                   for p in ss["deny_when_value_object_has_key_matching"]]


def _rules() -> _Rules | None:
    path = Path(os.environ.get("RAVEN_WRITE_LOCK_FILE") or _DEFAULT_FILE)
    try:
        key = (str(path), path.stat().st_mtime_ns)
        with _lock:
            if _cache["key"] != key:
                _cache["rules"] = _Rules(json.loads(path.read_text(encoding="utf-8")))
                _cache["key"] = key
            return _cache["rules"]
    except Exception as exc:  # noqa: BLE001 - an unreadable fence must close, not open
        _log.error("raven write-lock rules unreadable (%s): refusing every frame", exc)
        with _lock:
            _cache["key"] = None
        return None


def _denied_call(call, rules: _Rules) -> bool:
    if not isinstance(call, dict):
        return True
    method = call.get("method")
    if not isinstance(method, str):
        return True
    norm = _norm(method)
    if norm in rules.deny:
        return True
    if any(norm.startswith(ns) for ns in rules.namespaces) and method not in rules.allow:
        return True
    if norm == rules.settings_method:
        params = call.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("key"), str):
            return True
        nkey = _norm(params["key"])
        if any(p.search(nkey) for p in rules.key_patterns):
            return True
        value = params.get("value")
        if isinstance(value, dict) and any(
                p.search(_norm(str(k))) for k in value for p in rules.value_key_patterns):
            return True
    return False


def _error(call) -> dict:
    rid = call.get("id") if isinstance(call, dict) else None
    if not isinstance(rid, (int, str)) or isinstance(rid, bool):
        rid = None
    method = call.get("method") if isinstance(call, dict) else None
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": REFUSED_CODE, "message": _MESSAGE,
                      "data": {"refused_by": "eaf-console-proxy",
                               "method": method if isinstance(method, str) else None}}}


def gate_frame(frame: str) -> str | None:
    """None to forward the frame; a JSON-RPC error frame (str) to answer the browser instead."""
    rules = _rules()
    try:
        parsed = json.loads(frame)
    except ValueError:
        return json.dumps(_error(None))
    if rules is None:
        return json.dumps(_error(parsed))
    if isinstance(parsed, list):
        if not parsed or any(_denied_call(c, rules) for c in parsed):
            # Whole batch dropped: one error per call, so the browser can match ids.
            return json.dumps([_error(c) for c in parsed] or [_error(None)])
        return None
    if _denied_call(parsed, rules):
        return json.dumps(_error(parsed))
    return None
