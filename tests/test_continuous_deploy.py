"""The watcher must not be able to deploy on a false green.

Continuous deployment is only as trustworthy as the suite run that gates it, and this
session established — expensively — that a suite run on this host can go green or red for
reasons that have nothing to do with the commit under test. A watcher that inherits those
traps is worse than no watcher: it converts an environmental artefact into a production
deploy, automatically, at 3am, with nobody watching.

Each test here pins one trap that was actually paid for:

  7bb  the catalogue has two modes and one gitignored file. FORGE_API_KEY is ambient, so an
       unguarded render writes the real 148-model catalogue, and every later hermetic run
       then fails every chat turn on `illegal_model_request: fake-large` after burning a
       180s timeout each. Measured: a 6-minute suite became 18m39s and "failed" 6 tests on a
       commit that was green.
  af5  `make up` does not restart chat for a librechat.yaml change, so the suite can test a
       pre-checkout surface — green or red for the wrong reason.
  25f  the host has hit 98-99% disk three times; an ENOSPC mid-run presents as unrelated
       test failures, not as "out of disk".

These are static checks on the script. They cannot prove the watcher works — only running it
does that — but they prove the guards have not been deleted, which is the realistic
regression: every one of them looks like a redundant precondition to someone in a hurry.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WATCHER = REPO / "deploy" / "bin" / "watch-and-deploy.sh"


def body() -> str:
    return WATCHER.read_text()


def test_the_watcher_exists_and_runs():
    assert WATCHER.exists(), "deploy/bin/watch-and-deploy.sh is missing"
    assert WATCHER.stat().st_mode & 0o111, "watch-and-deploy.sh is not executable"


def test_it_tests_on_fakes_and_deploys_on_the_real_catalogue_in_that_order():
    """Render fakes -> test -> render real -> deploy. Any other order deploys a lie."""
    b = body()
    fakes = b.index("rendering fakes-only catalogue")
    tested = b.index("running the full suite")
    real = b.index("rendering the production catalogue")
    deployed = b.rindex("deploy/bin/deploy.sh")
    assert fakes < tested < real < deployed, (
        "the watcher's render/test/render/deploy order is wrong. Testing against the real "
        "catalogue fails every chat turn (7bb); deploying the fakes-only one would replace "
        "the production catalogue with stubs."
    )


def test_it_verifies_the_fakes_catalogue_rather_than_trusting_the_render():
    """`env -u FORGE_API_KEY` on the command is not proof the file on disk is fakes-only."""
    b = body()
    # The check itself, not the mention of it in the header comment.
    check = re.search(
        r"grep -q 'model_name: fake-large'[^\n]*\n?[^\n]*", b
    )
    assert check and "give_up" in check.group(0), (
        "the watcher does not verify fake-large is present after rendering. The whole 7bb "
        "lesson is that stripping the key from your own command says nothing about what a "
        "previous process wrote into the shared, gitignored catalogue."
    )


def test_it_refuses_to_deploy_a_fakes_only_catalogue_to_production():
    b = body()
    assert re.search(r"real_entries.*>\s*10", b) or re.search(r"\(\(\s*real_entries\s*>", b), (
        "nothing stops the watcher deploying a fakes-only catalogue to the cluster, which "
        "would replace the real models with stubs for every user"
    )


def test_it_force_recreates_chat_before_testing():
    """af5: make up leaves LibreChat on the pre-checkout config."""
    b = body()
    assert "--force-recreate chat" in b, (
        "the watcher does not force-recreate chat, so the suite it gates on can be testing a "
        "librechat.yaml that is not the one in the commit"
    )
    assert b.index("--force-recreate chat") < b.index("running the full suite")


def test_it_has_a_disk_floor_and_a_lock_and_a_wave_check():
    b = body()
    # The comparison, not merely the variable name — a floor that is never compared against
    # is decoration, and the name survives in the error string even when the guard is gone.
    assert re.search(r"free_gb\s*>=\s*MIN_FREE_GB", b), (
        "no enforced disk floor; an ENOSPC mid-run does not present as 'out of disk', it "
        "presents as unrelated test failures (25f), which a watcher would read as a red suite"
    )
    assert re.search(r"free_gb=\$\(df", b), "the floor is never measured against actual free space"
    assert "flock" in b, "no lock; two overlapping runs would fight over the same stack"
    assert "worktrees/wf_" in b, (
        "no check for a dispatch wave; agents drive the same compose stack, and a suite run "
        "during a wave produced 15 failures on a commit that was green"
    )


def test_it_records_what_it_deployed():
    b = body()
    assert "last-deployed-sha" in b, "without recording the deployed SHA the watcher redeploys forever"
    written = b.rindex('> "$STATE"')
    assert written > b.index("deploy/bin/deploy.sh"), (
        "the deployed SHA is recorded before the deploy succeeds, so a failed deploy would "
        "be remembered as done and never retried"
    )


def test_a_red_suite_does_not_deploy():
    b = body()
    red = b.index("SUITE RED")
    nxt = b.index("rendering the production catalogue")
    assert "exit 1" in b[red:nxt], "a red suite does not stop the watcher reaching the deploy"
