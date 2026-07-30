"""The fetch leg of web search — the one leg LibreChat has no free implementation for.

WHY THIS SERVICE EXISTS, AND WHY IT IS NOT A REIMPLEMENTATION

LibreChat's native `web_search` is three legs: search, scrape, rerank. Measured against
the pinned v0.8.7 image (enterpriseaiframework-0be), not inferred from docs:

  search   searxng | serper | tavily      SearXNG is self-hosted and needs no key. FREE.
  rerank   jina | cohere | none           `rerankerType: none` disables it. FREE.
  scrape   firecrawl | serper | tavily    EVERY option requires a paid API key.

The scrape table in librechat's own data-schemas (`webSearchAuth.scrapers`) marks
`firecrawlApiKey`, `serperApiKey` and `tavilyApiKey` all as required (1). There is no
`none`. And the consequence of leaving it unset is not an error — it is silence:

    // @librechat/agents/dist/cjs/tools/search/firecrawl.cjs, scrapeUrl()
    if (!this.apiKey) return [url, { success: false, error: 'FIRECRAWL_API_KEY is not set' }];

That returns BEFORE any HTTP request is made. So with SearXNG configured and no scraper
key, `web_search` still succeeds, still returns organic results, and still hands the model
a list of titles, links and search-engine snippets — with ZERO pages fetched. The model
then writes a fluent answer citing URLs that nothing ever retrieved. Well-formed and
wrong: the exact defect shape this codebase keeps producing. One `logger.warn` is the only
signal.

So the fetch leg has to be supplied. This service supplies it by speaking Firecrawl's
`/v2/scrape` wire shape, which makes it a drop-in for the `firecrawl` scraper LibreChat
already has a client for:

    webSearch.scraperProvider: firecrawl
    webSearch.firecrawlApiUrl:  http://webfetch:3002    <- this service
    webSearch.firecrawlApiKey:  <a local token>

LibreChat is unmodified and unpatched. This is the same integration rule already applied
to SearXNG and Perses: a separate service reached over HTTP, at a contract the component
already supports. It is a fourth implementation of an interface that already has three,
not a new subsystem — and a deployment that wants real Firecrawl (cloud or self-hosted)
changes those two config lines and nothing else.

WHY NOT SELF-HOSTED FIRECRAWL: it is AGPL-3.0 with a closed-source, cloud-only anti-bot
engine, and it brings Postgres, Redis, a browser pool and its own LLM keys. It stays a
documented swap, exactly as docs/design/design.md §7 records. This service is Apache-2.0
like the rest of the bundle and has no dependencies beyond the standard library.

WHAT IT DELIBERATELY DOES NOT DO: no JavaScript execution, no anti-bot evasion, no PDF
parsing, no crawling. It performs one HTTP GET, follows a bounded number of redirects,
and converts HTML to text. Sites that require a browser will return little or nothing —
and they return it HONESTLY, as `success: false` or as short content, so the absence is
visible rather than papered over.

THE FETCH LOG IS PART OF THE CONTRACT, NOT DEBUG PLUMBING. `GET /fetchlog` reports what
this service actually requested upstream: URL, HTTP status, byte count, timestamp. It is
the evidence that distinguishes "the model cited a page" from "the model fetched a page
and then cited it" — the distinction enterpriseaiframework-0be is entirely about. It is
bearer-authenticated like /v2/scrape, because a list of pages a user's searches caused to
be fetched is browsing history.
"""

import hmac
import html.parser
import ipaddress
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("WEBFETCH_PORT", "3002"))

# The bearer token LibreChat presents as `firecrawlApiKey`. Required and never defaulted:
# an empty token would make this service open to anything that can reach it, and — worse
# for the failure mode this whole file is about — LibreChat's own client SKIPS the request
# entirely when its key is empty, so an unset token here would silently reproduce the
# no-fetch behaviour we are removing.
TOKEN = os.environ.get("WEBFETCH_TOKEN", "")

