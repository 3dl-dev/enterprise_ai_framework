"""The workshop must let its agent build with real engines and dependencies, not just a
single hand-written index.html.

A user asked for a 3D voxel game; the terminal agent reached for Godot and bounced, citing
"everything must be in index.html, there is no godot, there is no internet". The pod has
had internet since its first commit (tests/test_workspace_network_claims.py proves the text
must agree with that), so the wall was entirely the house rules and a missing engine path,
not the machine. This suite pins the corrected capability so the wall cannot quietly come
back:

  - the agent's house rules (AGENTS.md) must GRANT installing dependencies, using engines
    and frameworks, and multi-file/build-step projects — and must no longer carry the three
    prohibitions that made the agent refuse (one-file-only, never-install, never-serve);
  - the engine path ships as turnkey helpers (install-godot, godot-web-export) that are
    syntactically sound and pinned+checksummed like every other download in the image;
  - the image can actually run them (unzip present, helpers copied and executable, the
    on-PVC tools dir on PATH);
  - `publish` no longer caps a share below the size of a normal engine export.

The preview server's half of this — serving a .wasm build as application/wasm from
subfolders — is exercised live in tests/test_workspace_shell.py
(test_preview_serves_a_wasm_engine_build), against the running server rather than by
reading the source.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WS = REPO / "deploy" / "workspace"
AGENTS = WS / "AGENTS.md"
DOCKERFILE = WS / "Dockerfile"
INSTALL_GODOT = WS / "install-godot"
GODOT_WEB_EXPORT = WS / "godot-web-export"
PUBLISH = WS / "publish"

GODOT_VERSION = "4.7.1"

# The exact prohibitions that made the agent refuse a 3D game, verbatim from the seed as it
# stood before this work. If any of these sentences reappears in AGENTS.md, the roadblock is
# back — a starter the child clicks or a rule the agent reads would re-forbid the engine.
RETIRED_PROHIBITIONS = (
    "One file: `index.html`",
    "No build step, no bundler, no framework",
    "Never start a server",
    "Never run `npm install` or `pip install`",
)


def test_house_rules_grant_dependencies_and_engines():
    text = AGENTS.read_text()
    lower = text.lower()
    # Positively grants the three things the agent had been refusing.
    assert "install" in lower and "internet" in lower, \
        "AGENTS.md no longer tells the agent it may install things / has a network"
    assert "engine" in lower or "godot" in lower, \
        "AGENTS.md does not tell the agent an engine is an option"
    # Points the agent at the turnkey helpers, so it does not have to rediscover them.
    assert "install-godot" in text and "godot-web-export" in text, \
        "AGENTS.md should name the install-godot / godot-web-export helpers"


@pytest.mark.parametrize("phrase", RETIRED_PROHIBITIONS)
def test_house_rules_no_longer_carry_the_old_prohibition(phrase):
    assert phrase not in AGENTS.read_text(), (
        f"AGENTS.md has re-introduced {phrase!r} — that is one of the rules that made the "
        "agent refuse to build a 3D game with an engine. The roadblock is back."
    )


@pytest.mark.parametrize("script", [INSTALL_GODOT, GODOT_WEB_EXPORT, PUBLISH])
def test_helper_scripts_exist_and_parse(script):
    # The repo stores these non-executable by convention; the Dockerfile's `chmod 0755` is
    # the source of truth for the run bit (asserted in the Dockerfile test below), so what
    # matters here is that they exist and are valid bash.
    assert script.is_file(), f"{script} is missing"
    proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, f"{script} has a syntax error:\n{proc.stderr}"


def test_install_godot_is_pinned_and_checksummed():
    text = INSTALL_GODOT.read_text()
    assert GODOT_VERSION in text, "install-godot does not pin a Godot version"
    # Two SHA512 digests (binary + templates), each exactly 128 hex chars — the same
    # supply-chain discipline the Dockerfile uses for ttyd/opencode.
    digests = re.findall(r"\b[0-9a-f]{128}\b", text)
    assert len(digests) >= 2, "install-godot must pin SHA512 for the binary and templates"
    assert "sha512sum -c" in text, "install-godot must verify the downloads it fetches"


def test_godot_web_export_defaults_to_single_threaded():
    """Single-threaded (thread_support=false) is what runs inside the preview's sandboxed
    iframe and on an iPad: it needs no SharedArrayBuffer and therefore no cross-origin
    isolation. A threaded default would export a build that silently fails to start there.
    """
    text = GODOT_WEB_EXPORT.read_text()
    assert "variant/thread_support=false" in text
    assert "--export-release" in text and 'platform="Web"' in text


def test_dockerfile_can_run_the_engine_path():
    text = DOCKERFILE.read_text()
    # install-godot unzips the release .zip and the .tpz; without unzip it dies on line one.
    assert re.search(r"\bunzip\b", text), "Dockerfile must apt-install unzip for install-godot"
    for helper in ("install-godot", "godot-web-export"):
        assert helper in text, f"Dockerfile does not COPY {helper} into the image"
    # The run bit these files lack in the repo is granted here; without it they are a
    # "permission denied" for the child. Both must appear in a chmod 0755 invocation.
    chmod_targets = " ".join(
        m.group(0) for m in re.finditer(r"chmod 0755[\s\S]*?(?=\nRUN |\nCOPY |\nUSER |\Z)", text)
    )
    for helper in ("install-godot", "godot-web-export"):
        assert helper in chmod_targets, f"Dockerfile does not chmod 0755 {helper}"
    # The on-PVC tools dir must be on PATH so a hand-typed `godot` resolves after install,
    # including under kubectl exec (which never runs entrypoint.sh).
    assert "/workspace/.tools/bin" in text, \
        "Dockerfile must put the install-godot tools dir on PATH"


def test_publish_cap_fits_a_normal_engine_export():
    """A Godot/Unity web build is tens of MB of .wasm + data. The old 20MB (20480 KB) cap
    ruled that out; the share cap must sit above a normal engine export.
    """
    text = PUBLISH.read_text()
    m = re.search(r"SIZE_KB\s*>\s*(\d+)", text)
    assert m, "publish no longer has a SIZE_KB cap check"
    cap_kb = int(m.group(1))
    assert cap_kb > 20480, "publish still caps a share at the old 20MB, below an engine export"
    assert cap_kb >= 131072, f"publish cap is {cap_kb}KB — too small for a real engine export"
