"""The default chat model must not carry a standing code-only bias, in either of two forms.

PART 1 (enterpriseaiframework-8b5): a promptPrefix that resolves ambiguity toward "build a
program". FOUND IN USE: a user asked chat for a short story, got a long one, and replied
with a typo ("make it a lot sorter" for "a lot shorter"). The model built and rendered an
HTML CAR LOT SORTER instead. Nobody asked for a program. The cause was
`modelSpecs.list[].preset.promptPrefix` in librechat.yaml: ~10 lines of unconditional,
every-turn context, every line about emitting runnable HTML, ending in a worked
`<!DOCTYPE html>` example, with nothing about prose. The only capability the model had been
explicitly told about was building a web app, so an ambiguous message resolved toward that
prior. That promptPrefix was also redundant on both of the two things it tried to do:

- WHEN to render something as an artifact: `artifacts: "default"` on each preset already
  makes LibreChat inject its own balanced artifact system prompt
  (api/app/clients/prompts/artifacts.js, generateArtifactsPrompt) -- which explicitly covers
  when NOT to use one ("prefer in-line content", "err on the side of not creating an
  artifact") in a way the removed prompt never did.
- HOW to open the fence: the removed prompt's other job was drilling GLM into using three
  colons instead of the two it actually emits. deploy/gateway/strip_reasoning.py's
  `_OPEN_FENCE` regex already rewrites `:{1,4}artifact{` to `:::artifact{` deterministically,
  on both the streaming and non-streaming paths, for every surface and every model (24fe929)
  -- whose own commit message records that prompting the fence was tried first, twice, and
  did not hold.

Removing the promptPrefix (8b5) was correct and necessary, but real-model measurement
showed it did NOT resolve the reported behaviour -- see PART 2.

PART 2 (enterpriseaiframework-52a): `preset.artifacts` turned off for the default model.
MEASURED, not assumed: called the real glm-5.2@deepinfra model directly via Forge,
reconstructing LibreChat's exact system prompt for both the promptPrefix-present and
promptPrefix-removed configs. Natural two-turn trials, n=5 each (fresh "write me a short
story...", then the literal reported typo "make it a lot sorter"):

  promptPrefix present (old prod): fresh story fenced as HTML 2/5. follow-up 0/5.
  promptPrefix removed (8b5's fix): fresh story fenced as HTML 4/5. follow-up 3/5.

Fisher's exact on both comparisons: p=0.52 and p=0.17 -- neither direction is significant
at n=5, so removing promptPrefix did not fix it: BOTH configs let glm-5.2 wrap a plain
prose request in HTML at a 2/5-4/5 rate, driven by LibreChat's own stock artifactsPrompt
interacting with glm-5.2's own tendency, not by our custom prompt in either direction.

ORCHESTRATOR RULING (founder-approved): turn `preset.artifacts` off for the default model
(glm-5-2) rather than chase the residual with counter-prompting -- which would reintroduce
exactly the standing per-turn context PART 1 just removed, on an axis PART 1's own A/B
shows prompting does not hold for. The asymmetry decides it: a MISSING artifact costs a
user one re-ask; a WRONG one is the reported incident. `glm-4-7` keeps
`artifacts: "default"` so at least one bundled model still proves the gateway's fence
rewrite carries a genuine build request through on its own (done-condition 2). Full
measurement and the residual-rate writeup: docs/design/dogfood-findings.md, Finding 41.

This test is a static regression guard on the shipped config: no modelSpec carries a
promptPrefix again, the default model (glm-5-2) does not enable artifacts, and a
non-default model (glm-4-7) still does -- so genuine-build-request rendering is still
provably exercised by something in the bundle.

WHAT THIS DOES NOT PROVE. This only proves the CONFIGURATION shipped. It cannot prove GLM's
actual behaviour -- that requires a real completion from the real model, which this hermetic
suite deliberately cannot do (bundle/fakeprovider/app.py is a deterministic hash of the
prompt, not a real LLM; it cannot exhibit or fail to exhibit an ambiguity bias, and with
artifacts off for glm-5-2 it cannot demonstrate the false-positive mechanism is actually
closed either -- that is a property of LibreChat's own build.js, read and cited above, not
observed here). The real, run-against-the-model proof for PART 1/2's measurement lives
outside `tests/` and `make test` -- see the item audit trails
(enterpriseaiframework-8b5, enterpriseaiframework-52a) for the counts. Rendering an actual
artifact PANEL (or its correct absence) in the chat UI is a further, still-unproven layer:
it needs a signed-in browser session against a chat container, which for this project only
exists against the k3s cluster (tests-live/test_browser.py, test_e2e_journey.py) -- both
explicitly cluster-only, and the cluster was read-only with a real user on it for the whole
of this work (Finding 33). The prerequisite to close that gap is either a compose-bundle
browser harness (OIDC login + Playwright pointed at the bundle's own chat container, which
does not exist today) or a maintenance window on the cluster.
"""

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
LIBRECHAT_YAML = REPO / "bundle" / "librechat" / "librechat.yaml"


