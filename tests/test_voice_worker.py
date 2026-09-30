"""The voice worker's shape, as files (enterpriseaiframework-82e, Contract H).

These are the invariants the design record and the 2026-09-29 rulings put on the worker, made
mechanical. Each is checked by a function that is ALSO pointed at a poisoned COPY of the file
it reads (the tests at the bottom), so a checker that cannot fail is caught here:

  * livekit-agents==1.6.0 exactly (1.6.1+ hard-depends on livekit-local-inference, whose
    LiveKit Model License is non-OSI), and no turn-detector / noise-cancellation / Krisp /
    local-inference package anywhere in the image's requirements;
  * the worker code constructs only the OpenAI-shaped STT/LLM/TTS plugins and Silero VAD,
    every one from the ONE base URL and session token, and never touches LiveKit Inference,
    noise cancellation, or a turn detector;
  * the manifest sets NO `OTEL_*` variable (livekit-agents 1.6.0 depends on
    opentelemetry-exporter-otlp; the no-telemetry invariant must not rest on its defaults),
    holds no gateway/user credential, and the NetworkPolicy admits egress to DNS, LiveKit and
    the control plane only;
  * the Dockerfile loads the VAD offline at build time.

The live proof that the real image does all of this is tests-live/test_voice_live.py.
"""
import ast
import importlib.util
import json
import re
import shutil
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WORKER = REPO / "voice-worker"
MANIFEST = REPO / "deploy" / "k8s" / "73-voice-worker.yaml"

FORBIDDEN_PACKAGES = ("turn-detector", "noise-cancellation", "krisp", "local-inference",
                      "ai-coustics", "livekit-plugins-deepgram", "livekit-plugins-cartesia")
ALLOWED_PLUGIN_IMPORTS = {"openai", "silero"}


def requirement_violations(path: Path) -> list[str]:
    bad = []
    lines = [ln.split("#")[0].strip() for ln in path.read_text().splitlines()]
    reqs = [ln for ln in lines if ln]
    if "livekit-agents==1.6.0" not in reqs:
        bad.append("livekit-agents is not pinned to ==1.6.0")
    for r in reqs:
        if any(f in r.lower() for f in FORBIDDEN_PACKAGES):
            bad.append(f"forbidden package: {r}")
        if r.startswith("livekit-plugins-") and not r.split("==")[0][len("livekit-plugins-"):] in ALLOWED_PLUGIN_IMPORTS:
            bad.append(f"unexpected plugin: {r}")
        if r.startswith("livekit") and "==" not in r:
            bad.append(f"unpinned: {r}")
    return bad


def worker_violations(path: Path) -> list[str]:
    src = path.read_text()
    tree = ast.parse(src)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "livekit.plugins":
                for a in node.names:
                    if a.name not in ALLOWED_PLUGIN_IMPORTS:
                        bad.append(f"imports plugin {a.name}")
            if node.module.startswith("livekit.plugins.") and \
                    node.module.split(".")[2] not in ALLOWED_PLUGIN_IMPORTS:
                bad.append(f"imports {node.module}")
            if "inference" in node.module or "noise" in node.module or "turn_detector" in node.module:
                bad.append(f"imports {node.module}")
            for a in node.names:
                if a.name in ("inference", "TurnDetector", "AdaptiveInterruptionDetector") \
                        or "noise" in a.name or "krisp" in a.name.lower():
                    bad.append(f"imports {a.name} from {node.module}")
        if isinstance(node, ast.Import):
            for a in node.names:
                if any(k in a.name for k in ("inference", "noise", "turn_detector", "krisp")):
                    bad.append(f"imports {a.name}")
        if isinstance(node, ast.Attribute) and node.attr in (
                "inference", "TurnDetector", "AdaptiveInterruptionDetector", "noise_cancellation"):
            bad.append(f"uses .{node.attr}")
    for token in ("noise_cancellation", "BVC", "turn_detector", "MultilingualModel", "inference."):
        if token in re.sub(r'""".*?"""', "", src, flags=re.S):
            bad.append(f"mentions {token} outside the docstring")
    # exactly one construction each of STT, LLM, TTS, from the shared auth kwargs
    calls = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "openai":
            calls.setdefault(node.func.attr, []).append(node)
    if sorted(calls) != ["LLM", "STT", "TTS"] or any(len(v) != 1 for v in calls.values()):
        bad.append(f"plugins constructed: {sorted(calls)} (want exactly one each of STT, LLM, TTS)")
    for name, (call,) in ((k, v) for k, v in calls.items() if len(v) == 1):
        splat = [k for k in call.keywords if k.arg is None]
        if not splat or ast.unparse(splat[0].value) != "auth":
            bad.append(f"openai.{name} is not built from the shared auth (base_url, api_key)")
        if any(k.arg in ("base_url", "api_key", "client") for k in call.keywords):
            bad.append(f"openai.{name} sets its own endpoint or credential")
    auth_assigns = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                    and any(getattr(t, "id", "") == "auth" for t in n.targets)]
    if len(auth_assigns) != 1 or ast.unparse(auth_assigns[0].value) != \
            "dict(base_url=cfg.base_url, api_key=cfg.session_token)":
        bad.append("`auth` is not exactly the settings' base_url and session token")
    if "close_on_disconnect=False" not in src:
        bad.append("close_on_disconnect is not False: the session closes on the first disconnect and a quick rejoin finds no agent")
    if "use_realtime=False" not in src:
        bad.append("STT may use the realtime websocket")
    if 'turn_handling={"turn_detection": "vad", "interruption": {"mode": "vad"}}' not in src:
        bad.append("turn/interruption handling is not pinned to VAD")
    return bad


