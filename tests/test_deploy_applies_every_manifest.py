"""A manifest that exists must be deployed, or be excluded on purpose and say why.

This is a drift trap, and it already cost the product two shipped-but-invisible features.

`deploy.sh` used to apply a hand-written list of five filenames while `deploy/k8s/` held
sixteen. Nothing enforced the correspondence, so the list fell behind the directory in the
quietest possible way: `51-file-search.yaml` (the rag-api behind document Q&A) and
`70-codeapi.yaml` (the sandbox behind code execution) were built, adversarially tested,
merged and closed — and never once reached the cluster. Every signal said done. The deploy
exited 0, every pod it knew about was Running, and the two capabilities simply were not
there. Measured 2026-08-01: the live chat Deployment was four days stale and carried none
of `SEARCH`, `MEILI_HOST`, `RAG_API_URL` or `ALLOW_SHARED_LINKS_PUBLIC`, all of which had
been correct in `deploy/k8s/50-chat.yaml` for days.

The fix is that the loop reads the directory, so shipping a manifest is sufficient to
deploy it. This test guards the property that made the old shape dangerous: that a file
could be left out *silently*. Skipping is still allowed — some manifests genuinely must not
be applied automatically — but only by name, and only with a stated reason. An exclusion is
a decision. An omission was an accident.

None of this asserts the manifests are *correct*; it asserts none of them is forgotten.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEPLOY_SH = REPO / "deploy" / "bin" / "deploy.sh"
K8S_DIR = REPO / "deploy" / "k8s"

# Kept deliberately small and spelled out here as well as in the script, so that widening it
# requires touching a test and explaining yourself, rather than editing one `case` arm.
EXPECTED_SKIPS = {
    "00-namespace.yaml",
    "01-tank-pvs.yaml",
    "61-workspace.template.yaml",
}


def _skip_block() -> str:
    """The body of deploy.sh's skip_manifest(), which is the allowlist of exclusions."""
    body = DEPLOY_SH.read_text()
    start = body.index("skip_manifest()")
    end = body.index("\n}", start)
    return body[start:end]


def _skipped_filenames() -> set[str]:
    return set(re.findall(r"^\s*([0-9]{2}-[a-z0-9.-]+\.yaml)\)", _skip_block(), re.M))


def test_the_apply_loop_reads_the_directory_rather_than_a_hand_written_list():
    """The regression itself: a list of filenames is what fell behind.

    A loop over `deploy/k8s/*.yaml` cannot drift, because adding a manifest is the same
    action as deploying it. A hand-written list can, and did, for two whole features.
    """
    body = DEPLOY_SH.read_text()
    assert "for path in deploy/k8s/*.yaml; do" in body, (
        "deploy.sh no longer iterates deploy/k8s/*.yaml. If the apply loop has gone back to "
        "naming files, every future manifest has to remember to add itself here — which is "
        "exactly how 51-file-search.yaml and 70-codeapi.yaml were merged but never deployed."
    )


def test_every_manifest_is_either_applied_or_skipped_by_name():
    """No manifest may be missing by accident — only by an exclusion that names it."""
    on_disk = {p.name for p in K8S_DIR.glob("*.yaml")}
    skipped = _skipped_filenames()

    unknown = skipped - on_disk
    assert not unknown, (
        f"deploy.sh skips {sorted(unknown)}, which no longer exist in deploy/k8s/. A stale "
        "exclusion silently becomes a trap the day someone adds a file back under that name."
    )
    assert skipped == EXPECTED_SKIPS, (
        f"the set of manifests deploy.sh refuses to apply changed: {sorted(skipped)} vs the "
        f"expected {sorted(EXPECTED_SKIPS)}. Every exclusion keeps working code off the "
        "cluster, so it needs a reason in the script and a deliberate update here."
    )
    # Everything else is applied purely by virtue of existing — which is the point.
    assert on_disk - skipped, "deploy/k8s/ has no appliable manifests at all"


def test_each_exclusion_states_why():
    """A `case` arm with no comment is indistinguishable from an oversight six months on."""
    block = _skip_block()
    for name in sorted(EXPECTED_SKIPS):
        arm = block.index(f"{name})")
        preceding = block[:arm]
        # The comment lines immediately above the arm, back to the previous arm or the top.
        last_arm = max(preceding.rfind(";;"), preceding.rfind("case "))
        reason = preceding[last_arm:]
        assert "#" in reason, (
            f"deploy.sh skips {name} with no comment explaining why. An undocumented "
            "exclusion reads as an accident, and the next person restores it or works "
            "around it instead of understanding it."
        )


def test_the_two_manifests_that_were_silently_missing_are_deployed_now():
    """The specific regression, pinned by name.

    These are the ones it actually happened to. If either reappears in the skip list, the
    feature behind it is off the cluster again and the item that closed it is a lie.
    """
    skipped = _skipped_filenames()
    for name, feature in (
        ("51-file-search.yaml", "file search / document Q&A (rag-api)"),
        ("70-codeapi.yaml", "code execution (the sandbox chat advertises)"),
    ):
        assert (K8S_DIR / name).exists(), f"{name} vanished; {feature} has no manifest"
        assert name not in skipped, (
            f"{name} is excluded from the deploy again, so {feature} is merged but not "
            "running. That is the exact defect this test exists to prevent."
        )


def test_a_failed_rollout_fails_the_deploy():
    """`|| true` on rollout status let a deploy exit 0 over a pod that never came up."""
    body = DEPLOY_SH.read_text()
    assert "rollout status" in body
    assert not re.search(r"rollout status[^\n]*\|\|\s*true", body), (
        "a `kubectl rollout status ... || true` is back in deploy.sh. That swallows failed "
        "rollouts, so the deploy reports success over a product that is down — the same "
        "class as enterpriseaiframework-0e97, where every signal said success and the first "
        "prompt returned 401."
    )
    assert "exit 1" in body.split("waiting for rollout")[1], (
        "nothing after the rollout wait exits non-zero, so a failed rollout cannot fail the "
        "deploy no matter what it prints"
    )


def test_the_deploy_runs_post_deploy():
    """enterpriseaiframework-0e97: the guard existed and was simply never invoked."""
    body = DEPLOY_SH.read_text()
    assert re.search(r"^(?!\s*#).*deploy/bin/post-deploy\.sh", body, re.M), (
        "deploy.sh does not invoke deploy/bin/post-deploy.sh. Its virtual-key reconciliation "
        "is what stands between a green deploy and a surface that 401s on the first prompt — "
        "which is how this was found, in production, by the founder."
    )
