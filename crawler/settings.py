BOT_NAME = "local_crawler"

SPIDER_MODULES = ["crawler.spiders"]
NEWSPIDER_MODULE = "crawler.spiders"

ROBOTSTXT_OBEY = True
USER_AGENT = "LocalPrototypeCrawler/0.1 (+local research crawler)"

ITEM_PIPELINES = {
    "crawler.pipelines.SQLitePipeline": 300,
}

CONCURRENT_REQUESTS_PER_DOMAIN = 1
DOWNLOAD_DELAY = 1.0
DOWNLOAD_TIMEOUT = 15
DOWNLOAD_DELAY_JITTER = 0
RETRY_ENABLED = True
RETRY_TIMES = 2
RETRY_HTTP_CODES = [408, 429, 500, 502, 503, 504, 522, 524]

LOG_LEVEL = "INFO"
