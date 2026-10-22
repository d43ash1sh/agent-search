import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from scrapy.crawler import CrawlerProcess
from scrapy.utils.project import get_project_settings

from crawler.search import SearchService
from crawler.spiders.site import SiteSpider
from crawler.urls import host_is_allowed, normalize_url


def _crawl(args: argparse.Namespace) -> int:
    if args.repeat_every > 0:
        command = [sys.executable, "-m", "crawler", "crawl"]
        for seed in args.seed:
            command.extend(("--seed", seed))
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
                "--max-pages",
                str(args.max_pages),
            )
        )
        while True:
            result = subprocess.run(command, check=False)
            if result.returncode:
                return result.returncode
            time.sleep(args.repeat_every)

    raw_seeds = [normalize_url(url) for url in args.seed]
    if any(url is None for url in raw_seeds):
        raise SystemExit("Every --seed value must be a valid HTTP(S) URL without credentials.")
    seeds: list[str] = [url for url in raw_seeds if url is not None]

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
    settings.set("CONCURRENT_REQUESTS_PER_DOMAIN", 1, priority="cmdline")
    if args.max_pages:
        settings.set("CLOSESPIDER_PAGECOUNT", args.max_pages, priority="cmdline")

    process = CrawlerProcess(settings)
    process.crawl(SiteSpider, start_urls=seeds, allow_domains=domains)
    process.start()
    return 0


def _allowed(url: str, domains: list[str]) -> bool:
    return any(host_is_allowed(url, [domain]) for domain in domains)


def _search(args: argparse.Namespace) -> int:
    database = Path(args.database).expanduser()
    if not database.exists():
        raise SystemExit(f"Search index does not exist: {database}")
    results, _ = SearchService(str(database)).search(
        args.query,
        limit=args.limit,
        match=args.match,
    )
    for result in results:
        print(f"{result.title or '(untitled)'}\n{result.canonical_url}\n{result.text}\n")
    return 0


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    os.environ["CRAWLER_DATABASE"] = str(Path(args.database).expanduser())
    uvicorn.run("crawler.api:app", host=args.host, port=args.port, log_level="info")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Crawl allowed sites into a local SQLite FTS5 index.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    crawl = subparsers.add_parser("crawl", help="crawl from one or more seed URLs")
    crawl.add_argument("--seed", action="append", required=True, help="seed URL; repeat for multiple seeds")
    crawl.add_argument(
        "--allow-domain",
        action="append",
        default=[],
        help="allowed hostname (subdomains included); defaults to seed hostnames",
    )
    crawl.add_argument("--database", default="data/crawler.sqlite3")
    crawl.add_argument("--timeout", type=float, default=15.0)
    crawl.add_argument("--retries", type=int, default=2)
    crawl.add_argument("--delay", type=float, default=1.0)
    crawl.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="stop after this many downloaded pages (0 means unlimited)",
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
    search.add_argument("--match", choices=("all", "any"), default="all")
    search.set_defaults(handler=_search)

    serve = subparsers.add_parser("serve", help="run the Agent Search HTTP API")
    serve.add_argument("--database", default="data/crawler.sqlite3")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(handler=_serve)

    args = parser.parse_args()
    if args.command == "crawl" and (
        args.timeout <= 0 or args.delay < 0 or args.retries < 0 or args.repeat_every < 0 or args.max_pages < 0
    ):
        parser.error("--timeout must be positive, --delay and --retries cannot be negative")
    if args.command == "search" and args.limit <= 0:
        parser.error("--limit must be positive")
    if args.command == "serve" and not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    handler = args.handler
    return int(handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
