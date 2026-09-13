"""Mock 博文生成器单元测试（纯逻辑 + 本地文件；不依赖数据库、不联网）。

覆盖团队要求：
1. **结构完整**：必备列 `id` / `source` / `content`（+ 规范列 + `POST_COLUMNS` 全集）；
2. **可正则解析**：每条文本都含"方向词"与"目标/止损"（中文/英文混排）；
3. **时间对齐**：`effective_at = max(published_at, collected_at)`，且时间窗口完全在过去（防泄漏）；
4. **分层可用**：长度三档 / 含媒体 / 来源上限 / 交易日覆盖，
   足以支撑 200 条抽样（`--require-full`）；
5. **红线**：每行 `is_mock=true`，且**不含任何标注结论列**（严禁伪造标注）。
"""

from __future__ import annotations

import csv
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts.generate_mock_posts import (
    _DIRECTION_WORDS,
    _NON_DIRECTIONAL_EDGE,
    DEFAULT_POST_COUNT,
    MOCK_ACCOUNTS,
    MOCK_BATCH,
    POST_COLUMNS,
    MockPost,
    generate_mock_posts,
    main,
    write_posts_csv,
)
from scripts.sample_annotation_set import load_candidates_from_csv, sample_candidates
from src.common.time import utc_now

pytestmark = pytest.mark.unit

#: 合成数据**绝对不能**出现的列（这些是"标注结论"，只能由人工填写）
JUDGEMENT_COLUMNS = (
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
    "annotated_by",
    "annotated_at",
)
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SOURCE_NAMES = {source for _author, source in MOCK_ACCOUNTS}


@pytest.fixture(scope="module")
def posts() -> list[MockPost]:
    """默认批次（250 条、固定种子）——模块内复用，避免重复生成。"""
    return generate_mock_posts()


# ---------------------------------------------------------------------------
# 1) 结构完整
# ---------------------------------------------------------------------------
def test_default_batch_size_and_unique_ids(posts: list[MockPost]) -> None:
    assert len(posts) == DEFAULT_POST_COUNT == 250
    ids = [post.post_id for post in posts]
    assert len(set(ids)) == 250
    assert ids[0] == "mock-post-0001"


def test_rows_cover_every_declared_column(posts: list[MockPost]) -> None:
    for post in posts:
        row = post.to_row()
        assert tuple(row) == POST_COLUMNS
        assert row["id"] and row["source"] and row["content"]
        assert row["effective_at"] and row["collected_at"]
        assert row["content_hash"]


def test_alias_columns_match_canonical_columns(posts: list[MockPost]) -> None:
    """`id`/`source`/`content` 是规范列的别名，内容必须完全一致（避免两处数据打架）。"""
    for post in posts:
        row = post.to_row()
        assert row["id"] == row["post_id"]
        assert row["source"] == row["source_name"]
        assert row["content"] == row["text_content"]


def test_every_row_is_marked_as_mock(posts: list[MockPost]) -> None:
    for post in posts:
        row = post.to_row()
        assert row["is_mock"] == "true"
        assert row["mock_batch"] == MOCK_BATCH
        assert post.url.startswith("https://example.invalid/")  # 保留域名，绝不指向真实站点


def test_mock_columns_never_contain_judgements(posts: list[MockPost]) -> None:
    """红线：Mock 数据不得携带任何标注结论（否则等于伪造标注）。"""
    fields = set(POST_COLUMNS)
    assert fields & set(JUDGEMENT_COLUMNS) == frozenset()


def test_sources_are_registered_news_sources(posts: list[MockPost]) -> None:
    """来源必须是 `database/seeds/sources.py` 里的已注册来源，且**不含微博**（团队裁决）。"""
    for post in posts:
        assert post.source_name in _SOURCE_NAMES
    assert "weibo_main" not in {post.source_name for post in posts}


# ---------------------------------------------------------------------------
# 2) 文本可解析（方向 + 目标 + 止损）
# ---------------------------------------------------------------------------
def test_every_text_has_stance_and_price_levels(posts: list[MockPost]) -> None:
    """有方向的样本（普通样本 + 有方向对抗样本）必须"方向 + 目标 + 止损"齐全。"""
    for post in posts:
        if post.edge_case in _NON_DIRECTIONAL_EDGE:
            continue
        text = post.text_content
        assert any(word in text for word in _DIRECTION_WORDS) or any(
            word in text for word in ("做多", "做空", "观望", "long", "short", "flat")
        ), text
        assert any(word in text for word in ("目标", "止盈", "target")), text
        assert any(word in text for word in ("止损", "stop")), text
        assert any("\u4e00" <= char <= "\u9fff" for char in text), text  # 含中文