# Hard ceilings. A fetch leg driven by search results is driven, indirectly, by whoever
# can influence those search results, so every bound here is a real limit rather than a
# tuning knob.
TIMEOUT = float(os.environ.get("WEBFETCH_TIMEOUT_SECONDS", "20"))
MAX_BYTES = int(os.environ.get("WEBFETCH_MAX_BYTES", str(2_000_000)))
MAX_REDIRECTS = int(os.environ.get("WEBFETCH_MAX_REDIRECTS", "5"))
LOG_SIZE = int(os.environ.get("WEBFETCH_LOG_SIZE", "200"))

# Hosts exempted from the private-address guard below, comma-separated. Empty by default.
# Exists for a deployment that genuinely wants its own intranet searchable, and for tests
# that stand up a loopback origin. It is an explicit allowlist and never a global off
# switch — same reasoning as librechat.yaml's `mcpSettings.allowedAddresses`.
ALLOW_HOSTS = {
    h.strip().lower()
    for h in os.environ.get("WEBFETCH_ALLOW_HOSTS", "").split(",")
    if h.strip()
}

# A real browser UA. Many sites serve a challenge page or a 403 to obvious robots, and a
# challenge page scraped successfully is worse than a failed fetch: it is well-formed
# content that says nothing about the question.
USER_AGENT = os.environ.get(
    "WEBFETCH_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36",
)

_SKIP_TAGS = frozenset(
    {"script", "style", "noscript", "template", "svg", "nav", "header", "footer",
     "aside", "form", "button", "iframe"}
)
_BLOCK_TAGS = frozenset(
    {"p", "div", "section", "article", "main", "br", "hr", "li", "tr", "blockquote",
     "pre", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "dl", "dd", "dt"}
)
_HEADING_LEVEL = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


class SSRFRefused(Exception):
    """The target resolved somewhere this service must not reach."""


