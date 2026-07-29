"""Detect a declared artifact that has diverged from its materialised copy.

THE PATTERN THIS EXISTS FOR (enterpriseaiframework-b51)

Something is declared in git; something else materialises it somewhere the running system
actually reads — a rendered file, a docker image layer, a ConfigMap, a venv — and NOTHING
notices when the two stop agreeing. Six instances landed in one session. Every one
presented as an unrelated assertion failing deep inside a test, so the diagnosis cost far
more than the fix:

  - a subPath-mounted ConfigMap kubelet never propagated (7bc)
  - house rules seeded only when absent, so corrections never reached an existing camp (644)
  - dependencies installed only inside the venv-CREATION guard (0dc)
  - config.generated.yaml rendered only by `make up`, twice in two merges (d98, 3f3)
  - a control-plane image built 25 minutes before the source it was built from (37a)

This module covers the two that recur: the rendered gateway config, and the control-plane
source the running container actually holds. It deliberately does NOT try to cover the
ConfigMap and venv instances — one mechanism stretched over all six would be fragile, and
these two are the ones that cost a red gate on every merge.

The functions here are pure and take paths or dicts, so the checks are unit-testable
without a bundle. The callers are tests/conftest.py (which refuses to run the suite
against a stale render) and tests/test_artifact_freshness.py.
"""

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

# The build inputs of the control-plane image, per control-plane/Dockerfile:
#   COPY requirements.txt .
#   COPY app ./app
# Nothing else in control-plane/ reaches the image, so nothing else can make it stale.
# control-plane/tests/ is deliberately outside this set: it is not COPYed in, and
# including it would demand a rebuild for a test-only edit.
CONTROL_PLANE_IMAGE_INPUTS = ("app", "requirements.txt")


class CallbacksUnparseable(Exception):
    """The callbacks list could not be read at all — never treated as 'there are none'."""


def parse_callbacks(text: str, *, origin: str) -> list[str]:
    """Module names in `litellm_settings.callbacks`, e.g. 'strip_reasoning.handler' -> 'strip_reasoning'.

    YAML first, regex only as a fallback, and an exception rather than an empty list when
    neither works. All three of those are deliberate.

    A regex on `callbacks: [ ... ]` reads only inline flow style. Rewriting the same list
    in block style — semantically identical YAML — made that parse return nothing, which
    turned the drift trap in test_gateway_callbacks.py into four SKIPS rather than four
    failures. Measured, not reasoned about: with the list reformatted, that file went from
    12 passing to `1 failed, 4 skipped`. A staleness check must not be defeatable by a
    formatting change, so the primary parse is a real YAML load.

    The regex fallback survives for the one case YAML cannot serve: a file with a
    templating marker or a partial render that will not load. If both fail, that is a
    finding — raise. An empty list is indistinguishable from "this config declares no
    callbacks", and passing quietly on that is the exact failure being closed here.
    """
    parsed = None
    try:
        import yaml

        doc = yaml.safe_load(text)
        if isinstance(doc, dict):
            settings = doc.get("litellm_settings")
            if isinstance(settings, dict) and "callbacks" in settings:
                raw = settings["callbacks"]
                if isinstance(raw, str):  # a single callback, unwrapped
                    raw = [raw]
                if isinstance(raw, list):
                    parsed = [str(e) for e in raw]
    except Exception:
        parsed = None

    if parsed is None:
        match = re.search(r"^\s*callbacks:\s*\[(?P<body>[^\]]*)\]", text, re.MULTILINE)
        if match:
            parsed = re.findall(r"['\"]([^'\"]+)['\"]", match.group("body"))

    if parsed is None:
        raise CallbacksUnparseable(
            f"could not read litellm_settings.callbacks from {origin}. Neither a YAML load "
            f"nor the inline-flow regex found the key. This is reported as a failure and "
            f"never as 'no callbacks are declared': an empty result would silently disable "
            f"every gateway-callback check instead of failing one."
        )

    # "module.attr" -> "module". A bare "module" is also legal.
    return sorted({e.split(".")[0] for e in parsed if e.strip()})


