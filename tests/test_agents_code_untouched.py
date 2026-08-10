"""The Code/workspace surface must be byte-identical to where the Agents epic started.

This is Contract 6 of docs/design/records/agents-surface.md, made mechanical. The camp
runs on the Code surface on 2026-08-11. Every item in the Agents epic — `-055` (this one,
which owns this test), `-627`, `-0e7`, `-39d`, `-914`, `-a4e`, `-ede` — is built from NEW
files that sit BESIDE the frozen set, and this test is what makes "we didn't touch it" a
measurement instead of a promise.

Why a pinned baseline commit and not `origin/main`: the design record specifies a pinned
`<PRE_AGENTS_BASELINE>`, and the reason is that `origin/main` moves. Once an Agents branch
merges, a diff against `origin/main` includes that merge — so the day somebody edits a
frozen file and merges it, the test starts comparing the damage against itself and goes
quiet forever. A pinned commit cannot do that.

The mechanism is `git diff --exit-code <baseline> -- <paths>` exactly as the record writes
it, plus one addition the record's command cannot cover on its own: `git diff` is blind to
UNTRACKED files, so a new file dropped into `deploy/workspace/` — which is a Docker build
context, so a new file there changes the image — would pass a diff-only check. Both halves
are asserted.
"""

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The commit at which the Agents epic began: the merge of the design record
# (docs/design/records/agents-surface.md) into main. Everything in the frozen set below
# was already at its camp-ready state here.
#
# THIS VALUE IS NOT A CACHE AND MUST NOT BE "REFRESHED" TO MAKE A FAILURE GO AWAY. If this
# test is red, the answer is to revert the edit to the frozen file, not to move the
# baseline forward — moving it forward is the same action as deleting the test.
PRE_AGENTS_BASELINE = "5942a5ccc3acea87b048a02d904cf33407718c6d"

# Exhaustive, quoted from Contract 6.
FROZEN = (
    "deploy/workspace",
    "deploy/k8s/60-workspace-common.yaml",
    "deploy/k8s/61-workspace.template.yaml",
    "deploy/bin/provision-workspace.sh",
    "tests/test_workspace_shell.py",
)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, timeout=120
    )


def _diff_against_baseline(paths) -> subprocess.CompletedProcess:
    """The record's command, verbatim, over whatever paths it is handed.

    Handed the frozen set it is the invariant. Handed a path that is known to have changed
    it is the fault injection — same code, same git, no mock of either.
    """
    return _git("diff", "--exit-code", PRE_AGENTS_BASELINE, "--", *paths)


def test_the_baseline_commit_is_present_in_this_checkout():
    """A missing baseline must be an error, never a silent pass.

    `git diff` against an unknown revision exits non-zero with an error on stderr, which
    looks exactly like a real violation and would be mistaken for one. Checked first and
    separately so the failure that reaches a human names the actual problem: a shallow
    clone, not a touched file.
    """
    proc = _git("cat-file", "-e", f"{PRE_AGENTS_BASELINE}^{{commit}}")
    assert proc.returncode == 0, (
        f"the pinned Agents baseline {PRE_AGENTS_BASELINE} is not in this checkout "
        f"({proc.stderr.strip()}). Fetch full history — a shallow clone cannot prove the "
        "Code surface is unchanged, and must not be allowed to look like it did."
    )


def test_the_code_surface_is_byte_identical_to_the_pre_agents_baseline():
    """Contract 6. Any diff at all, in any of the five paths, is the failure."""
    proc = _diff_against_baseline(FROZEN)
    assert proc.returncode == 0, (
        "the Code/workspace surface changed since the Agents epic began. The camp runs on "
        "this surface; Contract 6 of docs/design/records/agents-surface.md freezes it, and "
        "the Agents surface is built from new files beside it. Revert these hunks and put "
        "the change in a new file:\n\n"
        f"{proc.stdout}{proc.stderr}"
    )


def test_no_untracked_file_has_been_added_inside_the_frozen_paths():
    """The hole in a diff-only check.

    `deploy/workspace/` is a Docker build context. A new file there is a new file in the
    image, and `git diff <sha> -- deploy/workspace` will not mention it, because git
    diffing two trees never looks at anything untracked. Same class of hole for a new
    manifest dropped next to a frozen one.
    """
    proc = _git("ls-files", "--others", "--exclude-standard", "--", *FROZEN)
    stray = [line for line in proc.stdout.splitlines() if line.strip()]
    assert not stray, (
        f"untracked files inside the frozen Code surface paths: {stray}. "
        "deploy/workspace/ is a Docker build context, so a file added there changes the "
        "image the camp runs even though it never appears in a diff."
    )


def test_the_check_bites_when_a_frozen_file_is_touched():
    """Fault injection through the real path: a real byte, a real file, a real git diff.

    The invariant above passing is only worth something if it CAN come back red, and the
    only convincing demonstration of that is to break the actual thing it watches. So one
    comment line is appended to the real deploy/k8s/61-workspace.template.yaml on disk,
    the same check is re-run, and it must fail and must name that file. The original bytes
    are restored in `finally` and the check is asserted green again afterwards, so a crash
    in the middle cannot leave the camp's manifest dirty.

    Deliberately not done by diffing some path that happens to differ: that would prove
    `git diff` works, not that THIS path set is wired to THIS baseline. The wiring is what
    breaks — a typo'd path in FROZEN is silent, and silently watching nothing is the exact
    failure mode this whole test exists to prevent.
    """
    victim = REPO / "deploy/k8s/61-workspace.template.yaml"
    original = victim.read_bytes()
    try:
        victim.write_bytes(original + b"\n# fault injection: tests/test_agents_code_untouched.py\n")
        proc = _diff_against_baseline(FROZEN)
        assert proc.returncode != 0, (
            "a frozen file was edited on disk and the check still passed. It is watching "
            "nothing — check the paths in FROZEN and the baseline commit."
        )
        assert "61-workspace.template.yaml" in proc.stdout, (
            "the check went red but did not name the file that was touched, so a real "
            f"violation would not tell anyone what to revert:\n{proc.stdout}"
        )
    finally:
        victim.write_bytes(original)

    assert _diff_against_baseline(FROZEN).returncode == 0, (
        "the injected fault was not fully reverted — deploy/k8s/61-workspace.template.yaml "
        "is still dirty. Restore it from the baseline commit before doing anything else."
    )
