"""LIVE: a user speaks to their Raven from the portal and hears it answer in its registered voice.

Item enterpriseaiframework-82e (Contract H). Nothing in the path is mocked. The stack is a set of
throwaway `-82e` resources (tests-live/voice_stack.py): the branch's control plane, the voice
worker image, a real LiveKit server, the OSS profile's LiteLLM gateway with the local speech server
(Speaches: faster-whisper + Kokoro), and a real hosted Raven answering through a real model. The
"user" is headless Chromium loading the REAL portal page, with a fake microphone playing a spoken
question, joining the real room through the token endpoint. Every claim is read from a source
independent of the code under test:

  * WHAT WAS HEARD: the remote audio track the page received is captured (MediaRecorder) and
    transcribed by the speech server directly (no ledger row), not by anything the worker did;
  * WHICH VOICE: median fundamental frequency of the received audio against reference renders of
    the same words in each catalogue voice. The question is put in a FEMALE voice so a Raven that
    merely echoed the user, or ignored its pin, would not read as the pinned voice. The registry is
    changed (input poisoned through the API) and the audible voice must change with it;
  * SPEND: the gateway's own ledger (/spend/logs scoped server-side to the Raven's key hash), as a
    before/after delta, with the Raven's alias read off the ledger rows;
  * THE FENCE: the worker's NetworkPolicy is proven by a probe with a positive control, and
    by removing the policy and watching the refusal disappear (so the refusal was the fence's).

Running it needs kubectl on a dev cluster with the shared `enterprise-ai` namespace, ffmpeg, the
playwright browser binaries (`make test-browser`'s venv), and two images built on the cluster:

    tests-live/build_voice_images.sh            # prints the two image references
    VOICE_LIVE_CP_IMAGE=... VOICE_LIVE_WORKER_IMAGE=... \\
        .venv-test/bin/pytest tests-live/test_voice_live.py -v

A missing image, ffmpeg or browser FAILS the run; it never skips.
"""
import array
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import wave
import io
from pathlib import Path

import pytest

import voice_stack as vs

REGISTRY = os.environ.get("RAIL_REGISTRY", "192.168.2.43:30500")
CP_IMAGE = os.environ.get("VOICE_LIVE_CP_IMAGE", f"{REGISTRY}/enterprise-ai-control-plane:82e-live")
WORKER_IMAGE = os.environ.get("VOICE_LIVE_WORKER_IMAGE", f"{REGISTRY}/eaf-voice-worker:82e-live")
PORTS = {"speech": 18100, "cp": 18101, "gw": 18102}
NAME = "r82e"
ALIAS = f"{vs.USER}::agents/{NAME}"
QUESTION = "Hello. Please tell me what the capital of France is."
HDRS = {"X-Auth-Request-Preferred-Username": vs.USER, "content-type": "application/json"}


# ------------------------------------------------------------------ small http / audio helpers

def http(method, url, headers=None, body=None, timeout=120, raw=False):
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
            return r.status, payload if raw else json.loads(payload or b"null")
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload)
        except ValueError:
            return e.code, payload


def cp(method, path, body=None):
    return http(method, f"http://localhost:{PORTS['cp']}{path}", HDRS, body)


def ffmpeg(args: list[str], data: bytes | None = None) -> bytes:
    r = subprocess.run(["ffmpeg", "-loglevel", "error", *args], input=data, capture_output=True)
    assert r.returncode == 0, f"ffmpeg failed: {r.stderr[-300:]}"
    return r.stdout


def to_pcm8k(data: bytes) -> array.array:
    wav = ffmpeg(["-i", "pipe:0", "-ar", "8000", "-ac", "1", "-f", "s16le", "-"], data)
    a = array.array("h")
    a.frombytes(wav)
    return a


