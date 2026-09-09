"""Complete local source extraction and HTTPS transport for public documents.

This module never writes files. Callers retain the original bytes and the
extracted blocks inside their selected workspace. No provider text is executed.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import math
import os
import re
import selectors
import socket
import ssl
import subprocess
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any

from .errors import ProviderError

MAX_DOCUMENT_BYTES = 100 * 1024 * 1024
MAX_REDIRECTS = 5
_CREDENTIAL_QUERY_KEYS = frozenset(
    {"email", "mailto", "api_key", "apikey", "access_token", "authorization"}
)


def validate_public_url(url: str) -> urllib.parse.SplitResult:
    """Check URL syntax before resolution; connected addresses are checked too."""

    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 33 for c in url):
        raise ProviderError("document URL must be bounded text without whitespace")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ProviderError("invalid document URL") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
    ):
        raise ProviderError("document destinations require HTTPS on port 443 without credentials")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ProviderError("document destination is not public")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[a-z0-9.-]+", host) or "." not in host:
            raise ProviderError("document destination is not a public hostname")
    else:
        if not address.is_global:
            raise ProviderError("document destination is not public")
    return parsed


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    """Pin a validated DNS answer while preserving TLS hostname verification."""

    def connect(self) -> None:
        if self._tunnel_host:
            raise ProviderError("proxy tunnels are not supported for source downloads")
        try:
            answers = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ProviderError("public destination DNS lookup failed") from exc
        addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
        if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
            raise ProviderError("destination DNS includes a non-public address")
        deadline = time.monotonic() + float(self.timeout)
        last_error: OSError | None = None
        for address in addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                # An IP literal prevents a second DNS lookup from changing the
                # destination between validation and connection.
                raw = socket.create_connection((address, self.port), remaining)
                try:
                    self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
                except Exception:
                    raw.close()
                    raise
                return
            except OSError as exc:
                last_error = exc
        raise ProviderError("public HTTPS connection failed") from last_error


class _Response:
    def __init__(self, response: http.client.HTTPResponse, connection: Any, url: str, deadline: float):
        self.response = response
        self.connection = connection
        self.status = response.status
        self.headers = response.headers
        self.url = url
        self.deadline = deadline

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            raise ProviderError("provider reads require an explicit byte bound")
        chunks = bytearray()
        while len(chunks) < size:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError("provider response exceeded the absolute timeout")
            if self.connection.sock is not None:
                self.connection.sock.settimeout(remaining)
            chunk = self.response.read1(min(64 * 1024, size - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        return bytes(chunks)

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: Any) -> None:
        self.response.close()
        self.connection.close()


def open_public_https(
    request: urllib.request.Request,
    *,
    timeout: float,
    allow_cross_origin: bool = False,
    authorize_url: Callable[[str], None] | None = None,
) -> _Response:
    """Open GET only, without proxies/cookies or cross-host credential forwarding."""

    url = request.full_url
    initial = validate_public_url(url)
    headers = dict(request.header_items())
    deadline = time.monotonic() + timeout
    for redirect in range(MAX_REDIRECTS + 1):
        parsed = validate_public_url(url)
        if parsed.hostname != initial.hostname and not allow_cross_origin:
            raise ProviderError("metadata redirect changed provider origin")
        if allow_cross_origin:
            # Document requests carry only protocol controls. In particular,
            # they never inherit contact email, API keys, cookies, or a referer.
            headers = {"Accept": "application/pdf, application/xml, text/xml", "User-Agent": "ocsl-review/0.1"}
            if any(key.lower() in _CREDENTIAL_QUERY_KEYS for key, _ in urllib.parse.parse_qsl(parsed.query)):
                raise ProviderError("document URL contains a provider credential parameter")
        if authorize_url:
            authorize_url(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError("HTTPS redirects exceeded the configured timeout")
        connection = _PublicHTTPSConnection(parsed.hostname, 443, timeout=remaining, context=ssl.create_default_context())
        try:
            connection.request("GET", urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, "")), headers=headers)
            response = connection.getresponse()
        except Exception:
            connection.close()
            raise
        if response.status not in {301, 302, 303, 307, 308}:
            return _Response(response, connection, url, deadline)
        location = response.headers.get("Location")
        response.close()
        connection.close()
        if not location or redirect == MAX_REDIRECTS:
            raise ProviderError("document redirect chain is missing a target or exceeds its bound")
        url = urllib.parse.urljoin(url, location)
    raise ProviderError("document redirect bound exceeded")


def _text(element: ET.Element | None) -> str:
    return " ".join("".join(element.itertext()).split()) if element is not None else ""


def parse_xml_document(body: bytes) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, str], list[dict[str, str]]]:
    if re.search(br"<!ENTITY\b", body, flags=re.IGNORECASE):
        raise ProviderError("source XML contains entity declarations")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ProviderError("source contains malformed XML") from exc
    for element in root.iter():
        element.tag = element.tag.rsplit("}", 1)[-1]
    if root.tag != "article" or root.find("body") is None:
        raise ProviderError("source XML does not contain a complete article body")
    blocks: list[dict[str, Any]] = []
    supplements: list[dict[str, str]] = []
    identifiers = {
        {"pmc": "pmcid"}.get(element.get("pub-id-type", ""), element.get("pub-id-type", "")): _text(element)
        for element in root.findall("./front/article-meta/article-id") if element.get("pub-id-type")
    }
    visual_assets = [
        {"kind": element.tag, "href": value}
        for element in root.iter() if element.tag in {"graphic", "inline-graphic", "media"}
        for key, value in element.attrib.items() if key.rsplit("}", 1)[-1] == "href"
    ]
    # Inline supplementary links can sit inside a paragraph that is emitted as
    # one reading unit. Inventory them independently of that traversal.
    for element in root.iter("supplementary-material"):
        for child in element.iter():
            for key, value in child.attrib.items():
                if key.rsplit("}", 1)[-1] == "href":
                    item = {"label": _text(element.find("label")) or "Supplement", "href": value}
                    if item not in supplements:
                        supplements.append(item)

    def emit(kind: str, element: ET.Element, locator: str) -> None:
        value = _text(element)
        if value:
            blocks.append({"id": f"block-{len(blocks) + 1:06d}", "kind": kind, "locator": locator, "text": value})

    def walk(element: ET.Element, path: str) -> None:
        tag = element.tag
        locator = f"{path}/{tag}" + (f"[@id='{element.get('id')}']" if element.get("id") else "")
        if tag == "table-wrap":
            emit("table-label", element.find("label"), locator + "/label") if element.find("label") is not None else None
            if element.find("caption") is not None:
                emit("caption", element.find("caption"), locator + "/caption")
            for index, row in enumerate(element.findall(".//tr"), 1):
                values = [_text(cell) for cell in row if cell.tag in {"td", "th"}]
                if any(values):
                    blocks.append({"id": f"block-{len(blocks) + 1:06d}", "kind": "table-row", "locator": f"{locator}/row[{index}]", "text": " | ".join(values)})
            # Table footnotes are evidence too.
            for child in element:
                if child.tag not in {"label", "caption", "table"}:
                    walk(child, locator)
            return
        if tag in {"article-title", "title", "p", "caption", "disp-formula", "ref", "label"}:
            emit({"p": "paragraph", "ref": "reference", "disp-formula": "formula"}.get(tag, tag), element, locator)
            return
        children = list(element)
        if not children:
            # Preserve textual leaves in lists, metadata, acknowledgments, etc.
            emit("text", element, locator)
            return
        if element.text and element.text.strip():
            value = " ".join(element.text.split())
            blocks.append({"id": f"block-{len(blocks) + 1:06d}", "kind": "text", "locator": locator, "text": value})
        for index, child in enumerate(children, 1):
            walk(child, f"{locator}[{index}]")
            if child.tail and child.tail.strip():
                blocks.append({"id": f"block-{len(blocks) + 1:06d}", "kind": "text", "locator": locator, "text": " ".join(child.tail.split())})

    walk(root, "")
    if not _text(root.find("body")) or not blocks:
        raise ProviderError("source article has an empty body")
    return blocks, supplements, identifiers, visual_assets


def _extract_pdf_text(body: bytes, timeout: float) -> bytes:
    """Stream all three child pipes with hard output and elapsed-time bounds."""

    process = None
    selector = selectors.DefaultSelector()
    output = bytearray()
    error_bytes = 0
    sent = 0
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(["pdftotext", "-layout", "-", "-"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        for stream, event, label in ((process.stdin, selectors.EVENT_WRITE, "stdin"), (process.stdout, selectors.EVENT_READ, "stdout"), (process.stderr, selectors.EVENT_READ, "stderr")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, event, label)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError("complete PDF extraction exceeded its timeout")
            for key, _ in selector.select(remaining):
                if key.data == "stdin":
                    try:
                        sent += os.write(key.fd, body[sent:sent + 65536])
                    except BrokenPipeError:
                        sent = len(body)
                    if sent == len(body):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                else:
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    elif key.data == "stdout":
                        output.extend(chunk)
                        if len(output) > MAX_DOCUMENT_BYTES:
                            raise ProviderError("complete PDF extraction exceeded its output bound")
                    else:
                        error_bytes += len(chunk)
                        if error_bytes > 1024 * 1024:
                            raise ProviderError("PDF extractor exceeded its diagnostic output bound")
        remaining = deadline - time.monotonic()
        if remaining <= 0 or process.wait(timeout=remaining) != 0:
            raise ProviderError("complete PDF extraction failed")
        return bytes(output)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProviderError("complete PDF extraction could not run within its bounds") from exc
    finally:
        selector.close()
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


def parse_source_document(
    body: bytes,
    media_type: str | None = None,
    source_url: str | None = None,
    *,
    pdf_timeout: float = 60.0,
) -> dict[str, Any]:
    """Extract every article block; reject empty, unsupported, or oversized input."""

    if not body or len(body) > MAX_DOCUMENT_BYTES:
        raise ProviderError("source document is empty or exceeds the 100 MiB bound")
    if not math.isfinite(pdf_timeout):
        raise ProviderError("PDF extraction timeout must be finite")
    supplied_type = (media_type or "").split(";", 1)[0].strip().lower()
    supplements: list[dict[str, str]] = []
    identifiers: dict[str, str] = {}
    visual_assets: list[dict[str, str]] = []
    if body.lstrip().startswith(b"%PDF-"):
        extracted = _extract_pdf_text(body, min(max(pdf_timeout, 1.0), 120.0))
        try:
            pages = extracted.decode("utf-8").split("\f")
        except UnicodeDecodeError as exc:
            raise ProviderError("PDF extractor returned invalid UTF-8") from exc
        if pages and not pages[-1].strip():
            pages.pop()
        # A page block preserves table columns and captions in layout order.
        blocks = [{"id": f"page-{index:06d}", "kind": "page", "locator": f"page {index}", "text": page.strip()} for index, page in enumerate(pages, 1) if page.strip()]
        if not blocks or len(blocks) != len(pages):
            raise ProviderError("PDF has empty or image-only pages; complete text requires OCR")
        actual_type = "application/pdf"
    elif supplied_type in {"text/plain", "text/markdown"}:
        try:
            value = body.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ProviderError("local source is not UTF-8 text") from exc
        blocks = [{"id": "block-000001", "kind": "text", "locator": "complete text", "text": value}] if value else []
        actual_type = supplied_type
    else:
        blocks, supplements, identifiers, visual_assets = parse_xml_document(body)
        actual_type = "application/xml"
    if not blocks or not any(block["text"].strip() for block in blocks):
        raise ProviderError("source extraction produced no readable content")
    return {"body": body, "mediaType": actual_type, "sourceUrl": source_url, "sha256": hashlib.sha256(body).hexdigest(), "complete": True, "text": "\n\n".join(block["text"] for block in blocks), "blocks": blocks, "supplements": supplements, "identifiers": identifiers, "visualAssets": visual_assets}
