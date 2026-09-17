"""采集器包（Phase 1 第 2 步：Collector Framework）。

结构：
- ``types``：数据契约（payload / page / outcome），字段对齐 04_数据表结构及字段定义
- ``transport``：HTTP 传输层（3 次重试 + 超时；测试注入 Mock，不访问外部网络）
- ``base``：``BaseCollector`` —— ``collect()`` 只做站点无关的编排，
  ``_do_fetch()`` 留给微博 / 新闻 / 行情 / 宏观采集器继承实现
- ``registry``：采集器注册表（与 ``sources.config_json["collector"]`` 对齐）
- ``runner``：单轮运行编排（游标续采、``collector_runs`` 落库、单源失败隔离）

⚠️ 生产 / 研究环境必须使用 PostgreSQL；SQLite 仅用于本地开发与自动化测试。
"""

from src.collectors.base import BaseCollector
from src.collectors.errors import (
    CollectorError,
    CollectorFetchError,
    CollectorNotRegisteredError,
    FetchAttempt,
    TransportError,
    TransportTimeoutError,
)
from src.collectors.macro import MacroCollector

# 具体采集器：副作用导入，保证 ``import src.collectors`` 即完成注册（注册表不依赖导入顺序）。
# 已实现：market_collector（行情）、news_collector（新闻）、
# macro_collector（宏观，Step 3 第 3 个）。
# 待实现（禁止伪完成）：econ_calendar_collector；weibo_collector 属 Phase 2，且严禁未授权抓取。
from src.collectors.market import MarketCollector
from src.collectors.news import NewsCollector
from src.collectors.registry import (
    available_collectors,
    build_collector,
    collector_for_source,
    get_collector_class,
    register_collector,
    unregister_collector,
)
from src.collectors.rss_collector import (
    RSS_PARSER_VERSION,
    RssCollector,
    RssSourceSpec,
    load_rss_sources,
    parse_feed_entries,
)
from src.collectors.runner import (
    CollectorRunResult,
    load_resume_cursor,
    run_collector,
    run_collectors,
)
from src.collectors.transport import (
    AiohttpTransport,
    HttpRequest,
    HttpResponse,
    RetryPolicy,
    Transport,
    send_with_retry,
)
from src.collectors.types import (
    CollectorHealth,
    CollectOutcome,
    CollectWindow,
    FetchPage,
    MediaPayload,
    RawItemPayload,
    new_cursor,
)

__all__ = [
    "AiohttpTransport",
    "BaseCollector",
    "CollectOutcome",
    "CollectWindow",
    "CollectorError",
    "CollectorFetchError",
    "CollectorHealth",
    "CollectorNotRegisteredError",
    "CollectorRunResult",
    "FetchAttempt",
    "FetchPage",
    "HttpRequest",
    "HttpResponse",
    "MacroCollector",
    "MarketCollector",
    "MediaPayload",
    "NewsCollector",
    "RawItemPayload",
    "RetryPolicy",
    "RssCollector",
    "RssSourceSpec",
    "RSS_PARSER_VERSION",
    "Transport",
    "TransportError",
    "TransportTimeoutError",
    "available_collectors",
    "build_collector",
    "collector_for_source",
    "get_collector_class",
    "load_resume_cursor",
    "load_rss_sources",
    "new_cursor",
    "parse_feed_entries",
    "register_collector",
    "run_collector",
    "run_collectors",
    "send_with_retry",
    "unregister_collector",
]
