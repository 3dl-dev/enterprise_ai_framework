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

VERACITY RE-DISPATCH (enterpriseaiframework-0be): the first version of this test quoted
the grounding bound in this docstring and never measured it — every assertion it made
(fetch logged, cited URL fetched, a version present SOMEWHERE on ANY fetched page) also
passes under `rerankerType: none`, the snippet-grounded configuration this item replaces,
because kernel.org's own pages list mainline/stable/longterm versions side by side and a
version the model recalled from its WEIGHTS matches one of them regardless of whether
anything was read this turn. Claims 5-8 below fix that: Claim 5 parses the PERSISTED
web_search tool_call's own `output` string (chat_turn.tool_calls) into its highlight
blocks — the model-facing content format.ts's own comment says per-source `content`
never reaches (it "stays in the WEB_SEARCH artifact") — and measures
0 < len(that content) <= text_chars directly, rather than asserting it. Claim 6 joins a
non-degenerate rerank log entry to that same non-empty content in the SAME test, which is
what rules out JinaReranker's error-catch and a post-200 expandHighlights miss, either of
which can leave /reranklog non-degenerate while the model receives nothing real. Claim 8
requires the reported version to appear inside the highlight block belonging to the
SPECIFIC cited URL, not merely anywhere on any fetched page, which is what tells "read
this page this turn" apart from "knew the answer and cited a page that happens to agree".

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

# format.cjs's own line shapes (see rerank/'s companion service and
# @librechat/agents/dist/cjs/tools/search/format.cjs): every source section starts with
# "URL: <link>" and every highlight is "### Highlight N [Relevance: X.XX]" followed by a
# ```text fenced block. Parsing THESE exact markers, rather than guessing at a shape, is
# what lets Claim 4 below measure the actual persisted tool_call output instead of
# asserting something about it that was never checked.
_SOURCE_URL_RE = re.compile(r"^URL: (.+)$", re.MULTILINE)
_HIGHLIGHT_RE = re.compile(
    r"### Highlight \d+ \[Relevance: [\d.]+\]\n\n```text\n(.*?)\n```", re.DOTALL
)


def _highlight_blocks(tool_output: str) -> list[dict]:
    """Every highlight block in a persisted web_search tool_call's `output` string,
    each tagged with the URL of the source section it appeared under.

    This is the MODEL-FACING CONTENT the item's DONE condition names: format.ts's own
    comment says per-source `content` "stays in the WEB_SEARCH artifact for citations"
    and never reaches the prompt — only `highlights` do (see
    tests/test_web_search.py::TestWhatTheModelActuallyReceives). So counting and sizing
    these blocks, not the tool output's raw length, is what proves grounding rather than
    merely proving a search happened.
    """
    blocks = []
    current_url = None
    pos = 0
    # Walk URL: lines and highlight blocks in document order so each highlight is
    # attributed to the nearest preceding source, exactly as format.cjs emits them
    # (one "URL: ..." line per source, its highlights immediately after).
    markers = sorted(
        [(m.start(), "url", m.group(1).strip()) for m in _SOURCE_URL_RE.finditer(tool_output)]
        + [(m.start(), "highlight", m.group(1)) for m in _HIGHLIGHT_RE.finditer(tool_output)]
    )
    for _, kind, value in markers:
        if kind == "url":
            current_url = value
        else:
            blocks.append({"url": current_url, "text": value})
    return blocks


