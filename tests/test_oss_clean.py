"""The oss-clean gate — hoistable invariant #1 (docs/design/hoistable-and-operated.md).

The distributable must carry ZERO operator literals: no operator domains, LAN IPs, tailnet name,
internal hostnames, operator email, or a hardcoded operator catalogue. Deployment-identity is
CONFIGURATION (bundle/.env + the -3dl overlay), never baked into the shipped tree. This test is
the enforcement that makes that real: it greps the tracked tree and fails the build on any leak,
so the next one lands as a red test instead of quietly shipping — the way the chat-surface leak
that triggered the whole split did.

SCOPE — code, config, scripts, manifests. Deliberately NOT scanned, with the reason:
  * docs/                     — prose: design records, findings, and ops runbooks legitimately
                                discuss the real setup as examples/history.
  * tests/, tests-live/,      — sample/probe DATA. "baron" as a fixture principal or a live-probe
    **/__tests__/, **/fixtures/, result mentioning a model is not shipped config, and scrubbing it
    test_*.py                   would churn/break tests for no hoistability gain.
  * *.example                 — deliberately document "what the operator sets" (e.g. "3dl: ...").
  * .github/FUNDING.yml       — the maintainer project's own funding link.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# (pattern, human label). Each is an operator-identifying literal that must never ship.
DENY = [
    (re.compile(r"3dl\.(one|dev|network)|ai\.3dl|router\.3dl"), "operator domain (3dl)"),
    (re.compile(r"\b192\.168\.2\.\d"), "operator LAN IP"),
    (re.compile(r"tailcb6ef9"), "operator tailnet name"),
    (re.compile(r"stealth\.baron"), "operator internal domain"),
    (re.compile(r"baron@3dl"), "operator email"),
    (re.compile(r"zai-org/|glm-[0-9]+(?:\.[0-9]+)?@deepinfra"), "hardcoded operator catalogue slug"),
]


def _exempt(path: str) -> bool:
    if path.startswith(("docs/", "tests/", "tests-live/")):
        return True
    if path == ".github/FUNDING.yml":
        return True
    if path.endswith(".example"):
        return True
    if "/tests/" in path or "/__tests__/" in path or "/fixtures/" in path:
        return True
    base = path.rsplit("/", 1)[-1]
    return base.startswith("test_")


def _tracked_files() -> list[str]:
    out = subprocess.check_output(["git", "ls-files"], cwd=REPO, text=True)
    return [f for f in out.splitlines() if f and not _exempt(f)]


def test_distributable_carries_no_operator_literals():
    hits: list[str] = []
    for f in _tracked_files():
        try:
            text = (REPO / f).read_text(errors="replace")
        except (OSError, UnicodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for pattern, label in DENY:
                if pattern.search(line):
                    hits.append(f"{f}:{lineno}: {label}: {line.strip()[:90]}")
    assert not hits, (
        "Operator literals leaked into the distributable — deployment-identity must be "
        "configuration (bundle/.env / the -3dl overlay), not baked into the shipped tree "
        "(docs/design/hoistable-and-operated.md, invariant #1). Move each to the instance "
        "config with an agnostic default:\n  " + "\n  ".join(hits[:50])
        + (f"\n  ... and {len(hits) - 50} more" if len(hits) > 50 else "")
    )


def test_no_committed_runtime_export_data():
    """Export output (audit trail, key/spend inventory) is instance runtime data and must never
    be committed — it leaks who used the operator's instance and how. exit.sh writes these at
    runtime; the export test builds its own in a tmp dir."""
    forbidden = ["bundle/audit.jsonl", "bundle/keys.csv", "bundle/spend.csv"]
    tracked = set(subprocess.check_output(["git", "ls-files"], cwd=REPO, text=True).splitlines())
    leaked = [f for f in forbidden if f in tracked]
    assert not leaked, f"committed runtime export data (must be gitignored, not tracked): {leaked}"
