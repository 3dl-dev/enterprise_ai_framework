"""The workspace's own text must agree with the workspace's own NetworkPolicy.

enterpriseaiframework-644. `deploy/k8s/60-workspace-common.yaml` allows the workspace pod
egress to 0.0.0.0/0 minus the private ranges, on any port, captioned "The internet, for
pip / npm / git clone" and present since the commit that created the surface. Five places
in `deploy/workspace/` asserted the opposite as fact — "there is no internet here", "there
is no egress", "this room has no internet" — to the coding agent and to the child. An
agent that believes it has no network does not try; a child is handed a false explanation
for a blank page. Both are defects, and the one that is wrong is the text.

WHAT THIS FILE IS NOT. It does not hard-code "the pod has internet". It parses the
NetworkPolicy and derives the obligation from it, in both directions:

    policy allows general internet egress  ->  the text must not claim otherwise
    policy does not allow it               ->  the text must say so, or the agent will
                                               burn a child's turn on an install that hangs

So if the policy is ever tightened (see the escalation on -644 proposing a narrowing to
TCP 80/443), this test starts demanding the opposite text rather than silently continuing
to pass. That is the point: the two sides cannot drift apart again without something
going red. `test_a_restrictive_policy_flips_the_obligation` drives that second branch with
a synthetic spec, so it is executed rather than merely written down.

GROUND TRUTH, AND HOW MUCH OF IT THIS FILE CAN SEE. This suite is hermetic and has no
cluster, so it reads the checked-in YAML. Two things it therefore CANNOT prove, both
measured by hand on 2026-07-29 against the live cluster and both covered by
`tests-live/test_workspace_instructions.py` where a cluster exists:

  - the live `workspace-isolation` NetworkPolicy's egress matches this YAML (it did:
    `kubectl get networkpolicy workspace-isolation -o json`, structurally identical);
  - the pod behaviourally reaches the internet (it does: from inside
    `ws-student`'s ttyd container, `curl https://registry.npmjs.org/` -> 200 from
    104.16.4.34:443, and `curl https://pypi.org/simple/` -> 200 from 151.101.128.223:443).

The second measurement is the one that matters, because this cluster's CNI (kube-router)
resolves a packet on the destination's ingress rules without consulting the source's
egress — so an egress rule in YAML proves nothing on its own, in either direction.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
NETPOL_YAML = REPO / "deploy/k8s/60-workspace-common.yaml"
WORKSPACE = REPO / "deploy/workspace"

# Claims that are FALSE while the policy allows general internet egress, and REQUIRED
# (at least one of them, in the agent's own rules) while it does not.
#
# Every one of these was in the tree at the commit this item was opened against; see
# ORIGINAL_AGENTS_MD_RULES below, which is fault-injected through the same checker to
# prove the list actually matches real text rather than a paraphrase of it.
NETWORK_ABSENCE_CLAIMS = (
    "there is no internet",
    "no internet here",
    "has no internet",
    "there is no egress",
    "has no egress",
    "no egress except",
    "and this room has none",
)

# Files whose content is asserted to somebody — the agent (the two markdown files are
# loaded as opencode `instructions`) or the child (the shell's rendered copy and its
# user-visible strings).
SCANNED = (
    "AGENTS.md",
    "PLATFORM.md",
    "shell/index.html",
    "shell/app.js",
    "shell/style.css",
    "shell-server.py",
)

# PLATFORM.md deliberately quotes both retired claims below this heading in order to
# explain why they were dropped. Quoting a false claim while refuting it is not asserting
# it; the numbered list above the heading is the assertion surface. This is the same split
# tests-live/test_workspace_instructions.py already uses, kept identical on purpose.
PLATFORM_RETROSPECTIVE = "## What used to be here"

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_FULL_LINE_SLASH = re.compile(r"^[ \t]*//.*$", re.MULTILINE)
_FULL_LINE_HASH = re.compile(r"^[ \t]*#.*$", re.MULTILINE)


def _assertion_surface(rel: str, raw: str) -> str:
    """Strip the parts of a file that state nothing to anybody, and normalise the rest.

    Source comments are removed, because after -644 several of them quote the retired
    claim verbatim in order to record that it was false — which is documentation, not an
    assertion. String literals and rendered markup are NOT removed: `app.js`'s ribbon
    phrase and `index.html`'s Stuck? panel are exactly the sentences a child reads, and
    they were two of the five defects.

    Only FULL-LINE `//` and `#` comments are stripped, never trailing ones. A trailing
    comment therefore stays in scope and could produce a false positive. That is the safe
    direction to be wrong in: a false positive fails loudly and is one edit to fix, a
    false negative is this whole defect coming back unnoticed.
    """
    text = raw
    if rel == "PLATFORM.md":
        text = text.split(PLATFORM_RETROSPECTIVE, 1)[0]
    if rel.endswith(".html"):
        text = _HTML_COMMENT.sub(" ", text)
    if rel.endswith((".js", ".css")):
        text = _BLOCK_COMMENT.sub(" ", text)
        text = _FULL_LINE_SLASH.sub(" ", text)
    if rel.endswith(".py"):
        text = _FULL_LINE_HASH.sub(" ", text)
    text = text.lower().replace("*", "").replace("`", "")
    return re.sub(r"\s+", " ", text)


PACKAGE_PORTS = {80, 443}


def _public_egress_rules(spec: dict) -> list[dict]:
    """Egress rules whose destination is the public internet (a 0.0.0.0/0 ipBlock)."""
    out = []
    for rule in spec.get("egress", []):
        for to in rule.get("to", []):
            if to.get("ipBlock", {}).get("cidr") == "0.0.0.0/0":
                out.append(rule)
                break
    return out


def _reaches_public_internet(spec: dict) -> bool:
    """Can the pod reach a public host on a port a package manager uses?

    This — not "on every port" — is the question the workspace's TEXT makes a claim
    about. "There is no internet here" and "npm install hangs" are refuted by reachability
    on 443 alone; whether port 4444 is also open is a different question with different
    consequences, answered by `_reaches_arbitrary_public_port` below.

    Keeping these two apart matters for a concrete reason: the -644 escalation proposes
    narrowing the internet rule to TCP 80/443. If that lands, `pip` and `npm` still work,
    so the text must NOT start claiming the pod is offline. A predicate that collapsed the
    two questions would have demanded exactly that false statement — this file's own
    version of the defect it exists to catch.
    """
    for rule in _public_egress_rules(spec):
        ports = rule.get("ports")
        if ports is None:
            return True
        if any(p.get("port") in PACKAGE_PORTS for p in ports):
            return True
    return False


def _reaches_arbitrary_public_port(spec: dict) -> bool:
    """Can the pod open a connection to any public host on ANY port?

    Separate from reachability because it is a separate grant: this pod runs
    agent-written code next to a spendable virtual key. It is asserted on its own below so
    that changing it is a deliberate, reviewed act rather than a quiet one.
    """
    return any("ports" not in rule for rule in _public_egress_rules(spec))


def _claim_violations(spec: dict, surfaces: dict[str, str]) -> list[str]:
    """The whole check, as a pure function so it can be fault-injected.

    `surfaces` maps a label to already-normalised text.
    """
    problems: list[str] = []
    if _reaches_public_internet(spec):
        for label, text in surfaces.items():
            for claim in NETWORK_ABSENCE_CLAIMS:
                if claim in text:
                    problems.append(
                        f"{label} asserts {claim!r}, but the workspace NetworkPolicy "
                        f"lets the pod reach public hosts on a package-manager port — "
                        f"the text is what is wrong here, not the policy "
                        f"(enterpriseaiframework-644)"
                    )
    else:
        rules = surfaces.get("AGENTS.md", "")
        if not any(claim in rules for claim in NETWORK_ABSENCE_CLAIMS):
            problems.append(
                "the NetworkPolicy no longer lets the pod reach the public internet, but "
                "the agent's own rules (AGENTS.md) never say so — an agent that does not "
                "know it is offline will spend a user's turn on an install that hangs"
            )
    return problems


@pytest.fixture(scope="module")
def netpol_spec() -> dict:
    spec = None
    for doc in yaml.safe_load_all(NETPOL_YAML.read_text()):
        if doc and doc.get("kind") == "NetworkPolicy":
            spec = doc["spec"]
    assert spec is not None, f"no NetworkPolicy in {NETPOL_YAML}"
    return spec


@pytest.fixture(scope="module")
def surfaces() -> dict[str, str]:
    out = {}
    for rel in SCANNED:
        path = WORKSPACE / rel
        assert path.exists(), f"{path} is gone — update SCANNED, do not delete the check"
        out[rel] = _assertion_surface(rel, path.read_text())
    return out


def test_the_checked_in_policy_lets_the_pod_reach_the_public_internet(netpol_spec):
    """Ground truth anchor for the text obligation. If this ever fails, the pod really has
    been cut off from package registries and the workspace text now has to SAY so — which
    the next test enforces automatically. It is separate so that a flip shows up as a
    named, explained failure rather than as a confusing one inside a text assertion.

    Deliberately survives the TCP 80/443 narrowing proposed on -644: pip and npm still
    work through that, so the text must not change.
    """
    assert _reaches_public_internet(netpol_spec), (
        "deploy/k8s/60-workspace-common.yaml no longer lets the workspace pod reach public "
        "hosts on 80/443. If that is intended, the agent's rules must be updated to say "
        "the installers no longer work; the next test enforces it."
    )


def test_the_policy_currently_permits_arbitrary_public_ports(netpol_spec):
    """The authority grant, pinned on its own so that removing it is deliberate.

    The internet rule carries no port restriction, so a workspace pod — which runs
    agent-written code beside a spendable virtual key — can open a connection to any
    public host on any port. That is confirmed intentional (it predates every other
    decision in this file) but had never been *chosen*. -644 escalated a proposed
    narrowing to TCP 80/443 for founder review.

    IF THAT ESCALATION LANDS, THIS TEST IS THE ONE THAT GOES RED, and the correct
    response is to invert it here — not to touch the workspace text, which stays true
    either way because the test above stays green.
    """
    assert _reaches_arbitrary_public_port(netpol_spec), (
        "the workspace internet egress rule is now port-scoped. If that is the -644 "
        "narrowing landing, invert this assertion and record it in finding 36; the text "
        "obligations are unaffected."
    )


def test_no_workspace_surface_contradicts_the_policy(netpol_spec, surfaces):
    """The regression. Fails at the commit that opened -644: AGENTS.md rule 1 ('There is
    no internet here'), AGENTS.md rule 5 ('There is no egress'), index.html's Stuck? panel
    ('This room has no internet') and app.js's ribbon phrase ('and this room has none')
    were all live text at that point.
    """
    problems = _claim_violations(netpol_spec, surfaces)
    assert not problems, "\n".join(problems)


def test_the_two_egress_readers_answer_two_different_questions():
    """The cases NOT changed by this item, asserted rather than assumed.

    Everything above rests on the readers telling "the internet" apart from the three
    narrow destinations the policy also permits, AND on reachability being a different
    question from port breadth. A reader that returned True for any `to:` entry would make
    the whole file vacuous; a single reader answering both questions would demand a false
    "you are offline" the moment the -644 narrowing landed.
    """
    dns_gateway_and_tls_edge_only = {
        "egress": [
            {"to": [{"podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
             "ports": [{"protocol": "UDP", "port": 53}]},
            {"to": [{"podSelector": {"matchLabels": {"app": "gateway"}}}],
             "ports": [{"protocol": "TCP", "port": 4000}]},
            {"to": [{"ipBlock": {"cidr": "192.168.2.42/32"}}],
             "ports": [{"protocol": "TCP", "port": 8443}]},
        ]
    }
    assert not _reaches_public_internet(dns_gateway_and_tls_edge_only)
    assert not _reaches_arbitrary_public_port(dns_gateway_and_tls_edge_only)

    # The exact narrowing proposed in the -644 escalation. `pip` and `npm` still work
    # through it, so reachability must stay TRUE — otherwise the escalation lands and this
    # file starts insisting the workspace text tell the agent it is offline, which would
    # be a fresh instance of the very defect the file exists to prevent.
    narrowed_to_web_ports = {
        "egress": [
            {"to": [{"ipBlock": {"cidr": "0.0.0.0/0",
                                 "except": ["10.0.0.0/8", "192.168.0.0/16"]}}],
             "ports": [{"protocol": "TCP", "port": 80}, {"protocol": "TCP", "port": 443}]},
        ]
    }
    assert _reaches_public_internet(narrowed_to_web_ports)
    assert not _reaches_arbitrary_public_port(narrowed_to_web_ports)

    # And the narrowing landing must NOT make the current, corrected text non-compliant.
    # This is the whole point of splitting the predicates, so it is executed here rather
    # than argued in a comment.
    assert _claim_violations(
        narrowed_to_web_ports,
        {"AGENTS.md": _assertion_surface("AGENTS.md", (WORKSPACE / "AGENTS.md").read_text())},
    ) == []

    # A public rule on a port no package manager uses is not reachability in the sense the
    # text claims — an agent there genuinely cannot install anything.
    smtp_only = {
        "egress": [
            {"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}],
             "ports": [{"protocol": "TCP", "port": 25}]},
        ]
    }
    assert not _reaches_public_internet(smtp_only)
    assert not _reaches_arbitrary_public_port(smtp_only)


# The two rules exactly as they stood at the commit -644 was filed against
# (deploy/workspace/AGENTS.md, base 230e1fd). Kept verbatim so the checker is proven
# against the real defect rather than against a paraphrase of it that happens to contain
# the strings it is looking for.
ORIGINAL_AGENTS_MD_RULES = """\
1. **There is no internet here.** Never reference anything at `http://`, `https://` or `//` — no
   CDN, no Google Fonts, no remote image, no remote script, no remote stylesheet. Every line of
   CSS and JavaScript and every image goes inside the file itself. Anything loaded from the web
   arrives as nothing, and the page comes up blank with no explanation.

5. **Never run `npm install` or `pip install`.** There is no egress. Both hang and then fail.
"""

ORIGINAL_STUCK_PANEL = (
    "<p>This room has no internet, so anything the page loads from the web comes up blank.</p>"
)

ORIGINAL_RIBBON_PHRASE = (
    'return ["That page needs the internet, and this room has none. Press Stuck?", "warn"];'
)


@pytest.mark.parametrize(
    "label,original",
    [
        ("AGENTS.md", ORIGINAL_AGENTS_MD_RULES),
        ("shell/index.html", ORIGINAL_STUCK_PANEL),
        ("shell/app.js", ORIGINAL_RIBBON_PHRASE),
    ],
)
def test_the_checker_catches_each_original_defect(netpol_spec, label, original):
    """Fault injection. Feed the checker the pre-fix text against the REAL policy and it
    must complain — otherwise the passing test above proves only that someone reworded
    something, not that the check works.

    Each case is a different file and a different phrasing, so a ban list that happened to
    match one of them by luck cannot carry the other two.
    """
    problems = _claim_violations(
        netpol_spec, {label: _assertion_surface(label, original)}
    )
    assert problems, f"the checker did not catch {label}'s original text: {original!r}"
    assert label in problems[0]


def test_the_checker_is_not_simply_always_complaining(netpol_spec, surfaces):
    """The other half of the fault injection: the same checker, same policy, current text,
    must come back clean. Without this, a checker that returned a complaint for every
    input would pass the test above and fail nothing meaningful.
    """
    assert _claim_violations(netpol_spec, surfaces) == []


def test_a_restrictive_policy_flips_the_obligation(surfaces):
    """The branch this repo is NOT currently on, executed rather than assumed.

    If the -644 escalation lands and egress is narrowed, "the text must not claim there is
    no internet" becomes "the text must say there is limited egress". The current,
    corrected AGENTS.md deliberately says the opposite, so pointing the checker at a
    restrictive spec must complain — proving the else-branch is reachable and does
    something, rather than being dead code that keeps the file green by never running.
    """
    restrictive = {
        "egress": [
            {"to": [{"podSelector": {"matchLabels": {"app": "gateway"}}}],
             "ports": [{"protocol": "TCP", "port": 4000}]},
        ]
    }
    problems = _claim_violations(restrictive, surfaces)
    assert problems, (
        "a policy with no internet route produced no complaint about text that tells the "
        "agent the installers work — the restrictive branch is dead"
    )
    assert "AGENTS.md" in problems[0]

    # ...and the same restrictive spec against the ORIGINAL text must be clean, because
    # back then the text and that policy would have agreed. This is what makes the check a
    # consistency check and not a one-way string ban that always wants the same words.
    was_consistent = _claim_violations(
        restrictive, {"AGENTS.md": _assertion_surface("AGENTS.md", ORIGINAL_AGENTS_MD_RULES)}
    )
    assert was_consistent == []
