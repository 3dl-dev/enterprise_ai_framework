"""A declared artifact and its materialised copy must not be able to diverge unnoticed.

enterpriseaiframework-b51. Six instances of the same shape landed in one session: something
declared in git, something else materialising it where the running system reads it, and
nothing checking that the two still agree. Two are covered here — the rendered gateway
config, and the control-plane source the running container actually holds.

WHAT WAS MEASURED BEFORE WRITING THIS, on 7c8e59b with the bundle green at 160 passed:

  1. Stale render. config.generated.yaml edited down to two callbacks while
     config.base.yaml declared three (exactly the 3f3 merge), and config.base.yaml touched
     so it was newer. `pytest tests/test_gateway_callbacks.py` reported 9 PASSED. It did
     not merely fail to flag the staleness — it silently stopped checking
     flush_spend_on_shutdown at all, because it derives the list of modules to check FROM
     the stale file. 12 tests became 9 and the run was green.

  2. Block-style reformat. The same list rewritten as a YAML block sequence, semantically
     identical, produced `1 failed, 4 skipped`: the regex found nothing, "nothing" was
     read as "no callbacks are declared", and the drift trap was skipped rather than
     failed.

Both are now failures. The parse is a YAML load with a raise-on-unreadable contract, and
the suite refuses to collect at all against a stale render.

The unit tests below use tmp_path fixtures rather than the live bundle deliberately: the
conditions under test are "the render is wrong", and the only way to hold the real bundle
in that state is to break it. The one test that must see reality — the running container
holding the current source — talks to the real container and hashes real files.
"""

import subprocess
from pathlib import Path

import pytest

import artifact_freshness
from artifact_freshness import (
    CallbacksUnparseable,
    diverged,
    digest_tree,
    parse_callbacks,
    parse_sha256sum,
    stale_render_message,
    stale_render_reasons,
)
from conftest import BUNDLE, compose

REPO = Path(__file__).resolve().parent.parent
CONTROL_PLANE = REPO / "control-plane"

THREE = ["strip_reasoning.handler", "require_principal.handler", "flush_spend_on_shutdown.handler"]


def _config(callbacks, *, style="flow") -> str:
    """A minimal stand-in for the gateway config, in either YAML style.

    Only litellm_settings matters to these checks; the real files carry a 148-entry model
    catalogue that is irrelevant to whether the render is current. Both styles are
    generated from one list so a test cannot assert flow-style behaviour while believing
    it covered block style.
    """
    if style == "flow":
        rendered = "[" + ", ".join(f'"{c}"' for c in callbacks) + "]"
        body = f"  callbacks: {rendered}\n"
    elif style == "block":
        body = "  callbacks:\n" + "".join(f'    - "{c}"\n' for c in callbacks)
    else:
        raise AssertionError(style)
    return (
        "model_list:\n"
        "  - model_name: fake-large\n"
        "    litellm_params:\n"
        "      model: openai/fake-gpt-large\n"
        "litellm_settings:\n"
        + body
        + '  success_callback: ["postgres"]\n'
    )


def _pair(tmp_path: Path, base_callbacks, gen_callbacks, *, gen_style="flow", gen_newer=True):
    base = tmp_path / "config.base.yaml"
    gen = tmp_path / "config.generated.yaml"
    base.write_text(_config(base_callbacks))
    gen.write_text(_config(gen_callbacks, style=gen_style))
    # mtime is set explicitly rather than relying on write order: two writes in the same
    # filesystem timestamp tick would otherwise make gen_newer=True a coin flip.
    import os

    os.utime(base, (1_000_000, 1_000_000))
    os.utime(gen, (1_000_100, 1_000_100) if gen_newer else (999_900, 999_900))
    return base, gen


# ---------------------------------------------------------------------------
# parse_callbacks — the parse must not be defeatable by a reformat
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("style", ["flow", "block"])
def test_the_callbacks_list_reads_the_same_in_either_yaml_style(style):
    """The measured regression: block style used to return [] and skip the drift trap."""
    assert parse_callbacks(_config(THREE, style=style), origin="fixture") == [
        "flush_spend_on_shutdown",
        "require_principal",
        "strip_reasoning",
    ]