def test_non_directional_samples_have_no_direction_words(posts: list[MockPost]) -> None:
    """无方向对抗样本（纯信息播报 / 反问反讽 / 图表无文字）必须"干净"——期望标注就是 UNKNOWN。"""
    non_directional = [post for post in posts if post.edge_case in _NON_DIRECTIONAL_EDGE]

    assert len(non_directional) >= 30
    for post in non_directional:
        assert not any(word in post.text_content for word in _DIRECTION_WORDS), post.text_content
    assert all(
        post.has_media for post in posts if post.edge_case == "image_only"
    )  # 图表无文字样本必须带图


def test_texts_are_chinese_english_mixed(posts: list[MockPost]) -> None:
    languages = Counter(post.language for post in posts)
    assert languages["zh-en"] >= 50  # 中英混排样本充足
    assert languages["zh"] >= 1  # 也有纯中文样本
    assert set(languages) <= {"zh", "zh-en", "en"}


# ---------------------------------------------------------------------------
# 3) 时间语义（最高优先级：防未来数据泄漏）
# ---------------------------------------------------------------------------
def test_effective_at_equals_max_of_published_and_collected(posts: list[MockPost]) -> None:
    for post in posts:
        expected = (
            post.collected_at
            if post.published_at is None
            else max(post.published_at, post.collected_at)
        )
        assert post.effective_at == expected
        assert post.effective_at >= post.collected_at
        assert post.effective_at.tzinfo is not None


def test_all_timestamps_are_in_the_past(posts: list[MockPost]) -> None:
    now = utc_now()
    for post in posts:
        assert post.effective_at < now
        assert post.collected_at < now
        if post.published_at is not None:
            assert post.published_at < now


def test_time_edge_cases_are_present(posts: list[MockPost]) -> None:
    """演练 `docs/10 §3` 两种边界：缺发布时间、采集早于发布。"""
    missing_published = [post for post in posts if post.published_at is None]
    clock_skew = [
        post
        for post in posts
        if post.published_at is not None and post.published_at > post.collected_at
    ]

    assert len(missing_published) == 3
    assert all(post.time_precision == "collected_only" for post in missing_published)
    assert all(post.effective_at == post.collected_at for post in missing_published)
    assert len(clock_skew) == 2
    assert all(post.effective_at == post.published_at for post in clock_skew)
    assert all(
        post.time_precision == "published"
        for post in posts
        if post.published_at is not None
    )


def test_original_rows_land_on_weekdays_and_cover_many_days(posts: list[MockPost]) -> None:
    originals = [
        post for post in posts if not post.repost_of and post.published_at is not None
    ]
    assert len(originals) == 242  # 250 条 - 5 条转载 - 3 条刻意缺发布时间
    for post in originals:
        assert post.published_at is not None  # 上方过滤，供类型收窄
        assert post.published_at.astimezone(_SHANGHAI).weekday() < 5
    days = {post.effective_at.date() for post in posts}
    assert len(days) >= 5  # docs/10 §2.2 建议 ≥5 个交易日


# ---------------------------------------------------------------------------
# 4) 分层覆盖（要支撑 200 条抽样）
# ---------------------------------------------------------------------------
def test_length_buckets_cover_three_tiers(posts: list[MockPost]) -> None:
    buckets = Counter(post.length_bucket for post in posts)
    # 200 条抽样时三档目标为 67 / 67 / 66，因此每档可用量必须 ≥ 目标
    assert buckets["short"] >= 67
    assert buckets["medium"] >= 67
    assert buckets["long"] >= 66
    for post in posts:
        assert post.length_bucket == post.to_row()["length_bucket"]
        length = len(post.text_content)
        if post.length_bucket == "short":
            assert 1 <= length <= 50
        elif post.length_bucket == "medium":
            assert 51 <= length <= 200
        else:
            assert length >= 201


def test_media_quota_and_source_spread(posts: list[MockPost]) -> None:
    media = [post for post in posts if post.has_media]
    assert len(media) >= 20  # docs/10 §2.2 含媒体 ≥20 条
    assert all(post.media_type for post in media)
    assert all(post.media_type == "" for post in posts if not post.has_media)

    source_counts = Counter(post.source_name for post in posts)
    assert len(source_counts) == len(MOCK_ACCOUNTS) == 4
    assert max(source_counts.values()) / len(posts) < 0.4  # 单一来源 ≤40%


def test_reposts_duplicate_earlier_rows(posts: list[MockPost]) -> None:
    """末尾 5 条是"同文转载"：内容与原文一致、时间更晚、指向原文 id（用于演练去重）。"""
    reposts = [post for post in posts if post.repost_of]
    by_id = {post.post_id: post for post in posts}

    assert len(reposts) == 5
    for post in reposts:
        original = by_id[post.repost_of]
        assert post.text_content == original.text_content
        assert post.content_hash == original.content_hash
        assert post.source_name != original.source_name  # 跨来源传播
        assert post.effective_at > original.effective_at
        assert post.published_at is not None and original.published_at is not None
    assert sum(1 for post in posts if not post.repost_of) == len(posts) - 5


