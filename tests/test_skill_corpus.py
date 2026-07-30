"""enterpriseaiframework-6ff: invoking a named skill visibly changes behaviour.

THE ARCHITECTURE DECISION IS NOT THIS FILE'S TO RE-LITIGATE — it was made by the
orchestrator and is recorded in dogfood-findings.md finding 41 and in this item's own rd
notes: one SKILL.md corpus (`bundle/skills/`), loaded NATIVELY by both surfaces. LibreChat
loads it through `DEPLOYMENT_SKILLS_DIR` (packages/api/src/skills/deployment.ts); opencode
loads the same directory tree through its own native `skill({name})` tool and `.claude/skills`
/ `skills.paths` discovery (deploy/workspace/opencode.json, deploy/k8s/61-workspace.template.yaml).
Neither surface runs invocation code of ours — see the file-level comments in
bundle/docker-compose.yml and deploy/k8s/50-chat.yaml for the crash-loop hazard this
mechanism carries and how it is guarded (test-gated here, not patched in the image).

WHY THIS FILE PROVES SOMETHING RATHER THAN ASSERTING IT. `chat_turn`'s existing tests
(test_chat_surface_version.py) already lean on one property of `fakeprovider`: its reply is
`sha256(model + "\x00" + prompt)`, a pure function of exactly what LibreChat sent upstream.
That is enough to prove a turn happened, but proving "the model's behaviour differed BECAUSE
of a resource the skill directory carries and nothing else could have supplied" needs to see
the actual prompt text, not just a fingerprint of it — LibreChat's internal message-join
format (system prompt, history, skill primes, user turn) is not something this suite should
have to reconstruct byte-for-byte to make an assertion. So fakeprovider now also captures the
extracted prompt of every request it receives, inspectable over `GET /debug/prompts` — a test
seam on a component that IS test-only ("fakeprovider ... stands in for Anthropic and OpenAI").
Nothing about the deterministic reply/digest contract other tests rely on changes.

THE NEGATIVE CONTROL IS THE WHOLE TEST (operational guard 8). Two turns, byte-identical user
text, sent in the same test: one with the skill manually invoked, one without. The captured
prompt for the un-invoked turn must equal the user's literal text (proving fakeprovider saw
nothing extra); the captured prompt for the invoked turn must contain the exact secret marker
that lives nowhere but bundle/skills/incident-escalation/SKILL.md's body (proving the model
could not have produced it, and that invocation is what carried it in) — and must NOT contain
the second skill's marker (proving this is is that specific skill's content, not "some skill
priming happened").
"""

import re
from pathlib import Path

import httpx
import pytest
import yaml

import chat_turn
import oidc_login
from conftest import BUNDLE, DOGFOOD_USER, DOGFOOD_PASSWORD

pytestmark = pytest.mark.usefixtures("stack_up")

TIMEOUT = 60.0

REPO = Path(__file__).resolve().parent.parent
SKILLS_DIR = BUNDLE / "skills"
COMPOSE_FILE = BUNDLE / "docker-compose.yml"

# The literal secrets planted in the corpus. Neither string appears anywhere else in this
# repo's test fixtures or prompts — grepped before relying on that, not assumed.
ESCALATION_MARKER = "ESCALATION-CODE: TRIDENT-8841-QUARTZ"
MEETING_MARKER = "TEMPLATE-ID: MN-2024-FORMAT-7"

ALLOWED_FRONTMATTER_KEYS = {
    # Mirrors packages/data-schemas/src/methods/skill.ts's ALLOWED_FRONTMATTER_KEYS on the
    # pinned v0.8.7 image (finding 41). Anything outside this set is not a "future LibreChat
    # feature we haven't used yet" — it is a key `validateSkillFrontmatter` REJECTS outright,
    # which throws in `loadDeploymentSkill` and takes the boot down. A skill corpus written
    # with an opencode-only key (`slash`, `compatibility`) would be exactly finding 41's
    # asymmetry biting us: opencode tolerates LibreChat's keys by stripping them, LibreChat
    # does not tolerate opencode's.
    "name", "description", "when-to-use", "allowed-tools", "arguments", "argument-hint",
    "user-invocable", "disable-model-invocation", "always-apply", "alwaysApply", "model",
    "effort", "context", "agent", "paths", "shell", "hooks", "version", "license", "metadata",
}
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _parse_frontmatter(text: str) -> dict:
    assert text.startswith("---"), "SKILL.md must open with a YAML frontmatter block"
    _, _, rest = text.partition("---\n")
    fm_text, sep, _body = rest.partition("\n---")
    assert sep, "SKILL.md frontmatter has no closing '---'"
    return yaml.safe_load(fm_text) or {}


