"""Normalized records -> metrics.json (the shape the report page consumes).

Ports the practice kit's model-behavior-report.py aggregation into the product. Output is
the five arrays + composite indices that the operator's published analytics report reads, so the
in-product page (item -d27) renders the same way and a customer can line their numbers up
against the reference.

`build_metrics(turns, sessions, key=...)` groups records by ANY key function. Default is the
model; the slicing item (-2df) reuses this verbatim with key = surface / config / tenant /
effort. That is the whole reason grouping is a parameter and not hard-coded to the model.

Each row is {key,label,unit,better,note,v:{group:value}}. `better` ("high"/"low") says which
direction is good and drives the composite indices. Metadata is a fixed template (labels and
directions are a schema, not data); only `v` is computed here.

Not derivable from a transcript alone, and handled honestly rather than faked:
  * costedit  — needs the real billed spend; filled by the ledger join (-0e90) when session
                records carry `cost`, else null. NEVER estimated here (the product's whole
                edge is that this number is real).
  * survival / netcode — need git/file lifetime (was written code later rm'd or reverted).
                Out of scope for v1; emitted as null, not a misleading zero.
"""

from __future__ import annotations

import statistics as st

# Below this many turns a group is too thin to draw a column from (matches
# model-behavior-report.py). The SKILL's stronger caution — treat <~300 turns as too small
# to draw a LINE from — is a human-facing flag the slicing/report layers surface; here we
# only suppress the truly tiny. Groups below MIN_N are dropped from `v`.
MIN_N = 25

# Fixed row metadata — label/unit/better/note per metric key. Reproduced from the published
# metrics.json so the page reads our output unchanged. Only `v` is computed.
TEMPLATE: dict[str, list[dict]] = {
    "corpus": [
        {"key": "turns", "label": "Turns analysed", "unit": "turns", "better": "high", "note": ""},
        {"key": "sessions", "label": "Sessions", "unit": "sessions", "better": "high", "note": ""},
        {"key": "principals", "label": "Distinct users", "unit": "users", "better": "high", "note": ""},
    ],
    "orch": [
        {"key": "endcold", "label": "Dispatch turns ending cold", "unit": "%", "better": "low", "note": "Turn ends the moment workers report back"},
        {"key": "postdisp", "label": "Work after last dispatch", "unit": "calls", "better": "high", "note": ""},
        {"key": "dispatchrate", "label": "Turns containing a dispatch", "unit": "%", "better": "high", "note": ""},
        {"key": "zerotool", "label": "Turns doing no work at all", "unit": "%", "better": "low", "note": ""},
        {"key": "toolsturn", "label": "Tool calls per turn", "unit": "calls", "better": "high", "note": ""},
        {"key": "agentsperwave", "label": "Agents per dispatch wave", "unit": "agents", "better": "high", "note": "Wave width"},
        {"key": "subpersess", "label": "Subagents per session", "unit": "agents", "better": "high", "note": ""},
        {"key": "subtok", "label": "Worker output tokens/session", "unit": "k tok", "better": "high", "note": ""},
        {"key": "premstop", "label": "Premature stops (you had to push)", "unit": "/100 turns", "better": "low", "note": "Next human message was a bare nudge"},
    ],
    "esc": [
        {"key": "endq", "label": "Turns ending in a question", "unit": "%", "better": "low", "note": ""},
        {"key": "perm", "label": "Permission phrasing", "unit": "/1k ch", "better": "low", "note": "\"want me to\", \"should I\", \"let me know\""},
        {"key": "limit", "label": "Limitation phrasing", "unit": "/1k ch", "better": "low", "note": "\"I didn't\", \"not verified\", \"out of scope\""},
        {"key": "caveat", "label": "Caveat phrasing", "unit": "/1k ch", "better": "low", "note": ""},
        {"key": "hedge", "label": "Hedging", "unit": "/1k ch", "better": "low", "note": ""},
        {"key": "interrupt", "label": "Turns you interrupted", "unit": "%", "better": "low", "note": ""},
        {"key": "nudge", "label": "Turns needing a nudge", "unit": "%", "better": "low", "note": ""},
    ],
    "prose": [
        {"key": "finalmed", "label": "Closing message (median)", "unit": "chars", "better": "low", "note": ""},
        {"key": "bullets", "label": "Bullets", "unit": "/1k ch", "better": "high", "note": ""},
        {"key": "headers", "label": "Headers", "unit": "/1k ch", "better": "high", "note": ""},
        {"key": "bold", "label": "Bold anchors", "unit": "/1k ch", "better": "high", "note": ""},
        {"key": "tables", "label": "Table rows", "unit": "/1k ch", "better": "high", "note": ""},
        {"key": "digits", "label": "Digit density", "unit": "/1k ch", "better": "high", "note": "Numbers per 1k chars"},
        {"key": "wps", "label": "Words per sentence", "unit": "words", "better": "low", "note": ""},
        {"key": "uwr", "label": "Unique-word ratio", "unit": "ratio", "better": "high", "note": ""},
        {"key": "outtok", "label": "Output tokens per turn", "unit": "k tok", "better": "low", "note": ""},
    ],
    "code": [
        {"key": "csess", "label": "Sessions", "unit": "sessions", "better": "high", "note": "With coding activity"},
        {"key": "edits", "label": "Edits per session", "unit": "edits", "better": "high", "note": "Orchestrator + its workers"},
        {"key": "editchars", "label": "Code written per session", "unit": "k chars", "better": "high", "note": ""},
        {"key": "editfail", "label": "Edit failure rate", "unit": "%", "better": "low", "note": "Edit rejected by the tool"},
        {"key": "toolerr", "label": "Tool error rate", "unit": "/100 calls", "better": "low", "note": ""},
        {"key": "rework", "label": "Rework ratio", "unit": "ratio", "better": "low", "note": "Repeat edits to the same file"},
        {"key": "tests", "label": "Tests per 100 edits", "unit": "/100", "better": "high", "note": ""},
        {"key": "commits", "label": "Commits per session", "unit": "commits", "better": "high", "note": ""},
        {"key": "reverts", "label": "Reverts per session", "unit": "reverts", "better": "low", "note": ""},
        {"key": "delegation", "label": "Edits done by workers", "unit": "%", "better": "high", "note": ""},
        {"key": "costedit", "label": "Cost per edit", "unit": "$", "better": "low", "note": "Real billed spend (ledger), not an estimate"},
    ],
}

