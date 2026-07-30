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
time (so it postdates whatever the model's training cutoff was, without needing to guess
at that cutoff), it is unambiguous once "stable" is named as the specific release line
kernel.org itself labels that way (as opposed to mainline or longterm, which the page
lists alongside it), and kernel.org is a plain, stable, unthrottled page — the same site
TestTheSearchLeg and TestTheFetchLegRetrievesRealPages already rely on being reachable
from this host. The question is also phrased to BOUND the tool loop deterministically —
"perform exactly one web search... do not search again once you have a result" — because
an earlier re-dispatch's live run hit LangGraph's "Recursion limit of 50 reached" with an
empty reply on this same question phrased more open-endedly. If that recurs even bounded
this way, it is a defect in the search/agent pipeline, not a defect in the question, and
is filed as such rather than retried past.

VERACITY RE-DISPATCH (enterpriseaiframework-0be, wave 2): the first version of this test
quoted the grounding bound in this docstring and never measured it — every assertion it
made (fetch logged, cited URL fetched, a version present SOMEWHERE on ANY fetched page)
also passes under `rerankerType: none`, the snippet-grounded configuration this item
replaces, because kernel.org's own pages list mainline/stable/longterm versions side by
side and a version the model recalled from its WEIGHTS matches one of them regardless of
whether anything was read this turn. Claims 5-8 below fix that: Claim 5 parses the
PERSISTED web_search tool_call's own `output` string (chat_turn.tool_calls) into its
highlight blocks — the model-facing content format.ts's own comment says per-source
`content` never reaches (it "stays in the WEB_SEARCH artifact") — and measures
0 < len(that content) <= text_chars directly, rather than asserting it. Claim 6 joins a
non-degenerate rerank log entry to that same non-empty content in the SAME test, which is
what rules out JinaReranker's error-catch and a post-200 expandHighlights miss, either of
which can leave /reranklog non-degenerate while the model receives nothing real.

VERACITY RE-DISPATCH (enterpriseaiframework-0be, wave 2's own live runs, and the
ORCHESTRATOR RULING that followed): wave 2's Claim 8 required the reported version to sit
inside the highlight block belonging to the SPECIFIC cited URL. Three live runs against
the correct config surfaced that this is a DIFFERENT, STRONGER claim than grounding, and
sometimes fails on CORRECT behaviour: rerank/ is deliberately lexical BM25 over page
chunks, so on kernel.org's releases listing the query terms matched prose about
mainline/stable/longterm maintenance rather than the version-table row, and the fact
reached the model through a DIFFERENT fetched source's highlights while the model cited
the canonical kernel.org page anyway. Grounding held; the old single assertion failed
anyway. The ruling split it, exactly as this item already split BM25-ranking-quality from
grounding once before: Claim 8 now asserts only GROUNDING — the fact appears in this
turn's model-facing content from ANY fetched source (composed with Claim 7's "every cited
URL was fetched") — and a same-turn mismatch between "which source carried the fact" and
"which source was cited" is checked separately, printed plainly, and reported as a
CITATION-ATTRIBUTION finding rather than failing the grounding test. Claim 7 also now
normalises scheme and a leading `www.` before comparing URLs (challenge B) — a model
writing `https://kernel.org` for a page the fetch log recorded as `https://www.kernel.org/`
is citing the same served page, and the old `rstrip('/')`-only comparison measured string
formatting, not grounding.

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


def _normalize_url(u: str) -> str:
    """Compare URLs by scheme+host(minus a leading `www.`)+path(minus a trailing slash),
    not by string equality.

    ORCHESTRATOR RULING (enterpriseaiframework-0be, challenge B): a model that cites
    `https://kernel.org` for a page the fetch log recorded as `https://www.kernel.org/`
    is citing the SAME served page — a bare host and its `www` form are not a grounding
    failure, they are a string-formatting difference. `u.rstrip('/') == fu.rstrip('/')`
    only killed the trailing slash and left the `www` mismatch red for a reason with no
    bearing on grounding. This normalises scheme (both are just transport) and the `www`
    prefix (both name the same host) while still treating a genuinely different host or
    path as a genuinely different page.
    """
    parsed = urllib.parse.urlsplit(u.strip())
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/")
    return f"{host}{path}{'?' + parsed.query if parsed.query else ''}"


def _same_page(a: str, b: str) -> bool:
    return _normalize_url(a) == _normalize_url(b)