def _skill_dirs() -> list[Path]:
    return sorted(p.parent for p in SKILLS_DIR.glob("*/SKILL.md"))


def _token(client, chat_url: str) -> str:
    r = client.post(
        f"{chat_url}/api/auth/refresh",
        headers={"Cookie": oidc_login._cookie_header(client)},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(client, chat_url: str) -> dict:
    return {
        "Authorization": f"Bearer {_token(client, chat_url)}",
        "Cookie": oidc_login._cookie_header(client),
    }


@pytest.fixture(scope="module")
def fakeprovider_url(env) -> str:
    return f"http://localhost:{env.get('FAKEPROVIDER_PORT', '8090')}"


def _prompts_containing(fakeprovider_url: str, nonce: str) -> list[str]:
    r = httpx.get(f"{fakeprovider_url}/debug/prompts", params={"contains": nonce}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return [entry["prompt"] for entry in r.json()]


# ---------------------------------------------------------------------------
# The corpus itself: at least two skills, each loadable by BOTH surfaces
# ---------------------------------------------------------------------------


class TestTheCorpusIsShapedForBothSurfaces:
    def test_at_least_two_skills_exist(self):
        dirs = _skill_dirs()
        assert len(dirs) >= 2, (
            f"done-condition requires at least two skills; found {[d.name for d in dirs]} "
            f"under {SKILLS_DIR}"
        )

    def test_every_skill_name_is_kebab_case_and_matches_its_directory(self):
        """packages/data-schemas/src/methods/skill.ts#validateSkillName's exact pattern.

        A name that fails this throws in loadDeploymentSkill at boot — the crash-loop hazard
        operational guard 6 warns about — so this is checked here, on every commit, rather
        than discovered the first time someone edits the corpus and reruns `make up`.
        """
        dirs = _skill_dirs()
        assert dirs, f"no SKILL.md found under {SKILLS_DIR}"
        for d in dirs:
            frontmatter = _parse_frontmatter((d / "SKILL.md").read_text())
            name = frontmatter.get("name")
            assert name == d.name, (
                f"{d}/SKILL.md declares name {name!r}, which must equal its directory name "
                f"{d.name!r} — loadDeploymentSkill derives the skill from the directory it "
                "walked, not from an operator's intent"
            )
            assert SKILL_NAME_PATTERN.match(name or ""), (
                f"{d.name!r} is not kebab-case (lowercase letters, digits, hyphens only); "
                "LibreChat's validateSkillName rejects this at boot"
            )

    def test_every_skill_has_a_real_description(self):
        for d in _skill_dirs():
            frontmatter = _parse_frontmatter((d / "SKILL.md").read_text())
            description = frontmatter.get("description")
            assert isinstance(description, str) and description.strip(), (
                f"{d}/SKILL.md has no description — validateSkillDescription rejects an "
                "empty one at boot, and the model has no way to auto-discover the skill "
                "without one either"
            )

    def test_no_skill_uses_a_frontmatter_key_librechat_rejects(self):
        """The asymmetry finding 41 measured, checked against the actual corpus.

        opencode silently STRIPS keys outside its own small set (`name`, `description`,
        `slash`) — so this corpus is free to carry LibreChat-only keys like `license` and
        opencode will simply ignore them. The failure mode runs the other way: a key
        opencode would recognise (`slash`, `compatibility`) is NOT in LibreChat's
        ALLOWED_FRONTMATTER_KEYS and throws at boot. So the corpus is restricted to
        LibreChat's set, which is also the portable-by-construction direction.
        """
        for d in _skill_dirs():
            frontmatter = _parse_frontmatter((d / "SKILL.md").read_text())
            unknown = set(frontmatter) - ALLOWED_FRONTMATTER_KEYS
            assert not unknown, (
                f"{d}/SKILL.md has frontmatter key(s) {sorted(unknown)} outside LibreChat's "
                f"ALLOWED_FRONTMATTER_KEYS — this throws in loadDeploymentSkill and takes "
                "the shared chat deployment down at boot (operational guard 6)"
            )

    def test_the_cluster_manifests_mirror_the_bundle_mount(self):
        """Committed, NOT applied (operational guard 7) — a ConfigMap change here is a
        real deploy + chat restart, which `deploy/bin/deploy.sh` performs, this test does
        not, and this test does not pretend to.

        Checks the two k8s templates carry a volume + volumeMount pair for every skill
        currently in the corpus, so an added skill that forgets the YAML side (the real
        cost `deploy/bin/lib/tenant-skills.sh` documents) fails here rather than only
        being discovered against a live cluster.
        """
        chat_yaml = (REPO / "deploy" / "k8s" / "50-chat.yaml").read_text()
        workspace_yaml = (REPO / "deploy" / "k8s" / "61-workspace.template.yaml").read_text()
        assert "DEPLOYMENT_SKILLS_DIR" in chat_yaml, (
            "deploy/k8s/50-chat.yaml does not set DEPLOYMENT_SKILLS_DIR — the cluster chat "
            "surface would never load the skill ConfigMaps below even if they exist"
        )
        for d in _skill_dirs():
            configmap_name = f"chat-skill-{d.name}"
            assert configmap_name in chat_yaml, (
                f"deploy/k8s/50-chat.yaml has no volume for {configmap_name!r} — the chat "
                f"pod would never mount bundle/skills/{d.name}"
            )
            assert configmap_name in workspace_yaml, (
                f"deploy/k8s/61-workspace.template.yaml has no volume for "
                f"{configmap_name!r} — opencode's skills.paths would never see "
                f"bundle/skills/{d.name}"
            )

    def test_opencode_points_at_the_shared_tenant_skills_directory(self):
        """opencode's OWN native skill loader, pointed at the same corpus (finding 41):
        no invocation code of ours on this surface either.
        """
        config = yaml.safe_load((REPO / "deploy" / "workspace" / "opencode.json").read_text())
        paths = (config.get("skills") or {}).get("paths") or []
        assert "/etc/opencode/tenant-skills" in paths, (
            f"deploy/workspace/opencode.json's skills.paths is {paths!r} — opencode has no "
            "configured source for the tenant skill corpus"
        )

    def test_the_bundle_mounts_the_corpus_as_deployment_skills(self):
        """librechat.yaml's `skills` capability is necessary but not sufficient — this is
        the OTHER, filesystem mechanism (finding 41) that actually puts content there.
        """
        compose = yaml.safe_load(COMPOSE_FILE.read_text())
        chat_env = compose["services"]["chat"]["environment"]
        assert chat_env.get("DEPLOYMENT_SKILLS_DIR") == "/app/skill", (
            "bundle/docker-compose.yml does not set DEPLOYMENT_SKILLS_DIR=/app/skill on the "
            "chat service — without it the corpus in bundle/skills/ is never loaded, no "
            "matter how well-formed"
        )
        volumes = compose["services"]["chat"]["volumes"]
        assert any(
            v == "./skills:/app/skill:ro" for v in volumes
        ), f"chat service does not mount ./skills at /app/skill read-only: {volumes}"


# ---------------------------------------------------------------------------
# The measured behavioural difference — this IS the done condition
# ---------------------------------------------------------------------------


class TestInvokingASkillMeasurablyChangesTheTurn:
    def test_the_uninvoked_turn_carries_no_skill_content(
        self, chat_session, chat_url, fakeprovider_url
    ):
        """The negative control, on its own: the baseline this item's other test differs from.

        Sent with NO manualSkills and NO ephemeralAgent.skills toggle. If this fails —
        i.e. if either marker shows up here — the corpus is bleeding into every turn
        (an `always-apply` skill, most likely), which would make the next test's
        "measurable difference" claim vacuous.
        """
        nonce = f"skillprobe-baseline-{__import__('uuid').uuid4().hex[:12]}"
        prompt = f"what should I do right now, {nonce}"
        reply = chat_turn.send_turn(
            chat_session, chat_url, prompt, model="fake-large",
            endpoint="Enterprise AI", headers=_auth(chat_session, chat_url),
        )
        text = chat_turn.reply_text(reply)
        assert text, "no reply text at all for the baseline turn"

        captured = _prompts_containing(fakeprovider_url, nonce)
        assert captured, (
            f"fakeprovider never saw a request carrying nonce {nonce!r} — the turn did not "
            "reach the upstream this test asserts against"
        )
        assert captured[0] == prompt, (
            f"the un-invoked turn's upstream prompt was {captured[0]!r}, not the literal "
            f"user text {prompt!r} — something is already injecting content with no skill "
            "manually invoked and no always-apply skill should be doing that"
        )
        assert ESCALATION_MARKER not in captured[0]
        assert MEETING_MARKER not in captured[0]

    def test_invoking_the_skill_delivers_its_secret_and_only_its_secret(
        self, chat_session, chat_url, fakeprovider_url
    ):
        """The positive case, with the SAME shape of user text as the control above.

        `manual_skills=["incident-escalation"]` is the `$`-popover's wire shape
        (tests/chat_turn.py). The assertion is not "the reply looks different" — a fake,
        non-reasoning upstream would make that trivial to satisfy by accident — it is that
        the literal secret string living only in bundle/skills/incident-escalation/SKILL.md
        reached the request fakeprovider actually received, and that the OTHER skill's
        secret did not (proving this is that skill's content specifically, not "the corpus
        leaks into every turn" — which the control above already rules out, but re-checking
        it here pins the claim to THIS turn).
        """
        nonce = f"skillprobe-invoked-{__import__('uuid').uuid4().hex[:12]}"
        prompt = f"what should I do right now, {nonce}"
        reply = chat_turn.send_turn(
            chat_session, chat_url, prompt, model="fake-large",
            endpoint="Enterprise AI", manual_skills=["incident-escalation"],
            headers=_auth(chat_session, chat_url),
        )
        text = chat_turn.reply_text(reply)
        assert text, "no reply text at all for the skill-invoked turn"

        captured = _prompts_containing(fakeprovider_url, nonce)
        assert captured, (
            f"fakeprovider never saw a request carrying nonce {nonce!r} for the "
            "skill-invoked turn"
        )
        upstream_prompt = captured[0]
        assert ESCALATION_MARKER in upstream_prompt, (
            "invoking 'incident-escalation' did not deliver its SKILL.md body to the "
            f"upstream request — got: {upstream_prompt!r}"
        )
        assert MEETING_MARKER not in upstream_prompt, (
            "the OTHER skill's marker leaked into a turn that only invoked "
            "'incident-escalation' — manual invocation is priming more than was asked for"
        )
        assert upstream_prompt != prompt, (
            "the upstream prompt for the invoked turn is byte-identical to the raw user "
            "text — the skill body was not spliced in at all"
        )

    def test_the_same_invocation_is_deterministic_across_two_turns(
        self, chat_session, chat_url, fakeprovider_url
    ):
        """Rules out "it happened to differ" — same skill, same text, twice, same secret.

        A single passing/failing comparison could be explained by any incidental per-turn
        noise (a timestamp, a random id) unrelated to the skill mechanism. Two independent
        turns, same skill, same typed text, both carrying the marker is the repeatable
        claim: this is the skill's content reaching the model, not turn-to-turn jitter.
        """
        nonce_a = f"skillprobe-repeat-a-{__import__('uuid').uuid4().hex[:12]}"
        nonce_b = f"skillprobe-repeat-b-{__import__('uuid').uuid4().hex[:12]}"
        for nonce in (nonce_a, nonce_b):
            chat_turn.send_turn(
                chat_session, chat_url, f"help me, {nonce}", model="fake-large",
                endpoint="Enterprise AI", manual_skills=["incident-escalation"],
                headers=_auth(chat_session, chat_url),
            )
        for nonce in (nonce_a, nonce_b):
            captured = _prompts_containing(fakeprovider_url, nonce)
            assert captured, f"no captured prompt for {nonce}"
            assert ESCALATION_MARKER in captured[0], (
                f"turn {nonce} did not carry the skill's marker: {captured[0]!r}"
            )

    def test_the_second_skill_is_independently_invocable(
        self, chat_session, chat_url, fakeprovider_url
    ):
        """The SECOND of the two required skills, invoked on its own — not just present.

        Distinct from the corpus-shape test above (which only checks the file exists):
        this drives an actual turn and re-runs the same marker-delivery proof against
        'meeting-notes-format', so "at least two skills" means two that WORK, not one that
        works and one that merely parses.
        """
        nonce = f"skillprobe-second-{__import__('uuid').uuid4().hex[:12]}"
        chat_turn.send_turn(
            chat_session, chat_url, f"format these, {nonce}", model="fake-large",
            endpoint="Enterprise AI", manual_skills=["meeting-notes-format"],
            headers=_auth(chat_session, chat_url),
        )
        captured = _prompts_containing(fakeprovider_url, nonce)
        assert captured, f"no captured prompt for {nonce}"
        assert MEETING_MARKER in captured[0], (
            f"invoking 'meeting-notes-format' did not deliver its marker: {captured[0]!r}"
        )
        assert ESCALATION_MARKER not in captured[0]

    def test_both_skills_are_served_in_the_surfaces_own_catalog(
        self, chat_session, chat_url
    ):
        """Not just reachable by name — visible in what a user would see in the $ popover."""
        listed = chat_session.get(
            f"{chat_url}/api/skills", headers=_auth(chat_session, chat_url), timeout=TIMEOUT,
        )
        assert listed.status_code == 200, listed.text
        names = {s["name"] for s in listed.json().get("skills") or []}
        for expected in ("incident-escalation", "meeting-notes-format"):
            assert expected in names, (
                f"{expected!r} is not in the served skill catalog {sorted(names)} — it is "
                "not user-discoverable even if `manualSkills` can still reach it by name"
            )