@pytest.fixture(scope="module")
def model_specs() -> list[dict]:
    doc = yaml.safe_load(LIBRECHAT_YAML.read_text())
    specs = doc["modelSpecs"]["list"]
    assert specs, "librechat.yaml has no modelSpecs.list entries to check"
    return specs


def test_no_default_model_carries_a_standing_promptprefix(model_specs):
    """Regression guard: the redundant code-only promptPrefix must not come back.

    If a future change re-adds a promptPrefix, it should be a deliberate, reviewed
    decision -- not a silent reintroduction of the exact defect this item fixed.
    """
    offenders = [
        spec["name"] for spec in model_specs
        if "promptPrefix" in spec.get("preset", {})
    ]
    assert not offenders, (
        f"{offenders} carry a preset.promptPrefix again -- see enterpriseaiframework-8b5. "
        "A standing, code-only system prompt on every turn biases ambiguous messages "
        "toward 'build a program'; strip_reasoning.py already normalises the artifact "
        "fence deterministically at the gateway, so this should not be needed."
    )


def test_the_default_model_has_artifacts_disabled(model_specs):
    """enterpriseaiframework-52a: the default model must not render artifacts at all.

    Real-model measurement showed glm-5.2 wraps a plain prose request ("write me a short
    story", no code intent anywhere in the conversation) in an HTML artifact 2/5 to 4/5 of
    the time, driven by LibreChat's OWN stock artifactsPrompt (injected whenever
    `preset.artifacts` is a string) interacting with the model's own tendency -- not by our
    promptPrefix, which enterpriseaiframework-8b5 already removed without fixing this.
    Counter-prompting was rejected (it reintroduces the standing context 8b5 removed, on an
    axis 8b5's own A/B shows prompting does not hold for). The asymmetry decided it: a
    missing artifact costs a user one re-ask, a wrong one is the reported incident -- a
    user asks for a shorter story and gets a rendered HTML car lot sorter.

    `endpoints/custom/build.js` only calls `generateArtifactsPrompt` when
    `typeof artifacts === 'string'`, so the key must be ABSENT (not `artifacts: false`,
    which is a value the preset schema's `z.string().optional()` was never built to carry
    and risks being silently stripped in a different way than intended). See
    docs/design/dogfood-findings.md, Finding 41 for the measurement and residual rate.
    """
    defaults = [spec for spec in model_specs if spec.get("default")]
    assert len(defaults) == 1, (
        f"expected exactly one default modelSpec, found {len(defaults)}: "
        f"{[s['name'] for s in defaults]}"
    )
    default_spec = defaults[0]
    preset = default_spec.get("preset", {})
    assert "artifacts" not in preset, (
        f"default model spec {default_spec['name']!r} sets preset.artifacts="
        f"{preset.get('artifacts')!r} -- the default model must not render artifacts "
        "(enterpriseaiframework-52a); if this is a deliberate reversal of that ruling, "
        "update this test and Finding 41 in docs/design/dogfood-findings.md together, "
        "not silently."
    )


def test_a_non_default_model_still_uses_librechats_own_artifact_prompt(model_specs):
    """Turning artifacts off for the default model must not disable them everywhere.

    `artifacts: "default"` is what makes LibreChat inject its OWN balanced artifact
    prompt (when to use one and when not to). At least one non-default model must keep
    it, so a genuinely requested program (done-condition 2 of enterpriseaiframework-52a)
    still renders as a running artifact somewhere in the bundle, proving the gateway's
    fence rewrite (strip_reasoning.py) carries a build request through on its own.
    """
    non_default = [spec for spec in model_specs if not spec.get("default")]
    assert non_default, "no non-default modelSpec exists to carry artifacts: \"default\""
    enabled = [
        spec["name"] for spec in non_default
        if spec.get("preset", {}).get("artifacts") == "default"
    ]
    assert enabled, (
        "no non-default model spec sets preset.artifacts: \"default\" -- with the "
        "default model's artifacts off (enterpriseaiframework-52a), nothing in the "
        "bundle would ever render a genuinely requested build as an artifact, and "
        "done-condition 2 of that item would be unproven by construction"
    )
