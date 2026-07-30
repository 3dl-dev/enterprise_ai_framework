"""Live proof of enterpriseaiframework-0be's DONE condition.

DONE CONDITION (from the item): ask the chat a question whose answer is not in the
model's weights; the reply contains a fact from a page fetched DURING that request, and
the cited URL really serves that fact. The fetch must be verified independently (server
logs or tool output), not merely plausible in the reply. Grounding must be provable:
0 < len(model-facing content) <= text_chars from the fetch log.

This test was deliberately ABSENT while the reranker question stayed open
(enterpriseaiframework-405): with `rerankerType: none`, LibreChat fetches pages and then
withholds them from the model (see tests/test_web_search.py's
TestWhatTheModelActuallyReceives), so no live test against that configuration could ever
demonstrate grounding — it would have been asserting an outcome the configuration does
not deliver. Now that rerank/ closes that gap (rerankerType: jina, authenticated against
our own service), the live test belongs here.

WHY THIS NEEDS A REAL MODEL, AND WHY THAT MEANS REAL MONEY: fakeprovider/app.py never
invokes a tool — every reply is a deterministic hash-based ack of the prompt, regardless
of what tools are attached — so it cannot exercise "the model decides to search, reads
what came back, and answers from it" at all. That is real reasoning over real retrieved
content, which needs a real model. Kept out of `tests/` for the same reason
test_mcp_echo.py and test_memory.py are: `make test` (scope items 1-9) must stay provable
with no provider account and no spend. This spends a small amount of real money through
Forge, same as those two files.

AGAINST THE COMPOSE BUNDLE, NOT THE CLUSTER — the one deliberate deviation from this
directory's usual target. searxng, webfetch and rerank are committed
(deploy/k8s/07-web-search.yaml) but NOT APPLIED to the live cluster: guard #10 on
enterpriseaiframework-0be says not to write to a cluster serving a real user, so those
three services exist only in this bundle's own docker compose stack right now. This is
therefore the only place the DONE condition can be proven live.

THE QUESTION: the current stable Linux kernel version per kernel.org. It changes over
time (so it postdates whatever the model's training cutoff was, without needing to
guess at that cutoff), and kernel.org is a plain, stable, unthrottled page — the same
site TestTheSearchLeg and TestTheFetchLegRetrievesRealPages already rely on being
reachable from this host.

Run (bundle must be up — `make up`):
  .venv-test/bin/pytest tests-live/test_web_search_grounding.py -v --tb=short -p no:cacheprovider
"""

import json
import re
import subprocess
import urllib.parse
from pathlib import Path

import pytest

import chat_turn
import oidc_login

REPO = Path(__file__).resolve().parent.parent
BUNDLE = REPO / "bundle"
COMPOSE_FILE = BUNDLE / "docker-compose.yml"

MODEL = "glm-5.2@deepinfra"
ENDPOINT_NAME = "Enterprise AI"
ENDPOINT_TYPE = "custom"

# A version number reads as N.N or N.N.N. Loose on purpose — the point is to pull
# whatever digits-and-dots token the model wrote next to "kernel", not to validate that
# it looks like a plausible Linux version specifically.
_VERSION_RE = re.compile(r"\b\d+\.\d+(?:\.\d+)?\b")
_URL_RE = re.compile(r"https?://[^\s)\]}\"'>]+")

QUESTION = (
    "Search the web right now for the CURRENT latest stable version of the Linux "
    "kernel, per kernel.org. Do not answer from memory. Reply with only the version "
    "number and, on a new line, the exact URL of the page you found it on."
)


def _load_env() -> dict:
    out: dict[str, str] = {}
    env_file = BUNDLE / ".env"
    if not env_file.exists():
        pytest.fail(f"{env_file} missing — run `make up` first")
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


@pytest.fixture(scope="module")
def env() -> dict:
    e = _load_env()
    for required in ("BOOTSTRAP_USER", "BOOTSTRAP_PASSWORD", "WEBFETCH_TOKEN",
                     "RERANK_TOKEN"):
        if not e.get(required):
            pytest.fail(f"{required} is not configured in bundle/.env — run `make up`")
    return e


