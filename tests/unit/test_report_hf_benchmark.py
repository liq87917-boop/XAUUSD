"""《Phase 2 真实语料验收报告》生成器的单元测试（**全离线、零网络、零费用**）。

覆盖重点：
1. **Wilson 区间**：已知值、极端比例、空分母；
2. 读入层：`resolve_inputs` 派生路径、`read_eval_cells` 坏行报错、`read_texts`/`read_meta` 容错；
3. 统计层：`no_opinion_posts`、`unknown_stance`、`extra_info_cells`、`fixed_regressed`；
4. 案例层：`raw_direction_stance`、`suspicious_dataset_cases`（黄金情感/方向冲突、股吧线索冲突）；
5. 渲染层：报告必须含 N 与 95% CI、`not_evaluated` 不计假阳性、股吧单列且不进主结论；
6. CLI：缺失输入 → 退出码 2 且不写盘；`--dry-run` 只打印不写盘。
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.compare_model_annotations import file_digest
from scripts.evaluate_extractor import (
    EVAL_COLUMNS,
    OUTCOME_CORRECT_VALUE,
    OUTCOME_MISSED,
    OUTCOME_NOT_EVALUATED,
    OUTCOME_WRONG,
)
from scripts.report_hf_benchmark import (
    LAYER_GOLD,
    LAYER_GUBA,
    build_run,
    ci_text,
    extra_info_cells,
    fixed_regressed,
    label_for,
    main,
    matrix_lines,
    mock_reference_lines,
    no_opinion_posts,
    raw_direction_stance,
    read_eval_cells,
    read_meta,
    read_texts,
    render_report,
    repro_command,
    resolve_inputs,
    suspicious_dataset_cases,
    unknown_stance,
    wilson_interval,
)

pytestmark = pytest.mark.unit

REGEX_NAME = "mock-regex-v1"
LLM_NAME = "llm-deepseek-v13"


# ---------------------------------------------------------------------------
# 夹具：一份 4 条文本的"迷你 HF 基准"
# ---------------------------------------------------------------------------
def _eval_row(
    post_id: str,
    gold: str,
    pred: str,
    outcome: str,
    *,
    gold_source: str,
    field: str = "stance",
) -> dict[str, str]:
    return {
        "post_id": post_id,
        "field": field,
        "gold_value": gold,
        "gold_source": gold_source,
        "gold_raw": "raw",
        "pred_value": pred,
        "pred_raw": pred,
        "outcome": outcome,
        "note": "",
    }


def _write_eval(path: Path, rows: list[dict[str, str]]) -> Path:
    """写一份逐格明细 CSV（列与 `evaluate_extractor` 完全一致）。"""
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(EVAL_COLUMNS), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def _write_texts(path: Path) -> Path:
    """写文本 + 原始标签留档（4 条：黄金 2 + 股吧 2）。"""
    columns = [
        "post_id",
        "text_content",
        "has_media",
        "source",
        "raw_Price Direction Up",
        "raw_Price Direction Down",
        "raw_Price Sentiment",
        "raw_label",
    ]
    rows = [
        ["hf-gold-00000", "gold rises 1%", "false", LAYER_GOLD, "1", "0", "positive", ""],
        ["hf-gold-00001", "gold falls 1%", "false", LAYER_GOLD, "0", "1", "negative", ""],
        ["hf-guba-00000", "看多黄金，逢低做多", "false", LAYER_GUBA, "", "", "", "1"],
        ["hf-guba-00001", "震荡为主，等待方向", "false", LAYER_GUBA, "", "", "", "2"],
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(rows)
    return path


def _mini_benchmark(tmp_path: Path) -> Path:
    """造一份迷你基准（gold / texts / regex / llm / usage / meta 齐备）。"""
    prefix = tmp_path / "hf_benchmark"
    _write_texts(prefix.with_name(f"{prefix.name}_texts.csv"))
    prefix.with_name(f"{prefix.name}_gold.csv").write_text(
        "post_id,field,value,value_raw,source,provenance,note,scoring\n"
        f"hf-gold-00000,stance,LONG,Price Direction Up=1,{LAYER_GOLD},x,数据集方向标签,\n"
        f"hf-gold-00000,information_type,,positive,{LAYER_GOLD},x,本轮不评分,NOT_EVALUATED\n",
        encoding="utf-8",
    )
    regex_rows = [
        _eval_row("hf-gold-00000", "LONG", "FLAT", OUTCOME_WRONG, gold_source=LAYER_GOLD),
        _eval_row("hf-gold-00001", "SHORT", "SHORT", OUTCOME_CORRECT_VALUE, gold_source=LAYER_GOLD),
        _eval_row(
            "hf-gold-00000",
            "",
            "MACRO",
            OUTCOME_NOT_EVALUATED,
            gold_source=LAYER_GOLD,
            field="information_type",
        ),
        _eval_row("hf-guba-00000", "LONG", "UNKNOWN", OUTCOME_WRONG, gold_source=LAYER_GUBA),
        _eval_row("hf-guba-00001", "FLAT", "", OUTCOME_MISSED, gold_source=LAYER_GUBA),
    ]
    llm_rows = [
        _eval_row("hf-gold-00000", "LONG", "LONG", OUTCOME_CORRECT_VALUE, gold_source=LAYER_GOLD),
        _eval_row("hf-gold-00001", "SHORT", "UNKNOWN", OUTCOME_WRONG, gold_source=LAYER_GOLD),
        _eval_row(
            "hf-gold-00001",
            "",
            "",
            OUTCOME_NOT_EVALUATED,
            gold_source=LAYER_GOLD,
            field="information_type",
        ),
        _eval_row("hf-guba-00000", "LONG", "LONG", OUTCOME_CORRECT_VALUE, gold_source=LAYER_GUBA),
        _eval_row("hf-guba-00001", "FLAT", "UNKNOWN", OUTCOME_WRONG, gold_source=LAYER_GUBA),
    ]
    _write_eval(prefix.with_name(f"{prefix.name}_eval_regex.csv"), regex_rows)
    _write_eval(prefix.with_name(f"{prefix.name}_eval_llm.csv"), llm_rows)
    prefix.with_name(f"{prefix.name}_eval_llm_usage.json").write_text(
        json.dumps(
            {
                "parser_version": LLM_NAME,
                "usage": {"api": {"posts": 4}, "api_estimated_cost_usd_peak": 0.0042},
            }
        ),
        encoding="utf-8",
    )
    prefix.with_name(f"{prefix.name}_meta.json").write_text(
        json.dumps(
            {
                "datasets": {"gold": "mini/gold", "guba": "mini/guba", "split": "test"},
                "caveats": ["标题级情感分类任务，**不等于**博主帖子观点提取"],
            }
        ),
        encoding="utf-8",
    )
    return prefix


def _render(tmp_path: Path, *, case_limit: int = 2) -> str:
    """用迷你基准渲染报告文本（纯函数，不写盘）。"""
    prefix = _mini_benchmark(tmp_path)
    inputs = resolve_inputs(prefix)
    return render_report(
        inputs=inputs,
        texts=read_texts(inputs.texts),
        meta=read_meta(inputs.meta),
        regex=build_run(REGEX_NAME, inputs.regex_eval),
        llm=build_run(
            LLM_NAME,
            inputs.llm_eval,
            usage={"api": {"posts": 4}, "api_estimated_cost_usd_peak": 0.0042},
        ),
        generated_at=datetime(2026, 9, 14, 12, 0, tzinfo=UTC),
        case_limit=case_limit,
        repro_command="python scripts/report_hf_benchmark.py",
    )


# ---------------------------------------------------------------------------
# 1) Wilson 区间与百分比格式
# ---------------------------------------------------------------------------
def test_wilson_interval_matches_known_values() -> None:
    """`0/100` 与 `50/100` 的区间与教科书值一致（不做正态近似）。"""
    low_zero, high_zero = wilson_interval(0, 100)

    # 下界是**数学上的 0**，浮点运算会给到 1e-18 量级 → 用 approx 收口
    assert low_zero == pytest.approx(0.0, abs=1e-12)
    assert 0.036 <= high_zero <= 0.038  # 上界 ≈3.7%

    low_half, high_half = wilson_interval(50, 100)

    assert (low_half, high_half) == pytest.approx((0.4038, 0.5962), abs=1e-3)


def test_wilson_interval_is_safe_for_degenerate_inputs() -> None:
    """空分母返回 `(0, 0)`；全中/全不中都夹在 [0, 1] 内。"""
    assert wilson_interval(0, 0) == (0.0, 0.0)
    assert wilson_interval(10, 10)[1] == pytest.approx(1.0, abs=1e-12)
    assert wilson_interval(0, 10)[0] == pytest.approx(0.0, abs=1e-12)


def test_ci_text_always_carries_interval() -> None:
    """比例一律带 95% CI；分母为 0 时显式说明（报告里不允许出现裸数字）。"""
    assert ci_text(1, 2) == "50.0%（95% CI 9.5%~90.5%）"
    assert ci_text(0, 0) == "—（分母为 0）"


# ---------------------------------------------------------------------------
# 2) 读入层
# ---------------------------------------------------------------------------
def test_resolve_inputs_derives_expected_paths(tmp_path: Path) -> None:
    inputs = resolve_inputs(tmp_path / "logs" / "hf_benchmark")

    assert inputs.gold.name == "hf_benchmark_gold.csv"
    assert inputs.texts.name == "hf_benchmark_texts.csv"
    assert inputs.regex_eval.name == "hf_benchmark_eval_regex.csv"
    assert inputs.llm_eval.name == "hf_benchmark_eval_llm.csv"
    assert len(inputs.missing()) == 4  # 四件套都不存在
    assert inputs.llm_usage not in inputs.missing()  # 台账是可选输入


def test_read_eval_cells_parses_and_rejects_bad_rows(tmp_path: Path) -> None:
    """明细能读回；缺 `post_id`/`field` 的行**直接报错**（绝不静默丢格子）。"""
    good = tmp_path / "ok.csv"
    _write_eval(
        good, [_eval_row("p1", "LONG", "LONG", OUTCOME_CORRECT_VALUE, gold_source=LAYER_GOLD)]
    )

    cells = read_eval_cells(good)

    assert len(cells) == 1 and cells[0].outcome == OUTCOME_CORRECT_VALUE

    bad = tmp_path / "bad.csv"
    bad.write_text("post_id,field,gold_value\n,stance,LONG\n", encoding="utf-8")
    with pytest.raises(ValueError, match="缺少 post_id/field"):
        read_eval_cells(bad)


def test_read_texts_and_meta_tolerate_missing_files(tmp_path: Path) -> None:
    assert read_meta(tmp_path / "nope.json") == {}

    texts = read_texts(_write_texts(tmp_path / "t.csv"))

    assert set(texts) == {
        "hf-gold-00000",
        "hf-gold-00001",
        "hf-guba-00000",
        "hf-guba-00001",
    }
    assert texts["hf-guba-00000"]["raw_label"] == "1"


# ---------------------------------------------------------------------------
# 3) 统计层（层过滤 + 五类构成）
# ---------------------------------------------------------------------------
def test_run_layer_and_statistics(tmp_path: Path) -> None:
    inputs = resolve_inputs(_mini_benchmark(tmp_path))
    regex = build_run(REGEX_NAME, inputs.regex_eval)
    llm = build_run(LLM_NAME, inputs.llm_eval)

    assert len(regex.layer(LAYER_GOLD)) == 3  # 2 条 stance + 1 条未评估的 information_type
    assert len(regex.layer(LAYER_GUBA)) == 2
    assert len(regex.cells) == 5
    assert len(llm.cells) == 5
    assert regex.outcomes(LAYER_GOLD)[OUTCOME_WRONG][0].post_id == "hf-gold-00000"
    assert llm.outcomes(LAYER_GUBA)[OUTCOME_WRONG][0].post_id == "hf-guba-00001"
    assert no_opinion_posts(regex) == 1  # hf-guba-00001 整帖无抽取值
    assert no_opinion_posts(regex, layer=LAYER_GUBA) == 1
    assert no_opinion_posts(regex, layer=LAYER_GOLD) == 0  # 黄金层两条都给了值（含 FLAT）
    assert no_opinion_posts(llm) == 0
    assert unknown_stance(llm) == (2, 4)  # 2 格 UNKNOWN / 4 格给出了方向
    assert unknown_stance(llm, layer=LAYER_GOLD) == (1, 2)
    assert extra_info_cells(regex) == (1, 1)
    assert extra_info_cells(llm) == (1, 0)  # 模型未给出 → 只记「未评估」


def test_fixed_and_regressed_are_symmetric(tmp_path: Path) -> None:
    """`固定/退化` 只比 stance：正则错而 LLM 对 = 修好，反之 = 弄坏。"""
    inputs = resolve_inputs(_mini_benchmark(tmp_path))
    regex = build_run(REGEX_NAME, inputs.regex_eval)
    llm = build_run(LLM_NAME, inputs.llm_eval)

    fixed, regressed = fixed_regressed(regex, llm, layer=LAYER_GOLD)

    assert fixed == [("hf-gold-00000", "stance")]
    assert regressed == [("hf-gold-00001", "stance")]


def test_matrix_lines_shape_and_labels(tmp_path: Path) -> None:
    inputs = resolve_inputs(_mini_benchmark(tmp_path))
    llm = build_run(LLM_NAME, inputs.llm_eval)

    lines = matrix_lines(llm, layer=LAYER_GOLD)

    header = lines[0]
    row_count = sum(1 for line in lines[2:] if line.startswith("| **"))
    assert header.startswith("| 金标准 ↓")
    assert "LONG" in header and "∅（未给出）" in header
    assert row_count == 4  # LONG / SHORT / UNKNOWN / ∅（本夹具出现过的标签）
    assert lines[-1].startswith("| **∅（未给出）** |")  # "未给出" 永远收尾


# ---------------------------------------------------------------------------
# 4) 案例层与「标注存疑」启发式
# ---------------------------------------------------------------------------
def test_raw_direction_stance_reads_archived_columns() -> None:
    """方向列 → stance（`texts.csv` 里是字符串 `1`/`True`）。"""
    assert raw_direction_stance({"raw_Price Direction Up": "1"}) == "LONG"
    assert raw_direction_stance({"raw_Price Direction Down": "True"}) == "SHORT"
    assert raw_direction_stance({"raw_Price Direction Constant": "1"}) == "FLAT"
    assert raw_direction_stance({"raw_Price Direction Up": "0"}) == ""


def test_suspicious_dataset_cases_flags_conflicts() -> None:
    """黄金：情感与方向相反；股吧：看多线索 + `label=0` → 都进"待人工复核"候选。"""
    texts = {
        "hf-gold-00000": {
            "source": LAYER_GOLD,
            "text_content": "gold rises 1%",
            "raw_Price Sentiment": "positive",
            "raw_Price Direction Down": "1",
        },
        "hf-guba-00000": {
            "source": LAYER_GUBA,
            "text_content": "看多黄金，逢低做多",
            "raw_label": "0",
        },
    }

    lines = suspicious_dataset_cases(texts)

    assert any("情感与方向相反" in line for line in lines)
    assert any("看多线索" in line for line in lines)


def test_suspicious_dataset_cases_is_quiet_on_consistent_data() -> None:
    """一致数据不得产生噪音（启发式宁可漏报，不可乱报）。"""
    texts = {
        "hf-gold-00000": {
            "source": LAYER_GOLD,
            "text_content": "gold rises 1%",
            "raw_Price Sentiment": "positive",
            "raw_Price Direction Up": "1",
        },
        "hf-guba-00000": {
            "source": LAYER_GUBA,
            "text_content": "震荡为主",
            "raw_label": "2",
        },
    }

    assert suspicious_dataset_cases(texts) == ["- （启发式未发现存疑案例）"]


# ---------------------------------------------------------------------------
# 5) 渲染层：N、95% CI、未评估口径、股吧分层必须写清
# ---------------------------------------------------------------------------
def test_render_report_has_required_sections(tmp_path: Path) -> None:
    text = _render(tmp_path)

    for section in (
        "## 免责声明与适用范围",
        "## 0. 结论摘要",
        "## 1. 数据来源与口径",
        "## 2. 置信度边界",
        "## 3. 主结论",
        "## 4. 混淆矩阵",
        "## 5. 正则 vs LLM v13",
        "## 6. 典型错误案例",
        "## 7. 辅助层：中文股吧",
        "## 8. 局限与下一步",
        "## 附录 A. 复现命令",
        "## 附录 B. 输入文件校验",
    ):
        assert section in text

    assert "黄金（N=2）" in text  # 样本量必须显式
    assert "1/2 | 50.0%（95% CI" in text  # 比例必须带区间
    assert "$0.0042" in text  # 费用台账
    assert "### 4.1 " in text and "### 4.2 " in text
    assert "### 5.3" in text  # 与 Mock 基线的对读小节


def test_render_report_carries_executive_disclaimer(tmp_path: Path) -> None:
    """用户裁决的免责声明四条必须写进报告（任务错配 / UNKNOWN 合规 / 能证明什么 / Phase 3）。"""
    text = _render(tmp_path)

    assert "## 免责声明与适用范围" in text
    assert "标题级方向分类" in text and "本质差异" in text
    assert "`UNKNOWN` 是合规行为，不是错误" in text
    assert "opinion-prompt-v13" in text and "保持冻结" in text
    assert "正则抽取器在英文/标题级语料上完全失效" in text
    assert "News Alpha" in text


def test_render_report_keeps_not_evaluated_out_of_main_conclusion(tmp_path: Path) -> None:
    """`information_type` 未评估 → 报告必须写明「不计假阳性」，且不计入主表准确率。"""
    text = _render(tmp_path)

    assert "scoring=NOT_EVALUATED" in text
    assert "不计假阳性" in text
    assert "在 1 个未评估格子里给出了 0 个值" in text  # LLM 未给值 → 只记「未评估」
    assert "| 信息类型" not in text  # 未评估字段不得出现在主结论表里


def test_render_report_layers_guba_separately(tmp_path: Path) -> None:
    """股吧层只能出现在 §7 与分层说明里，且必须写明"不进主结论"。"""
    text = _render(tmp_path)

    assert "中文股吧层（N=2）**只作辅助参考**" in text
    assert "不进主结论" in text
    assert "上证50ETF" in text  # 必须提示"非黄金"的语料差异


def test_render_report_states_zero_spurious_is_not_precision(tmp_path: Path) -> None:
    """正则零假阳性必须解释成「几乎不作为」，不能当作精确性证据。"""
    text = _render(tmp_path)

    assert "0 格假阳性" in text
    assert "不是精确性的证据" in text


def test_render_report_carries_cases_and_repro(tmp_path: Path) -> None:
    text = _render(tmp_path, case_limit=1)

    assert "gold rises 1%" in text  # 案例原文
    assert "hf-gold-00000" in text
    assert "python scripts/report_hf_benchmark.py" in text
    assert "llm-deepseek-v13" in text


def test_render_report_digests_inputs(tmp_path: Path) -> None:
    prefix = _mini_benchmark(tmp_path)

    text = _render(tmp_path)
    inputs = resolve_inputs(prefix)

    assert file_digest(inputs.gold) in text
    assert file_digest(inputs.llm_eval) in text


def test_label_for_and_repro_command() -> None:
    """显示名与复现命令：未知版本不许猜名字，命令必须四步齐全。"""
    assert label_for(REGEX_NAME).startswith("RegexOpinionExtractor")
    # 未知 parser_version → **原名返回，不猜显示名**（避免报告写错抽取器）
    assert label_for("llm-deepseek-v99") == "llm-deepseek-v99"
    assert label_for("unknown-v9") == "unknown-v9"

    command = repro_command(Path("logs") / "hf_benchmark")

    assert "load_hf_benchmark.py" in command and "--information-type empty" in command
    assert "--extractor regex" in command and "--extractor llm" in command
    assert "--llm-usage-out" in command
    assert command.rstrip().endswith("python scripts/report_hf_benchmark.py")


def test_mock_reference_lines_skips_when_absent(monkeypatch, tmp_path: Path) -> None:
    """Mock 基线明细不存在时**显式说明本节跳过**（不留空白、不报错）。"""
    monkeypatch.setattr("scripts.report_hf_benchmark.MOCK_EVAL_REGEX", tmp_path / "a.csv")
    monkeypatch.setattr("scripts.report_hf_benchmark.MOCK_EVAL_LLM", tmp_path / "b.csv")

    assert mock_reference_lines() == [
        "- （未找到 Mock 基线明细 `logs/extractor_eval*.csv`，本节跳过）"
    ]


# ---------------------------------------------------------------------------
# 6) CLI
# ---------------------------------------------------------------------------
def test_main_dry_run_prints_and_writes_nothing(tmp_path: Path, capsys) -> None:
    prefix = _mini_benchmark(tmp_path)
    report_path = tmp_path / "report.md"

    code = main(["--prefix", str(prefix), "--report", str(report_path), "--dry-run"])

    out = capsys.readouterr().out
    assert code == 0
    assert "## 0. 结论摘要" in out
    assert "--dry-run：未写任何文件" in out
    assert not report_path.exists()


def test_main_writes_report_file(tmp_path: Path, capsys) -> None:
    prefix = _mini_benchmark(tmp_path)
    report_path = tmp_path / "out" / "report.md"

    code = main(["--prefix", str(prefix), "--report", str(report_path), "--case-limit", "1"])

    assert code == 0 and report_path.exists()
    text = report_path.read_text(encoding="utf-8")
    assert "llm-deepseek-v13" in text  # 台账里的 parser_version 优先于 CLI 默认
    assert "## 8. 局限与下一步" in text
    assert "已写出" in capsys.readouterr().out


def test_main_missing_inputs_returns_2(tmp_path: Path, capsys) -> None:
    """缺输入 → 退出码 2，打印缺哪些文件，且**不写出半成品报告**。"""
    report_path = tmp_path / "r.md"

    code = main(["--prefix", str(tmp_path / "nope"), "--report", str(report_path)])

    assert code == 2
    assert not report_path.exists()
    assert "缺少必需输入" in capsys.readouterr().err