def test_edge_samples_cover_documented_rules(posts: list[MockPost]) -> None:
    """对抗样本覆盖 `docs/10 §2.2` 的对抗样本表 + §4.1/§4.4 规则，且每类 ≥13 条。"""
    edge = Counter(post.edge_case for post in posts if post.edge_case)

    assert set(edge) == {
        "conditional",
        "quote_only",
        "post_hoc",
        "double_negative",
        "inconsistent_price",
        "macro_only",
        "rhetorical",
        "image_only",
    }
    assert min(edge.values()) >= 13  # 200 条抽样后每类仍能 ≥10 条
    conditional = next(post for post in posts if post.edge_case == "conditional")
    assert "如果" in conditional.text_content
    inconsistent = next(post for post in posts if post.edge_case == "inconsistent_price")
    assert "做多" in inconsistent.text_content
    macro_only = next(post for post in posts if post.edge_case == "macro_only")
    assert "CPI" in macro_only.text_content  # 期望标注：UNKNOWN + information_type=MACRO


# ---------------------------------------------------------------------------
# 5) 写盘 + 与抽样脚本联通（把 250 条喂给 --limit 200 的分层约束）
# ---------------------------------------------------------------------------
def test_write_posts_csv_creates_parent_directory(tmp_path: Path) -> None:
    out = tmp_path / "logs" / "posts.csv"

    rows = write_posts_csv(out, generate_mock_posts(count=12))

    assert rows == 12
    assert out.exists()
    assert out.read_bytes().startswith(b"\xef\xbb\xbf")  # utf-8-sig：Excel 双击不乱码
    with out.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == list(POST_COLUMNS)
        data = list(reader)
    assert len(data) == 12
    assert all(row["is_mock"] == "true" for row in data)


def test_small_batch_skips_reposts_and_stays_consistent() -> None:
    """条数太小时不生成转载样本（否则"原文"不存在），且结构仍然自洽。"""
    small = generate_mock_posts(count=12)

    assert len(small) == 12
    assert all(post.repost_of == "" for post in small)
    assert all(post.length_bucket for post in small)


def test_written_csv_round_trips_into_sampler(tmp_path: Path) -> None:
    out = tmp_path / "posts.csv"
    write_posts_csv(out, generate_mock_posts())

    candidates, problems = load_candidates_from_csv(out)

    assert problems == []
    assert len(candidates) == 250
    assert all(candidate.effective_at.tzinfo is not None for candidate in candidates)


def test_generated_batch_supports_full_200_sample(tmp_path: Path) -> None:
    """把生成的 250 条喂给真正的抽样逻辑：必须凑满 200 条且守住全部分层约束。"""
    out = tmp_path / "posts.csv"
    write_posts_csv(out, generate_mock_posts())
    candidates, _problems = load_candidates_from_csv(out)

    report = sample_candidates(candidates, limit=200)

    assert len(report.selected) == 200
    assert report.source_cap_respected
    assert report.media_quota_met
    assert report.media_selected >= 20
    assert report.distinct_days >= 5
    assert report.notes == []  # 没有任何"放宽约束"的告警
    assert report.bucket_counts == report.bucket_targets == {"short": 67, "medium": 67, "long": 66}
    assert report.candidates_total - report.duplicates_removed == 245  # 5 条转载被去掉


def test_sampled_200_keeps_every_adversarial_class_above_10(tmp_path: Path) -> None:
    """`docs/10 §2.2`：对抗样本每类 ≥10 条——**抽样之后**也必须满足（抽取器分数才有意义）。"""
    posts = generate_mock_posts()
    out = tmp_path / "posts.csv"
    write_posts_csv(out, posts)
    candidates, _problems = load_candidates_from_csv(out)
    edge_by_post = {post.post_id: post.edge_case for post in posts}

    report = sample_candidates(candidates, limit=200)

    selected_edge = Counter(
        edge_by_post[candidate.post_id]
        for candidate in report.selected
        if edge_by_post.get(candidate.post_id)
    )
    assert len(report.selected) == 200
    assert set(selected_edge) == {
        "conditional",
        "quote_only",
        "post_hoc",
        "double_negative",
        "inconsistent_price",
        "macro_only",
        "rhetorical",
        "image_only",
    }
    assert min(selected_edge.values()) >= 10


