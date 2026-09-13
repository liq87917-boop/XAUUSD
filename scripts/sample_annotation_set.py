"""Phase 2 标注抽样脚本（生成待人工标注的 CSV，**绝不代替人工标注**）。

对应文档：
- `docs/10_标注规范.md`（已批准 v1.0）§2 抽样方法、§3 时间语义、附录 A 标注 CSV 列定义
- `docs/08` §5 Phase 2 验收：「抽样至少 200 条观点人工检查」

做什么：
1. 从 `author_posts`（作者帖子）读取候选样本（或从 `--input-csv` 读本地 CSV，用于
   作者库尚未导入时的演练——团队裁决：作者来源先用「机构/分析师 RSS + 本地 CSV」）；
2. **去重**（内容哈希 + 文本规范化，保留最早可用的一条）；
3. **分层抽样**：文本长度三档各约 1/3、单一来源占比 ≤40%、含媒体样本 ≥20 条、
   尽量覆盖 ≥5 个交易日；随机种子写入元数据，**同种子结果可复现**；
4. 导出标注 CSV：只预填"客观"列（原文、来源、时间、哈希等），
   **所有判定列（stance/horizon/confidence/点位/信息类型/rationale/notes…）一律留空**。

用法::

    # 1) 从数据库抽样（默认 DATABASE_URL，输出 logs/annotation_sample.csv）
    python -m scripts.sample_annotation_set --limit 200 --seed 20260912
    # 2) 无作者库时先用本地 CSV 演练（文件不存在会自动生成 Mock 演练数据）
    python -m scripts.sample_annotation_set --input-csv logs/posts.csv --limit 200 --require-full
    # 3) 严格模式：样本不足 200 条即失败（退出码 3），避免"悄悄少抽"
    python -m scripts.sample_annotation_set --require-full

说明：
- 本脚本**不生成任何标注内容**（团队裁决：标注必须由人工完成）；
- `--input-csv` 的列名支持别名（`id` / `source` / `content` / `published`…），
  最小可用输入 = `id,source,content,published_at,effective_at`；
- **输入 CSV 不存在时**（未加 `--no-mock-fill`）：自动用 `scripts/generate_mock_posts.py`
  生成 `--mock-fill` 条（默认 250）Mock 演练数据（每行 `is_mock=true`，批次写入元数据），
  并在 stderr 与控制台给出醒目警告——这些数据**只能用来跑通流程**，不得用于研究结论；
- 时间字段全部转 UTC 并保留 ISO8601；缺 `published_at` 的行标 `time_precision=collected_only`；
- CSV 模式缺少 UUID 时用 `uuid5(内容哈希)` 生成**确定性** id，保证多次运行可对齐。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 兼容两种运行方式（见 `scripts/__init__.py` 的说明）：
#   1) 脚本模式 `python scripts/sample_annotation_set.py` —— 需要仓库根在 sys.path 上，
#      否则 `from scripts import generate_mock_posts` 会 ModuleNotFoundError；
#   2) 包模式 `from scripts.sample_annotation_set import ...`（测试 / mypy）
#      —— 已是包内导入，无需处理。
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - 已安装（editable）时通常已包含
    sys.path.insert(0, str(REPO_ROOT))

# ruff: noqa: E402 —— 上面的 sys.path 引导必须先于仓库内模块的导入执行
import argparse
import csv
import json
import math
import random
import uuid
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import sqlalchemy as sa

from database.models import Author, AuthorAccount, AuthorPost, RawItem, Source
from scripts import generate_mock_posts as mock_posts
from src.common.hashing import content_hash, normalize_for_hash
from src.common.time import parse_iso8601, utc_now

DEFAULT_OUTPUT = REPO_ROOT / "logs" / "annotation_sample.csv"
DEFAULT_SPEC_VERSION = "spec-v1.0"
DEFAULT_SEED = 20260912
DEFAULT_LIMIT = 200
#: 单一来源占比上限（docs/10 §2.2）
DEFAULT_SOURCE_CAP_RATIO = 0.4
#: 含媒体样本最少条数（docs/10 §2.2，用于后续 Level-1 OCR 阶段）
DEFAULT_MEDIA_QUOTA = 20
#: 文本长度分档边界（docs/10 §2.2）
LENGTH_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("short", 1, 50),
    ("medium", 51, 200),
    ("long", 201, 1 << 30),
)
#: 标注 CSV 列（docs/10 附录 A；`author_name` / `spec_version` 为便于人工标注的补充列）
CSV_COLUMNS: tuple[str, ...] = (
    "annotation_id",
    "post_id",
    "raw_item_id",
    "author_id",
    "author_name",
    "source_name",
    "published_at",
    "effective_at",
    "text_content",
    "has_media",
    "no_opinion",
    "opinion_index",
    "stance",
    "instrument",
    "horizon",
    "confidence",
    "entry_low",
    "entry_high",
    "stop_loss",
    "take_profit",
    "information_type",
    "rationale",
    "leakage_suspect",
    "time_precision",
    "annotation_version",
    "annotated_by",
    "annotated_at",
    "notes",
    "spec_version",
)
#: 由脚本预填的列（其余列留给人工标注，**一律留空**）
PREFILLED_COLUMNS: frozenset[str] = frozenset(
    {
        "annotation_id",
        "post_id",
        "raw_item_id",
        "author_id",
        "author_name",
        "source_name",
        "published_at",
        "effective_at",
        "text_content",
        "has_media",
        "opinion_index",
        "time_precision",
        "spec_version",
    }
)
#: UUID5 命名空间（CSV 模式生成确定性 id 用）
_ID_NAMESPACE = uuid.UUID("6f1d3e4a-0a6b-4f0d-9d1e-6b7f3c2a8c11")


@dataclass(frozen=True, slots=True)
class Candidate:
    """一条待抽样候选（`author_posts` 的一行 + 归属与来源信息）。"""

    post_id: str
    raw_item_id: str
    author_id: str
    author_name: str
    source_name: str
    published_at: datetime | None
    effective_at: datetime
    text_content: str
    has_media: bool
    content_hash: str

    @property
    def length_bucket(self) -> str:
        length = len(self.text_content)
        for name, low, high in LENGTH_BUCKETS:
            if low <= length <= high:
                return name
        return "long"  # pragma: no cover - 分档覆盖全部长度

    @property
    def trading_day(self) -> str:
        return self.effective_at.date().isoformat()

    @property
    def time_precision(self) -> str:
        return "published" if self.published_at is not None else "collected_only"


@dataclass(slots=True)
class SamplingReport:
    """抽样结果与达成情况（写入元数据 JSON，供周一复核）。"""

    selected: list[Candidate] = field(default_factory=list)
    candidates_total: int = 0
    duplicates_removed: int = 0
    empty_text_removed: int = 0
    source_counts: dict[str, int] = field(default_factory=dict)
    bucket_counts: dict[str, int] = field(default_factory=dict)
    media_selected: int = 0
    distinct_days: int = 0
    source_cap: int = 0
    source_cap_respected: bool = False
    bucket_targets: dict[str, int] = field(default_factory=dict)
    media_quota_met: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def selected_count(self) -> int:
        return len(self.selected)

    def to_metadata(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("selected")
        return payload


# ---------------------------------------------------------------------------
# 读取候选样本
# ---------------------------------------------------------------------------
def _as_utc(value: object) -> datetime | None:
    """把数据库返回的时间统一成 UTC datetime。

    为什么需要容错：`sa.text()` 查询在 SQLite 下返回**字符串**（PG 由驱动自动转 datetime），
    因此本函数同时接受 ``datetime``（naive 视为 UTC，因为 SQLite 不保存偏移）与 ISO8601 字符串，
    避免"只在 PG 上能跑"的隐性方言耦合（曾真实发生，由集成测试锁定）。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, str):
        return parse_iso8601(value)
    raise TypeError(f"无法解析时间字段：{type(value).__name__}")