def _web_search_tool_outputs(reply) -> list[str]:
    """Every persisted web_search tool_call's `output` string for this turn — there may
    be several, since the model can call the tool more than once before answering."""
    calls = chat_turn.tool_calls(reply)
    return [
        (c.get("tool_call") or {}).get("output") or ""
        for c in calls
        if "web_search" in ((c.get("tool_call") or {}).get("name") or "")
    ]


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

        # Claim 5 (enterpriseaiframework-0be re-dispatch, challenge 1): the grounding
        # bound the docstring quotes — 0 < len(model-facing content) <= text_chars — is
        # measured here, not merely asserted about the fetch log. "Model-facing content"
        # is the persisted web_search tool_call's own `output` string
        # (chat_turn.tool_calls), which is exactly what LibreChat put in the prompt: the
        # only thing this turn's fetches could have contributed to a reply is what
        # survived into that output's highlight blocks (format.ts: per-source `content`
        # never leaves the WEB_SEARCH artifact). Counting THOSE blocks, not the fetch
        # log's byte count, is what tells "the tool ran" apart from "its output reached
        # the model".
        web_outputs = _web_search_tool_outputs(reply)
        assert web_outputs, (
            "no web_search tool_call block was persisted on this message at all, so "
            "there is no model-facing output to measure grounding against"
        )
        all_highlights = [h for out in web_outputs for h in _highlight_blocks(out)]
        model_facing_chars = sum(len(h["text"]) for h in all_highlights)
        assert len(all_highlights) > 0, (
            "the persisted web_search tool output carries zero highlight blocks — "
            "exactly the rerankerType:none shape this item replaces, where pages are "
            "fetched and then withheld from the model before the prompt is built"
        )
        assert 0 < model_facing_chars, (
            "highlight blocks exist but are all empty text, so no fetched content "
            "actually reached the model"
        )
        fetch_text_chars_total = sum(f["text_chars"] for f in successful)
        assert model_facing_chars <= fetch_text_chars_total, (
            f"model-facing content ({model_facing_chars} chars) exceeds the total text "
            f"this turn's fetches produced ({fetch_text_chars_total} chars) — LibreChat "
            f"only ever shrinks what it receives, so content this large cannot have come "
            f"from these fetches"
        )

        # Claim 6 (challenge 2 — the JOIN): a non-degenerate rerank entry alone proves
        # only that OUR SERVICE was called and ranked; JinaReranker's own catch (network
        # failure -> first-N-chunks-score-0) and a post-200 expandHighlights miss
        # (content.indexOf finding nothing) can each leave /reranklog non-degenerate
        # while the model receives no real highlights at all. Asserting non-degenerate
        # reranking AND non-empty, correctly-bounded model-facing content (Claim 5) IN
        # THE SAME TEST, against THE SAME TURN, is the join — either alone is exactly the
        # shape this item's DONE condition rules out.
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

        # Claim 7: the cited URL matches something this request actually fetched — not
        # merely plausible, since a model can cite a well-known URL it never retrieved.
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

        # Claim 8 (challenge 4 — the strong one, composed with Claim 5): the reported
        # fact must appear in the MODEL-FACING CONTENT of the highlight block belonging
        # to the SPECIFIC URL the model cited — not merely somewhere on that page, and
        # not merely on some OTHER fetched page. A full-page check would pass on
        # kernel.org's own homepage, which lists mainline, stable and several longterm
        # version lines side by side, so a number the model recalled from its WEIGHTS
        # would match the page even though nothing this turn read it there. Requiring
        # the match inside the specific cited source's own highlight text is what proves
        # this fact came from what the model actually read this turn, not what it knows.
        cited_highlights = [
            h for h in all_highlights
            if h["url"] and any(
                h["url"] == cu or h["url"].rstrip("/") == cu.rstrip("/") for cu in cited_urls
            )
        ]
        assert cited_highlights, (
            f"the cited URL(s) {cited_urls} have no corresponding highlight block in "
            f"the persisted tool output, so nothing ties the citation to content the "
            f"model actually saw: highlight URLs were "
            f"{sorted({h['url'] for h in all_highlights if h['url']})}"
        )
        assert any(v in h["text"] for h in cited_highlights for v in versions), (
            f"the version number(s) the model reported {versions} do not appear in the "
            f"model-facing highlight content for the cited URL(s) {cited_urls} "
            f"({[h['text'] for h in cited_highlights]!r}) — the reply's fact is not "
            f"tied to what this request actually fetched and fed to the model"
        )