# ---------------------------------------------------------------------------
# 6) 确定性与参数校验
# ---------------------------------------------------------------------------
def test_generation_is_deterministic_for_same_seed() -> None:
    first = generate_mock_posts(count=40, seed=20260912)
    second = generate_mock_posts(count=40, seed=20260912)

    assert [post.to_row() for post in first] == [post.to_row() for post in second]


def test_different_seed_changes_long_text_filler_order() -> None:
    first = generate_mock_posts(count=40, seed=1)
    second = generate_mock_posts(count=40, seed=2)

    assert [post.text_content for post in first] != [post.text_content for post in second]
    assert [post.post_id for post in first] == [post.post_id for post in second]  # id 与种子无关


def test_generation_rejects_non_positive_count() -> None:
    with pytest.raises(ValueError, match="count"):
        generate_mock_posts(count=0)


def test_generation_rejects_future_window() -> None:
    """窗口落在未来必须直接报错（防止把"未来数据"混进演练集）。"""
    with pytest.raises(ValueError, match="过去"):
        generate_mock_posts(count=5, window_start=date(2999, 1, 1))


# ---------------------------------------------------------------------------
# 7) CLI
# ---------------------------------------------------------------------------
def test_cli_writes_mock_batch(tmp_path: Path, capsys) -> None:
    out = tmp_path / "logs" / "posts.csv"

    exit_code = main(["--out", str(out), "--count", "30"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert out.exists()
    assert "合成演练数据" in captured.out
    assert MOCK_BATCH in captured.out
    assert "sample_annotation_set" in captured.out  # 给出下一步命令
    with out.open("r", encoding="utf-8-sig", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 30


def test_cli_refuses_to_overwrite_without_force(tmp_path: Path, capsys) -> None:
    """默认拒绝覆盖：`logs/posts.csv` 可能已被真实导出数据占用。"""
    out = tmp_path / "posts.csv"
    out.write_text("keep me\n", encoding="utf-8")

    exit_code = main(["--out", str(out), "--count", "10"])

    assert exit_code == 4
    assert out.read_text(encoding="utf-8") == "keep me\n"
    assert "拒绝覆盖" in capsys.readouterr().err


def test_cli_force_overwrites_existing_file(tmp_path: Path) -> None:
    out = tmp_path / "posts.csv"
    out.write_text("old\n", encoding="utf-8")

    exit_code = main(["--out", str(out), "--count", "10", "--force"])

    assert exit_code == 0
    with out.open("r", encoding="utf-8-sig", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 10


def test_default_output_path_follows_documented_location() -> None:
    """`docs/10 §2.2` 约定的演练输入路径就是 `logs/posts.csv`。"""
    from scripts.generate_mock_posts import DEFAULT_POSTS_CSV

    assert DEFAULT_POSTS_CSV.name == "posts.csv"
    assert DEFAULT_POSTS_CSV.parent.name == "logs"


def test_extra_columns_do_not_break_sampler(tmp_path: Path) -> None:
    """Mock CSV 里的额外列（is_mock / edge_case / …）必须被抽样脚本安全忽略。"""
    out = tmp_path / "posts.csv"
    write_posts_csv(out, generate_mock_posts(count=6))

    candidates, problems = load_candidates_from_csv(out)

    assert problems == []
    assert [candidate.source_name for candidate in candidates] == [
        post.source_name for post in generate_mock_posts(count=6)
    ]


def test_default_window_is_recent_past(posts: list[MockPost]) -> None:
    """默认窗口应落在"最近"的历史区间（人工标注时可对照真实行情）。"""
    newest = max(post.effective_at for post in posts)
    oldest = min(post.effective_at for post in posts)

    assert newest <= utc_now()
    assert (newest - oldest) <= timedelta(days=21)


# ---------------------------------------------------------------------------
# 8) 控制台编码（Windows GBK 控制台不能强切 UTF-8，否则中文乱码）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("encoding", "expected"),
    [
        ("gbk", False),  # 中文控制台：可直接输出中文，不要切
        ("cp936", False),
        ("utf-8", False),
        ("utf8", False),
        ("ascii", True),  # 管道/CI：必须切，否则 UnicodeEncodeError
        ("cp1252", True),
        ("latin-1", True),
        (None, True),  # 拿不到编码时保守切换
        ("not-a-real-codec", True),
    ],
)
def test_stdout_needs_utf8_detection(encoding: str | None, expected: bool) -> None:
    from scripts._console import stdout_needs_utf8

    assert stdout_needs_utf8(encoding) is expected


def test_configure_stdout_is_safe_under_pytest_capture(capsys) -> None:
    """`configure_stdout()` 必须在 pytest 捕获对象上安全（无 reconfigure 也不能炸）。"""
    from scripts.generate_mock_posts import configure_stdout

    configure_stdout()
    print("黄金 做多，目标 2450，止损 2430")

    assert "做多" in capsys.readouterr().out
