import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from scrapy.crawler import CrawlerProcess
from scrapy.utils.project import get_project_settings

from crawler.mcp import AgentSearchMCPServer
from crawler.search import SearchService
from crawler.spiders.site import SiteSpider
from crawler.urls import host_is_allowed, normalize_url


def _load_seeds(args: argparse.Namespace) -> list[str]:
    raw_list: list[str] = list(args.seed or [])
    if args.seeds_file:
        file_path = Path(args.seeds_file).expanduser()
        if not file_path.exists():
            raise SystemExit(f"Seeds file not found: {file_path}")
        content = file_path.read_text(encoding="utf-8").strip()
        if content.startswith("["):
            try:
                parsed = json.loads(content)
                if isinstance(parsed, list):
                    raw_list.extend(str(item).strip() for item in parsed if item)
            except Exception as error:
                raise SystemExit(f"Failed to parse seeds JSON file: {error}") from error
        else:
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    raw_list.append(line)

    if not raw_list:
        raise SystemExit("At least one --seed or a --seeds-file must be provided.")

    normalized_seeds = [normalize_url(url) for url in raw_list]
    if any(url is None for url in normalized_seeds):
        raise SystemExit("Every seed value must be a valid HTTP(S) URL without credentials.")
    return [url for url in normalized_seeds if url is not None]


def _crawl(args: argparse.Namespace) -> int:
    if args.repeat_every > 0:
        command = [sys.executable, "-m", "crawler", "crawl"]
        for seed in args.seed or []:
            command.extend(("--seed", seed))
        if args.seeds_file:
            command.extend(("--seeds-file", args.seeds_file))
        for domain in args.allow_domain:
            command.extend(("--allow-domain", domain))
        command.extend(
            (
                "--database",
                args.database,
                "--timeout",
                str(args.timeout),
                "--retries",
                str(args.retries),
                "--delay",
                str(args.delay),
                "--concurrency",
                str(args.concurrency),
                "--concurrency-per-domain",
                str(args.concurrency_per_domain),
                "--max-pages",
                str(args.max_pages),
                "--max-pages-per-domain",
                str(args.max_pages_per_domain),
                "--max-depth",
                str(args.max_depth),
            )
        )
        if args.sitemaps:
            command.append("--sitemaps")
        if getattr(args, "ignore_robots", False):
            command.append("--ignore-robots")
        if args.jobdir:
            command.extend(("--jobdir", args.jobdir))
        while True:
            result = subprocess.run(command, check=False)
            if result.returncode:
                return result.returncode
            time.sleep(args.repeat_every)

    seeds = _load_seeds(args)

    domains = [domain.lower().strip(".") for domain in args.allow_domain]
    if not domains:
        domains = list(dict.fromkeys((urlsplit(url).hostname or "") for url in seeds))
    elif any(not domain for domain in domains):
        raise SystemExit("--allow-domain values must be hostnames.")

    rejected = [url for url in seeds if not _allowed(url, domains)]
    if rejected:
        raise SystemExit(f"Seed URL is outside the domain allowlist: {rejected[0]}")

    settings = get_project_settings()
    settings.set("DATABASE_PATH", str(Path(args.database).expanduser()), priority="cmdline")
    settings.set("DOWNLOAD_TIMEOUT", args.timeout, priority="cmdline")
    settings.set("RETRY_TIMES", args.retries, priority="cmdline")
    settings.set("DOWNLOAD_DELAY", args.delay, priority="cmdline")
    settings.set("CONCURRENT_REQUESTS", args.concurrency, priority="cmdline")
    settings.set("CONCURRENT_REQUESTS_PER_DOMAIN", args.concurrency_per_domain, priority="cmdline")
    if args.delay == 0:
        settings.set("AUTOTHROTTLE_ENABLED", False, priority="cmdline")
    if args.max_pages:
        settings.set("CLOSESPIDER_PAGECOUNT", args.max_pages, priority="cmdline")
    if args.max_depth:
        settings.set("DEPTH_LIMIT", args.max_depth, priority="cmdline")
    if args.jobdir:
        settings.set("JOBDIR", str(Path(args.jobdir).expanduser()), priority="cmdline")
    if getattr(args, "ignore_robots", False):
        settings.set("ROBOTSTXT_OBEY", False, priority="cmdline")

    process = CrawlerProcess(settings)
    process.crawl(
        SiteSpider,
        start_urls=seeds,
        allow_domains=domains,
        max_pages_per_domain=args.max_pages_per_domain,
        follow_sitemaps=args.sitemaps,
    )
    process.start()
    return 0


