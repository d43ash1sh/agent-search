import logging

BOT_NAME = "agent_search_crawler"

SPIDER_MODULES = ["crawler.spiders"]
NEWSPIDER_MODULE = "crawler.spiders"

ROBOTSTXT_OBEY = True
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 (AgentSearchBot/1.0; +https://github.com/agent-search)"
)
DEFAULT_REQUEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

ITEM_PIPELINES = {
    "crawler.pipelines.SQLitePipeline": 300,
}

# ── Redis Deduplication Filter ───────────────────────────────────────────────
DUPEFILTER_CLASS = "crawler.dupefilter.RedisDupeFilter"
REDIS_URL = "redis://127.0.0.1:6379/0"

# ── Broad Crawl Concurrency Settings ──────────────────────────────────────────
# Scaled for 500-1000 domains: high global concurrency with polite per-domain limits
CONCURRENT_REQUESTS = 64
CONCURRENT_REQUESTS_PER_DOMAIN = 2
REACTOR_THREADPOOL_MAXSIZE = 32

# ── DNS Optimization ──────────────────────────────────────────────────────────
# In-memory DNS cache prevents threadpool starvation when querying hundreds of domains
DNSCACHE_ENABLED = True
DNSCACHE_SIZE = 20000
DNS_TIMEOUT = 10

# ── Fail-Fast Downloader Protections ──────────────────────────────────────────
DOWNLOAD_DELAY = 0.5
DOWNLOAD_DELAY_JITTER = 0.5
DOWNLOAD_TIMEOUT = 10
DOWNLOAD_MAXSIZE = 10 * 1024 * 1024  # 10 MiB limit prevents huge memory allocations

# Retries: only 1 retry on transient errors to prevent tarpits from blocking slots
RETRY_ENABLED = True
RETRY_TIMES = 1
RETRY_HTTP_CODES = [408, 429, 500, 502, 503, 504, 522, 524]

# Disable cookies for broad crawls (saves memory and avoids state leak across domains)
COOKIES_ENABLED = False

# ── Breadth-First Crawl & Multi-Domain Priority Queue ─────────────────────────
# BFO ensures shallow pages across all 500-1000 domains are fetched before deep dives
DEPTH_PRIORITY = 1
SCHEDULER_DISK_QUEUE = "scrapy.squeues.PickleLifoDiskQueue"
SCHEDULER_MEMORY_QUEUE = "scrapy.squeues.LifoMemoryQueue"
SCHEDULER_PRIORITY_QUEUE = "scrapy.pqueues.DownloaderAwarePriorityQueue"

# ── AutoThrottle Extension ─────────────────────────────────────────────────────
# Dynamically adapts request latency per domain based on remote server load
AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 0.5
AUTOTHROTTLE_MAX_DELAY = 10.0
AUTOTHROTTLE_TARGET_CONCURRENCY = 2.0

# ── Logging Configuration ───────────────────────────────────────────────────
LOG_LEVEL = "INFO"

# Silence noisy third-party internal parser warnings (e.g. empty link icons, missing href in docs)
logging.getLogger("trafilatura").setLevel(logging.ERROR)
logging.getLogger("trafilatura.xml").setLevel(logging.ERROR)
logging.getLogger("trafilatura.core").setLevel(logging.ERROR)
logging.getLogger("trafilatura.htmlprocessing").setLevel(logging.ERROR)