def test_an_unreadable_callbacks_list_raises_rather_than_reading_as_empty():
    """An empty result and an unreadable file are different claims.

    Conflating them is what turned four assertions into four skips. This is the
    negative case the fix exists for, so it is asserted directly rather than inferred.
    """
    with pytest.raises(CallbacksUnparseable) as exc:
        parse_callbacks("model_list:\n  - model_name: fake-large\n", origin="fixture")
    assert "fixture" in str(exc.value)


def test_deleting_the_callbacks_key_outright_raises_rather_than_reading_as_empty():
    """The realistic silent-disable: the key is gone, not empty.

    Distinguished from `callbacks: []` below on purpose. An empty list is a deliberate
    statement that there are no callbacks; a missing key is indistinguishable from a
    truncated write, a bad render, or a reorganised config, and treating it as "none
    declared" would turn every gateway-callback obligation off without a word.
    """
    text = (
        "litellm_settings:\n"
        '  success_callback: ["postgres"]\n'
        "  drop_params: true\n"
    )
    with pytest.raises(CallbacksUnparseable):
        parse_callbacks(text, origin="fixture")


def test_a_config_that_declares_no_callbacks_reads_as_no_callbacks():
    """The case NOT being changed: `callbacks: []` is legitimately empty, not unreadable.

    Without this, "raise when the list cannot be parsed" could be implemented as "raise
    whenever the list is empty", which would fail a config that has deliberately removed
    every callback.
    """
    assert parse_callbacks(_config([]), origin="fixture") == []


def test_a_bare_module_name_without_an_attribute_is_accepted():
    """litellm allows `callbacks: ["mymodule"]`; the module is still an obligation."""
    assert parse_callbacks(_config(["strip_reasoning"]), origin="fixture") == ["strip_reasoning"]


def test_the_generated_catalogue_shape_does_not_defeat_the_yaml_load():
    """A rendered catalogue must not make the parse fall back or fail.

    The generated file the red gates happened on is not the fake-only one in a fresh
    worktree — on a Forge-credentialed checkout it carries 148 machine-written entries with
    model ids like `deepseek-v3.2@deepinfra` and `glm-5.2:free`. Verified by rendering the
    real 148-model file from the cached Forge catalogue in a sandbox and loading it: 148
    entries, parsed by YAML with no regex fallback, reported current against its own base.
    That file cannot be a fixture here because the caches are gitignored, so the awkward
    shapes are pinned as a fixture instead.
    """
    catalogue = "".join(
        f"  - model_name: {mid}\n"
        "    litellm_params:\n"
        f"      model: openai/{mid}\n"
        "      api_base: https://forge.3dl.dev/v1\n"
        "      input_cost_per_token: 0.000000930000\n"
        for mid in ("deepseek-v3.2@deepinfra", "glm-5.2:free", "qwen3-235b-a22b-thinking-2507")
    )
    text = _config(THREE).replace(
        "  - model_name: fake-large\n"
        "    litellm_params:\n"
        "      model: openai/fake-gpt-large\n",
        catalogue,
    )
    import yaml

    assert len(yaml.safe_load(text)["model_list"]) == 3, "fixture no longer loads as YAML"
    assert parse_callbacks(text, origin="fixture") == [
        "flush_spend_on_shutdown",
        "require_principal",
        "strip_reasoning",
    ]


def test_the_real_base_config_parses_and_names_callbacks():
    """Guard the guard against the fixtures having drifted from the real file's shape.

    Every check above runs on a hand-written fixture. If the real config.base.yaml stopped
    being parseable — a templating marker, a new nesting — the fixtures would keep passing
    while the live check silently found nothing. There is no skip here: this file exists
    and is in git.
    """
    got = parse_callbacks(
        (BUNDLE / "litellm" / "config.base.yaml").read_text(), origin="config.base.yaml"
    )
    assert "strip_reasoning" in got, got


# ---------------------------------------------------------------------------
# stale_render_reasons — the render must not be able to lag the base unnoticed
# ---------------------------------------------------------------------------

