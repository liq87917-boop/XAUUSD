"""Phase 1 基础数据种子（instruments / sources）。

用法：
    python -m database.seeds                      # 使用 .env / 环境变量 DATABASE_URL
    python -m database.seeds --scope instruments  # 只写标的白名单
    python -m database.seeds --dry-run            # 演练（执行后回滚，不落库）

⚠️ 环境说明：本地开发可使用 SQLite，但 **生产与研究环境必须使用 PostgreSQL**。
"""

from database.seeds.authors import (
    AUTHOR_SEEDS,
    CSV_COLUMNS,
    AuthorImportReport,
    AuthorSeed,
    import_author_seeds,
    import_authors_from_csv,
    load_author_seeds_from_csv,
)
from database.seeds.instruments import INSTRUMENT_SEEDS, REQUIRED_PHASE1_SYMBOLS, InstrumentSeed
from database.seeds.runner import (
    SeedResult,
    seed_all,
    seed_authors,
    seed_database,
    seed_instruments,
    seed_sources,
)
from database.seeds.sources import SOURCE_SEEDS, SourceSeed

__all__ = [
    "AUTHOR_SEEDS",
    "CSV_COLUMNS",
    "INSTRUMENT_SEEDS",
    "REQUIRED_PHASE1_SYMBOLS",
    "SOURCE_SEEDS",
    "AuthorImportReport",
    "AuthorSeed",
    "InstrumentSeed",
    "SeedResult",
    "SourceSeed",
    "import_author_seeds",
    "import_authors_from_csv",
    "load_author_seeds_from_csv",
    "seed_all",
    "seed_authors",
    "seed_database",
    "seed_instruments",
    "seed_sources",
]
