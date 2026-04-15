"""Minimal MCP server that fetches public PDFs and returns extracted text."""

# These standard-library imports handle URL parsing and IP-based safety checks.
import io
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

# Third-party dependencies: httpx downloads the file, pypdf extracts the text.
import httpx
from mcp.server.fastmcp import FastMCP
from pypdf import PdfReader

# Limit redirect chains so a remote server cannot bounce us forever.
MAX_REDIRECTS = 5
# Cap downloaded PDFs to keep memory usage bounded.
MAX_PDF_BYTES = 25 * 1024 * 1024
# Cap page windows so a single tool call stays predictable.
MAX_PAGES_PER_REQUEST = 30
# Cap total response text so the MCP response remains manageable.
MAX_OUTPUT_CHARS = 200_000
# Accept common PDF content types seen in the wild.
PDF_CONTENT_TYPES = {
    "application/octet-stream",
    "application/pdf",
    "binary/octet-stream",
}
# Handle redirects ourselves so we can validate each target URL.
REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
# Use explicit request timeouts instead of waiting indefinitely.
REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# FastMCP exposes the functions marked with @mcp.tool() over stdio.
mcp = FastMCP("pdf-reader")


def _is_public_ip(address: str) -> bool:
    # Reject loopback, private, multicast, and other non-routable addresses.
    ip = ipaddress.ip_address(address)
    return not (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_private
        or ip.is_reserved
        or ip.is_unspecified
    )