# Which metrics build each composite index. persist + code are the two the SKILL names.
INDEX_MEMBERS = {
    "persist": [("orch", "endcold"), ("orch", "postdisp"),
                ("orch", "agentsperwave"), ("orch", "subpersess")],
    "code": [("code", "edits"), ("code", "editchars"), ("code", "editfail"),
             ("code", "toolerr"), ("code", "rework"), ("code", "tests"),
             ("code", "commits"), ("code", "reverts")],
}


# ------------------------------------------------------------------ small stats


def _median(xs):
    return st.median(xs) if xs else None


def _mean(xs):
    return st.mean(xs) if xs else None


def _round(v, nd=3):
    return round(v, nd) if isinstance(v, (int, float)) else v


# ------------------------------------------------------------------ per-group aggregate


class _Agg:
    """Everything the rows need for one group, computed once."""

    def __init__(self, turns, sessions):
        self.turns = turns
        self.sessions = sessions
        self.n_turns = len(turns)
        self.styles = [t["style"] for t in turns if t.get("style")]
        self.coded = [s for s in sessions if s.get("code")]

    def per1k(self, key):
        ch = sum(s["chars"] for s in self.styles) or 1
        return 1000 * sum(s.get(key, 0) for s in self.styles) / ch

    def code_sum(self, key):
        return sum((s["code"] or {}).get(key, 0) for s in self.coded)


def _corpus(a: _Agg):
    return {
        "turns": a.n_turns,
        "sessions": len({t["sess"] for t in a.turns}),
        "principals": len({t["principal"] for t in a.turns}),
    }


def _orch(a: _Agg):
    n = a.n_turns or 1
    posts = [t["post_disp"] for t in a.turns if t.get("post_disp") is not None]
    n_disp_turns = len(posts)
    return {
        "endcold": 100 * sum(1 for p in posts if p == 0) / max(1, n_disp_turns),
        "postdisp": _mean(posts) or 0,
        "dispatchrate": 100 * sum(1 for t in a.turns if t.get("dispatch")) / n,
        "zerotool": 100 * sum(1 for t in a.turns if not t["tools"]) / n,
        "toolsturn": sum(t["tools"] for t in a.turns) / n,
        "agentsperwave": _mean([s["agents_per_wave"] for s in a.sessions]) or 0,
        "subpersess": _mean([s["n_subagents"] for s in a.sessions]) or 0,
        "subtok": (_mean([s["subagent_output_tokens"] for s in a.sessions]) or 0) / 1000,
        "premstop": 100 * sum(t["nudge"] for t in a.turns) / n,
    }


def _esc(a: _Agg):
    n = a.n_turns or 1
    return {
        "endq": 100 * sum(t["ends_q"] for t in a.turns) / n,
        "perm": a.per1k("permission"),
        "limit": a.per1k("limitation"),
        "caveat": a.per1k("caveat"),
        "hedge": a.per1k("hedge"),
        "interrupt": 100 * sum(t["interrupted"] for t in a.turns) / n,
        "nudge": 100 * sum(t["nudge"] for t in a.turns) / n,
    }


