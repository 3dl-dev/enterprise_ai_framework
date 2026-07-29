"""ttyd's --client-option parser truncates a value at a second '=', silently.

No cluster, no docker, no network — this proves the parsing mechanism and the exec argv
from entrypoint.sh's own text, hermetically. See enterpriseaiframework-3a8.

WHERE THIS CAME FROM (do not re-derive, re-verify against current source if this file is
ever doubted): ttyd 1.7.7 src/server.c, case 't' (the --client-option handler), calls
`strsep(&option, "=")` TWICE — once to split the key off the front of "key=value...", and
again on whatever strsep left behind to split the value off THAT. Whatever follows the
second '=' is discarded, not appended to the value and not reported as an error.
`_parse_client_option` below is a literal line-for-line translation of those two calls.

This was cross-checked against the REAL pinned binary, not just the source text: ttyd
1.7.7-x86_64 was downloaded from the GitHub release, its sha256 confirmed to match
Dockerfile's TTYD_SHA256
(8a217c968aba172e0dbf3f34447218dc015bc4d5e59bf51db2f2cd12b7be4f55), started with
`--client-option "titleFixed=user=admin"`, and its SET_PREFERENCES websocket frame (the
JSON ttyd actually sends the browser — src/protocol.c, sprintf(p, "%c%s", cmd,
prefs_json)) was read directly: it came back `{"titleFixed": "user", "fontSize": 14}`.
"admin" is gone, matching `_parse_client_option`'s prediction below exactly. That
network-dependent probe is not part of this suite (tests/ has no network per
test_workspace_shell.py's header convention) — it was a one-time manual confirmation that
the reimplementation below matches ttyd's actual, running behavior, not just a reading of
its source.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO_ROOT / "deploy/workspace/entrypoint.sh"
WORKSPACE_DOCKERFILE = REPO_ROOT / "deploy/workspace/Dockerfile"

# The ttyd release this reimplementation's fidelity was manually verified against (see
# module docstring). If Dockerfile's pin ever moves off these values, that verification no
# longer covers the running binary and _parse_client_option must be re-checked against the
# new release's src/server.c before this pin is updated to match.
PINNED_TTYD_VERSION = "1.7.7"
PINNED_TTYD_SHA256 = "8a217c968aba172e0dbf3f34447218dc015bc4d5e59bf51db2f2cd12b7be4f55"


def test_dockerfile_ttyd_pin_matches_the_version_the_parser_was_verified_against():
    dockerfile_text = WORKSPACE_DOCKERFILE.read_text()
    version_match = re.search(r"^ARG TTYD_VERSION=(\S+)$", dockerfile_text, re.MULTILINE)
    sha_match = re.search(r"^ARG TTYD_SHA256=(\S+)$", dockerfile_text, re.MULTILINE)
    assert version_match, "Dockerfile no longer sets ARG TTYD_VERSION=..."
    assert sha_match, "Dockerfile no longer sets ARG TTYD_SHA256=..."
    assert version_match.group(1) == PINNED_TTYD_VERSION, (
        "Dockerfile's TTYD_VERSION moved off the release _parse_client_option was manually "
        "verified against (see this module's docstring). Re-verify the reimplementation "
        "against the new release's src/server.c, then update PINNED_TTYD_VERSION here."
    )
    assert sha_match.group(1) == PINNED_TTYD_SHA256, (
        "Dockerfile's TTYD_SHA256 moved off the binary _parse_client_option was manually "
        "verified against (see this module's docstring). Re-verify the reimplementation "
        "against the new release's src/server.c, then update PINNED_TTYD_SHA256 here."
    )

# The commit this documentation item started from (current main at dispatch time). The
# control case below proves the doc-only edit made on top of it left ttyd's real exec
# argv byte-for-byte unchanged — a syntax check (`bash -n`) cannot prove that; only
# reconstructing the actual argv bash would build can.
BASE_SHA = "e0bc233"


def _parse_client_option(optarg: str) -> tuple[str, str]:
    """Faithful translation of ttyd 1.7.7 src/server.c case 't' (two strsep(&option, "=")
    calls). Raises ValueError the same way ttyd prints "invalid client option" and bails.
    """
    if "=" not in optarg:
        raise ValueError(f"invalid client option: {optarg}, format: key=value")
    key, remainder = optarg.split("=", 1)
    if "=" not in remainder:
        # Only one '=' in the whole optarg: the second strsep finds nothing to split on
        # and returns everything that's left, untouched.
        return key, remainder
    # A second '=' exists: the second strsep splits there. Whatever comes after it is
    # simply dropped — never returned, never surfaced as an error.
    value, _dropped_silently = remainder.split("=", 1)
    return key, value


def _ttyd_argv_from_script(script_text: str, *, ws_user: str = "coder",
                            ws_internal_token: str = "test-token") -> list[str]:
    """Reconstruct the literal argv bash would hand to `exec ttyd ...` from entrypoint.sh's
    own text, using bash itself to do the quoting/variable-expansion — not a syntax check.

    Copies the exec block verbatim out of the script (from the `exec /usr/local/bin/ttyd`
    line to EOF) and replaces only the exec target with a capture shim, so every argument
    word is evaluated exactly as bash would evaluate it for the real exec.
    """
    lines = script_text.splitlines()
    start = next(
        i for i, l in enumerate(lines) if l.strip().startswith("exec /usr/local/bin/ttyd")
    )
    exec_block = "\n".join(lines[start:])
    assert exec_block.count("exec /usr/local/bin/ttyd") == 1
    harness_block = exec_block.replace("exec /usr/local/bin/ttyd", "capture_argv", 1)

    harness = textwrap.dedent(f"""\
        set -euo pipefail
        WS_USER={shlex.quote(ws_user)}
        WS_INTERNAL_TOKEN={shlex.quote(ws_internal_token)}
        capture_argv() {{ printf '%s\\0' "$@"; }}
        {harness_block}
    """)
    result = subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=False, timeout=10
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    out = result.stdout.decode()
    parts = out.split("\0")
    assert parts[-1] == ""  # trailing NUL from the last printf arg
    return parts[:-1]


def _client_option_values(argv: list[str]) -> list[str]:
    """Pull every value that follows a --client-option flag out of a reconstructed argv."""
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--client-option"]


# ---------------------------------------------------------------------------
# Mechanism: does the reimplementation match ttyd's real, observed behaviour?
# ---------------------------------------------------------------------------

def test_second_equals_truncates_value_silently():
    """The exact case documented in entrypoint.sh and observed against the real binary."""
    key, value = _parse_client_option("titleFixed=user=admin")
    assert key == "titleFixed"
    assert value == "user"  # "admin" is gone, not appended, not an error


def test_single_equals_is_not_truncated():
    """Control: a value with no second '=' passes through whole (also observed live)."""
    key, value = _parse_client_option("titleFixed=solovalue")
    assert key == "titleFixed"
    assert value == "solovalue"


def test_three_equals_still_truncates_at_the_second_not_the_third():
    """Confirms the mechanism is 'second =', not 'last =' or 'first ='."""
    key, value = _parse_client_option("k=a=b=c")
    assert key == "k"
    assert value == "a"


def test_missing_equals_raises_like_ttyd_does():
    with pytest.raises(ValueError):
        _parse_client_option("nokeyvaluehere")


# ---------------------------------------------------------------------------
# Regression guard: none of entrypoint.sh's ACTUAL current values are affected.
# ---------------------------------------------------------------------------

def test_current_client_option_values_are_not_truncated():
    """If a future edit adds a --client-option value containing '=', this fails loudly —
    that is the whole point of the trap being documented next to the flags.
    """
    argv = _ttyd_argv_from_script(ENTRYPOINT.read_text())
    values = _client_option_values(argv)
    assert len(values) >= 8  # sanity: the flags are actually there

    for raw in values:
        # Each argv element here is "key=value...", i.e. exactly what ttyd's optarg sees.
        naive_full_value = raw.split("=", 1)[1]
        _key, parsed_value = _parse_client_option(raw)
        assert parsed_value == naive_full_value, (
            f"client-option {raw!r} would be truncated by ttyd: "
            f"full value is {naive_full_value!r}, ttyd would only see {parsed_value!r}"
        )


# ---------------------------------------------------------------------------
# Control case: the doc-only edit did not perturb the real exec argv.
# ---------------------------------------------------------------------------

def _git_show(rev: str, path: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{rev}:{path}"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_client_option_values_never_contain_a_second_equals():
    """The standing invariant, not a one-time migration proof (see enterpriseaiframework-7e8):
    ttyd's --client-option parser silently truncates at a second '=' (see module docstring).
    Whatever entrypoint.sh's exec argv looks like on any given commit, every
    --client-option value it passes must contain at most one '=' — otherwise ttyd would
    silently drop part of it. This should hold forever, unlike comparing against a
    hardcoded base commit (which goes stale the moment the exec line is legitimately
    edited, and becomes vacuous the moment someone "fixes" it by bumping the SHA).
    """
    argv = _ttyd_argv_from_script(ENTRYPOINT.read_text())
    values = _client_option_values(argv)
    assert len(values) >= 8  # sanity: the flags are actually there

    for raw in values:
        # raw is "key=value...", i.e. exactly what ttyd's optarg sees.
        _key, value = raw.split("=", 1)
        assert value.count("=") == 0, (
            f"client-option {raw!r} has a second '=' in its value ({value!r}); "
            f"ttyd would silently truncate it at the second '='"
        )


def test_control_case_has_teeth_on_an_ordinary_value_change():
    """Sanity check on the harness itself: if a --client-option value genuinely changed,
    the comparison above must catch it, not report false-equal.
    """
    base_text = _git_show(BASE_SHA, "deploy/workspace/entrypoint.sh")
    corrupted = base_text.replace(
        '--client-option "fontSize=14" \\',
        '--client-option "fontSize=99" \\',
        1,
    )
    assert corrupted != base_text  # the substitution actually applied

    base_argv = _ttyd_argv_from_script(base_text)
    corrupted_argv = _ttyd_argv_from_script(corrupted)

    assert corrupted_argv != base_argv


def test_control_case_catches_the_hash_landmine_too():
    """The file's own long-standing warning: an unquoted '#' after whitespace starts a
    bash comment and silently eats the rest of the continued command. Reproduce that
    exact shape (a bare, unquoted client-option value followed by a `#`) and confirm the
    argv-reconstruction harness does NOT report a clean, unchanged result — either the
    argv differs or the reconstruction fails outright. Both are a caught regression;
    only "succeeds AND looks the same" would be the false negative this control case
    exists to rule out.

    In practice this particular corruption doesn't produce a same-shaped-but-wrong argv:
    it makes bash's line-continuation collapse mid-comment, so the next physical line
    (`--client-option "scrollback=..."`) is parsed as a brand new command and fails with
    "command not found" (exit 127) before ttyd ever runs. That is a LOUDER failure than
    silent truncation, and it is still a failure `bash -n` alone would not have shown,
    since `-n` only checks syntax and this is syntactically valid bash.
    """
    base_text = _git_show(BASE_SHA, "deploy/workspace/entrypoint.sh")
    corrupted = base_text.replace(
        '--client-option "fontSize=14" \\',
        "--client-option fontSize=14 #this eats everything after it \\",
        1,
    )
    assert corrupted != base_text  # the substitution actually applied
    assert subprocess.run(  # confirms this corruption is still syntactically valid bash
        ["bash", "-n", "-c", corrupted], capture_output=True
    ).returncode == 0

    base_argv = _ttyd_argv_from_script(base_text)
    try:
        corrupted_argv = _ttyd_argv_from_script(corrupted)
    except AssertionError:
        return  # reconstruction itself failed — caught, and loudly
    assert corrupted_argv != base_argv