def _validate_public_url(url: str) -> None:
    # Only allow plain HTTP(S) URLs with a real hostname.
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http:// and https:// URLs are allowed.")
    if not parsed.hostname:
        raise ValueError("URL must include a hostname.")

    try:
        # If the hostname is already an IP literal, validate it directly.
        host_ip = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        host_ip = None

    if host_ip is not None:
        if not _is_public_ip(host_ip.compressed):
            raise ValueError("URL host must resolve to a public IP address.")
        return

    try:
        # Resolve DNS and reject hostnames that point at any private address.
        resolved_addresses = {
            result[4][0]
            for result in socket.getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve host: {exc}") from exc

    if not resolved_addresses:
        raise ValueError("URL host did not resolve to an address.")
    if any(not _is_public_ip(address) for address in resolved_addresses):
        raise ValueError("URL host must resolve only to public IP addresses.")


def _validate_page_window(start_page: int, end_page: int, total_pages: int) -> int:
    # Validate the requested page range before we try to read from the PDF.
    if start_page < 0:
        raise ValueError("start_page must be 0 or greater.")
    if end_page <= start_page:
        raise ValueError("end_page must be greater than start_page.")
    if start_page >= total_pages:
        raise ValueError("start_page is beyond the end of the PDF.")
    # Clamp the end page both to the document length and our per-request cap.
    return min(end_page, total_pages, start_page + MAX_PAGES_PER_REQUEST)


async def _download_pdf(url: str) -> bytes:
    # Track the current URL explicitly because we validate every redirect hop.
    current_url = url

    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=REQUEST_TIMEOUT,
        trust_env=False,
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            # Stop immediately if the current URL points at an unsafe destination.
            _validate_public_url(current_url)

            async with client.stream(
                "GET",
                current_url,
                headers={"Accept": "application/pdf, application/octet-stream;q=0.9"},
            ) as response:
                if response.status_code in REDIRECT_STATUS_CODES:
                    # Follow redirects manually so the next target gets revalidated.
                    location = response.headers.get("Location")
                    if not location:
                        raise ValueError("Redirect response did not include a Location header.")
                    current_url = urljoin(str(response.url), location)
                    continue

                # Any non-success status becomes a clear fetch error.
                response.raise_for_status()

                # Content-Type is advisory, but rejecting obvious non-PDF responses is still useful.
                content_type = response.headers.get("Content-Type", "")
                if content_type:
                    content_type = content_type.split(";", 1)[0].strip().lower()
                    if content_type not in PDF_CONTENT_TYPES:
                        raise ValueError(f"Unexpected content type: {content_type}")

                # If the server declares the size up front, reject large files before downloading them.
                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        declared_size = int(content_length)
                    except ValueError as exc:
                        raise ValueError("Response included an invalid Content-Length header.") from exc
                    if declared_size > MAX_PDF_BYTES:
                        raise ValueError(
                            f"PDF is too large; limit is {MAX_PDF_BYTES // (1024 * 1024)} MB."
                        )

                # Stream the file into memory while enforcing the byte limit incrementally.
                chunks = []
                total_bytes = 0
                async for chunk in response.aiter_bytes():
                    total_bytes += len(chunk)
                    if total_bytes > MAX_PDF_BYTES:
                        raise ValueError(
                            f"PDF is too large; limit is {MAX_PDF_BYTES // (1024 * 1024)} MB."
                        )
                    chunks.append(chunk)

                pdf_bytes = b"".join(chunks)
                # PDFs normally start with the %PDF signature; reject obviously wrong payloads.
                if not pdf_bytes.startswith(b"%PDF"):
                    raise ValueError("Response does not look like a valid PDF file.")
                return pdf_bytes

    raise ValueError(f"Too many redirects; limit is {MAX_REDIRECTS}.")


def _open_pdf(pdf_bytes: bytes) -> PdfReader:
    # pypdf reads from a file-like object, so wrap the downloaded bytes in BytesIO.
    return PdfReader(io.BytesIO(pdf_bytes))


@mcp.tool()
async def read_pdf(url: str, start_page: int = 0, end_page: int = MAX_PAGES_PER_REQUEST) -> str:
    """
    Fetch a PDF from a URL and extract its text.
    Use start_page and end_page to read in chunks (0-indexed, end_page exclusive).
    A single request reads at most 30 pages and up to 25 MB.
    """
    try:
        # Download and validate the remote file before attempting to parse it.
        pdf_bytes = await _download_pdf(url)
        pdf = _open_pdf(pdf_bytes)

        # Compute the safe page window after we know the actual document length.
        total_pages = len(pdf.pages)
        end = _validate_page_window(start_page, end_page, total_pages)

        # Start the response with a summary so callers know what slice they received.
        parts = [f"PDF has {total_pages} total pages. Showing pages {start_page + 1}-{end}.\n"]
        current_length = len(parts[0])
        truncated = False

        for page_number in range(start_page, end):
            # extract_text() can return None for image-only pages, so normalize to an empty string.
            page_text = pdf.pages[page_number].extract_text() or ""
            page_output = f"\n--- Page {page_number + 1} ---\n{page_text}"

            # Stop appending before the MCP response becomes too large.
            if current_length + len(page_output) > MAX_OUTPUT_CHARS:
                remaining = MAX_OUTPUT_CHARS - current_length
                if remaining > 0:
                    parts.append(page_output[:remaining])
                truncated = True
                break

            parts.append(page_output)
            current_length += len(page_output)

        if truncated:
            # Make truncation explicit so callers know there may be more content available.
            parts.append("\n\nOutput truncated to keep the response manageable.")

        return "".join(parts)
    except ValueError as exc:
        return f"Error: {exc}"
    except httpx.HTTPError as exc:
        return f"Error fetching PDF: {exc}"
    except Exception as exc:
        return f"Error opening or reading PDF: {exc}"


@mcp.tool()
async def pdf_page_count(url: str) -> str:
    """Return the total number of pages in a PDF."""
    try:
        # Reuse the same download and validation path as read_pdf().
        pdf_bytes = await _download_pdf(url)
        pdf = _open_pdf(pdf_bytes)
        return f"PDF has {len(pdf.pages)} pages."
    except ValueError as exc:
        return f"Error: {exc}"
    except httpx.HTTPError as exc:
        return f"Error fetching PDF: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


if __name__ == "__main__":
    # Run the server over stdio so MCP clients can launch it as a subprocess.
    mcp.run(transport="stdio")