def _allowed(url: str, domains: list[str]) -> bool:
    return any(host_is_allowed(url, [domain]) for domain in domains)


def _search(args: argparse.Namespace) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    database = Path(args.database).expanduser()
    if not database.exists():
        raise SystemExit(f"Search index does not exist: {database}")
    results, tokens = SearchService(str(database)).search(
        args.query,
        limit=args.limit,
        max_tokens=args.tokens,
        domains=args.domain,
        match=args.match,
        mode=args.mode,
    )
    print(f"Results: {len(results)} | Estimated Tokens: {tokens}/{args.tokens} | Mode: {args.mode}\n")
    for idx, result in enumerate(results, start=1):
        print(f"[{idx}] {result.title or '(untitled)'}")
        print(f"    URL: {result.canonical_url}")
        print(f"    Evidence ID: {result.evidence_id}")
        print(f"    Score: {result.score:.4f} | Tokens: ~{result.estimated_tokens}")
        print(f"    {result.text}\n")
    return 0


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    os.environ["CRAWLER_DATABASE"] = str(Path(args.database).expanduser())
    uvicorn.run("crawler.api:app", host=args.host, port=args.port, log_level="info")
    return 0


def _mcp(args: argparse.Namespace) -> int:
    try:
        from services.mcp.server import mcp

        mcp.run()
        return 0
    except Exception:
        database = str(Path(args.database).expanduser())
        server = AgentSearchMCPServer(database=database)
        server.run_stdio()
        return 0


def _stats(args: argparse.Namespace) -> int:
    database = str(Path(args.database).expanduser())
    service = SearchService(database)
    stats = service.get_stats()
    print(json.dumps(stats, indent=2))
    return 0


