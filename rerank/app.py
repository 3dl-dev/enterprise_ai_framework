"""The rerank leg of web search — closes the gap webfetch's own docstring names.

WHY THIS SERVICE EXISTS, AND WHY IT IS NOT A REIMPLEMENTATION

Measured against the pinned v0.8.7 image (@librechat/agents/dist/cjs/tools/search), not
inferred from docs: LibreChat FETCHES pages (webfetch/ supplies that leg) and then
WITHHOLDS the fetched text from the model unless a reranker produced `highlights`.

    createReranker({rerankerType: "none"})   -> undefined
    getHighlights({reranker: undefined})     -> warns, returns undefined
    addHighlights()                          -> source.highlights = undefined
    formatSource()                           -> emits `Summary: <snippet>` always, and
                                                the Highlights section (the ONLY place
                                                per-source scraped content reaches the
                                                model) only if highlights is non-empty

format.ts's own comment: "per-source `content` stays in the WEB_SEARCH artifact for
citations" — the artifact, not the prompt. So `rerankerType: none` is free and
authenticates, but leaves every answer grounded in SearXNG's search-engine snippets
rather than in any page the system actually read. `rerankerType: jina` with no key is a
trap, not a fix: `loadWebSearchAuth` cannot authenticate jina without `jinaApiKey`, so it
leaves `rerankerType` unset; `createSearchTool` then falls back to its OWN default of
`cohere`; `CohereReranker` finds no `COHERE_API_KEY`, logs "Using default ranking." and
returns the first N chunks with score 0. Write `jina`, get Cohere, with no outbound call
and no highlights either way you didn't ask for.

So this service exists for exactly the reason webfetch/ does: to speak the wire contract
of a reranker LibreChat can authenticate — Jina's, verified against rerankers.cjs's
`JinaReranker` above — with a key WE mint, so the unmodified client can be pointed at it.

    POST <jinaApiUrl> {model, query, top_n, documents, return_documents:true}
      Authorization: Bearer <jinaApiKey>
    -> {model, usage, results:[{index, relevance_score, document:{text}}]}

Apache-2.0, standard library only, no outbound network call, no paid dependency.

HONESTY OBLIGATION, IN CAPITALS BECAUSE IT WILL OTHERWISE BE MISREAD AS SEMANTIC SEARCH:
RANKING HERE IS OKAPI BM25 — LEXICAL TERM OVERLAP, NOT SEMANTIC. A CHUNK THAT ANSWERS THE
QUESTION IN DIFFERENT WORDS THAN THE QUERY WILL SCORE LOWER THAN A CHUNK THAT PARROTS THE
QUERY'S OWN WORDS BACK, EVEN IF THE PARROTING CHUNK SAYS LESS. THE SCORE IS A SQUASHED
BM25 SUM, NOT A CALIBRATED PROBABILITY OF RELEVANCE. What this service delivers is
GROUNDING — highlights that make LibreChat put the real fetched text in front of the
model. Ranking QUALITY is a separate, weaker claim, and this docstring is what stops the
first from silently masquerading as the second.

WHY BM25 AND NOT "RETURN INPUT ORDER": the ruling that authorised this service allowed a
reranker that "merely returns chunks in input order" as acceptable IF the config comment
and a test say so plainly — that would buy grounding with zero ranking value. BM25 over
the request's own chunks costs nothing to compute (no model, no warm-up, no GPU: routine
with ~30 chunks from one page) and is honest about what it is, so this service does that
extra bit of work instead of taking the cheaper permitted-but-empty path.

THE CONSTRAINT NOBODY WOULD GUESS: `document.text` in the response MUST be
BYTE-IDENTICAL to the input chunk. `expandHighlights` (highlights.cjs) locates a highlight
inside the fetched page's full content with `content.indexOf(highlight.text)` and then
expands it +/-300 chars to give the model a paragraph, not a fragment. Normalise
whitespace, trim, or reflow the text and `indexOf` returns -1 (or, worse, a wrong offset
in a lookalike string), and the model gets a bare 150-char sliver — or the fallback
stripped-text search — instead of real context. So this service echoes chunks back
verbatim; the only bytes it manufactures are the score.

/reranklog EXISTS BECAUSE THE FALLBACK IS SILENT. If this service is unreachable, rejects
the bearer token, or errors, JinaReranker's own `catch` swallows it and returns
`getDefaultRanking` — the first N chunks, score 0 — and LibreChat still emits highlights.
Grounding APPEARS to work either way; only this log distinguishes "our reranker ran" from
"the client's built-in fallback ran instead". It carries counts only — no query text, no
document text — because a list of what a user searched for is not worth holding, and the
counts are all a test needs.
"""