def _stamp(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def stale_render_reasons(base: Path, generated: Path) -> list[str]:
    """Why `generated` is not a current render of `base`. Empty list means it is current.

    Two independent checks, because either alone leaves a hole:

    - mtime: catches a base edit that has not been re-rendered at all, including edits
      that do not touch the callbacks list.
    - callbacks set: catches a render that IS newer but was produced from a different
      base — a `touch`, a partial write, a hand-edit, a file copied in from elsewhere.
      Compared symmetrically, so REMOVING a callback from the base is caught as well as
      adding one. The renderer only substitutes the upstream marker and drops fake
      catalogue entries; it never touches litellm_settings, so the two sets must match
      exactly.
    """
    if not generated.exists():
        return [f"{generated.name} does not exist"]

    reasons = []

    base_mtime = base.stat().st_mtime
    gen_mtime = generated.stat().st_mtime
    if gen_mtime < base_mtime:
        # Sub-second precision on purpose. A merge writes config.base.yaml and the render
        # that should follow it can land in the same wall-clock second, and a message
        # reading "OLDER ... 0s behind" reads as a bug in the check rather than as a
        # finding, which is how a real staleness report gets dismissed.
        reasons.append(
            f"{generated.name} is OLDER than {base.name} "
            f"({_stamp(gen_mtime)} vs {_stamp(base_mtime)}, "
            f"{base_mtime - gen_mtime:.3f}s behind)"
        )

    base_cbs = parse_callbacks(base.read_text(), origin=base.name)
    gen_cbs = parse_callbacks(generated.read_text(), origin=generated.name)
    missing = sorted(set(base_cbs) - set(gen_cbs))
    extra = sorted(set(gen_cbs) - set(base_cbs))
    if missing:
        reasons.append(
            f"{base.name} declares callbacks {missing} that {generated.name} does not load"
        )
    if extra:
        reasons.append(
            f"{generated.name} loads callbacks {extra} that {base.name} no longer declares"
        )

    return reasons


def stale_render_message(base: Path, generated: Path, reasons: list[str]) -> str:
    """The one line an operator should see instead of an assertion 200 lines into a test."""
    detail = "\n".join(f"  - {r}" for r in reasons)
    return (
        "\n"
        "RUN `make up` — the gateway config the suite would test against is STALE.\n"
        f"\n{detail}\n\n"
        f"{generated.name} is gitignored and is rendered from {base.name} by "
        "bin/render-gateway-config.py, which only `make up` invokes. A merge that touches "
        f"{base.name} therefore leaves the running gateway loading the OLD config, and the "
        "suite fails on whatever behaviour the new config was supposed to provide rather "
        "than on the staleness. That cost a red gate on the d98 merge and again on the 3f3 "
        "merge.\n\n"
        "  env -u FORGE_API_KEY -u FORGE_ADMIN_KEY make up\n"
    )


def digest_tree(root: Path, entries=CONTROL_PLANE_IMAGE_INPUTS) -> dict[str, str]:
    """sha256 of every file under `entries`, keyed by path relative to `root`.

    Content, not mtime. The item that filed this suggested comparing the image's created
    timestamp against the newest source mtime; content hashing is preferred because a
    timestamp comparison has a failure mode that cannot be escaped. Docker's build cache
    is keyed on content, so editing a file and reverting it leaves the source mtime newer
    than an image `make up` will legitimately decline to rebuild — the check would then
    demand a rebuild forever. Hashing also catches the case a timestamp misses: the image
    was rebuilt but the container was never recreated from it.
    """
    out = {}
    for entry in entries:
        target = root / entry
        if not target.exists():
            continue
        paths = [target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file())
        for p in paths:
            if "__pycache__" in p.parts or p.suffix == ".pyc":
                continue
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def parse_sha256sum(output: str) -> dict[str, str]:
    """Parse `sha256sum` output ("<hex>  <path>") into {path: hex}."""
    out = {}
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        digest, _, path = line.partition(" ")
        path = path.strip()
        if path and re.fullmatch(r"[0-9a-f]{64}", digest):
            out[path] = digest
    return out


def diverged(host: dict[str, str], materialised: dict[str, str]) -> dict[str, list[str]]:
    """Compare declared source against the copy the running system holds.

    Reports all three directions. `changed` is the instance that filed this item; `missing`
    is a file added to the repo that no rebuild has shipped; `unexpected` is a file deleted
    from the repo that the running copy still serves — the direction a
    "does every declared file exist?" presence check passes on while being wrong.
    """
    return {
        "changed": sorted(k for k in host.keys() & materialised.keys() if host[k] != materialised[k]),
        "missing": sorted(host.keys() - materialised.keys()),
        "unexpected": sorted(materialised.keys() - host.keys()),
    }
