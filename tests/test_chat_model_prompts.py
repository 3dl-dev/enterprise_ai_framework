"""The default chat models must not carry a standing code-only bias (enterpriseaiframework-8b5).

FOUND IN USE: a user asked chat for a short story, got a long one, and replied with a typo
("make it a lot sorter" for "a lot shorter"). The model built and rendered an HTML CAR LOT
SORTER instead. Nobody asked for a program. The cause was `modelSpecs.list[].preset.promptPrefix`
in librechat.yaml: ~10 lines of unconditional, every-turn context, every line about emitting
runnable HTML, ending in a worked `<!DOCTYPE html>` example, with nothing about prose. The
only capability the model had been explicitly told about was building a web app, so an
ambiguous message resolved toward that prior.

That promptPrefix was also redundant on both of the two things it tried to do:

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

This test is a static regression guard on the shipped config: it makes sure the code-only
promptPrefix does not silently come back, and that removing it did not also remove
`artifacts: "default"` (which is what keeps LibreChat's own balanced artifact prompt, and
therefore artifact rendering for genuine build requests, in place).

WHAT THIS DOES NOT PROVE. This only proves the CONFIGURATION shipped. It cannot prove GLM's
actual behaviour -- that requires a real completion from the real model, which this hermetic
suite deliberately cannot do (bundle/fakeprovider/app.py is a deterministic hash of the
prompt, not a real LLM; it cannot exhibit or fail to exhibit an ambiguity bias). The real,
run-against-the-model proof for this item lives outside `tests/` and `make test`: five
natural two-turn trials per system prompt (fresh 'write me a short story', then the literal
reported typo 'make it a lot sorter') against the real glm-5.2@deepinfra model via Forge,
comparing the removed promptPrefix against what ships now. See the item's audit trail
(enterpriseaiframework-8b5) for the counts. Rendering an actual artifact PANEL in the chat
UI is a further, still-unproven layer: it needs a signed-in browser session against a chat
container, which for this project only exists against the k3s cluster
(tests-live/test_browser.py, test_e2e_journey.py) -- both explicitly cluster-only, and the
cluster was read-only with a real user on it for the whole of this work. The prerequisite to
close that gap is either a compose-bundle browser harness (OIDC login + Playwright pointed
at the bundle's own chat container, which does not exist today) or a maintenance window on
the cluster.
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


def test_default_model_specs_still_use_librechats_own_artifact_prompt(model_specs):
    """Removing the redundant promptPrefix must not also disable artifacts entirely.

    `artifacts: "default"` is what makes LibreChat inject its OWN balanced artifact
    prompt (when to use one and when not to), which is what still lets a genuinely
    requested program render as a running artifact with no promptPrefix at all.
    """
    for spec in model_specs:
        preset = spec.get("preset", {})
        assert preset.get("artifacts") == "default", (
            f"{spec['name']} does not set preset.artifacts: \"default\" -- without it, "
            "LibreChat never injects its own artifact system prompt, and a genuinely "
            "requested program (DONE condition 2 of enterpriseaiframework-8b5) would not "
            "render as an artifact at all with no promptPrefix to fall back on."
        )