class _Extractor(html.parser.HTMLParser):
    """HTML to text, keeping enough structure that headings and paragraphs survive.

    Not a markdown converter and not trying to be one: LibreChat chunks this text and
    hands it to a model, so what matters is that the page's sentences arrive intact, in
    order, without navigation furniture. Headings get `#` prefixes because they are the
    cheapest signal of where in a page a fact came from.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: str | None = None
        self._skip_depth = 0
        self._in_title = False
        self._heading: int | None = None

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
            return
        if tag in _HEADING_LEVEL:
            self._heading = _HEADING_LEVEL[tag]
            self.parts.append("\n\n" + "#" * self._heading + " ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        elif tag in _HEADING_LEVEL:
            self._heading = None
            self.parts.append("\n\n")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title = ((self.title or "") + data).strip() or None
            return
        if data.strip():
            self.parts.append(data)

    def text(self) -> str:
        joined = "".join(self.parts)
        # Collapse runs of horizontal whitespace, then runs of blank lines. Order matters:
        # doing it the other way round leaves lines of spaces that read as content.
        joined = re.sub(r"[ \t ]+", " ", joined)
        joined = re.sub(r" *\n *", "\n", joined)
        joined = re.sub(r"\n{3,}", "\n\n", joined)
        return joined.strip()


def html_to_text(raw: str) -> tuple[str, str | None]:
    """Return (text, title). Malformed HTML yields whatever parsed, never an exception."""
    extractor = _Extractor()
    try:
        extractor.feed(raw)
        extractor.close()
    except Exception:
        # A parse error mid-document still leaves everything parsed so far, which is more
        # useful than discarding the page — but it must never propagate as a 500.
        pass
    return extractor.text(), extractor.title


def _guard_host(host: str) -> None:
    """Refuse targets that resolve into address space this service must not reach.

    The URLs this service is asked for come from search results, so they are attacker
    influenceable at one remove. Without this, `web_search` becomes a read primitive
    against the gateway, the control plane, the cluster API and the cloud metadata
    endpoint, with the retrieved content delivered straight into a model's context.

    Checked on the RESOLVED addresses, not on the literal string: a hostname that
    resolves to 127.0.0.1 or 169.254.169.254 is the whole point of the check.
    """
    if host.lower() in ALLOW_HOSTS:
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SSRFRefused(f"cannot resolve {host}: {exc}") from exc
    if not infos:
        raise SSRFRefused(f"cannot resolve {host}")
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        ):
            raise SSRFRefused(
                f"{host} resolves to {addr}, which is not a public address"
            )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are followed by hand so every hop is guarded and counted.

    urllib's own redirect handling would follow a 302 into private address space without
    re-running _guard_host — the classic bypass of exactly the check above.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)

_log_lock = threading.Lock()
_fetch_log: list[dict] = []


def _record(entry: dict) -> None:
    with _log_lock:
        _fetch_log.append(entry)
        if len(_fetch_log) > LOG_SIZE:
            del _fetch_log[: len(_fetch_log) - LOG_SIZE]


def fetch(url: str) -> dict:
    """One guarded HTTP GET with bounded redirects. Returns a Firecrawl-shaped result.

    Every outcome, success or failure, appends to the fetch log — a fetch that was
    attempted and failed is evidence too, and the absence of an entry is what proves a
    page was never requested.
    """
    started = time.time()
    seen: set[str] = set()
    current = url

    for _hop in range(MAX_REDIRECTS + 1):
        parsed = urllib.parse.urlsplit(current)
        if parsed.scheme not in ("http", "https"):
            entry = {"url": current, "ok": False, "error": f"scheme {parsed.scheme!r} refused"}
            _record({**entry, "requested": url, "at": started})
            return {"success": False, "error": entry["error"]}
        if not parsed.hostname:
            _record({"url": current, "requested": url, "ok": False,
                     "error": "no host", "at": started})
            return {"success": False, "error": "no host in URL"}
        try:
            _guard_host(parsed.hostname)
        except SSRFRefused as exc:
            _record({"url": current, "requested": url, "ok": False,
                     "error": f"refused: {exc}", "at": started})
            return {"success": False, "error": f"refused: {exc}"}

        if current in seen:
            _record({"url": current, "requested": url, "ok": False,
                     "error": "redirect loop", "at": started})
            return {"success": False, "error": "redirect loop"}
        seen.add(current)

        request = urllib.request.Request(
            current,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
                "Accept-Language": "en",
            },
            method="GET",
        )
        try:
            with _opener.open(request, timeout=TIMEOUT) as response:
                status = response.status
                content_type = response.headers.get("Content-Type", "")
                # read(n+1) so a body at exactly the cap is still detectable as truncated
                # rather than silently reported as complete.
                body = response.read(MAX_BYTES + 1)
                final_url = response.geturl()
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location") if exc.headers else None
            if exc.code in (301, 302, 303, 307, 308) and location:
                _record({"url": current, "requested": url, "ok": True,
                         "status": exc.code, "bytes": 0, "at": time.time(),
                         "redirect_to": urllib.parse.urljoin(current, location)})
                current = urllib.parse.urljoin(current, location)
                continue
            _record({"url": current, "requested": url, "ok": False,
                     "status": exc.code, "error": f"HTTP {exc.code}", "at": time.time()})
            return {"success": False, "error": f"HTTP {exc.code} from {current}"}
        except Exception as exc:
            _record({"url": current, "requested": url, "ok": False,
                     "error": f"{type(exc).__name__}: {exc}", "at": time.time()})
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"}

        truncated = len(body) > MAX_BYTES
        body = body[:MAX_BYTES]
        _record({
            "url": current, "requested": url, "ok": True, "status": status,
            "bytes": len(body), "truncated": truncated, "at": time.time(),
            "content_type": content_type,
        })

        charset = "utf-8"
        match = re.search(r"charset=([\w\-]+)", content_type, re.I)
        if match:
            charset = match.group(1)
        try:
            raw = body.decode(charset, errors="replace")
        except LookupError:
            raw = body.decode("utf-8", errors="replace")

        is_html = "html" in content_type.lower() or bool(
            re.search(r"<\s*(html|body|head|div|p)\b", raw[:4000], re.I)
        )
        if is_html:
            text, title = html_to_text(raw)
        else:
            text, title = raw.strip(), None

        if not text:
            return {
                "success": False,
                "error": f"nothing extractable from {final_url} "
                         f"(content-type {content_type or 'unknown'})",
            }

        return {
            "success": True,
            "data": {
                # LibreChat's FirecrawlScraper.extractContent reads `markdown` first, and
                # falls through to `html` then `rawHtml`. It processes html+markdown
                # together when BOTH are present, so `rawHtml` is supplied under the key
                # it actually looks for and `html` is deliberately left unset.
                "markdown": text,
                "rawHtml": raw if is_html else None,
                "metadata": {
                    "title": title or "",
                    "sourceURL": final_url,
                    "statusCode": status,
                    "contentType": content_type,
                    "truncated": truncated,
                },
            },
        }

    return {"success": False, "error": f"more than {MAX_REDIRECTS} redirects"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "enterprise-ai-webfetch"

    def log_message(self, fmt, *args):  # noqa: A003 - stdlib hook name
        # Access logging is on stderr in the container log; the structured record that
        # tests and operators read is /fetchlog.
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
            # Fail closed. An unset token is a misconfiguration, not "auth disabled".
            return False
        presented = self.headers.get("Authorization", "")
        expected = f"Bearer {TOKEN}"
        # hmac.compare_digest, not `==` and not a hand-rolled loop: this token is a
        # credential, and both of those short-circuit on the first differing byte. An
        # earlier version of this function used `not any(a != b for a, b in zip(...))`
        # under a comment claiming to be constant time, which it was not — `any` returns
        # as soon as it finds a mismatch.
        #
        # Compared as BYTES: compare_digest on str raises TypeError for any non-ASCII
        # character, so a garbage Authorization header would come back as a 500 from an
        # unhandled exception instead of a 401. Encoding first makes a malformed header
        # simply wrong rather than exceptional.
        return hmac.compare_digest(
            presented.encode("utf-8", "surrogateescape"), expected.encode("utf-8")
        )

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/health":
            # Unauthenticated on purpose: this is what the container healthcheck and the
            # Kubernetes probes call, and it reveals nothing but liveness and whether a
            # token was configured at all.
            self._json(200, {
                "status": "ok",
                "service": "webfetch",
                "token_configured": bool(TOKEN),
            })
            return
        if path == "/fetchlog":
            if not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return
            with _log_lock:
                self._json(200, {"fetches": list(_fetch_log)})
            return
        self._json(404, {"error": "not found"})

    def do_DELETE(self):
        # Clearing the log is what lets a test say "no fetch happened DURING this turn"
        # rather than "no fetch has ever happened", which is a much weaker claim.
        if urllib.parse.urlsplit(self.path).path != "/fetchlog":
            self._json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        with _log_lock:
            _fetch_log.clear()
        self._json(200, {"cleared": True})

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        # Both API versions map to the same handler: LibreChat's client builds
        # `<apiUrl>/<version>/scrape` and defaults version to v2, but a deployment may
        # pin firecrawlVersion: v1. Answering only one of them would fail as a 404 that
        # the scraper reports as a generic request failure.
        if path not in ("/v2/scrape", "/v1/scrape"):
            self._json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            self._json(400, {"success": False, "error": f"bad JSON: {exc}"})
            return
        url = payload.get("url")
        if not isinstance(url, str) or not url:
            self._json(400, {"success": False, "error": "url is required"})
            return

        result = fetch(url)
        # 200 even for an unsuccessful scrape: Firecrawl's client treats a non-2xx as a
        # transport failure and discards the body, which would hide the reason. The
        # `success` flag in the body is the channel LibreChat actually reads.
        self._json(200, result)


def main():
    if not TOKEN:
        # Loud, and still serving: /health must be able to say token_configured=false so
        # the misconfiguration is diagnosable from outside rather than as a crash loop.
        print(
            "webfetch: WEBFETCH_TOKEN is not set — every scrape request will be "
            "rejected with 401. Set it in bundle/.env.",
            flush=True,
        )
    print(f"webfetch listening on 0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
