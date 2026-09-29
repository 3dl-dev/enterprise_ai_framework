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


def test_speech_server_is_sealed_to_dns_only_and_offline():
    docs = _k8s_docs()
    (pol,) = [d for d in docs if d["kind"] == "NetworkPolicy"]
    assert pol["spec"]["podSelector"]["matchLabels"] == {"app": "speech"}
    assert set(pol["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    for rule in pol["spec"]["egress"]:
        assert {p["port"] for p in rule["ports"]} == {53}, "egress beyond DNS"
    assert [r["from"][0]["podSelector"]["matchLabels"] for r in pol["spec"]["ingress"]] == [
        {"app": "gateway"}
    ], "only the gateway may reach the speech server"
    (dep,) = [d for d in docs if d["kind"] == "Deployment"]
    env = {e["name"]: e["value"] for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["HF_HUB_OFFLINE"] == "1"
    # The fetch Job is the ONLY hub-reaching workload, and must not be covered by the policy.
    (job,) = [d for d in docs if d["kind"] == "Job"]
    assert job["spec"]["template"]["metadata"]["labels"] != {"app": "speech"}


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
