# Agent Search

[![CI](https://github.com/siddharthaborah/agent-search/actions/workflows/ci.yml/badge.svg)](https://github.com/siddharthaborah/agent-search/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![PostgreSQL pgvector](https://img.shields.io/badge/PostgreSQL-pgvector-336791?logo=postgresql&logoColor=white)](https://github.com/pgvector/pgvector)
[![Redis](https://img.shields.io/badge/Redis-7-222222?logo=redis&logoColor=FF4438)](https://redis.io/)
[![Playwright](https://img.shields.io/badge/Playwright-Chromium-2EAD33?logo=playwright&logoColor=white)](https://playwright.dev/)
[![Model Context Protocol](https://img.shields.io/badge/MCP-Protocol-7B1FA2)](https://modelcontextprotocol.io/)

**Agent Search** is an enterprise-grade, high-performance web crawl, extraction, search, and retrieval engine purpose-built for autonomous AI agents, LLM toolchains, and RAG pipelines.

---

## Key Highlights

- **Decoupled 25x Concurrency Pipeline**: Asynchronous HTTP/2 fetchers with an isolated 3-worker Playwright Chromium pool providing automated fallback on JavaScript SPAs, 403/401/429/503 access challenges, and SSL negotiation failures.
- **100% Sovereign Autonomous Search & Link Graph Engine**: Eliminates external commercial search engine reliance (DuckDuckGo/Google/Bing) through open Common Crawl CDX discovery, an autonomous directed link graph, internal PageRank calculation, and recursive outlink harvesting.
- **Continuous 24/7 Autonomous Crawler Daemon**: Background worker that continuously dequeues prioritized frontier URLs, respects per-domain politeness delays, cleans content, chunks passages, and updates vector indexes in real time.
- **Autonomous Live Web Metasearch**: Real-time zero-API-key SERP discovery, on-the-fly parallel fetch, real-time vector embeddings, and cross-encoder neural rerank without needing prior crawling.
- **Multi-Modal Vision & Dynamic Browser Actions**: High-resolution base64 PNG screenshot capture for vision LLMs (Claude 3.5 Sonnet, GPT-4o, Gemini 2.0) with automated action sequences (`click`, `scroll`, `wait`, `wait_for_selector`, `type`).
- **SSRF & Intranet Defense**: Pre-flight DNS resolution blocking loopback, link-local, RFC1918 subnets, and cloud metadata services (`169.254.169.254`).
- **Domain Politeness & Idempotency**: 24-hour cached `robots.txt` compliance, token bucket rate-limiting, domain concurrency leases, and deterministic `SHA256(URL + options)` deduplication.
- **Precision Extraction & Sentence Chunking**: Trafilatura readability engine with aggressive boilerplate pruning (XPath navigation, header, footer, and social widget removal) and sentence-level windowing ($\le 1000$ chars / ~180 tokens) maintaining exact character offsets into original clean documents.
- **Hybrid Retrieval & Neural Reranking**: PostgreSQL 16 `pgvector` HNSW index + FastEmbed ONNX (`BAAI/bge-small-en-v1.5`) dense semantic embeddings + FlashRank TinyBERT Cross-Encoder neural reranker + 64-bit SimHash near-duplicate deduplication.
- **Model Context Protocol (MCP)**: Native stdio and HTTP/SSE JSON-RPC transports compatible with Claude Desktop, Cursor, Gemini CLI, and agent frameworks.
- **Async & Sync REST Endpoints**: Synchronous `/v1/scrape`, Live Metasearch `/v1/search/live`, Asynchronous `/v1/crawl`, `/v1/map`, `/v1/extract`, and `/v1/search`.

---

## Architecture Overview

```
                      ┌──────────────────────────────────────┐
                      │    FastAPI Gateway & MCP Server      │
                      └──────────────────┬───────────────────┘
                                         │ Enqueue Fetch
                                         ▼
                      ┌──────────────────────────────────────┐
                      │    Redis Queue & URL Frontier        │
                      └──────────────────┬───────────────────┘
                                         │
                 ┌───────────────────────┴───────────────────────┐
                 ▼                                               ▼
    ┌─────────────────────────┐                     ┌─────────────────────────┐
    │   Fast HTTP/2 Fetcher   │──[SPA / Challenge]─▶│ Playwright Worker Pool  │
    │  (SSRF Guard + Robots)  │                     │  (3 Sandboxed Browsers) │
    └────────────┬────────────┘                     └────────────┬────────────┘
                 │                                               │
                 └───────────────────────┬───────────────────────┘
                                         │ Raw HTML
                                         ▼
                      ┌──────────────────────────────────────┐
                      │      Trafilatura Readability         │
                      │    + Boilerplate XPath Pruning       │
                      └──────────────────┬───────────────────┘
                                         │
                                         ▼
                      ┌──────────────────────────────────────┐
                      │ Sentence-Level Window Chunker        │
                      │ (<=1,000 chars, exact char offsets)  │
                      └──────────────────┬───────────────────┘
                                         │
                                         ▼
                      ┌──────────────────────────────────────┐
                      │   64-bit SimHash Deduplication       │
                      │   FastEmbed Dense Vector Generator   │
                      └──────────────────┬───────────────────┘
                                         │
                                         ▼
                      ┌──────────────────────────────────────┐
                      │ PostgreSQL 16 + pgvector HNSW Index  │
                      │  + FlashRank Cross-Encoder Reranker  │
                      └──────────────────────────────────────┘
```

---

## Quickstart

### 1. Prerequisites
- Python 3.12 or 3.13
- Docker and Docker Compose

### 2. Infrastructure Setup (PostgreSQL + Redis)
Start the PostgreSQL (with pgvector) and Redis containers:

```sh
docker compose up -d
```

### 3. Environment Installation
Clone the repository, create a virtual environment, and install dependencies:

```sh
# Clone repository
git clone https://github.com/siddharthaborah/agent-search.git
cd agent-search

# Setup virtual environment (Windows PowerShell)
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
playwright install chromium

# Setup virtual environment (Linux / macOS)
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
```

### 4. Run All Services
Launch the complete orchestration stack (FastAPI server, parallel crawler workers, and indexing pipelines):

```sh
python run_all.py
```

The web dashboard and OpenAPI documentation will be live at:
- **Interactive API Documentation:** [http://localhost:8000/docs](http://localhost:8000/docs)
- **Live Monitoring Dashboard:** [http://localhost:8000/dashboard](http://localhost:8000/dashboard)

---

## REST API Reference

### 1. Synchronous Live Scrape with Vision & Browser Actions (`POST /v1/scrape`)
Fetches and cleans a web page synchronously. Supports automated UI interactions (`click`, `scroll`, `wait`, `type`) and captures high-resolution base64 PNG screenshots for vision-capable models (Claude 3.5 Sonnet, GPT-4o, Gemini 2.0):

```sh
# Scrape with dynamic scroll and base64 screenshot
curl -X POST http://localhost:8000/v1/scrape -H "Content-Type: application/json" -d "{\"url\": \"https://simonwillison.net\", \"render_js\": true, \"screenshot\": true, \"actions\": [{\"type\": \"scroll\", \"direction\": \"down\", \"amount\": 1000}, {\"type\": \"wait\", \"timeout\": 500}]}"
```

### 2. Autonomous Live Web Metasearch (`POST /v1/search/live`)
Searches the live web in real time using zero-API-key DuckDuckGo SERP discovery, crawls top candidate URLs concurrently, cleans and indexes them into PostgreSQL (`pgvector`), and returns cross-encoder reranked evidence passages:

```sh
curl -X POST http://localhost:8000/v1/search/live -H "Content-Type: application/json" -d "{\"query\": \"Python 3.13 free threading PEP 703\", \"limit\": 5, \"live_crawl\": true}"
```

### 3. Asynchronous Background Crawl (`POST /v1/crawl`)
Submits a crawl job that executes asynchronously, respecting domain politeness and rate limits. Returns a `job_id`.

```sh
curl -X POST http://localhost:8000/v1/crawl -H "Content-Type: application/json" -d "{\"urls\": [\"https://simonwillison.net\"], \"max_pages\": 20}"
```

**Response:**
```json
{
  "job_id": "job_7e44a2b918df",
  "status": "queued",
  "enqueued": 1,
  "poll_url": "/v1/jobs/job_7e44a2b918df"
}
```

Check job progress at any time:
```sh
curl http://localhost:8000/v1/jobs/job_7e44a2b918df
```

### 4. Domain Outlink & Sitemap Mapping (`POST /v1/map`)
Discovers authoritative URLs and internal paths without scraping full page content:

```sh
curl -X POST http://localhost:8000/v1/map -H "Content-Type: application/json" -d "{\"url\": \"https://simonwillison.net\", \"limit\": 50}"
```

### 5. Structured Item Extraction (`POST /v1/extract`)
Extracts structured entity records using CSS selectors or JSON field definitions:

```sh
curl -X POST http://localhost:8000/v1/extract -H "Content-Type: application/json" -d "{\"url\": \"https://news.ycombinator.com\", \"selectors\": {\"title\": \".titleline > a\", \"score\": \".score\"}}"
```

### 6. Hybrid Semantic Search (`POST /v1/search`)
Executes dense vector search combined with BM25 lexical matching, Reciprocal Rank Fusion (RRF), and FlashRank neural reranking. Pass `live: true` to trigger automatic live web fallback if local index is cold:

```sh
curl -X POST http://localhost:8000/v1/search -H "Content-Type: application/json" -d "{\"query\": \"prompt engineering evaluation\", \"limit\": 5, \"tokens\": 800, \"hybrid\": true, \"rerank\": true}"
```

Each result contains:
- `evidence_id`: Deterministic passage identifier.
- `citation`: Exact string start and end offsets (`start_offset`, `end_offset`) into the parent document.
- `estimated_tokens`: Token budget estimation.

---

## Model Context Protocol (MCP) Server

Agent Search provides a native Model Context Protocol (MCP) server allowing direct integration with Claude Desktop, Cursor, Gemini CLI, and custom agent toolchains.

### Available MCP Tools
- `live_web_search`: Autonomous live web metasearch that queries the live internet, extracts and indexes content on the fly, and returns neural-reranked cited passages.
- `search_passages`: Hybrid search across indexed documents with token limits and neural reranking.
- `get_evidence_context`: Fetches neighboring context passages around any cited passage.
- `get_document`: Retrieves the full document text by document ID or URL.
- `fetch_live_page`: Live scrape with Trafilatura cleaning, optional Playwright JS execution, UI action automation, and multimodal base64 screenshot capture.
- `autonomous_search`: 100% self-hosted search without commercial SERPs, combining local hybrid retrieval, PageRank domain authority multipliers, cross-encoder reranking, and open discovery.
- `query_common_crawl`: Discovers open web URLs from the petabyte-scale Common Crawl CDX index.
- `get_autonomous_crawler_status`: Reports live telemetry and metrics of the autonomous 24/7 web crawler daemon.
- `enqueue_crawl_url`: Queues URLs into the distributed crawler frontier.
- `crawl_frontier_batch`: Executes a batch crawl from the pending URL queue.
- `get_search_stats`: Reports index size, vector counts, cache hit rates, and worker statuses.

### Claude Desktop Configuration
Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "agent-search": {
      "command": "python",
      "args": ["d:/project-base-url/run_mcp.py"]
    }
  }
}
```

Or connect via the MCP HTTP endpoint at `http://localhost:8000/mcp`.

---

## Testing & Quality Assurance

The codebase includes an extensive automated test suite covering unit, integration, and security layers:

```sh
# Run full pytest suite (94 tests)
pytest

# Ruff linting check
ruff check crawler/ services/ tests/

# Ruff formatting check
ruff format --check crawler/ services/ tests/

# Strict Mypy type check
mypy crawler services tests
```

---

## License

This project is licensed under the MIT License.