def manifest_violations(path: Path) -> list[str]:
    docs = [d for d in yaml.safe_load_all(path.read_text()) if d]
    bad = []
    dep = next(d for d in docs if d["kind"] == "Deployment")
    pod = dep["spec"]["template"]["spec"]
    if pod.get("hostNetwork"):
        bad.append("hostNetwork")
    if pod.get("automountServiceAccountToken") is not False:
        bad.append("the pod may read the Kubernetes API token")
    for c in pod["containers"]:
        for e in c.get("env", []):
            if e["name"].startswith("OTEL_"):
                bad.append(f"telemetry variable {e['name']}")
            ref = (e.get("valueFrom") or {}).get("secretKeyRef") or {}
            if ref and ref.get("key") not in ("LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
                bad.append(f"credential {ref.get('key')} in the worker env")
            if e["name"] == "VOICE_AUDIO_BASE" and e.get("value") != "http://control-plane:8000/voice/v1":
                bad.append(f"audio base is {e.get('value')}")
        if any(e["name"] == "OPENAI_API_KEY" for e in c.get("env", [])):
            bad.append("OPENAI_API_KEY in the worker env")
        if c["image"] != "__VOICE_WORKER_IMAGE__":
            bad.append(f"image {c['image']} bypasses the substituted reference")
    pol = next(d for d in docs if d["kind"] == "NetworkPolicy")["spec"]
    if pol.get("ingress"):
        bad.append("something may connect to the worker")
    if set(pol["policyTypes"]) != {"Ingress", "Egress"}:
        bad.append("policy does not govern both directions")
    allowed = []
    for rule in pol["egress"]:
        for peer in rule["to"]:
            if "ipBlock" in peer:
                bad.append(f"egress to ipBlock {peer['ipBlock']}")
            sel = (peer.get("podSelector") or {}).get("matchLabels") or {}
            allowed.append(sel.get("k8s-app") or sel.get("app") or "?")
        if not rule.get("ports"):
            bad.append("an egress rule with no port restriction")
    if sorted(allowed) != ["control-plane", "kube-dns", "livekit"]:
        bad.append(f"egress peers are {sorted(allowed)}")
    return bad


def dockerfile_violations(path: Path) -> list[str]:
    t = "\n".join(ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#"))
    bad = []
    if "HF_HUB_OFFLINE=1" not in t:
        bad.append("image does not run offline")
    if "silero.VAD.load()" not in t:
        bad.append("build does not load the VAD")
    elif "HF_HUB_OFFLINE=1" in t and t.index("HF_HUB_OFFLINE=1") > t.index("silero.VAD.load()"):
        bad.append("the VAD is loaded before the image is offline")
    if "download-files" in t:
        bad.append("build downloads model files")
    return bad


# ------------------------------------------------------------------ the real files

def test_requirements_are_pinned_and_carry_no_cloud_or_non_osi_piece():
    assert requirement_violations(WORKER / "requirements.txt") == []


def test_the_worker_uses_only_the_openai_shaped_plugins_and_silero_from_one_base():
    assert worker_violations(WORKER / "worker.py") == []


def test_the_manifest_sets_no_telemetry_holds_no_gateway_key_and_egresses_to_three_peers():
    assert manifest_violations(MANIFEST) == []


def test_the_image_loads_the_vad_offline_at_build_time():
    assert dockerfile_violations(WORKER / "Dockerfile") == []


def test_the_rendered_manifest_has_no_otel_exporter_endpoint_anywhere():
    # render exactly as deploy.sh does, then look at the whole document, not just env names
    text = MANIFEST.read_text().replace("__VOICE_WORKER_IMAGE__", "reg/eaf-voice-worker:t")
    assert "OTEL_EXPORTER" not in text.split("apiVersion:", 1)[1]
    for d in yaml.safe_load_all(text):
        assert "OTEL" not in json.dumps(d)


def test_deploy_substitutes_the_worker_image_reference():
    sh = (REPO / "deploy" / "bin" / "deploy.sh").read_text()
    assert "__VOICE_WORKER_IMAGE__" in sh and "eaf-voice-worker" in sh


# ------------------------------------------------------------------ settings (pure)

def _settings():
    spec = importlib.util.spec_from_file_location("voice_worker_settings", WORKER / "settings.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_settings_read_the_dispatch_metadata_and_take_the_base_from_the_environment_only():
    s = _settings()
    meta = json.dumps({"session": "tok", "stt_model": "gpt-4o-transcribe", "tts_model": "gpt-4o-mini-tts",
                       "voice": "af_heart", "base_url": "http://evil.example/v1"})
    cfg = s.load(meta, env={})
    assert (cfg.session_token, cfg.voice) == ("tok", "af_heart")
    # the plugin-side models are the contract names whatever the metadata says: tts-1 is what
    # makes livekit-plugins-openai stream plain audio (any other name is parsed as SSE)
    assert (cfg.stt_model, cfg.tts_model) == ("whisper-1", "tts-1")
    assert cfg.base_url == "http://control-plane:8000/voice/v1", "a metadata base_url is ignored"
    assert s.load(meta, env={"VOICE_AUDIO_BASE": "http://cp:8000/voice/v1/"}).base_url == \
        "http://cp:8000/voice/v1"


@pytest.mark.parametrize("bad", ["", "not json", "{}", '{"session":"t"}', "[]",
                                 json.dumps({"session": "", "voice": "c"}),
                                 json.dumps({"session": "t", "voice": 1})])
def test_settings_refuse_metadata_that_is_not_a_voice_session(bad):
    with pytest.raises(ValueError):
        _settings().load(bad, env={})


# ------------------------------------------------------------------ the checkers can fail

def _copy(tmp_path, src: Path, name=None) -> Path:
    dst = tmp_path / (name or src.name)
    shutil.copy(src, dst)
    return dst


def test_checker_catches_a_version_bump_and_a_smuggled_package(tmp_path):
    p = _copy(tmp_path, WORKER / "requirements.txt")
    p.write_text(p.read_text().replace("livekit-agents==1.6.0", "livekit-agents==1.6.1"))
    assert any("not pinned" in v for v in requirement_violations(p))
    p.write_text((WORKER / "requirements.txt").read_text() + "livekit-plugins-turn-detector==1.6.0\n")
    assert any("turn-detector" in v for v in requirement_violations(p))
    p.write_text((WORKER / "requirements.txt").read_text() + "livekit-plugins-noise-cancellation==0.2\n")
    assert any("noise-cancellation" in v for v in requirement_violations(p))
    p.write_text((WORKER / "requirements.txt").read_text() + "livekit-plugins-deepgram==1.6.0\n")
    assert requirement_violations(p)


def test_checker_catches_otel_in_env_and_a_widened_egress(tmp_path):
    p = _copy(tmp_path, MANIFEST)
    doc = p.read_text()
    p.write_text(doc.replace('- { name: HF_HUB_OFFLINE, value: "1" }',
                             '- { name: HF_HUB_OFFLINE, value: "1" }\n'
                             '            - { name: OTEL_EXPORTER_OTLP_ENDPOINT, value: "http://collector:4317" }'))
    assert any("OTEL_EXPORTER_OTLP_ENDPOINT" in v for v in manifest_violations(p))
    # a nearby wrong value: the same fault under a different OTEL_ name
    p.write_text(doc.replace('- { name: HF_HUB_OFFLINE, value: "1" }',
                             '- { name: HF_HUB_OFFLINE, value: "1" }\n'
                             '            - { name: OTEL_TRACES_EXPORTER, value: "otlp" }'))
    assert any("OTEL_TRACES_EXPORTER" in v for v in manifest_violations(p))
    p.write_text(doc.replace("        - podSelector: { matchLabels: { app: control-plane } }\n      ports:",
                             "        - ipBlock: { cidr: 0.0.0.0/0 }\n      ports:"))
    assert any("ipBlock" in v for v in manifest_violations(p))
    p.write_text(doc.replace("  ingress: []", "  ingress: [{}]"))
    assert any("connect to the worker" in v for v in manifest_violations(p))


def test_checker_catches_a_gateway_key_and_a_foreign_audio_base(tmp_path):
    p = _copy(tmp_path, MANIFEST)
    doc = p.read_text()
    p.write_text(doc.replace('value: "http://control-plane:8000/voice/v1"', 'value: "http://gateway:4000/v1"'))
    assert any("audio base" in v for v in manifest_violations(p))
    p.write_text(doc.replace("key: LIVEKIT_API_SECRET", "key: GATEWAY_MASTER_KEY"))
    assert any("GATEWAY_MASTER_KEY" in v for v in manifest_violations(p))


def test_checker_catches_livekit_inference_a_second_base_and_a_turn_detector(tmp_path):
    src = (WORKER / "worker.py").read_text()
    p = tmp_path / "worker.py"
    p.write_text(src.replace("from livekit.plugins import openai, silero",
                             "from livekit.plugins import openai, silero, noise_cancellation"))
    assert worker_violations(p)
    p.write_text(src.replace("openai.TTS(model=cfg.tts_model, voice=cfg.voice, response_format=\"wav\", **auth)",
                             "openai.TTS(model=cfg.tts_model, voice=cfg.voice, base_url=\"https://api.openai.com/v1\", **auth)"))
    assert any("own endpoint" in v for v in worker_violations(p))
    p.write_text(src.replace('llm=openai.LLM(model=cfg.llm_model, **auth)', 'llm="openai/gpt-4o"'))
    assert worker_violations(p)
    p.write_text(src.replace('"interruption": {"mode": "vad"}', '"interruption": {"mode": "adaptive"}'))
    assert any("pinned to VAD" in v for v in worker_violations(p))
    mutated = src.replace("from livekit.agents import (\n", "from livekit.agents import (\n    inference,\n")
    assert mutated != src, "the mutation must actually change the file"
    p.write_text(mutated)
    assert worker_violations(p)
    # and a nearby wrong value: the same fault as a separate statement
    p.write_text(src.replace("import settings\n", "import settings\nfrom livekit.agents import inference\n"))
    assert any("inference" in v for v in worker_violations(p))
    # the session must not end under a rejoining user
    p.write_text(src.replace("close_on_disconnect=False", "close_on_disconnect=True"))
    assert any("close_on_disconnect" in v for v in worker_violations(p))


def test_checker_catches_a_dockerfile_that_fetches_or_loads_online(tmp_path):
    p = _copy(tmp_path, WORKER / "Dockerfile")
    doc = p.read_text()
    p.write_text(doc.replace("ENV HF_HUB_OFFLINE=1 ", "ENV "))
    assert dockerfile_violations(p)
    p.write_text(doc.replace("RUN pip install", "RUN python worker.py download-files\nRUN pip install"))
    assert any("downloads" in v for v in dockerfile_violations(p))