def _deterministic_id(seed_text: str) -> str:
    """由内容派生确定性 UUID（CSV 模式缺 id 时使用，保证多次运行可对齐）。"""
    return str(uuid.uuid5(_ID_NAMESPACE, seed_text))


def load_candidates_from_db(engine: sa.Engine) -> list[Candidate]:
    """从 `author_posts` 读取候选样本（JOIN 归属 / 作者 / 来源 / 原始哈希）。

    使用 ORM 类型化 select（而非 `sa.text()`）：SQLite 与 PostgreSQL 都能返回
    真正的 datetime / UUID / bool，避免方言差异（`sa.text()` 在 SQLite 返回字符串）。
    """
    statement = (
        sa.select(
            AuthorPost.id,
            AuthorPost.raw_item_id,
            AuthorPost.author_id,
            Author.display_name,
            Source.name,
            AuthorPost.published_at,
            AuthorPost.effective_at,
            AuthorPost.text_content,
            AuthorPost.has_media,
            RawItem.content_hash,
        )
        .join(RawItem, RawItem.id == AuthorPost.raw_item_id)
        .join(Author, Author.id == AuthorPost.author_id)
        .join(AuthorAccount, AuthorAccount.id == AuthorPost.author_account_id)
        .join(Source, Source.id == AuthorAccount.source_id)
        .order_by(AuthorPost.effective_at, AuthorPost.id)
    )
    with engine.connect() as connection:
        rows = connection.execute(statement).all()

    candidates: list[Candidate] = []
    for row in rows:
        effective_at = _as_utc(row[6])
        if effective_at is None:  # pragma: no cover - 表约束保证非空
            continue
        candidates.append(
            Candidate(
                post_id=str(row[0]),
                raw_item_id=str(row[1]),
                author_id=str(row[2]),
                author_name=str(row[3] or ""),
                source_name=str(row[4] or ""),
                published_at=_as_utc(row[5]),
                effective_at=effective_at,
                text_content=str(row[7] or ""),
                has_media=bool(row[8]),
                content_hash=str(row[9] or ""),
            )
        )
    return candidates