def test_a_current_render_is_reported_as_current(tmp_path):
    """The unchanged path. A check that fires on a good render is worse than no check."""
    base, gen = _pair(tmp_path, THREE, THREE)
    assert stale_render_reasons(base, gen) == []


def test_a_render_missing_a_callback_the_base_declares_is_stale(tmp_path):
    """The d98 and 3f3 merges, exactly: base gained a callback, the render did not.

    gen_newer=True is the load-bearing part — this must be caught by CONTENT even when the
    timestamps look fine, because a merge is not the only way to get here.
    """
    base, gen = _pair(tmp_path, THREE, THREE[:2], gen_newer=True)
    reasons = stale_render_reasons(base, gen)
    assert reasons, "a render two callbacks short of its base was reported as current"
    assert any("flush_spend_on_shutdown" in r for r in reasons), reasons


def test_a_render_still_loading_a_removed_callback_is_stale(tmp_path):
    """The direction I did not change: the base REMOVED a callback and the render kept it.

    A one-way "is everything declared also present?" check passes here while the gateway
    goes on importing a module the configuration no longer wants — and if the module file
    was deleted with it, the proxy crash-loops on next restart.
    """
    base, gen = _pair(tmp_path, THREE[:2], THREE, gen_newer=True)
    reasons = stale_render_reasons(base, gen)
    assert reasons, "a render loading a callback the base dropped was reported as current"
    assert any("flush_spend_on_shutdown" in r for r in reasons), reasons


def test_a_render_older_than_its_base_is_stale_even_with_matching_callbacks(tmp_path):
    """A base edit that does not touch callbacks still invalidates the render.

    Model entries, cache settings and general_settings all pass through the same file.
    Without the mtime half, changing any of them would leave the suite testing the old
    gateway with nothing to notice.
    """
    base, gen = _pair(tmp_path, THREE, THREE, gen_newer=False)
    reasons = stale_render_reasons(base, gen)
    assert reasons, "a render older than its base was reported as current"
    assert any("OLDER" in r for r in reasons), reasons


def test_a_block_style_render_is_not_mistaken_for_a_stale_one(tmp_path):
    """Reformatting must not be reported as staleness either — the check reads YAML.

    The failure this rules out: implementing the content half with the old inline-flow
    regex would report every callback as "missing from the render" the moment somebody
    reformatted the list, sending an operator to `make up` forever.
    """
    base, gen = _pair(tmp_path, THREE, THREE, gen_style="block")
    assert stale_render_reasons(base, gen) == []


def test_a_missing_render_is_stale(tmp_path):
    base = tmp_path / "config.base.yaml"
    base.write_text(_config(THREE))
    reasons = stale_render_reasons(base, tmp_path / "config.generated.yaml")
    assert reasons and "does not exist" in reasons[0], reasons


def test_the_staleness_message_says_run_make_up(tmp_path):
    """The whole point of the item: the operator reads `make up`, not an assertion.

    Six instances cost more in diagnosis than in fix because each surfaced as an unrelated
    failure deep in a test. The wording is therefore part of the fix, not decoration.
    """
    base, gen = _pair(tmp_path, THREE, THREE[:2])
    message = stale_render_message(base, gen, stale_render_reasons(base, gen))
    assert "make up" in message
    assert "STALE" in message
    assert "flush_spend_on_shutdown" in message


def test_the_live_bundle_render_is_current():
    """The invariant on the real files, asserted where a reader will look for it.

    tests/conftest.py already refuses to collect when this is false, so in a stale checkout
    the suite never reaches this line. It is here so the invariant is visible as a test
    rather than only as a precondition, and so it fails rather than passing vacuously if
    that precondition is ever loosened to a warning.
    """
    assert stale_render_reasons(
        BUNDLE / "litellm" / "config.base.yaml",
        BUNDLE / "litellm" / "config.generated.yaml",
    ) == []


# ---------------------------------------------------------------------------
# the running container must hold the source that is in git
# ---------------------------------------------------------------------------