import hmac
import json
import math
import os
import re
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("RERANK_PORT", "3003"))

# The bearer token LibreChat presents as `jinaApiKey`. Required and never defaulted, for
# the same reason webfetch's WEBFETCH_TOKEN is: an empty token here is not "auth
# disabled", it fails closed, and /health still reports token_configured so the
# misconfiguration is diagnosable rather than a crash loop.
TOKEN = os.environ.get("RERANK_TOKEN", "")

MAX_DOCUMENTS = int(os.environ.get("RERANK_MAX_DOCUMENTS", "2000"))
MAX_DOCUMENT_CHARS = int(os.environ.get("RERANK_MAX_DOCUMENT_CHARS", "20000"))
MAX_QUERY_CHARS = int(os.environ.get("RERANK_MAX_QUERY_CHARS", "2000"))
MAX_BODY_BYTES = int(os.environ.get("RERANK_MAX_BODY_BYTES", str(8 * 1024 * 1024)))
LOG_SIZE = int(os.environ.get("RERANK_LOG_SIZE", "200"))

MODEL_NAME = "enterprise-ai-bm25-reranker-v1"

# BM25 constants. k1 and b are the Okapi defaults, not tuned for this corpus shape —
# there is nothing to tune against, this ranks whatever chunks one search request
# produced, not a fixed collection.
K1 = 1.5
B = 0.75

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def bm25_rank(query: str, documents: list[str]) -> tuple[list[float], bool]:
    """Okapi BM25 (k1=1.5, b=0.75) over the documents in THIS request only.

    Returns (scores, degenerate) where scores[i] is the squashed score for documents[i]
    and degenerate is True iff no query term matched any document (all scores are then
    0.0 and the caller falls back to input order).

    IDF uses the NON-NEGATIVE form ln(1 + (N-df+0.5)/(df+0.5)), not the classic
    ln((N-df+0.5)/(df+0.5)). The classic form goes negative once a term appears in more
    than half the corpus — routine here, with ~30 chunks pulled from one page that all
    share the page's own vocabulary — and a negative IDF would PENALISE a chunk for
    containing a common query word, which is backwards.
    """
    n = len(documents)
    doc_tokens = [_tokenize(d) for d in documents]
    doc_lens = [len(t) for t in doc_tokens]
    avg_len = (sum(doc_lens) / n) if n else 0.0

    query_terms = _tokenize(query)
    if not query_terms or n == 0:
        return [0.0] * n, True

    # document frequency per query term, over THIS request's documents
    df: Counter = Counter()
    doc_term_counts = [Counter(t) for t in doc_tokens]
    unique_query_terms = set(query_terms)
    for term in unique_query_terms:
        df[term] = sum(1 for counts in doc_term_counts if counts.get(term))

    idf = {
        term: math.log(1.0 + (n - df[term] + 0.5) / (df[term] + 0.5))
        for term in unique_query_terms
    }

    any_match = any(df[term] > 0 for term in unique_query_terms)
    if not any_match:
        return [0.0] * n, True

    raw_scores = []
    for i in range(n):
        counts = doc_term_counts[i]
        dl = doc_lens[i]
        score = 0.0
        for term in unique_query_terms:
            f = counts.get(term, 0)
            if f == 0:
                continue
            denom = f + K1 * (1 - B + B * (dl / avg_len if avg_len else 0.0))
            score += idf[term] * (f * (K1 + 1)) / denom
        raw_scores.append(score)

    # Squash to [0,1), ABSOLUTE and deliberately NOT batch-max-normalised: dividing by the
    # best score in the batch would award 1.00 to the least-bad chunk of a page that says
    # nothing about the question, which is exactly the false confidence this project keeps
    # finding and removing elsewhere.
    scores = [raw / (raw + 8.0) for raw in raw_scores]
    return scores, False


def rerank(query: str, documents: list[str], top_n: int) -> tuple[list[dict], bool]:
    """Return the top_n (index, score) pairs, ties broken on original index, plus
    whether the ranking was degenerate (no query term matched anything)."""
    scores, degenerate = bm25_rank(query, documents)
    order = sorted(range(len(documents)), key=lambda i: (-scores[i], i))
    top = order[: max(0, min(top_n, len(documents)))]
    return [{"index": i, "score": scores[i]} for i in top], degenerate


_log_lock = threading.Lock()
_rerank_log: list[dict] = []