#: 输入 CSV 的列别名（大小写不敏感）→ 规范列名。
#: 最小可用输入只需 `id` + `source` + `content`（外加 `published_at` / `effective_at`），
#: 这样人工手搓的小 CSV（或 `scripts/generate_mock_posts.py` 的 Mock CSV）都能直接读。
_COLUMN_ALIASES: Final[dict[str, str]] = {
    "id": "post_id",
    "post_id": "post_id",
    "postid": "post_id",
    "raw_item_id": "raw_item_id",
    "raw_id": "raw_item_id",
    "author_id": "author_id",
    "author": "author_name",
    "author_name": "author_name",
    "source": "source_name",
    "source_name": "source_name",
    "content": "text_content",
    "text": "text_content",
    "text_content": "text_content",
    "published_at": "published_at",
    "published": "published_at",
    "publish_time": "published_at",
    "collected_at": "collected_at",
    "crawl_time": "collected_at",
    "effective_at": "effective_at",
    "available_at": "effective_at",
    "media": "has_media",
    "has_media": "has_media",
    "hash": "content_hash",
    "content_hash": "content_hash",
}


def _normalize_row(row: Mapping[str, str]) -> dict[str, str]:
    """把一行输入按 `_COLUMN_ALIASES` 映射为规范列名（**规范列优先于别名**）。

    两遍扫描：先取规范列（`text_content` / `post_id`…），再用别名补空缺。
    这样"规范列与别名同现"时以规范列为准（与 `docs/10` 的列定义一致）。
    """
    normalized: dict[str, str] = {}
    for key, value in row.items():
        if key is None:
            continue
        name = key.strip().lower()
        canonical = _COLUMN_ALIASES.get(name)
        if canonical is not None and canonical == name:
            normalized[canonical] = value if value is not None else ""
    for key, value in row.items():
        if key is None:
            continue
        canonical = _COLUMN_ALIASES.get(key.strip().lower())
        if canonical is not None and canonical not in normalized:
            normalized[canonical] = value if value is not None else ""
    return normalized


#: 输入来源体检最多扫描的行数（超大 CSV 不至于拖慢抽样）
MAX_PROVENANCE_SCAN: Final[int] = 20_000
#: `is_mock` 列的真值写法
_TRUTHY_VALUES: Final[frozenset[str]] = frozenset({"1", "true", "yes", "y", "on"})