def test_digest_tree_covers_the_image_build_inputs_and_nothing_else():
    """What is hashed is what the Dockerfile COPYs — checked against the Dockerfile.

    If a COPY line is added, this fails and forces the input list to be updated, rather
    than the freshness check quietly stopping short of a new build input.
    """
    dockerfile = (CONTROL_PLANE / "Dockerfile").read_text()
    copied = [
        ln.split()[1]
        for ln in dockerfile.splitlines()
        if ln.strip().upper().startswith("COPY ")
    ]
    assert sorted(copied) == sorted(artifact_freshness.CONTROL_PLANE_IMAGE_INPUTS), (
        f"control-plane/Dockerfile COPYs {sorted(copied)} but the freshness check watches "
        f"{sorted(artifact_freshness.CONTROL_PLANE_IMAGE_INPUTS)}. A build input nobody "
        f"watches is a stale image nobody notices."
    )

    digests = digest_tree(CONTROL_PLANE)
    assert "app/main.py" in digests
    assert "requirements.txt" in digests
    assert not [p for p in digests if p.startswith("tests/")], (
        "control-plane/tests/ is not COPYed into the image, so hashing it would demand a "
        "rebuild for a test-only edit"
    )
    assert not [p for p in digests if "__pycache__" in p or p.endswith(".pyc")]


def test_diverged_reports_all_three_directions():
    """Including the two a presence check passes on."""
    host = {"a": "1", "b": "2", "c": "3"}
    running = {"a": "1", "b": "CHANGED", "d": "4"}
    assert diverged(host, running) == {
        "changed": ["b"],
        "missing": ["c"],
        "unexpected": ["d"],
    }
    assert diverged(host, host) == {"changed": [], "missing": [], "unexpected": []}


def test_parse_sha256sum_ignores_anything_that_is_not_a_digest_line():
    """A warning on stdout must not silently shrink the set of files compared."""
    zeros = "0" * 64
    got = parse_sha256sum(
        "warning: something\n"
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  app/__init__.py\n"
        "\n"
        f"{zeros}  requirements.txt\n"
    )
    assert got == {
        "app/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "requirements.txt": "0" * 64,
    }


def test_the_running_control_plane_holds_the_source_that_is_in_git(stack_up):
    """Ground truth for the fifth instance: the image ran 25 minutes behind its source.

    The 37a merge went in correct and the gate came back red on 37a's own test, because
    only `make up` rebuilds (`up -d --build`) and `make test` runs against whatever
    containers are already up. The image had been built at 19:00:23 from source last
    changed at 19:25:16, so the new export code was simply not running and the failure
    presented as an assertion about spend attribution.

    Content is compared rather than the image's created timestamp against the newest source
    mtime. Two reasons, both concrete. Docker's build cache is content-keyed, so editing a
    file and reverting it leaves the mtime newer than an image `make up` will correctly
    decline to rebuild — a timestamp check would then demand a rebuild that changes
    nothing, forever. And hashing catches what a timestamp cannot: an image rebuilt but the
    container never recreated from it.
    """
    cid = compose("ps", "-q", "control-plane").stdout.strip()
    assert cid, "control-plane container is not running"

    probe = subprocess.run(
        ["docker", "exec", cid, "sh", "-c",
         "cd /srv && find app requirements.txt -type f ! -path '*__pycache__*' "
         "| LC_ALL=C sort | xargs sha256sum"],
        capture_output=True, text=True,
    )
    assert probe.returncode == 0, probe.stderr
    running = parse_sha256sum(probe.stdout)
    assert running, f"no digests read from the control-plane container: {probe.stdout!r}"

    host = digest_tree(CONTROL_PLANE)
    assert host, "no build inputs found under control-plane/"

    delta = diverged(host, running)
    assert delta == {"changed": [], "missing": [], "unexpected": []}, (
        "\nRUN `make up` — the running control-plane does NOT hold the source in git.\n"
        f"  changed in git, old copy still running: {delta['changed']}\n"
        f"  in git, never shipped into the image:   {delta['missing']}\n"
        f"  gone from git, still in the container:  {delta['unexpected']}\n\n"
        "Only `make up` rebuilds (`up -d --build`); `make test` runs against whatever is "
        "already up. Without this the suite reports on code that is not executing, which "
        "is how the 37a merge failed its own integration test.\n\n"
        "  env -u FORGE_API_KEY -u FORGE_ADMIN_KEY make up\n"
    )