QUESTION = (
    "Perform exactly one web search for the CURRENT stable version of the Linux kernel "
    "(the release line kernel.org itself labels \"stable\", not mainline or longterm), "
    "per kernel.org. Do not answer from memory, and do not search again once you have a "
    "result — if the first search does not give you a clear version number, say so "
    "rather than guessing. Reply with only the version number and, on a new line, the "
    "exact URL of the page you found it on."
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
            web_search=True, timeout=170.0,
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

        # Claim 7 (challenge B fix — normalised comparison): EVERY cited URL is among the
        # URLs this request actually fetched — not merely plausible, since a model can
        # cite a well-known URL it never retrieved. This is half of the ORCHESTRATOR
        # RULING's two-part grounding proof: "(b) every cited URL is among the URLs
        # fetched during that request." Comparison goes through `_same_page`, which
        # normalises scheme and a leading `www.` — a model that writes the bare-host form
        # of a page the fetch log recorded with `www.` is citing the SAME page; that is a
        # string-formatting difference, not a grounding failure, and the old
        # `rstrip('/')`-only comparison measured the wrong thing.
        fetched_urls = {f["requested"] for f in successful} | {
            f.get("url") for f in successful if f.get("url")
        }
        unfetched_citations = [
            u for u in cited_urls if not any(_same_page(u, fu) for fu in fetched_urls)
        ]
        assert not unfetched_citations, (
            f"cited URL(s) {unfetched_citations} match nothing this request actually "
            f"fetched {sorted(fetched_urls)} — a citation not tied to a retrieval is "
            f"exactly the fabricated-citation shape this item exists to rule out"
        )

        # Claim 8 (ORCHESTRATOR RULING 2026-07-30, splitting the claim exactly as this
        # item already split BM25-ranking-quality from grounding once before):
        #
        # GROUNDING is (a) the reported fact appears in the MODEL-FACING content of THIS
        # TURN — the highlight text actually delivered to the prompt, from ANY source
        # fetched during the request — AND (b) every cited URL is among the URLs fetched
        # during the request (Claim 7, above). Those two together kill both
        # fabricated-citation shapes: citing a page nothing retrieved, and answering from
        # the weights while an unrelated fetch happened in the background. That is what
        # the item's DONE condition asks for, and it is NOT the same claim as "the fact
        # sits in the highlight block belonging to the specific cited URL" — rerank/ is
        # deliberately lexical BM25 over page chunks, so on a page like kernel.org's
        # releases listing the query terms can match prose about mainline/stable/longterm
        # maintenance rather than the version-table row, and the fact then reaches the
        # model through a DIFFERENT fetched source's highlights while the model cites the
        # canonical page anyway. That was MEASURED, not theorised, in wave 2's live runs:
        # grounding worked (the fact was in this turn's model-facing content, the cited
        # URL was really fetched) and the old single claim failed anyway, on correct
        # behaviour. So Claim 8 below asserts ONLY grounding (a); a same-turn mismatch
        # between "which source carried the fact" and "which source the model cited" is
        # a CITATION-ATTRIBUTION defect, checked separately and reported, not asserted.
        assert any(v in h["text"] for h in all_highlights for v in versions), (
            f"the version number(s) the model reported {versions} do not appear in ANY "
            f"of this turn's model-facing highlight content — reply was {text!r}, cited "
            f"{cited_urls}, sources fetched were "
            f"{sorted({h['url'] for h in all_highlights if h['url']})}, and the highlight "
            f"text was {[h['text'] for h in all_highlights]!r} — the reply's fact is not "
            f"tied to anything this request actually fetched and fed to the model"
        )

        # CITATION-ATTRIBUTION CHECK (not part of grounding, per the ruling above — NOT
        # WAIVED, just not conflated with grounding). If the fact instead lands only in a
        # DIFFERENT fetched source's highlights than the one(s) the model cited, that is
        # a real defect in citation attribution and must be surfaced, not silently
        # accepted — this block reports it plainly (visible in -v output and captured by
        # whoever reads this run) without failing the grounding test itself.
        cited_highlights = [
            h for h in all_highlights
            if h["url"] and any(_same_page(h["url"], cu) for cu in cited_urls)
        ]
        fact_in_cited = any(
            v in h["text"] for h in cited_highlights for v in versions
        )
        if not fact_in_cited:
            carrying_urls = sorted({
                h["url"] for h in all_highlights
                if h["url"] and any(v in h["text"] for v in versions)
            })
            print(
                "\nCITATION-ATTRIBUTION DEFECT (grounding held, attribution did not): "
                f"question={QUESTION!r} cited_url(s)={cited_urls} "
                f"reported_version(s)={versions} — the fact appears in the "
                f"model-facing highlights of {carrying_urls or '(no source)'}, not in "
                f"the highlight(s) for the cited URL "
                f"({sorted({h['url'] for h in cited_highlights})}). rerank/'s lexical "
                f"BM25 matched query terms to a different chunk than the one carrying "
                f"the fact on the cited page. FILE per the orchestrator ruling — this "
                f"is not waived, just not conflated with the grounding assertion above."
            )