@dataclass(frozen=True, slots=True)
class InputProvenance:
    """输入 CSV 的来源体检结果（合成数据必须被识别出来，见 `.clinerules` 红线）。"""

    rows_scanned: int = 0
    mock_rows: int = 0
    mock_batches: tuple[str, ...] = ()

    @property
    def is_mock(self) -> bool:
        return self.mock_rows > 0

    @property
    def mock_batch(self) -> str:
        return " / ".join(self.mock_batches)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "input_rows_scanned": self.rows_scanned,
            "input_is_mock": self.is_mock,
            "input_mock_rows": self.mock_rows,
            "input_mock_batch": self.mock_batch or None,
        }


def detect_mock_provenance(path: Path) -> InputProvenance:
    """扫描输入 CSV 的 `is_mock` / `mock_batch` 列，判断这批数据是否为合成演练数据。

    为什么要做这件事：合成数据一旦被静默当成真实数据进入**人工标注与验收**，
    就是科研诚信事故（`.clinerules` 红线 + `docs/10 §7`）。因此每批 `--input-csv`
    都要体检，并把结论写进抽样元数据（`annotation_sample.meta.json`）。
    """
    if not path.exists():
        raise FileNotFoundError(f"输入 CSV 不存在：{path}")

    scanned = 0
    mock_rows = 0
    batches: list[str] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        field_names = {(field or "").strip().lower() for field in (reader.fieldnames or ())}
        if not field_names & {"is_mock", "mock_batch"}:
            return InputProvenance()
        for row in reader:
            if scanned >= MAX_PROVENANCE_SCAN:
                break
            scanned += 1
            flag = ""
            batch = ""
            for key, value in row.items():
                if key is None:
                    continue
                name = key.strip().lower()
                if name == "is_mock":
                    flag = (value or "").strip().lower()
                elif name == "mock_batch":
                    batch = (value or "").strip()
            if flag in _TRUTHY_VALUES or batch:
                mock_rows += 1
            if batch and batch not in batches:
                batches.append(batch)
    return InputProvenance(
        rows_scanned=scanned, mock_rows=mock_rows, mock_batches=tuple(batches)
    )


def load_candidates_from_csv(path: Path) -> tuple[list[Candidate], list[str]]:
    """从本地 CSV 读取候选样本（作者库尚未导入时的演练路径）。

    列名支持别名（`id` / `source` / `content` / `published`…），详见 `_COLUMN_ALIASES`；
    唯一硬要求是"必须有正文列"（`text_content` / `content` / `text` 任一）。

    Returns:
        ``(candidates, problems)``；无法解析的行进入 ``problems``（不静默丢弃）。
    """
    if not path.exists():
        raise FileNotFoundError(f"输入 CSV 不存在：{path}")

    candidates: list[Candidate] = []
    problems: list[str] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or ())
        canonical_fields = {
            _COLUMN_ALIASES.get(field.strip().lower()) for field in fields if field is not None
        }
        if "text_content" not in canonical_fields:
            raise ValueError(
                "输入 CSV 必须包含 text_content 列（也接受 content / text），"
                f"实际列：{sorted(fields)}"
            )

        for index, row in enumerate(reader, start=2):
            data = _normalize_row(row)
            text = (data.get("text_content") or "").strip()
            if not text:
                problems.append(f"第 {index} 行：text_content 为空，已跳过")
                continue
            published_raw = (data.get("published_at") or "").strip()
            effective_raw = (data.get("effective_at") or "").strip()
            collected_raw = (data.get("collected_at") or "").strip()
            try:
                published_at = parse_iso8601(published_raw) if published_raw else None
                effective_at = (
                    parse_iso8601(effective_raw)
                    if effective_raw
                    else (published_at or parse_iso8601(collected_raw))
                )
            except Exception as exc:  # noqa: BLE001 - 逐行容错，问题写入 problems
                problems.append(f"第 {index} 行：时间无法解析（{exc}），已跳过")
                continue

            digest = (data.get("content_hash") or "").strip() or content_hash(
                normalize_for_hash(text)
            )
            seed_text = f"{published_raw}|{effective_raw}|{digest}"
            candidates.append(
                Candidate(
                    post_id=(data.get("post_id") or "").strip() or _deterministic_id(seed_text),
                    raw_item_id=(data.get("raw_item_id") or "").strip()
                    or _deterministic_id(f"raw|{seed_text}"),
                    author_id=(data.get("author_id") or "").strip()
                    or _deterministic_id(f"author|{(data.get('author_name') or '').strip()}"),
                    author_name=(data.get("author_name") or "").strip(),
                    source_name=(data.get("source_name") or "local-csv").strip(),
                    published_at=published_at,
                    effective_at=effective_at,
                    text_content=text,
                    has_media=(data.get("has_media") or "").strip().lower()
                    in {"1", "true", "yes", "y"},
                    content_hash=digest,
                )
            )
    return candidates, problems


