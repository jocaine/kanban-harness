"""Lightweight Firecrawl-compatible API for web content extraction.

Exposes POST /v1/scrape — accepts a URL, returns markdown content.
Uses httpx for fetching and trafilatura for content extraction.
Single-file, single-container, minimal dependencies.
"""

import asyncio
import json
import logging
from urllib.parse import urlparse

import httpx
import trafilatura
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("firecrawl-lite")

app = FastAPI(title="Firecrawl Lite", version="0.1.0")

TIMEOUT = 30
MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5MB
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"


async def fetch_and_extract(url: str) -> dict:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
    async with httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT, max_redirects=5) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "text/html" not in content_type and "application/xhtml" not in content_type:
            return {"markdown": f"[Non-HTML content: {content_type}]", "metadata": {"statusCode": resp.status_code}}
        html = resp.text[:MAX_CONTENT_LENGTH]

    markdown = await asyncio.to_thread(
        trafilatura.extract,
        html,
        output_format="txt",
        include_links=True,
        include_tables=True,
        include_images=False,
        favor_recall=True,
    )

    title = trafilatura.extract(html, output_format="xml", include_links=False)
    meta_title = ""
    if title:
        import re
        m = re.search(r"<title>(.*?)</title>", title)
        if m:
            meta_title = m.group(1)

    return {
        "markdown": markdown or "[No extractable content]",
        "metadata": {
            "title": meta_title,
            "sourceURL": url,
            "statusCode": resp.status_code,
        },
    }


@app.post("/v1/scrape")
@app.post("/v2/scrape")
async def scrape(request: Request):
    body = await request.json()
    url = body.get("url", "")
    if not url:
        return JSONResponse(status_code=400, content={"success": False, "error": "url is required"})

    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return JSONResponse(status_code=400, content={"success": False, "error": "invalid url"})

    try:
        result = await fetch_and_extract(url)
        return {"success": True, "data": result}
    except httpx.HTTPStatusError as e:
        logger.warning("HTTP error for %s: %s", url, e.response.status_code)
        return JSONResponse(
            status_code=200,
            content={"success": False, "error": f"HTTP {e.response.status_code}", "data": {"metadata": {"statusCode": e.response.status_code}}},
        )
    except Exception as e:
        logger.error("Extract failed for %s: %s", url, str(e))
        return JSONResponse(status_code=200, content={"success": False, "error": str(e)})


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3002)
