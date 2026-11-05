"""Web search for research steps. Only short search queries leave the machine.

* One fixed host (DuckDuckGo's HTML endpoint). No user- or model-supplied URL is ever fetched.
* No cookies, no API key, a hard timeout and a response size cap.
* Results are snippets only: pages are never opened, so nothing here can say a source was "read".
* Everything returned is untrusted data; callers must neutralize it before it reaches a model.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from pydantic import BaseModel, Field

SEARCH_HOST = "html.duckduckgo.com"
SEARCH_URL = f"https://{SEARCH_HOST}/html/"
MAX_RESULTS = 5
MAX_QUERY_CHARS = 120
MAX_RESPONSE_BYTES = 400_000
USER_AGENT = "Mozilla/5.0 (compatible; TaskWrite/0.1; local research helper)"


class Source(BaseModel):
    id: str  # S1, S2, ...
    title: str = Field(max_length=300)
    url: str = Field(max_length=600)
    snippet: str = Field(default="", max_length=600)
    query: str = Field(default="", max_length=200)
    excerpt: str = Field(default="", max_length=4000)  # text from the page itself, only if the user approved opening it

    @property
    def opened(self) -> bool:
        return bool(self.excerpt)


_BRACKETS = str.maketrans({"\u3010": "[", "\u3011": "]", "\uff3b": "[", "\uff3d": "]"})  # 【 】 ［ ］ -> [ ]
_CITE_GROUP = re.compile(r"\[\s*S\d{1,3}(?:\s*[,;]\s*S?\d{1,3})*\s*\]", re.I)


def normalize_citations(text: str) -> str:
    """Models sometimes write 【S2】 or [S1, S3]. Make every citation a plain [S2] so it can be checked."""
    text = text.translate(_BRACKETS)

    def split(match: re.Match) -> str:
        nums = re.findall(r"\d{1,3}", match.group(0))
        return "".join(f"[S{int(n)}]" for n in nums)

    return _CITE_GROUP.sub(split, text)


def cited_ids(text: str) -> set[str]:
    return {f"S{int(n)}" for n in re.findall(r"\[S(\d{1,3})\]", normalize_citations(text), re.I)}


class SearchError(Exception):
    """The search could not be completed; the message is shown to the user as-is."""


def clean_query(raw: str) -> str:
    """A model-proposed query, reduced to plain words: no URLs, no operators, bounded length."""
    text = re.sub(r"https?://\S+", " ", raw)
    text = re.sub(r"[^\w\s'\-,.]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()[:MAX_QUERY_CHARS]


def _real_url(href: str) -> str:
    """DuckDuckGo sometimes wraps the target in a redirect (…/l/?uddg=<encoded>)."""
    if "uddg=" in href:
        target = parse_qs(urlparse(href if "//" in href else "//" + href).query).get("uddg", [""])[0]
        href = unquote(target)
    return href.strip()


def is_web_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    own = host == "duckduckgo.com" or host.endswith(".duckduckgo.com")  # the search engine's own pages and ads
    return parsed.scheme in ("http", "https") and bool(host) and not own


class _ResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._field: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        classes = (dict(attrs).get("class") or "").split()
        href = dict(attrs).get("href") or ""
        if "result__a" in classes:
            self._field, self._buf = "title", []
            self.results.append({"title": "", "url": _real_url(href), "snippet": ""})
        elif "result__snippet" in classes and self.results:
            self._field, self._buf = "snippet", []

    def handle_data(self, data: str) -> None:
        if self._field:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._field and self.results:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            self.results[-1][self._field] = text
            self._field = None


def parse_results(html: str) -> list[dict[str, str]]:
    parser = _ResultParser()
    parser.feed(html)
    out = []
    for r in parser.results:
        if r["title"] and is_web_url(r["url"]):
            out.append(r)
    return out


Searcher = Callable[[str], list[dict[str, str]]]


def web_search(query: str, timeout_s: float = 15.0, client: httpx.Client | None = None) -> list[dict[str, str]]:
    """Search DuckDuckGo for `query`. Raises SearchError with a plain-language reason on failure."""
    query = clean_query(query)
    if not query:
        raise SearchError("The search query was empty after cleaning.")
    own = client is None
    http = client or httpx.Client(timeout=timeout_s, follow_redirects=False, headers={"User-Agent": USER_AGENT})
    try:
        resp = http.post(SEARCH_URL, data={"q": query})
    except httpx.TimeoutException as exc:
        raise SearchError("The search timed out.") from exc
    except httpx.HTTPError as exc:
        raise SearchError("Could not reach the search service.") from exc
    finally:
        if own:
            http.close()
    if resp.status_code in (202, 403, 429):
        raise SearchError("The search service is rate-limiting or blocking requests right now. Try again later.")
    if resp.status_code != 200:
        raise SearchError(f"The search service returned HTTP {resp.status_code}.")
    return parse_results(resp.text[:MAX_RESPONSE_BYTES])[:MAX_RESULTS]


# ---------------------------------------------------------------------------------------------
# Opening result pages (only after the user approves the exact addresses)
# ---------------------------------------------------------------------------------------------

MAX_PAGE_BYTES = 600_000
MAX_EXCERPT_CHARS = 3000
MAX_REDIRECTS = 3
ALLOWED_PORTS = (None, 80, 443)
PageReader = Callable[[str], str]
Resolver = Callable[[str], list[str]]


class PageError(SearchError):
    """A page could not be opened or was refused; the message is shown to the user as-is."""


def resolve_host(host: str) -> list[str]:
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)})
    except OSError as exc:
        raise PageError(f"Could not look up {host}.") from exc


def _is_public_ip(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text.split("%")[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local or ip.is_private)


def check_page_url(url: str, resolver: Resolver = resolve_host) -> str:
    """Refuse anything that is not a plain public web address. Returns the normalised URL.

    This stops a search result (or a redirect) from pointing TaskWrite at your own computer or network:
    only http/https, only ports 80/443, no credentials in the address, and every address the host
    resolves to must be a public internet address.
    """
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise PageError("Only normal web addresses (http or https) can be opened.")
    if parsed.username or parsed.password:
        raise PageError("Addresses with a login in them are not opened.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise PageError("That address has an invalid port.") from exc
    if port not in ALLOWED_PORTS:
        raise PageError("Only the standard web ports are allowed.")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        raise PageError("Addresses on your own computer or network are never opened.")
    addresses = [host] if re.fullmatch(r"[0-9a-f:.]+", host) and (":" in host or host.replace(".", "").isdigit()) else resolver(host)
    if not addresses or not all(_is_public_ip(a) for a in addresses):
        raise PageError("That address points at a private or local network, so it is not opened.")
    return parsed.geturl()


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "nav", "header", "footer", "aside", "form", "iframe", "head"}
    BLOCK = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "blockquote", "pre"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def extract_text(html: str, limit: int = MAX_EXCERPT_CHARS) -> str:
    """The readable body text of a page: sentences, not menus. Short fragments are dropped."""
    parser = _TextExtractor()
    parser.feed(html)
    lines = [re.sub(r"\s+", " ", line).strip() for line in "".join(parser.parts).splitlines()]
    kept = [line for line in lines if len(line) >= 40]  # menu items, buttons and labels are short
    return " ".join(kept)[:limit].strip()


def fetch_page_text(url: str, client=None, resolver: Resolver = resolve_host, timeout_s: float = 10.0) -> str:
    """Open one public web page and return its readable text (at most MAX_EXCERPT_CHARS characters)."""
    own = client is None
    http = client or httpx.Client(timeout=timeout_s, follow_redirects=False, headers={"User-Agent": USER_AGENT})
    try:
        current = check_page_url(url, resolver)
        for _ in range(MAX_REDIRECTS + 1):
            try:
                with http.stream("GET", current, headers={"Accept": "text/html,text/plain;q=0.9"}) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        target = resp.headers.get("location", "")
                        if not target:
                            raise PageError("The page redirected nowhere.")
                        current = check_page_url(urljoin(current, target), resolver)  # every hop is checked again
                        continue
                    if resp.status_code != 200:
                        raise PageError(f"The page answered HTTP {resp.status_code}.")
                    kind = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if kind not in ("text/html", "application/xhtml+xml", "text/plain"):
                        raise PageError("That link is not a web page (it is a file or another type), so it was not opened.")
                    body = b""
                    for chunk in resp.iter_bytes():
                        body += chunk
                        if len(body) >= MAX_PAGE_BYTES:
                            break
                    encoding = resp.encoding or "utf-8"
            except httpx.TimeoutException as exc:
                raise PageError("The page took too long to answer.") from exc
            except httpx.HTTPError as exc:
                raise PageError("The page could not be reached.") from exc
            try:
                text = body[:MAX_PAGE_BYTES].decode(encoding, "replace")
            except LookupError:  # an unknown charset name in the header
                text = body[:MAX_PAGE_BYTES].decode("utf-8", "replace")
            if kind == "text/plain":
                return re.sub(r"\s+", " ", text).strip()[:MAX_EXCERPT_CHARS]
            return extract_text(text)
        raise PageError("The page redirected too many times.")
    finally:
        if own:
            http.close()