# ---------------------------------------------------------------------------
# 去重
# ---------------------------------------------------------------------------
def deduplicate(candidates: list[Candidate]) -> tuple[list[Candidate], int, int]:
    """按内容哈希去重（保留最早可用的一条），并丢弃空文本。

    Returns:
        ``(kept, duplicates_removed, empty_text_removed)``
    """
    kept: dict[str, Candidate] = {}
    empty_removed = 0
    for candidate in candidates:
        if not candidate.text_content.strip():
            empty_removed += 1
            continue
        key = candidate.content_hash or content_hash(normalize_for_hash(candidate.text_content))
        existing = kept.get(key)
        if existing is None or candidate.effective_at < existing.effective_at:
            kept[key] = candidate

    removed = len(candidates) - empty_removed - len(kept)
    ordered = sorted(kept.values(), key=lambda item: (item.effective_at, item.post_id))
    return ordered, removed, empty_removed


# ---------------------------------------------------------------------------
# 分层抽样（随机种子 + 长度分档 + 来源上限 + 媒体配额 + 时间分散）
# ---------------------------------------------------------------------------
def _bucket_targets(pool: list[Candidate], limit: int) -> dict[str, int]:
    """长度分档目标：尽量三等分，余数给靠前的档位，再按可用量收敛（结果确定）。"""
    available = Counter(candidate.length_bucket for candidate in pool)
    names = [name for name, _low, _high in LENGTH_BUCKETS]
    base, remainder = divmod(limit, len(names))
    targets = {name: base for name in names}
    for name in names[:remainder]:
        targets[name] += 1
    return {name: min(targets[name], available.get(name, 0)) for name in names}


def _spread_by_day(pool: list[Candidate]) -> list[Candidate]:
    """按交易日轮转展开（提升时间覆盖）；天序固定 → 结果可复现。"""
    by_day: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in pool:
        by_day[candidate.trading_day].append(candidate)
    ordered_days = sorted(by_day)
    longest = max((len(items) for items in by_day.values()), default=0)
    spread: list[Candidate] = []
    for index in range(longest):
        for day in ordered_days:
            items = by_day[day]
            if index < len(items):
                spread.append(items[index])
    return spread