def _record(entry: dict) -> None:
    with _log_lock:
        _rerank_log.append(entry)
        if len(_rerank_log) > LOG_SIZE:
            del _rerank_log[: len(_rerank_log) - LOG_SIZE]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "enterprise-ai-rerank"

    def log_message(self, fmt, *args):  # noqa: A003 - stdlib hook name
        # The structured record a test or operator reads is /reranklog, not the access log.
        pass

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not TOKEN:
            # Fail closed. An unset token is a misconfiguration, not "auth disabled" —
            # same rule as webfetch/app.py.
            return False
        presented = self.headers.get("Authorization", "")
        expected = f"Bearer {TOKEN}"
        # hmac.compare_digest on BYTES, not str: compare_digest raises TypeError on a
        # non-ASCII str, which would turn a garbage Authorization header into a 500
        # instead of a 401. Encoding first makes a malformed header simply wrong rather
        # than exceptional.
        return hmac.compare_digest(
            presented.encode("utf-8", "surrogateescape"), expected.encode("utf-8")
        )

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            # Unauthenticated on purpose, and it reports token_configured: a reranker
            # that 401s every request looks healthy to LibreChat's own healthcheck
            # otherwise, and chat's compose/k8s dependency is on THIS service being
            # healthy before it starts taking real traffic.
            self._json(200, {
                "status": "ok",
                "service": "rerank",
                "token_configured": bool(TOKEN),
            })
            return
        if path == "/reranklog":
            if not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return
            with _log_lock:
                self._json(200, {"reranks": list(_rerank_log)})
            return
        self._json(404, {"error": "not found"})

    def do_DELETE(self):
        if self.path.split("?", 1)[0] != "/reranklog":
            self._json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        with _log_lock:
            _rerank_log.clear()
        self._json(200, {"cleared": True})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        # Both paths map to the same handler: the client POSTs jinaApiUrl VERBATIM (it
        # appends nothing), so JINA_API_URL is configured as the full
        # http://rerank:3003/v1/rerank path. /rerank is answered too so a deployment that
        # points jinaApiUrl at the bare origin gets a real response instead of a 404
        # silently swallowed into JinaReranker's "Using default ranking" fallback.
        if path not in ("/v1/rerank", "/rerank"):
            self._json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            self._json(413, {"error": f"body exceeds {MAX_BODY_BYTES} bytes"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            self._json(400, {"error": f"bad JSON: {exc}"})
            return

        query = payload.get("query")
        documents = payload.get("documents")
        top_n = payload.get("top_n", 5)

        if not isinstance(query, str) or not query:
            self._json(400, {"error": "query is required"})
            return
        if not isinstance(documents, list) or not documents:
            self._json(400, {"error": "documents is required and must be a non-empty list"})
            return
        if len(documents) > MAX_DOCUMENTS:
            self._json(400, {"error": f"documents exceeds {MAX_DOCUMENTS} entries"})
            return
        if len(query) > MAX_QUERY_CHARS:
            self._json(400, {"error": f"query exceeds {MAX_QUERY_CHARS} characters"})
            return

        # Jina's `documents` may be plain strings or {"text": ...} objects; the client
        # LibreChat uses (rerankers.cjs) always sends plain strings, but the wire
        # contract itself allows either, so both are accepted rather than 400ing a
        # spec-legal caller.
        texts: list[str] = []
        for doc in documents:
            if isinstance(doc, str):
                texts.append(doc)
            elif isinstance(doc, dict) and isinstance(doc.get("text"), str):
                texts.append(doc["text"])
            else:
                self._json(400, {"error": "every document must be a string or {text: string}"})
                return
        for t in texts:
            if len(t) > MAX_DOCUMENT_CHARS:
                self._json(400, {"error": f"a document exceeds {MAX_DOCUMENT_CHARS} characters"})
                return

        if not isinstance(top_n, int) or top_n < 0:
            top_n = len(texts)

        ranked, degenerate = rerank(query, texts, top_n)
        total_chars = len(query) + sum(len(t) for t in texts)

        _record({
            "at": time.time(),
            "query_chars": len(query),
            "documents": len(texts),
            "returned": len(ranked),
            "top_score": ranked[0]["score"] if ranked else None,
            "degenerate": degenerate,
        })

        self._json(200, {
            "model": MODEL_NAME,
            "usage": {"total_chars": total_chars},
            "results": [
                {
                    "index": r["index"],
                    "relevance_score": r["score"],
                    # BYTE-IDENTICAL to the input chunk. See the module docstring:
                    # expandHighlights locates this text in the source's full content
                    # with a plain indexOf, and any normalisation here breaks that.
                    "document": {"text": texts[r["index"]]},
                }
                for r in ranked
            ],
        })


def main():
    if not TOKEN:
        print(
            "rerank: RERANK_TOKEN is not set — every rerank request will be rejected "
            "with 401. Set it in bundle/.env.",
            flush=True,
        )
    print(f"rerank listening on 0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