def _reclean(args: argparse.Namespace) -> int:
    import sqlite3

    from crawler.cleaning import clean_legacy_text
    from crawler.pipelines import _passage_records

    database_path = Path(args.database).expanduser()
    if not database_path.exists():
        print(f"Database not found: {database_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(database_path, check_same_thread=False)
    try:
        rows = conn.execute("SELECT url, canonical_url, title, text FROM documents").fetchall()
        print(f"Sanitizing {len(rows)} documents in {database_path}...")
        updated = 0
        for url, _canonical_url, _title, raw_text in rows:
            clean_text = clean_legacy_text(raw_text)
            if not clean_text:
                continue

            conn.execute("UPDATE documents SET text = ? WHERE url = ?", (clean_text, url))
            passage_records = _passage_records(url, clean_text)
            conn.execute("DELETE FROM passages WHERE document_url = ?", (url,))
            conn.executemany(
                """
                INSERT INTO passages(
                    document_url, ordinal, text, evidence_id, start_offset, end_offset
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (url, ordinal, passage, evidence_id, start, end)
                    for ordinal, (passage, evidence_id, start, end) in enumerate(passage_records)
                ],
            )
            updated += 1

        conn.commit()
        print(f"Successfully cleaned and re-indexed {updated} documents.")
        return 0
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Crawl allowed sites into a local SQLite FTS5 index.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    crawl = subparsers.add_parser("crawl", help="crawl from one or more seed URLs or seeds file")
    crawl.add_argument("--seed", action="append", default=[], help="seed URL; repeat for multiple seeds")
    crawl.add_argument(
        "--seeds-file",
        help="path to a text file with one URL per line or a JSON array of URLs",
    )
    crawl.add_argument(
        "--allow-domain",
        action="append",
        default=[],
        help="allowed hostname (subdomains included); defaults to seed hostnames",
    )
    crawl.add_argument("--database", default="data/crawler.sqlite3")
    crawl.add_argument("--timeout", type=float, default=10.0)
    crawl.add_argument("--retries", type=int, default=1)
    crawl.add_argument("--delay", type=float, default=0.5)
    crawl.add_argument(
        "--concurrency",
        type=int,
        default=64,
        help="maximum concurrent requests across all domains (default: 64)",
    )
    crawl.add_argument(
        "--concurrency-per-domain",
        type=int,
        default=2,
        help="maximum concurrent requests per domain (default: 2)",
    )
    crawl.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="stop after this many downloaded pages across all domains (0 means unlimited)",
    )
    crawl.add_argument(
        "--max-pages-per-domain",
        type=int,
        default=0,
        help="maximum pages to index from any single domain (0 means unlimited)",
    )
    crawl.add_argument(
        "--max-depth",
        type=int,
        default=0,
        help="maximum link recursion depth from seed URLs (0 means unlimited)",
    )
    crawl.add_argument(
        "--sitemaps",
        action="store_true",
        default=False,
        help="automatically discover and crawl sitemaps (/sitemap.xml)",
    )
    crawl.add_argument(
        "--ignore-robots",
        action="store_true",
        default=False,
        help="bypass robots.txt restrictions (use responsibly with polite delays)",
    )
    crawl.add_argument(
        "--jobdir",
        help="directory to persist crawl state on disk for pause/resume capability",
    )
    crawl.add_argument(
        "--repeat-every",
        type=float,
        default=0,
        help="repeat full crawl after this many seconds (0 disables repeat mode)",
    )
    crawl.set_defaults(handler=_crawl)

    search = subparsers.add_parser("search", help="search the local document index")
    search.add_argument("query", help="FTS5 search query")
    search.add_argument("--database", default="data/crawler.sqlite3")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--tokens", type=int, default=1200, help="maximum estimated token budget")
    search.add_argument(
        "--mode",
        choices=("compact", "balanced", "research"),
        default="compact",
        help="retrieval mode: compact (strict dedup), balanced, or research (diverse domains)",
    )
    search.add_argument("--domain", action="append", help="filter by domain (repeat for multiple)")
    search.add_argument("--match", choices=("all", "any"), default="all")
    search.set_defaults(handler=_search)

    serve = subparsers.add_parser("serve", help="run the Agent Search HTTP API")
    serve.add_argument("--database", default="data/crawler.sqlite3")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(handler=_serve)

    mcp_parser = subparsers.add_parser("mcp", help="run the Agent Search MCP server over stdio")
    mcp_parser.add_argument("--database", default="data/crawler.sqlite3")
    mcp_parser.set_defaults(handler=_mcp)

    stats_parser = subparsers.add_parser("stats", help="display search index statistics")
    stats_parser.add_argument("--database", default="data/crawler.sqlite3")
    stats_parser.set_defaults(handler=_stats)

    reclean_parser = subparsers.add_parser(
        "reclean", help="sanitize and clean legacy scraped content in SQLite"
    )
    reclean_parser.add_argument("--database", default="data/crawler.sqlite3")
    reclean_parser.set_defaults(handler=_reclean)

    args = parser.parse_args()
    if args.command == "crawl":
        if not args.seed and not args.seeds_file:
            parser.error("At least one --seed or a --seeds-file must be provided.")
        if (
            args.timeout <= 0
            or args.delay < 0
            or args.retries < 0
            or args.repeat_every < 0
            or args.max_pages < 0
            or args.max_pages_per_domain < 0
            or args.max_depth < 0
            or args.concurrency <= 0
            or args.concurrency_per_domain <= 0
        ):
            parser.error(
                "Invalid numeric argument: concurrency/timeout must be positive, limits non-negative"
            )
    if args.command == "search" and (args.limit <= 0 or args.tokens <= 0):
        parser.error("--limit and --tokens must be positive")
    if args.command == "serve" and not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    handler = args.handler
    return int(handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