def sample_candidates(
    candidates: list[Candidate],
    *,
    limit: int = DEFAULT_LIMIT,
    seed: int = DEFAULT_SEED,
    source_cap_ratio: float = DEFAULT_SOURCE_CAP_RATIO,
    media_quota: int = DEFAULT_MEDIA_QUOTA,
) -> SamplingReport:
    """按 `docs/10 §2.2` 做分层抽样，返回带达成情况的报告。

    抽样顺序（三遍扫描，约束依次放宽，全部基于**同一个随机种子**）：
    1. 含媒体配额（保证后续 OCR 阶段有样本）；
    2. 严格：单一来源占比 ≤ `source_cap_ratio` 且长度分档不超配额；
    3. 放宽长度分档（仍守来源上限）；
    4. 最后才放宽来源上限（并在 `notes` 中明确记录，绝不静默降级）。
    """
    report = SamplingReport(
        candidates_total=len(candidates),
        source_cap=max(1, math.ceil(limit * source_cap_ratio) if limit else 1),
    )
    pool, duplicates_removed, empty_text_removed = deduplicate(candidates)
    report.duplicates_removed = duplicates_removed
    report.empty_text_removed = empty_text_removed
    report.bucket_targets = _bucket_targets(pool, limit)

    rng = random.Random(seed)
    rng.shuffle(pool)
    spread = _spread_by_day(pool)

    selected: list[Candidate] = []
    chosen: set[str] = set()
    source_counts: Counter[str] = Counter()
    bucket_counts: Counter[str] = Counter()
    media_selected = 0

    def take(candidate: Candidate) -> None:
        nonlocal media_selected
        selected.append(candidate)
        chosen.add(candidate.post_id)
        source_counts[candidate.source_name] += 1
        bucket_counts[candidate.length_bucket] += 1
        if candidate.has_media:
            media_selected += 1

    def allowed(candidate: Candidate, *, enforce_bucket: bool, enforce_source: bool) -> bool:
        if candidate.post_id in chosen:
            return False
        if enforce_source and source_counts[candidate.source_name] >= report.source_cap:
            return False
        at_bucket_limit = (
            bucket_counts[candidate.length_bucket]
            >= report.bucket_targets[candidate.length_bucket]
        )
        return not (enforce_bucket and at_bucket_limit)

    available_media = sum(1 for candidate in pool if candidate.has_media)
    if media_quota > 0:
        for candidate in spread:
            if media_selected >= media_quota or len(selected) >= limit:
                break
            if candidate.has_media and allowed(
                candidate, enforce_bucket=False, enforce_source=True
            ):
                take(candidate)

    for candidate in spread:
        if len(selected) >= limit:
            break
        if allowed(candidate, enforce_bucket=True, enforce_source=True):
            take(candidate)

    for candidate in spread:
        if len(selected) >= limit:
            break
        if allowed(candidate, enforce_bucket=False, enforce_source=True):
            take(candidate)

    if len(selected) < limit:
        for candidate in spread:
            if len(selected) >= limit:
                break
            if allowed(candidate, enforce_bucket=False, enforce_source=False):
                take(candidate)
        report.notes.append("样本池来源集中：为凑满 limit 已放宽单一来源占比上限")

    report.selected = selected
    report.source_counts = dict(sorted(source_counts.items()))
    report.bucket_counts = dict(sorted(bucket_counts.items()))
    report.media_selected = media_selected
    report.distinct_days = len({candidate.trading_day for candidate in selected})
    report.source_cap_respected = all(
        count <= report.source_cap for count in source_counts.values()
    )
    report.media_quota_met = media_selected >= min(media_quota, available_media)

    if len(selected) < limit:
        report.notes.append(
            f"候选样本不足：需要 {limit} 条，实际抽到 {len(selected)} 条"
            "（请扩充数据或先用 --input-csv 演练）"
        )
    if report.distinct_days < 5:
        report.notes.append(
            f"时间覆盖仅 {report.distinct_days} 个交易日（docs/10 §2.2 建议 ≥5）"
        )
    if not report.media_quota_met:
        report.notes.append(
            f"含媒体样本 {report.media_selected} 条，未达配额 {media_quota}"
            f"（可用 {available_media} 条）"
        )
    return report


# ---------------------------------------------------------------------------
# 导出（标注列一律留空，绝不代替人工标注）
# ---------------------------------------------------------------------------
def annotation_row(candidate: Candidate, *, spec_version: str) -> dict[str, str]:
    """把候选样本转成一行待标注 CSV（判定列全部为空字符串）。"""
    row: dict[str, str] = {column: "" for column in CSV_COLUMNS}
    row.update(
        {
            "annotation_id": _deterministic_id(f"annotation|{candidate.post_id}"),
            "post_id": candidate.post_id,
            "raw_item_id": candidate.raw_item_id,
            "author_id": candidate.author_id,
            "author_name": candidate.author_name,
            "source_name": candidate.source_name,
            "published_at": candidate.published_at.isoformat() if candidate.published_at else "",
            "effective_at": candidate.effective_at.isoformat(),
            "text_content": candidate.text_content,
            "has_media": "true" if candidate.has_media else "false",
            # 预置 1 行；同帖多观点由标注人另起一行（保持同一 post_id，序号自增）
            "opinion_index": "1",
            "time_precision": candidate.time_precision,
            "spec_version": spec_version,
        }
    )
    return row