def _prose(a: _Agg):
    n = a.n_turns or 1
    return {
        "finalmed": _median([s["chars"] for s in a.styles]) or 0,
        "bullets": a.per1k("bullet"),
        "headers": a.per1k("header"),
        "bold": a.per1k("bold"),
        "tables": a.per1k("table"),
        "digits": a.per1k("digit"),
        "wps": _mean([s["wps"] for s in a.styles if s["wps"]]) or 0,
        "uwr": _mean([s["uwr"] for s in a.styles]) or 0,
        "outtok": (sum(t["out_tok"] for t in a.turns) / n) / 1000,
    }


def _code(a: _Agg):
    edits = a.code_sum("edits")
    tools = sum(t["tools"] for t in a.turns)
    n_sess = len(a.coded) or 1
    ledger = sum(
        (s.get("cost") or {}).get("ledger_spend_usd", 0)
        for s in a.coded
        if (s.get("cost") or {}).get("source") == "ledger"
    )
    have_cost = any((s.get("cost") or {}).get("source") == "ledger" for s in a.coded)
    return {
        "csess": len(a.coded),
        "edits": edits / n_sess,
        "editchars": (a.code_sum("chars_written") / n_sess) / 1000,
        "editfail": 100 * a.code_sum("edit_failures") / max(1, edits),
        "toolerr": 100 * a.code_sum("tool_errors") / max(1, tools),
        "rework": a.code_sum("rework") / max(1, edits),
        "tests": 100 * a.code_sum("tests") / max(1, edits),
        "commits": a.code_sum("commits") / n_sess,
        "reverts": a.code_sum("reverts") / n_sess,
        "delegation": 100 * a.code_sum("worker_edits") / max(1, edits),
        # Real billed cost per edit, or null when the ledger has not been joined in yet
        # (-0e90). Never estimated here — an estimate would defeat the point.
        "costedit": (ledger / edits) if (have_cost and edits) else None,
    }


_ARRAY_FN = {"corpus": _corpus, "orch": _orch, "esc": _esc, "prose": _prose, "code": _code}


def _index(arrays: dict, members: list[tuple[str, str]], groups: list[str]) -> dict:
    """Score each group against the best performer on each member metric, then average.

    100*value/best when higher is better, 100*best/value when lower is better (SKILL). A
    metric where every group is 0, or a group whose own value is 0 on a lower-is-better
    metric, is handled without dividing by zero — a 0 on a low metric is a perfect score.
    """
    row_by_key = {(arr, r["key"]): r for arr in arrays for r in arrays[arr]}
    scores: dict[str, list[float]] = {g: [] for g in groups}
    for arr, mkey in members:
        row = row_by_key.get((arr, mkey))
        if not row:
            continue
        better = row["better"]
        vals = {g: row["v"].get(g) for g in groups if isinstance(row["v"].get(g), (int, float))}
        if not vals:
            continue
        if better == "high":
            best = max(vals.values())
            for g, v in vals.items():
                scores[g].append(100.0 if best == 0 else 100 * v / best)
        else:  # low is better
            positive = [v for v in vals.values() if v > 0]
            best = min(positive) if positive else 0
            for g, v in vals.items():
                scores[g].append(100.0 if v <= 0 or best == 0 else 100 * best / v)
    return {g: _round(_mean(s), 1) for g, s in scores.items() if s}


def build_metrics(turns, sessions, *, key=lambda r: r.get("model"), min_n=MIN_N, meta=None):
    """Aggregate normalized turn/session records into the metrics.json structure.

    `key` chooses the grouping dimension (model by default; surface/config/tenant/effort for
    the slicing item). Groups with fewer than `min_n` turns are dropped from every `v` — too
    thin to draw a column from. `turn_counts` in the output lets the UI flag near-thin groups.
    """
    turns = [t for t in turns if key(t) is not None]
    sessions = [s for s in sessions if key(s) is not None]

    tg: dict[str, list] = {}
    sg: dict[str, list] = {}
    for t in turns:
        tg.setdefault(key(t), []).append(t)
    for s in sessions:
        sg.setdefault(key(s), []).append(s)

    groups = sorted(g for g in tg if len(tg[g]) >= min_n)
    aggs = {g: _Agg(tg.get(g, []), sg.get(g, [])) for g in groups}

    out: dict = {}
    for arr, fn in _ARRAY_FN.items():
        rows = []
        computed = {g: fn(aggs[g]) for g in groups}
        for tmpl in TEMPLATE[arr]:
            v = {}
            for g in groups:
                val = computed[g].get(tmpl["key"])
                if val is not None:
                    v[g] = _round(val)
            rows.append({**tmpl, "v": v})
        out[arr] = rows

    out["index"] = {
        name: _index(out, members, groups) for name, members in INDEX_MEMBERS.items()
    }
    out["index"]["members"] = list(INDEX_MEMBERS)
    out["meta"] = {
        **(meta or {}),
        "groups": groups,
        "turn_counts": {g: len(tg[g]) for g in groups},
        "min_n": min_n,
        # groups below the SKILL's line-drawing threshold — the UI should mark these.
        "thin": [g for g in groups if len(tg[g]) < 300],
    }
    return out