def f0_median(x: array.array, rate: int = 8000) -> float | None:
    """Median fundamental over voiced frames (normalised autocorrelation), stdlib only."""
    frame, hop = int(0.04 * rate), int(0.02 * rate)
    lo, hi = rate // 400, rate // 70
    energies = [sum(v * v for v in x[i:i + frame]) / frame for i in range(0, len(x) - frame, hop)]
    if not energies:
        return None
    thr = 0.1 * sorted(energies)[int(0.9 * len(energies))]
    vals = []
    for k, i in enumerate(range(0, len(x) - frame, hop)):
        if energies[k] < thr or energies[k] == 0:
            continue
        seg = x[i:i + frame]
        m = sum(seg) / frame
        seg = [v - m for v in seg]
        r0 = sum(v * v for v in seg)
        if r0 <= 0:
            continue
        best, lag = 0.0, 0
        for L in range(lo, hi):
            c = sum(seg[j] * seg[j + L] for j in range(frame - L)) / r0
            if c > best:
                best, lag = c, L
        if best > 0.5:
            vals.append(rate / lag)
    vals.sort()
    return vals[len(vals) // 2] if len(vals) >= 10 else None


def multipart(fields: dict, fname: str, data: bytes):
    b = uuid.uuid4().hex
    body = b"".join(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                    for k, v in fields.items())
    body += (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{fname}"\r\n'
             f'Content-Type: audio/wav\r\n\r\n').encode() + data + f"\r\n--{b}--\r\n".encode()
    return body, {"content-type": f"multipart/form-data; boundary={b}"}


def speaches_tts(text: str, voice: str) -> bytes:
    s, body = http("POST", f"http://localhost:{PORTS['speech']}/v1/audio/speech",
                   {"content-type": "application/json"},
                   {"model": "tts-1", "voice": voice, "input": text, "response_format": "wav"}, raw=True)
    assert s == 200, body[:200]
    return body


def speaches_stt(wav: bytes) -> str:
    body, h = multipart({"model": "whisper-1", "response_format": "json"}, "a.wav", wav)
    s, out = http("POST", f"http://localhost:{PORTS['speech']}/v1/audio/transcriptions", h, body, timeout=180)
    assert s == 200, out
    return out["text"].strip()


# ------------------------------------------------------------------ the stack

@pytest.fixture(scope="module")
def stack():
    for tool in ("ffmpeg", "kubectl"):
        assert subprocess.run(["which", tool], capture_output=True).returncode == 0, f"{tool} is required"
    for repo, tag in ((CP_IMAGE.split("/", 1)[1].split(":")[0], CP_IMAGE.rsplit(":", 1)[1]),
                      (WORKER_IMAGE.split("/", 1)[1].split(":")[0], WORKER_IMAGE.rsplit(":", 1)[1])):
        s, tags = http("GET", f"http://{REGISTRY}/v2/{repo}/tags/list")
        assert s == 200 and tag in tags.get("tags", []), (
            f"image {repo}:{tag} is not in the registry; run tests-live/build_voice_images.sh")
    vs.teardown()
    time.sleep(3)
    vs.apply(CP_IMAGE, WORKER_IMAGE, REGISTRY)
    pfs = []
    try:
        for dep in ("postgres-82e", "valkey-82e", "gateway-82e", "speech-82e", "livekit-82e", "voice-worker-82e"):
            vs.kubectl("rollout", "status", f"deploy/{dep}", "--timeout=420s")
        end = time.time() + 180
        while "Running" not in vs.kubectl("get", "pod", "cp-82e", "-o", "jsonpath={.status.phase}"):
            assert time.time() < end, "cp-82e never ran"
            time.sleep(2)
        for target, key, remote in (("svc/speech-82e", "speech", 8000), ("pod/cp-82e", "cp", 8000),
                                    ("svc/gateway-82e", "gw", 4000)):
            pfs.append(subprocess.Popen(["kubectl", "-n", vs.NS, "port-forward", target, f"{PORTS[key]}:{remote}"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(4)
        end = time.time() + 120
        while True:  # the control plane needs its schema before it answers
            try:
                if cp("GET", "/portal/api/agents")[0] == 200:
                    break
            except OSError:
                pass
            assert time.time() < end, "the control plane never answered"
            time.sleep(3)
        s, created = cp("POST", "/portal/api/agents", {"name": NAME, "type": "raven", "model": vs.RAVEN_MODEL})
        assert s == 201 and created["alias"] == ALIAS, created
        vs.kubectl("rollout", "status", f"deploy/agent-{vs.USER}-{NAME}", "--timeout=420s")
        yield
    finally:
        for p in pfs:
            p.terminate()
        vs.teardown()


@pytest.fixture(scope="module")
def mic(stack, tmp_path_factory) -> Path:
    """The user's spoken question, in a female voice, after 9s of silence (the worker joins and
    the VAD warms while the fake microphone plays nothing)."""
    wav = speaches_tts(QUESTION, "af_heart")
    out = tmp_path_factory.mktemp("mic") / "mic.wav"
    out.write_bytes(ffmpeg(["-i", "pipe:0", "-af", "adelay=9000|9000,apad=pad_dur=20",
                            "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", "-f", "wav", "-"], wav))
    return out


def ravens_key() -> str:
    sec = json.loads(vs.kubectl("get", "secret", f"agent-{vs.USER}-{NAME}-key", "-o", "json"))
    return base64.b64decode(sec["data"]["OPENAI_API_KEY"]).decode()


def ledger() -> tuple[dict, set]:
    """(per call type: rows and spend, aliases seen), from the gateway's own spend log, scoped to
    the Raven's key hash server-side. The hash comes from the gateway's own /key/info (the key
    identifies itself), never computed here from the plaintext key."""
    s, info = http("GET", f"http://localhost:{PORTS['gw']}/key/info",
                   {"Authorization": f"Bearer {ravens_key()}"}, timeout=60)
    assert s == 200, info
    h = info["info"].get("token") or info["key"]
    s, rows = http("GET", f"http://localhost:{PORTS['gw']}/spend/logs?api_key={h}",
                   {"Authorization": f"Bearer {vs.MASTER_KEY}"}, timeout=120)
    assert s == 200, rows
    mine = [r for r in rows if r.get("api_key") == h]
    by: dict = {}
    for r in mine:
        t = by.setdefault(r["call_type"], {"n": 0, "spend": 0.0})
        t["n"] += 1
        t["spend"] += r["spend"]
    return by, {r["metadata"].get("user_api_key_alias") for r in mine}


BROWSER = r"""
import base64, json, sys, time
from playwright.sync_api import sync_playwright
portal, agent, mic, out, seconds, user = sys.argv[1:7]
seconds = float(seconds)
REC = "() => new Promise((res, rej) => { const el = document.querySelector('#voice-audio audio'); if (!el || !el.srcObject) return rej('none'); const r = new MediaRecorder(el.srcObject, {mimeType: 'audio/webm;codecs=opus'}); window.__rec = {c: [], r}; r.ondataavailable = e => window.__rec.c.push(e.data); r.start(500); res(true); })"
STOP = "() => new Promise(res => { const q = window.__rec; q.r.onstop = async () => { const b = new Uint8Array(await new Blob(q.c).arrayBuffer()); let s = ''; for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode(...b.subarray(i, i + 0x8000)); res(btoa(s)); }; q.r.stop(); })"
rep = {}
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
        f"--use-file-for-fake-audio-capture={mic}%noloop", "--autoplay-policy=no-user-gesture-required"])
    ctx = b.new_context(extra_http_headers={"X-Auth-Request-Preferred-Username": user}, permissions=["microphone"])
    page = ctx.new_page()
    rep["ws"] = []
    page.on("websocket", lambda w: rep["ws"].append(w.url))
    page.goto(portal + "/portal/", wait_until="load")
    page.click("#tab-agents")
    talk = page.locator(f'button[data-voice-talk="{agent}"]')
    talk.wait_for(timeout=30000)
    rep["button_before"] = talk.inner_text()
    talk.click()
    page.wait_for_function("window.eafVoice && window.eafVoice.state === 'live'", timeout=60000)
    rep["button_live"] = talk.inner_text(); rep["voice_id"] = page.evaluate("window.eafVoice.voice")
    t0 = time.time(); end = t0 + 40
    while True:
        try:
            page.evaluate(REC); break
        except Exception as e:
            if time.time() > end:
                rep["error"] = "no remote audio track within 40s"; break
            time.sleep(0.5)
    if "error" not in rep:
        time.sleep(max(0, seconds - (time.time() - t0)))
        open(out, "wb").write(base64.b64decode(page.evaluate(STOP)))
    talk.click()
    page.wait_for_function("window.eafVoice.state === 'idle'", timeout=10000)
    rep["button_after"] = talk.inner_text()
    b.close()
print(json.dumps(rep))
"""


def user_talks(mic: Path, out: Path, seconds: int = 50) -> dict:
    """One real portal session in headless Chromium; returns the page's own report."""
    r = subprocess.run(
        [sys.executable, "-c", BROWSER, f"http://localhost:{PORTS['cp']}", NAME,
         str(mic), str(out), str(seconds), vs.USER], capture_output=True, text=True, timeout=seconds + 150)
    assert r.returncode == 0, f"browser session failed: {r.stderr[-600:]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def heard(webm: Path) -> dict:
    """What the page received: transcript, and how close its pitch is to each catalogue voice."""
    wav = ffmpeg(["-i", str(webm), "-ar", "16000", "-ac", "1", "-f", "wav", "-"])
    text = speaches_stt(wav)
    pcm = to_pcm8k(webm.read_bytes())
    f0 = f0_median(pcm)
    refs = {}
    for v in ("am_michael", "af_heart", "bf_emma"):
        refs[v] = f0_median(to_pcm8k(speaches_tts(text or "The capital of France is Paris.", v)))
    return {"text": text, "f0": f0, "refs": refs,
            "nearest": min(refs, key=lambda v: abs(refs[v] - f0)) if f0 else None}


# ------------------------------------------------------------------ the claims

def test_a_user_says_a_sentence_and_hears_the_raven_answer_in_its_registered_voice(stack, mic, tmp_path):
    s, body = cp("POST", f"/portal/api/agents/{NAME}/voice", {"voice": "kokoro-michael"})
    assert s == 200 and body["entry"]["voice"] == "am_michael", body
    ann = json.loads(vs.kubectl("get", "deploy", f"agent-{vs.USER}-{NAME}", "-o",
                                "jsonpath={.metadata.annotations}"))
    assert ann["agent.enterprise-ai/voice"] == "kokoro-michael", "the pin lives on the object"

    before, _ = ledger()
    page = user_talks(mic, tmp_path / "male.webm")
    assert "error" not in page, page
    assert page["button_before"] == "Talk" and page["button_live"] == "Hang up" and page["button_after"] == "Talk"
    assert page["voice_id"] == "kokoro-michael"
    # SIGNALLING went through the portal origin's /rtc (Baron ruling 2026-09-30), read off the
    # browser's own websocket list, never to a LiveKit address or NodePort
    portal_ws = f"ws://localhost:{PORTS['cp']}/rtc"
    assert page["ws"] and all(u.startswith(portal_ws) for u in page["ws"]), page["ws"]
    assert not any(str(port) in u for u in page["ws"] for port in (7880, *vs.NODEPORTS)), page["ws"]
    got = heard(tmp_path / "male.webm")
    assert "paris" in got["text"].lower(), f"the Raven's answer did not come back as speech: {got}"
    assert got["f0"] and got["nearest"] == "am_michael" and abs(got["f0"] - got["refs"]["am_michael"]) < 12, got
    assert got["refs"]["af_heart"] - got["f0"] > 40, f"not clearly distinct from the asker's voice: {got}"

    # SPEND: every leg of that one conversation is on the Raven's alias, in the gateway's own ledger
    deadline = time.time() + 90
    while True:
        after, aliases = ledger()
        delta = {k: after[k]["n"] - before.get(k, {"n": 0})["n"] for k in after}
        if all(delta.get(k, 0) >= 1 for k in ("aspeech", "atranscription", "acompletion")) or time.time() > deadline:
            break
        time.sleep(3)
    assert aliases == {ALIAS}, aliases
    for k in ("aspeech", "atranscription", "acompletion"):
        spent = after[k]["spend"] - before.get(k, {"spend": 0.0})["spend"]
        assert delta.get(k, 0) >= 1 and spent > 0, f"{k}: no priced ledger row on {ALIAS}: before={before} after={after}"


def test_changing_the_registry_changes_the_voice_the_user_hears(stack, mic, tmp_path):
    """The registry is the only input changed (through the API, onto the Deployment annotation);
    the audible voice must follow, in the other direction from the first test."""
    s, body = cp("POST", f"/portal/api/agents/{NAME}/voice", {"voice": "kokoro-heart"})
    assert s == 200 and body["entry"]["voice"] == "af_heart", body
    page = user_talks(mic, tmp_path / "female.webm")
    assert "error" not in page and page["voice_id"] == "kokoro-heart", page
    got = heard(tmp_path / "female.webm")
    assert "paris" in got["text"].lower(), got
    assert got["f0"] and got["nearest"] == "af_heart" and abs(got["f0"] - got["refs"]["af_heart"]) < 12, got
    assert got["f0"] - got["refs"]["am_michael"] > 40, got


def test_hanging_up_and_talking_again_at_once_still_gets_an_agent(stack, mic, tmp_path):
    """The rejoin race: the room outlives the user by its empty timeout and the previous agent is
    still leaving. Two sessions back to back, both must be answered."""
    for i in (1, 2):
        page = user_talks(mic, tmp_path / f"rejoin{i}.webm")
        assert "error" not in page, (i, page)
        assert "paris" in heard(tmp_path / f"rejoin{i}.webm")["text"].lower(), i


def test_voice_audio_and_turns_are_audited_against_the_ravens_owner(stack):
    rows = vs.kubectl("exec", "deploy/postgres-82e", "--", "psql", "-U", "eaf", "-d", "controlplane", "-At", "-F", "|",
                      "-c", "select actor, action, target, detail from audit_event where action like 'voice.%'")
    kinds = {json.loads(r.split("|", 3)[3]).get("kind") for r in rows.splitlines() if "voice.audio" in r}
    assert {"stt", "tts"} <= kinds, rows[-600:]
    assert all(r.startswith(f"{vs.USER}|") and f"|{vs.USER}/{NAME}|" in r for r in rows.splitlines()), rows[-600:]
    assert any("|voice.turn|" in r for r in rows.splitlines())
    assert "am_michael" in rows and "af_heart" in rows, "each audited TTS call names the registry's voice"


PROBE = ("import socket,sys\n"
         "try:\n socket.create_connection((sys.argv[1],int(sys.argv[2])),timeout=4).close();print('CONNECTED')\n"
         "except Exception as e:\n print('REFUSED')\n")


def probe(pod: str, host: str, port: int) -> str:
    return vs.kubectl("exec", "-i", pod, "--", "python", "-c", PROBE, host, str(port)).strip()


def test_the_workers_network_fence_is_the_thing_that_refuses_and_only_three_peers_are_open(stack):
    worker = vs.kubectl("get", "pod", "-l", "app=voice-worker-82e", "-o", "jsonpath={.items[0].metadata.name}")
    ctl = "cp-82e"  # no policy governs it: the positive control for every probe
    closed = [("gateway-82e", 4000), ("postgres-82e", 5432), ("1.1.1.1", 443),
              (f"agent-{vs.USER}-{NAME}", 18793)]
    for host, port in closed:
        assert probe(ctl, host, port) == "CONNECTED", f"control cannot reach {host}:{port}; the probe proves nothing"
        assert probe(worker, host, port) == "REFUSED", f"the worker reached {host}:{port}"
    for host, port in [("cp-82e", 8000), ("livekit-82e", 7880)]:
        assert probe(worker, host, port) == "CONNECTED", f"the worker cannot reach {host}:{port}"

    # The refusal is the POLICY's. Mutation 1: remove it and the gateway becomes reachable.
    manifest = vs.kubectl("get", "networkpolicy", "voice-worker-isolation-82e", "-o", "yaml")
    vs.kubectl("delete", "networkpolicy", "voice-worker-isolation-82e")
    try:
        time.sleep(8)
        assert probe(worker, "gateway-82e", 4000) == "CONNECTED", "refusal was not caused by the policy"
    finally:
        vs.kubectl("apply", "-f", "-", inp=re.sub(r"\n  (uid|resourceVersion|creationTimestamp):.*", "", manifest))
    time.sleep(8)
    assert probe(worker, "gateway-82e", 4000) == "REFUSED"
    # Mutation 2, a different one: WIDEN the policy by one peer instead of removing it.
    vs.kubectl("patch", "networkpolicy", "voice-worker-isolation-82e", "--type=json", "-p", json.dumps([
        {"op": "add", "path": "/spec/egress/-", "value": {
            "to": [{"podSelector": {"matchLabels": {"app": "gateway-82e"}}}],
            "ports": [{"protocol": "TCP", "port": 4000}]}}]))
    try:
        time.sleep(8)
        assert probe(worker, "gateway-82e", 4000) == "CONNECTED", "a widened policy did not open the gateway"
        assert probe(worker, "postgres-82e", 5432) == "REFUSED", "widening one peer opened another"
    finally:
        vs.kubectl("apply", "-f", "-", inp=re.sub(r"\n  (uid|resourceVersion|creationTimestamp):.*", "", manifest))
        time.sleep(8)
    assert probe(worker, "gateway-82e", 4000) == "REFUSED"


def test_the_running_worker_sets_no_telemetry_endpoint_and_holds_no_gateway_key(stack):
    env = vs.kubectl("exec", "deploy/voice-worker-82e", "--", "env")
    names = {ln.split("=", 1)[0] for ln in env.splitlines() if "=" in ln}
    assert not [n for n in names if n.startswith("OTEL_")], names
    assert "OPENAI_API_KEY" not in names and "LITELLM_MASTER_KEY" not in names
    assert "sk-" not in env, "a gateway key is in the worker's environment"
    assert "VOICE_AUDIO_BASE=http://cp-82e:8000/voice/v1" in env
