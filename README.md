# Agent Search prototype

This prototype is organized as a crawl-to-evidence pipeline:

- **Crawl infrastructure:** Scrapy fetches seed URLs and allowed links, obeys
  `robots.txt`, applies per-domain delays, retries/timeouts, and stores a
  content hash plus last-changed/fetched timestamps.
- **Content processing:** HTML is reduced to page text and structured text
  blocks, nested blocks and exact duplicate blocks are removed, then content
  is split into passages. Navigation, footer, header, script, and style text is
  excluded from the structured extraction.
- **Search index:** SQLite FTS5 provides lexical passage search with source
  filtering and canonical-URL deduplication.
- **Agent retrieval API:** FastAPI returns compact passages with citation
  metadata and a conservative token estimate/budget.
- **Protocol integration:** HTTP is the first interface; the API publishes
  OpenAPI docs at `/docs`. MCP can later wrap the same search service.

Semantic retrieval is not implemented yet. The first retrieval mode is
explicitly lexical; an embedding model/backend is needed before semantic or
hybrid search can be truthfully offered.

## Setup

Create and activate a virtual environment, then install the package in
editable mode with development dependencies:

```sh
# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Install the pre-commit hooks (runs linting and formatting on every commit):

```sh
pre-commit install
```

## Crawl

### Single or Multi-Seed Crawl

```sh
python -m crawler crawl \
  --seed https://example.com/ \
  --seed https://docs.example.com/ \
  --allow-domain example.com \
  --database data/crawler.sqlite3
```

### Broad Crawl (500–1,000 Websites)

For crawling hundreds of domains, supply a seeds file (`seeds.txt` or `seeds.json`) with per-domain limits, sitemap discovery, and broad-scale concurrency:

```sh
python -m crawler crawl \
  --seeds-file seeds.txt \
  --concurrency 100 \
  --concurrency-per-domain 2 \
  --max-pages-per-domain 25 \
  --max-depth 2 \
  --sitemaps \
  --jobdir data/crawls/job1 \
  --database data/crawler.sqlite3
```

- `--seeds-file`: Path to a text file with one URL per line (supports `#` comments) or a JSON array of URLs.
- `--max-pages-per-domain N`: Caps the number of indexed pages per domain (default `0` = unlimited) to prevent any single domain from starving others.
- `--max-depth N`: Maximum link recursion depth from seed URLs (default `0` = unlimited).
- `--sitemaps`: Automatically discovers and parses XML sitemaps (`/sitemap.xml`) to directly index authoritative documentation URLs.
- `--jobdir DIR`: Persists the crawl queue and dupefilter state to disk, allowing interrupted crawls to be safely resumed.
- `--concurrency N`: Maximum concurrent requests in-flight globally (default `64`).
- `--concurrency-per-domain N`: Maximum concurrent requests per individual domain (default `2`).

If `--allow-domain` is omitted, each seed's hostname is used as an allowed
domain. Exact hostnames and their subdomains are permitted. The crawler obeys
`robots.txt`, waits the configured delay between requests to each domain,
allows one concurrent request per domain, uses a 15-second download timeout,
and retries selected transient errors twice by default. Tune with `--delay`,
`--timeout`, and `--retries`.

Re-running a crawl upserts documents. `changed_at` moves when the canonical
URL, title, or extracted text changes; `fetched_at` tracks the most recent
successful fetch. Re-crawl existing sites after changing extraction logic so
their already-indexed passages are rebuilt from the cleaned content.

For a continuously refreshed prototype, opt into a full recrawl interval:

```sh
python -m crawler crawl --seed https://example.com/ \
  --allow-domain example.com --repeat-every 3600
```

The first crawl starts immediately; subsequent crawls start after the configured
sleep interval. This is a simple periodic refresh, not a change-aware frontier
or per-URL scheduler.

Use `--max-pages N` to bound the number of downloaded pages in one crawl;
`0` means unlimited.

## HTTP API

Start the API against the same SQLite index:

```sh
python -m crawler serve --database data/crawler.sqlite3 --host 127.0.0.1 --port 8000
```

The index must exist first. If search returns `503`, crawl at least one seed
using the same database path passed to `serve`:

```sh
python -m crawler crawl --seed https://example.com/ \
  --allow-domain example.com --database data/crawler.sqlite3
python -m crawler serve --database data/crawler.sqlite3
```

Search with a token budget and optional repeated domain filters:

```sh
curl 'http://127.0.0.1:8000/v1/search?q=retry+policy&limit=5&max_tokens=700&domain=docs.scrapy.org'
```

By default, every query term must match a passage (`match=all`), improving
precision over the previous broad OR behavior. Use `match=any` to broaden
recall when a strict query returns too few results:

```sh
curl 'http://127.0.0.1:8000/v1/search?q=retry+policy&match=any&domain=docs.scrapy.org'
```

The JSON response includes ranked `results`, passage text, URL/canonical URL,
title, source domain, fetch/change timestamps, per-result `estimated_tokens`,
total estimated tokens, the requested budget, and `retrieval_mode: "lexical"`.
It also returns the applied `match_mode`.
Token counts are model-independent estimates (roughly one token per four
characters), not exact tokenizer counts. `GET /health` is a liveness check;
interactive API documentation is available at `/docs`.

Each result has a stable `evidence_id`. `citation.start_offset` and
`citation.end_offset` are zero-based, end-exclusive Python string offsets into
the indexed cleaned document text. Retrieve that passage later with:

```sh
curl 'http://127.0.0.1:8000/v1/evidence/<evidence_id>'
```

The API migrates existing SQLite indexes on startup to add passage IDs and
offsets.

The JSON CLI remains available:

```sh
python -m crawler search "retry policy" --match all --database data/crawler.sqlite3
```

## Tests

```sh
.venv/bin/python -m unittest discover -s tests
```

## Documentation

- [Scrapy tutorial](https://docs.scrapy.org/en/latest/intro/tutorial.html)
- [Scrapy downloader middleware and retries](https://docs.scrapy.org/en/latest/topics/downloader-middleware.html#retrymiddleware)
- [Scrapy AutoThrottle](https://docs.scrapy.org/en/latest/topics/autothrottle.html)
- [FastAPI first steps](https://fastapi.tiangolo.com/tutorial/first-steps/)
