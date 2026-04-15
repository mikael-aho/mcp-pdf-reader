# MCP PDF Reader

Small MCP server that downloads a public PDF, extracts text, and returns it over stdio.

## What It Does

- Exposes `read_pdf(url, start_page, end_page)` to fetch and extract text from a PDF.
- Exposes `pdf_page_count(url)` to return the total page count.
- Runs as an MCP server, so an MCP client can launch it as a subprocess.

## How A Request Flows

1. The MCP client calls `read_pdf` or `pdf_page_count`.
2. The server validates that the URL uses `http` or `https`.
3. The hostname is resolved and checked to make sure it points only to public IP addresses.
4. The PDF is downloaded with redirect validation, timeout limits, and a 25 MB size cap.
5. The downloaded bytes are checked for a PDF file signature.
6. `pypdf` opens the PDF from memory.
7. The server either counts pages or extracts text from the requested page range.
8. The response is truncated if it grows too large for a practical MCP response.

## Libraries In Use

- `mcp` and `FastMCP` expose the server tools over stdio.
- `httpx` downloads remote PDFs with explicit timeout handling.
- `pypdf` opens the downloaded PDF in memory and extracts page text.

## Safety Limits

- Only public `http` and `https` URLs are allowed.
- Redirects are validated one hop at a time.
- Maximum download size is 25 MB.
- Maximum page window per request is 30 pages.
- Maximum text response size is capped to avoid oversized MCP output.

## Run With Docker

Build the image:

```bash
docker build -f dockerfile -t mcp-pdf-reader .
```

Run the server:

```bash
docker run --rm -i mcp-pdf-reader
```

## Run Without Docker

Install dependencies:

```bash
pip install -r requirements.txt
```

Start the server:

```bash
python pdf_server.py
```

## Files

- `pdf_server.py`: MCP server and PDF handling logic.
- `requirements.txt`: pinned Python dependencies.
- `dockerfile`: lightweight container image definition.