def write_annotation_csv(
    path: Path, candidates: Sequence[Candidate], *, spec_version: str
) -> int:
    """写出标注 CSV，返回行数。列定义见 `docs/10` 附录 A。

    编码 ``utf-8-sig``（带 BOM）：人工标注大概率用 Excel 打开，
    无 BOM 的 UTF-8 在 Excel 里会显示乱码；脚本自身读取用 ``utf-8-sig``，两者都兼容。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        for candidate in candidates:
            writer.writerow(annotation_row(candidate, spec_version=spec_version))
    return len(candidates)


def write_metadata(path: Path, payload: Mapping[str, Any]) -> None:
    """写出元数据 JSON（种子、样本量、分层达成情况、问题清单）—— 周一复核依据。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _masked_url(url: str) -> str:
    try:
        return sa.engine.make_url(url).render_as_string(hide_password=True)
    except sa.exc.ArgumentError:  # pragma: no cover - 非法 URL 由连接阶段报错
        return "<无法解析的 DATABASE_URL>"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.sample_annotation_set",
        description="Phase 2 标注抽样（随机种子 + 分层 + 去重 → 待人工标注 CSV）",
    )
    parser.add_argument("--db-url", default=None, help="数据库 URL（默认取 DATABASE_URL）")
    parser.add_argument(
        "--input-csv",
        default=None,
        help=(
            "改为从本地 CSV 读取候选样本（列名支持 id/source/content 等别名）；"
            "文件不存在时自动生成 Mock 演练数据，见 --mock-fill"
        ),
    )
    parser.add_argument(
        "--mock-fill",
        type=int,
        default=mock_posts.DEFAULT_POST_COUNT,
        help=f"输入 CSV 不存在时自动生成的 Mock 条数（默认 {mock_posts.DEFAULT_POST_COUNT}）",
    )
    parser.add_argument(
        "--mock-seed",
        type=int,
        default=mock_posts.DEFAULT_SEED,
        help=f"Mock 生成种子（默认 {mock_posts.DEFAULT_SEED}）",
    )
    parser.add_argument(
        "--no-mock-fill",
        action="store_true",
        help="输入 CSV 不存在时直接报错（不做任何自动生成）",
    )
    parser.add_argument("--out", default=str(DEFAULT_OUTPUT), help="标注 CSV 输出路径")
    parser.add_argument(
        "--meta-out", default=None, help="元数据 JSON 输出路径（默认 <out>.meta.json）"
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="抽样条数（默认 200）")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="随机种子（默认 20260912）")
    parser.add_argument(
        "--media-quota",
        type=int,
        default=DEFAULT_MEDIA_QUOTA,
        help="含媒体样本最少条数（默认 20）",
    )
    parser.add_argument(
        "--spec-version",
        default=DEFAULT_SPEC_VERSION,
        help="标注规范版本（写入 CSV 的 spec_version 列）",
    )
    parser.add_argument(
        "--require-full",
        action="store_true",
        help="样本不足 limit 时以退出码 3 失败（避免静默少抽）",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """执行抽样并导出 CSV；退出码 0=成功 / 2=无候选 / 3=样本不足（--require-full）。

    输入缺失时：`--input-csv` 指向的文件不存在且未加 `--no-mock-fill`，
    则先用 `scripts/generate_mock_posts.py` 生成 `--mock-fill` 条 Mock 演练数据
    （每行 `is_mock=true`），并把这件事写进元数据与控制台警告——**只用于把流程跑通**。
    """
    args = _parse_args(argv)
    problems: list[str] = []
    mock_filled = False
    mock_rows = 0
    provenance = InputProvenance()

    if args.input_csv:
        input_path = Path(args.input_csv)
        if not input_path.exists() and not args.no_mock_fill:
            mock_rows = mock_posts.write_posts_csv(
                input_path,
                mock_posts.generate_mock_posts(count=args.mock_fill, seed=args.mock_seed),
            )
            mock_filled = True
            print(
                f"[mock][警告] 输入 CSV 不存在 → 已生成 {mock_rows} 条**合成 Mock 演练数据**"
                f"（批次 {mock_posts.MOCK_BATCH}）到 {input_path}",
                file=sys.stderr,
            )
            print(
                "[mock][警告] 这些**不是真实数据**：只用于跑通抽样与标注流程，"
                "禁止用于研究结论 / 模型训练；正式验收必须换成真实数据。",
                file=sys.stderr,
            )
        candidates, problems = load_candidates_from_csv(input_path)
        provenance = detect_mock_provenance(input_path)
        source = f"csv:{args.input_csv}"
    else:
        from database.session import build_engine

        engine = build_engine(args.db_url)
        try:
            candidates = load_candidates_from_db(engine)
            source = f"db:{_masked_url(args.db_url or 'DATABASE_URL')}"
        finally:
            engine.dispose()

    if not candidates:
        print("[sample] 未找到候选样本（author_posts 为空？）", file=sys.stderr)
        print(
            "[sample] 提示：作者库导入（TECH_DEBT TD-16）完成前，可用 --input-csv 演练。",
            file=sys.stderr,
        )
        return 2

    report = sample_candidates(
        candidates, limit=args.limit, seed=args.seed, media_quota=args.media_quota
    )
    output = Path(args.out)
    rows = write_annotation_csv(output, report.selected, spec_version=args.spec_version)
    meta_path = Path(args.meta_out) if args.meta_out else output.with_suffix(".meta.json")
    write_metadata(
        meta_path,
        {
            "generated_at": utc_now().isoformat(),
            "source": source,
            "seed": args.seed,
            "limit": args.limit,
            "spec_version": args.spec_version,
            "rows_written": rows,
            "annotation_columns_left_empty": sorted(set(CSV_COLUMNS) - PREFILLED_COLUMNS),
            "input_problems": problems,
            "input_csv": args.input_csv or None,
            "mock_filled": mock_filled,
            "mock_batch": mock_posts.MOCK_BATCH if mock_filled else None,
            "mock_rows_written": mock_rows,
            **provenance.to_metadata(),
            **report.to_metadata(),
        },
    )

    mock_posts.configure_stdout()
    print(f"[sample] 标注 CSV：{output}（{rows} 行）")
    print(f"[sample] 元数据：{meta_path}")
    deduped = report.candidates_total - report.duplicates_removed - report.empty_text_removed
    print(
        f"[sample] 候选 {report.candidates_total} 条 → 去重后 {deduped} 条"
        f"（重复 {report.duplicates_removed} / 空文本 {report.empty_text_removed}）"
    )
    print(f"[sample] 长度分档：{report.bucket_counts}（目标 {report.bucket_targets}）")
    print(f"[sample] 来源分布：{report.source_counts}（单一来源上限 {report.source_cap}）")
    print(f"[sample] 含媒体 {report.media_selected} 条；覆盖 {report.distinct_days} 个交易日")
    for note in report.notes:
        print(f"[sample][注意] {note}")
    if mock_filled:
        print(
            f"[sample][警告] 本次输入是**合成 Mock 演练数据**（批次 {mock_posts.MOCK_BATCH}，"
            f"{mock_rows} 条，每行 is_mock=true）：只用于跑通流程，不能作为研究或验收结论。"
        )
    elif provenance.is_mock:
        print(
            f"[sample][警告] 输入 CSV 自带合成标记（批次 {provenance.mock_batch}，"
            f"{provenance.mock_rows}/{provenance.rows_scanned} 行 is_mock=true）："
            "只用于跑通流程，不能作为研究或验收结论。"
        )

    if args.require_full and rows < args.limit:
        print(
            f"[sample] 失败：需要 {args.limit} 条，实际 {rows} 条（--require-full）",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover - 由 CLI 测试以 in-process 方式覆盖
    raise SystemExit(main())