"""The OSS profile serves /v1/audio/* through its own gateway (enterpriseaiframework-8cb).

Two layers, both against ground truth rather than mocks:

  * FILE INVARIANTS on what an install ships: the catalogue prices both audio routes (an
    unpriced model meters at $0 SILENTLY — measured on this very route, see config.base.yaml),
    the speech server can reach nothing but DNS, and the alias file compose mounts is the one
    the cluster mounts.
  * A LIVE ROUND TRIP through the running gateway with a named agent-style key: text ->
    /v1/audio/speech -> audio bytes -> /v1/audio/transcriptions -> the same text back, and
    the ledger row for that key carries a non-zero spend for both calls.

The live half needs the bundle up (`make up`), like the rest of this directory. It fails, not
skips, when the gateway is not serving audio.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
BASE = REPO / "bundle" / "litellm" / "config.base.yaml"
K8S = REPO / "deploy" / "k8s" / "32-speech.yaml"
COMPOSE = REPO / "bundle" / "docker-compose.yml"
ALIASES = REPO / "bundle" / "speech" / "model_aliases.json"


def _catalogue() -> dict:
    return {m["model_name"]: m for m in yaml.safe_load(BASE.read_text())["model_list"]}


def _k8s_docs() -> list[dict]:
    return [d for d in yaml.safe_load_all(K8S.read_text()) if d]


# ---- what an install ships ---------------------------------------------------------------


def test_contract_model_ids_are_the_names_the_speech_server_serves():
    """The audio routes keep the OpenAI contract ids (openai/whisper-1, openai/tts-1). Those
    ids are what LiteLLM prices and meters by; renaming them to the self-hosted repo id
    (e.g. Systran/faster-whisper-small) silently meters at $0. The independent source of
    truth is the alias file the speech server itself mounts: every contract id must be a key
    there (so the server resolves it) and must be exactly the pinned literal."""
    cat = _catalogue()
    aliases = json.loads(ALIASES.read_text())
    assert cat["speech-stt"]["litellm_params"]["model"] == "openai/whisper-1"
    assert cat["speech-tts"]["litellm_params"]["model"] == "openai/tts-1"
    for name in ("speech-stt", "speech-tts"):
        wire = cat[name]["litellm_params"]["model"].removeprefix("openai/")
        assert wire in aliases, f"{name}: {wire!r} is not an id the speech server resolves"


def test_both_audio_routes_are_in_the_catalogue_priced_and_pointed_at_the_local_server():
    cat = _catalogue()
    stt, tts = cat["speech-stt"], cat["speech-tts"]
    assert stt["model_info"]["mode"] == "audio_transcription"
    assert tts["model_info"]["mode"] == "audio_speech"
    for entry in (stt, tts):
        assert entry["litellm_params"]["api_base"] == "http://speech:8000/v1"
    p = stt["litellm_params"]
    # Both per-second rates: LiteLLM bills a transcription at the OUTPUT rate (measured).
    assert p["input_cost_per_second"] > 0 and p["output_cost_per_second"] > 0
    assert tts["litellm_params"]["input_cost_per_character"] > 0


def test_audio_routes_survive_rendering_with_real_upstreams_configured():
    """The render script drops every entry whose upstream is the fake provider. These are
    not fakes; if the filter ever matched them the OSS audio backend would vanish exactly on
    installs that have real models."""
    import importlib.machinery
    import importlib.util

    path = str(REPO / "bundle" / "bin" / "render-gateway-config.py")
    loader = importlib.machinery.SourceFileLoader("render_gateway_config", path)
    spec = importlib.util.spec_from_loader("render_gateway_config", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    kept = yaml.safe_load(mod._without_fakes(BASE.read_text()).replace(mod.MARKER, ""))
    names = {m["model_name"] for m in kept["model_list"]}
    assert {"speech-stt", "speech-tts"} <= names
    assert "fake-large" not in names, "premise: the fake filter is really running"


SPEECH_SEEDS = [("enterprise-ai", {"app": "gateway"}), ("kube-system", {"k8s-app": "kube-dns"})]


def _speech_netpol_violations(policies) -> list:
    """Evaluated (-d7b), not pattern-matched. Hand-written expectation: the speech pod reaches
    only kube-dns-labelled pods on :53 (either protocol), no external address; and on its
    ingress only the gateway pod (platform namespace) on :8000."""
    from netpol_eval import (PROTOCOLS, Dest, admitted_egress_peers, admitted_peers,
                             build_universe, probe_ports)
    speech = Dest("enterprise-ai", {"app": "speech"})
    bad = []
    eg = {p for p, _ in build_universe(policies, "enterprise-ai", "egress", SPEECH_SEEDS)[0]}
    ing = {p for p, _ in build_universe(policies, "enterprise-ai", "ingress", SPEECH_SEEDS)[0]}
    for proto in PROTOCOLS:
        for port in probe_ports(policies, extra=(53, 8000, 443)):
            pods, ips = admitted_egress_peers(policies, speech, port, proto, SPEECH_SEEDS)
            want = {p for p in eg if port == 53 and proto in ("TCP", "UDP")
                    and dict(p.labels).get("k8s-app") == "kube-dns"}
            if set(pods) != want or ips:
                bad.append(("egress", proto, port))
            pods, ips = admitted_peers(policies, speech, port, proto, SPEECH_SEEDS)
            want = {p for p in ing if proto == "TCP" and port == 8000
                    and p.namespace == "enterprise-ai" and dict(p.labels).get("app") == "gateway"}
            if set(pods) != want or ips:
                bad.append(("ingress", proto, port))
    return bad


def _speech_policies(edit=None, tmp=None):
    from netpol_eval import load_policies
    docs = [d for d in _k8s_docs() if d["kind"] == "NetworkPolicy"]
    extra = edit(docs) if edit else []
    if tmp is None:
        return docs + (extra or [])
    f = tmp / "speech.yaml"
    f.write_text(yaml.safe_dump_all(docs + (extra if isinstance(extra, list) else [])))
    return load_policies([f])


def test_speech_server_is_sealed_to_dns_only_and_offline():
    docs = _k8s_docs()
    assert _speech_netpol_violations(_speech_policies()) == []
    (dep,) = [d for d in docs if d["kind"] == "Deployment"]
    env = {e["name"]: e["value"] for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["HF_HUB_OFFLINE"] == "1"
    # The fetch Job is the ONLY hub-reaching workload, and must not be covered by the policy.
    from netpol_eval import selector_matches
    (job,) = [d for d in docs if d["kind"] == "Job"]
    (pol,) = [d for d in docs if d["kind"] == "NetworkPolicy"]
    assert not selector_matches(pol["spec"]["podSelector"], job["spec"]["template"]["metadata"]["labels"])
    assert selector_matches(pol["spec"]["podSelector"], dep["spec"]["template"]["metadata"]["labels"])


_ANY = {"namespaceSelector": {}}
SPEECH_DRIFTS = {
    "egress rule {}": lambda d: d[0]["spec"]["egress"].append({}),
    "egress to [] + 443": lambda d: d[0]["spec"]["egress"].append({"to": [], "ports": [{"port": 443}]}),
    "egress ipBlock 0.0.0.0/0": lambda d: d[0]["spec"]["egress"].append(
        {"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}]}),
    # IPv6 and SCTP forms (-1ed)
    "egress ipBlock ::/0": lambda d: d[0]["spec"]["egress"].append({"to": [{"ipBlock": {"cidr": "::/0"}}]}),
    "egress ipBlock 2000::/3 :443": lambda d: d[0]["spec"]["egress"].append(
        {"to": [{"ipBlock": {"cidr": "2000::/3"}}], "ports": [{"port": 443}]}),
    "egress ipBlock fc00::/7": lambda d: d[0]["spec"]["egress"].append({"to": [{"ipBlock": {"cidr": "fc00::/7"}}]}),
    "egress ipBlock v4-mapped": lambda d: d[0]["spec"]["egress"].append(
        {"to": [{"ipBlock": {"cidr": "::ffff:0:0/96"}}]}),
    "egress SCTP to 0.0.0.0/0": lambda d: d[0]["spec"]["egress"].append(
        {"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}], "ports": [{"protocol": "SCTP", "port": 443}]}),
    "egress SCTP to podSelector {}": lambda d: d[0]["spec"]["egress"].append(
        {"to": [{"podSelector": {}}], "ports": [{"protocol": "SCTP"}]}),
    "egress dns + SCTP 53": lambda d: d[0]["spec"]["egress"][0]["ports"].append(
        {"protocol": "SCTP", "port": 53}),
    "ingress SCTP 8000 from any pod": lambda d: d[0]["spec"]["ingress"].append(
        {"from": [{"podSelector": {}}], "ports": [{"protocol": "SCTP", "port": 8000}]}),
    "ingress 8000 from ipBlock ::/0": lambda d: d[0]["spec"]["ingress"].append(
        {"from": [{"ipBlock": {"cidr": "::/0"}}], "ports": [{"port": 8000}]}),
    "egress dns ports []": lambda d: d[0]["spec"]["egress"][0].__setitem__("ports", []),
    "egress dns port range 1-1024": lambda d: d[0]["spec"]["egress"][0]["ports"].append(
        {"port": 1, "endPort": 1024}),
    "egress dns any pod": lambda d: d[0]["spec"]["egress"][0].__setitem__("to", [_ANY]),
    "egress to every namespace 443": lambda d: d[0]["spec"]["egress"].append(
        {"to": [_ANY], "ports": [{"port": 443, "protocol": "TCP"}]}),
    "egress policyType dropped": lambda d: d[0]["spec"].__setitem__("policyTypes", ["Ingress"]),
    "ingress from omitted": lambda d: d[0]["spec"]["ingress"][0].pop("from"),
    "ingress from any pod": lambda d: d[0]["spec"]["ingress"][0].__setitem__("from", [{"podSelector": {}}]),
    "ingress + other namespaces": lambda d: d[0]["spec"]["ingress"][0]["from"].append(_ANY),
    "ingress port range 8000-9000 from any": lambda d: d[0]["spec"]["ingress"].append(
        {"from": [{"podSelector": {}}], "ports": [{"port": 8000, "endPort": 9000}]}),
    "ingress gateway relabelled": lambda d: d[0]["spec"]["ingress"][0]["from"][0]["podSelector"][
        "matchLabels"].__setitem__("app", "gateway-v2"),
    "policy podSelector relabelled (selects nothing)": lambda d: d[0]["spec"]["podSelector"][
        "matchLabels"].__setitem__("app", "speech2"),
}



@pytest.mark.parametrize("drift", sorted(SPEECH_DRIFTS))
def test_every_drift_of_the_speech_seal_is_caught(tmp_path, drift):
    assert _speech_netpol_violations(_speech_policies(SPEECH_DRIFTS[drift], tmp_path)), drift


def test_compose_puts_the_speech_server_on_an_internal_network_only():
    c = yaml.safe_load(COMPOSE.read_text())
    assert c["networks"]["speech-internal"]["internal"] is True
    assert c["services"]["speech"]["networks"] == ["speech-internal"]
    assert "speech-internal" in c["services"]["gateway"]["networks"]
    assert c["services"]["speech"]["environment"]["HF_HUB_OFFLINE"] == "1"


def test_alias_file_is_identical_in_compose_and_cluster():
    (cm,) = [d for d in _k8s_docs() if d["kind"] == "ConfigMap"]
    assert json.loads(cm["data"]["model_aliases.json"]) == json.loads(ALIASES.read_text())
    assert json.loads(ALIASES.read_text())["whisper-1"].startswith("Systran/faster-whisper-"), (
        "whisper-1 must not fall back to Speaches' large-v3 default: a GPU-sized download"
    )


# ---- the live path -----------------------------------------------------------------------


def _multipart_transcribe(gateway_url, headers, wav: bytes) -> httpx.Response:
    return httpx.post(
        f"{gateway_url}/v1/audio/transcriptions",
        headers=headers,
        data={"model": "speech-stt"},
        files={"file": ("speech.wav", wav, "audio/wav")},
        timeout=180,
    )


def test_agent_key_speaks_then_transcribes_and_both_calls_are_billed_to_it(
    gateway_url, master_headers, named_key_headers
):
    phrase = "The gateway routes speech locally."
    spoken = httpx.post(
        f"{gateway_url}/v1/audio/speech",
        headers=named_key_headers,
        json={"model": "speech-tts", "voice": "af_heart", "input": phrase, "response_format": "wav"},
        timeout=180,
    )
    assert spoken.status_code == 200, spoken.text[:300]
    assert spoken.content[:4] == b"RIFF", "TTS did not return WAV audio"

    heard = _multipart_transcribe(gateway_url, named_key_headers, spoken.content)
    assert heard.status_code == 200, heard.text[:300]
    got = re.sub(r"[^a-z ]", "", heard.json()["text"].lower()).split()
    want = re.sub(r"[^a-z ]", "", phrase.lower()).split()
    assert got == want, f"round trip changed the words: {heard.json()['text']!r}"

    alias = None
    deadline = time.time() + 90
    spend: dict[str, float] = {}
    while time.time() < deadline and len(spend) < 2:
        # Scoped server-side to this key's hash. Unscoped, /spend/logs returns the whole
        # ledger: measured at 323MB / 55s on a stack that has run the suite a few times,
        # which blew the 60s read timeout and failed this test on ledger size, not on
        # speech. The client-side filter below is kept, so the claim is unchanged.
        rows = httpx.get(
            f"{gateway_url}/spend/logs", headers=master_headers,
            params={"api_key": _hash(named_key_headers)}, timeout=60,
        ).json()
        # The newest key is ours: named_key_headers mints one per test.
        mine = [r for r in rows if r["call_type"] in ("aspeech", "atranscription")
                and r["api_key"] == _hash(named_key_headers)]
        spend = {r["call_type"]: r["spend"] for r in mine}
        alias = alias or (mine[0]["metadata"].get("user_api_key_alias") if mine else None)
        time.sleep(3)
    assert set(spend) == {"aspeech", "atranscription"}, f"no ledger row per call: {spend}"
    assert all(v > 0 for v in spend.values()), f"audio metered at $0: {spend}"
    assert alias and alias.endswith("::terminal")


def _hash(headers: dict) -> str:
    import hashlib

    return hashlib.sha256(headers["Authorization"].removeprefix("Bearer ").encode()).hexdigest()


def test_the_master_key_cannot_buy_audio(gateway_url, master_headers):
    r = _multipart_transcribe(gateway_url, master_headers, b"RIFF" + uuid.uuid4().bytes)
    assert r.status_code >= 400
    assert "no_attributable_principal" in r.text


# ---- the live seal -----------------------------------------------------------------------

_PROBE = (
    "import socket,sys\n"
    "try:\n"
    "    socket.create_connection((sys.argv[1],443),timeout=5).close();print('CONNECTED')\n"
    "except Exception as e:\n"
    "    print('REFUSED',type(e).__name__)\n"
)


def _probe(service: str, host: str) -> str:
    from conftest import compose

    r = compose("exec", "-T", service, "python3", "-c", _PROBE, host, check=False)
    assert r.returncode == 0, f"probe could not run in {service}: {r.stderr[-300:]}"
    return r.stdout.strip()


def test_running_speech_container_cannot_reach_the_internet_but_a_peer_can():
    """LIVE, on the compose stack. The same probe runs from the speech container and from the
    gateway (a non-sealed container on the default network). The positive control proves the
    host has egress and the probe works; only then is the speech refusal meaningful, and the
    only thing that differs between the two is the speech-internal network."""
    for host in ("huggingface.co", "1.1.1.1"):
        assert _probe("gateway", host) == "CONNECTED", f"positive control failed for {host}"
        assert _probe("speech", host).startswith("REFUSED"), f"speech reached {host}"