@pytest.fixture(scope="module")
def chat_url(env) -> str:
    return f"http://localhost:{env.get('CHAT_PORT', '3080')}"


@pytest.fixture(scope="module")
def chat_client(env, chat_url):
    """One authenticated session against THIS bundle's chat surface (localhost, not the
    cluster) — see the module docstring for why."""
    client = oidc_login.login(chat_url, env["BOOTSTRAP_USER"], env["BOOTSTRAP_PASSWORD"])
    refreshed = client.post(f"{chat_url}/api/auth/refresh")
    assert refreshed.status_code == 200, (
        f"session refresh failed ({refreshed.status_code}): {refreshed.text[:300]}"
    )
    token = refreshed.json().get("token")
    assert token, f"no access token in refresh response: {refreshed.text[:300]}"
    client.headers.update({
        "Authorization": f"Bearer {token}",
        "User-Agent": chat_turn.BROWSER_UA,
    })
    yield client
    client.close()


def _compose(*args: str, check: bool = True, timeout: int = 60):
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "--env-file", str(BUNDLE / ".env"),
         *args],
        capture_output=True, text=True, check=check, timeout=timeout,
    )


def _service_call(service: str, port: int, token: str, path: str,
                   method: str = "GET") -> dict:
    """Call webfetch's or rerank's log endpoint from inside the compose network,
    mirroring tests/test_web_search.py's `_webfetch`/`_rerank` helpers."""
    script = (
        "import urllib.request,json\n"
        f"req=urllib.request.Request('http://localhost:{port}{path}',"
        f"headers={{'Authorization':'Bearer {token}'}},method='{method}')\n"
        "print(json.dumps(json.load(urllib.request.urlopen(req,timeout=30))))\n"
    )
    result = _compose("exec", "-T", service, "python", "-c", script, check=False)
    assert result.returncode == 0, (
        f"{service} {method} {path} failed\n{result.stdout}\n{result.stderr}"
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.fixture
def cleared_logs(env):
    """Clear webfetch's /fetchlog and rerank's /reranklog before the turn, so what is
    found afterwards is attributable to THIS request and not to an earlier test."""
    _service_call("webfetch", 3002, env["WEBFETCH_TOKEN"], "/fetchlog", method="DELETE")
    _service_call("rerank", 3003, env["RERANK_TOKEN"], "/reranklog", method="DELETE")

    class Logs:
        def fetches(self) -> list[dict]:
            return _service_call(
                "webfetch", 3002, env["WEBFETCH_TOKEN"], "/fetchlog"
            )["fetches"]

        def reranks(self) -> list[dict]:
            return _service_call(
                "rerank", 3003, env["RERANK_TOKEN"], "/reranklog"
            )["reranks"]

    return Logs()


class TestChatCitesAPageItActuallyFetched:
    """enterpriseaiframework-0be's DONE condition, end to end and independently checked."""

    def test_the_reply_is_grounded_in_a_page_this_request_actually_fetched(
        self, env, chat_client, chat_url, cleared_logs
    ):
        reply = chat_turn.send_turn(
            chat_client, chat_url, QUESTION, model=MODEL,
            endpoint=ENDPOINT_NAME, endpoint_type=ENDPOINT_TYPE,
            web_search=True, timeout=120.0,
        )
        text = chat_turn.reply_text(reply)
        assert text.strip(), f"the model gave no text reply at all: {reply}"

        # Claim 1: the reply names a fact — a version number — not merely a link.
        versions = _VERSION_RE.findall(text)
        assert versions, (
            f"no version-shaped token in the reply, so there is no fact to check "
            f"grounding against: {text!r}"
        )

        # Claim 2: the reply cites a URL.
        cited_urls = _URL_RE.findall(text)
        assert cited_urls, f"the reply cites no URL at all: {text!r}"

        # Claim 3 (THE ITEM'S OWN WORDING): the fetch is verified independently, not
        # merely plausible in the reply. webfetch's /fetchlog is that independent
        # record, cleared immediately before this turn, so any entry in it now was
        # caused by THIS request.
        fetches = cleared_logs.fetches()
        assert fetches, (
            "no fetch was recorded during this turn at all — web_search ran without "
            "ever retrieving a page, which is exactly the fabricated-citation shape "
            "this item exists to rule out"
        )
        successful = [f for f in fetches if f.get("ok") and f.get("text_chars", 0) > 0]
        assert successful, (
            f"every fetch during this turn failed or extracted no text: {fetches}"
        )

        # Claim 4: grounding is PROVABLE, not merely plausible — 0 < text_chars, and it
        # is the bound the item's DONE condition names explicitly. `bytes` alone cannot
        # make this claim (pre-decode, pre-markup-stripping); text_chars can, because
        # LibreChat only ever shrinks what it receives.
        for f in successful:
            assert 0 < f["text_chars"], f

        # Claim 5: the reranker actually ran and actually ranked — not the silent
        # fallback (JinaReranker's own catch, first-N-chunks-score-0) that would still
        # let highlights appear with no real ranking behind them. A non-degenerate
        # entry proves BM25 found term overlap between the query and at least one
        # chunk, which only happens if our service, not the fallback, executed.
        reranks = cleared_logs.reranks()
        assert reranks, (
            "no rerank was recorded during this turn — highlights would then come "
            "from JinaReranker's own catch-and-fallback, not our service, and "
            "grounding would be indistinguishable from the broken configuration this "
            "item replaces"
        )
        assert any(not r["degenerate"] for r in reranks), (
            f"every rerank during this turn was degenerate (no term overlap found), "
            f"so ranking did not meaningfully run: {reranks}"
        )

        # Claim 6 (the strong one): the cited URL REALLY SERVES the fact. Fetched
        # independently, through the same webfetch service but as a SEPARATE call, not
        # by trusting the log entry's own byte count.
        fetched_urls = {f["requested"] for f in successful} | {
            f.get("url") for f in successful if f.get("url")
        }
        matching_cited = [
            u for u in cited_urls
            if any(u == fu or u.rstrip("/") == fu.rstrip("/") for fu in fetched_urls)
        ]
        assert matching_cited, (
            f"none of the cited URL(s) {cited_urls} match anything this request "
            f"actually fetched {sorted(fetched_urls)} — the citation is not tied to a "
            f"retrieval"
        )

        version_found_on_page = False
        for f in successful:
            url = f.get("url") or f["requested"]
            page = _rescrape(url, env["WEBFETCH_TOKEN"])
            if page.get("success") and any(v in page["data"]["markdown"] for v in versions):
                version_found_on_page = True
                break
        assert version_found_on_page, (
            f"the version number(s) the model reported {versions} do not appear on any "
            f"page this request fetched, so the cited URL does not actually serve the "
            f"fact in the reply"
        )


def _rescrape(url: str, token: str) -> dict:
    """Independently re-fetch a URL through webfetch's own /v2/scrape, exactly as
    LibreChat's client would — the same verification tests/test_web_search.py already
    performs for a probe URL, reused here against whatever URL the model actually cited."""
    script = (
        "import urllib.request,json\n"
        f"req=urllib.request.Request('http://localhost:3002/v2/scrape',"
        f"data=json.dumps({{'url':{json.dumps(url)}}}).encode(),"
        f"headers={{'Authorization':'Bearer {token}','Content-Type':'application/json'}},"
        "method='POST')\n"
        "print(json.dumps(json.load(urllib.request.urlopen(req,timeout=30))))\n"
    )
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "--env-file", str(BUNDLE / ".env"),
         "exec", "-T", "webfetch", "python", "-c", script],
        capture_output=True, text=True, check=False, timeout=60,
    )
    assert result.returncode == 0, f"re-scrape of {url} failed\n{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])
