"""LLM 抽取器的单元测试（零网络、零 token：MockTransport + tmp 缓存）。

覆盖重点：
1. **正常路径**：模型 JSON → 契约 draft；`parser_version` 由系统注入；警告含来源标记；
2. **不信任模型**：模型自填 `parser_version` / `effective_at` → 剔除 + 诊断，
   但**它真正的观点必须保住**（不能因为一个越界键就把整条丢掉）；
3. **容错解析**：markdown 代码块 / 顶层数组 / 非对象元素 / 未知顶层键；
4. **解析失败**：非 JSON → `LLM_PARSE_ERROR` 诊断（**不抛异常**，批量评估不断流）；
5. **失败降级**：5xx 重试耗尽 → `LLM_API_ERROR` 诊断 + **失败也写缓存**（第二次不联网）；
6. **系统性故障必须抛**：无 Key（`LLMConfigError`）、401（`LLMAuthError`）不得静默降级；
7. **缓存语义**：同文本第二次零请求；`has_media` / prompt 版本变化 → 新请求；
   `refresh` 重跑覆盖；`readonly` 命中不需 Key、未命中显式报错；
8. **协议一致性**：满足 `OpinionExtractor` 协议，可作为 `RegexOpinionExtractor` 的替代品。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from config.settings import Settings
from src.processors.llm_client import (
    DEFAULT_MODEL,
    DeepSeekClient,
    LLMAuthError,
    LLMConfigError,
    LLMError,
)
from src.processors.llm_extractor import DEFAULT_PARSER_VERSION, LLMOpinionExtractor
from src.processors.opinion_extractor import DiagnosticCode, OpinionExtractor
from src.processors.prompt_opinion import PROMPT_VERSION

pytestmark = pytest.mark.unit

FAKE_KEY = "sk-test-deadbeefdeadbeefdeadbeef0123"
POST = "黄金 2380 做多，止损 2365，目标 2450，日内短线。"

VALID_PAYLOAD: dict[str, Any] = {
    "opinions": [
        {
            "stance": "LONG",
            "instrument": "XAUUSD",
            "horizon": "1h",
            "confidence": 0.7,
            "entry_low": 2378,
            "entry_high": 2382,
            "stop_loss": 2365,
            "take_profit": 2450,
            "information_type": "TECHNICAL",
            "rationale": "日内做多，止损 2365，目标 2450",
        }
    ],
    "no_opinion": False,
}


class ScriptedTransport(httpx.MockTransport):
    """按脚本返回响应；记录请求数（用于断言"缓存命中不联网"）。"""

    def __init__(self, script: Sequence[Any]) -> None:
        self.script = list(script)
        self.calls = 0
        super().__init__(self._handler)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if not self.script:
            raise AssertionError(f"脚本已耗尽：预期之外的第 {self.calls} 次 HTTP 请求")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _ok(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "model": "deepseek-chat",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 80},
        },
    )


def _payload_response(payload: dict[str, Any]) -> httpx.Response:
    return _ok(json.dumps(payload, ensure_ascii=False))


def _extractor(
    script: Sequence[Any] = (),
    *,
    tmp: Path | None = None,
    cache_mode: str = "auto",
    max_retries: int = 3,
    **kwargs: Any,
) -> tuple[LLMOpinionExtractor, ScriptedTransport]:
    transport = ScriptedTransport(script)
    client = DeepSeekClient(
        FAKE_KEY,
        # 用 **默认模型名**：缓存键含模型名，若这里写死旧名，
        # "缓存命中"类测试会因为与 replay 侧模型不一致而假失败。
        model=DEFAULT_MODEL,
        transport=transport,
        max_retries=max_retries,
    )
    extractor = LLMOpinionExtractor(
        client=client,
        cache_dir=tmp,
        cache_mode=cache_mode,
        settings=Settings(deepseek_api_key=None),
        **kwargs,
    )
    return extractor, transport


def _codes(result: Any) -> list[str]:
    return [diagnostic.code.value for diagnostic in result.diagnostics]


def _no_key_extractor(
    cache_dir: Path, *, cache_mode: str = "auto", **kwargs: Any
) -> LLMOpinionExtractor:
    """没有客户端、也没有 Key 的抽取器（验证"纯缓存复跑无需密钥"）。"""
    return LLMOpinionExtractor(
        cache_dir=cache_dir,
        cache_mode=cache_mode,
        settings=Settings(deepseek_api_key=None),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------
def test_valid_response_becomes_contract_draft(tmp_path: Path) -> None:
    extractor, transport = _extractor([_payload_response(VALID_PAYLOAD)], tmp=tmp_path)

    result = extractor.extract(POST)

    assert transport.calls == 1
    assert len(result.drafts) == 1
    draft = result.drafts[0]
    assert draft.stance.value == "LONG"
    assert str(draft.entry_low) == "2378"
    assert str(draft.take_profit) == "2450"
    assert draft.parser_version == DEFAULT_PARSER_VERSION
    assert result.parser_version == DEFAULT_PARSER_VERSION
    assert "llm-source=api" in result.warnings
    assert extractor.stats()["posts_via_api"] == 1


def test_system_injects_parser_version_not_model(tmp_path: Path) -> None:
    """模型自填 parser_version → 剔除并诊断，但我们注入的版本必须生效。"""
    payload = json.loads(json.dumps(VALID_PAYLOAD))
    payload["opinions"][0]["parser_version"] = "gpt-999"
    extractor, _ = _extractor([_payload_response(payload)], tmp=tmp_path)

    result = extractor.extract(POST)

    assert len(result.drafts) == 1
    assert result.drafts[0].parser_version == DEFAULT_PARSER_VERSION
    assert DiagnosticCode.INVALID_DRAFT.value in _codes(result)


def test_forbidden_time_and_author_fields_are_stripped(tmp_path: Path) -> None:
    payload = json.loads(json.dumps(VALID_PAYLOAD))
    payload["opinions"][0]["effective_at"] = "2099-01-01T00:00:00Z"
    payload["opinions"][0]["author_id"] = "123e4567"
    extractor, _ = _extractor([_payload_response(payload)], tmp=tmp_path)

    result = extractor.extract(POST)

    draft = result.drafts[0]
    assert draft.stance.value == "LONG"  # 观点保住了
    assert not hasattr(draft, "effective_at")  # 模型编造的时间不存在
    assert DiagnosticCode.INVALID_DRAFT.value in _codes(result)


def test_no_opinion_response_yields_empty_drafts(tmp_path: Path) -> None:
    extractor, _ = _extractor(
        [_payload_response({"opinions": [], "no_opinion": True})], tmp=tmp_path
    )

    result = extractor.extract("美国 8 月 CPI 同比 3.2%，高于市场预期。")

    assert result.no_opinion is True
    assert result.drafts == ()
    assert result.diagnostics == ()


def test_unknown_top_level_key_warns_but_keeps_opinions(tmp_path: Path) -> None:
    payload = dict(VALID_PAYLOAD, note="模型多写的字段")
    extractor, _ = _extractor([_payload_response(payload)], tmp=tmp_path)

    result = extractor.extract(POST)

    assert len(result.drafts) == 1
    assert any("未知顶层键" in warning for warning in result.warnings)


def test_empty_text_short_circuits_without_api_call(tmp_path: Path) -> None:
    extractor, transport = _extractor([_payload_response(VALID_PAYLOAD)], tmp=tmp_path)

    result = extractor.extract("   ")

    assert transport.calls == 0
    assert _codes(result) == [DiagnosticCode.EMPTY_TEXT.value]


def test_invalid_draft_is_reported_not_raised(tmp_path: Path) -> None:
    payload = {
        "opinions": [
            {"stance": "LONG", "entry_low": 2400, "entry_high": 2300}  # 区间倒置
        ],
        "no_opinion": False,
    }
    extractor, _ = _extractor([_payload_response(payload)], tmp=tmp_path)

    result = extractor.extract(POST)

    assert result.drafts == ()
    assert DiagnosticCode.INVALID_DRAFT.value in _codes(result)


def test_instrument_is_not_defaulted_by_the_extractor(tmp_path: Path) -> None:
    """模型没给 instrument → 保留 null，抽取器**不得**替它填 XAUUSD。"""
    payload = {"opinions": [{"stance": "SHORT"}], "no_opinion": False}
    extractor, _ = _extractor([_payload_response(payload)], tmp=tmp_path)

    draft = extractor.extract(POST).drafts[0]

    assert draft.instrument is None
    assert draft.stance.value == "SHORT"


# ---------------------------------------------------------------------------
# 容错解析
# ---------------------------------------------------------------------------
def test_markdown_code_fence_is_tolerated(tmp_path: Path) -> None:
    wrapped = "```json\n" + json.dumps(VALID_PAYLOAD, ensure_ascii=False) + "\n```"
    extractor, _ = _extractor([_ok(wrapped)], tmp=tmp_path)

    result = extractor.extract(POST)

    assert len(result.drafts) == 1
    assert result.drafts[0].stance.value == "LONG"


def test_top_level_array_is_tolerated_with_warning(tmp_path: Path) -> None:
    payload = json.dumps(VALID_PAYLOAD["opinions"], ensure_ascii=False)
    extractor, _ = _extractor([_ok(payload)], tmp=tmp_path)

    result = extractor.extract(POST)

    assert len(result.drafts) == 1
    assert any("顶层数组" in warning for warning in result.warnings)


def test_non_object_opinion_item_is_skipped(tmp_path: Path) -> None:
    payload = {"opinions": ["这是一段解释文字", VALID_PAYLOAD["opinions"][0]]}
    extractor, _ = _extractor([_payload_response(payload)], tmp=tmp_path)

    result = extractor.extract(POST)

    assert len(result.drafts) == 1
    assert any("不是 JSON 对象" in warning for warning in result.warnings)


def test_multi_opinion_response_keeps_order(tmp_path: Path) -> None:
    """多空并存必须拆成多条（docs/10 §5）。"""
    payload = {
        "opinions": [
            {"stance": "SHORT", "take_profit": 2325},
            {"stance": "LONG", "take_profit": 2450},
        ]
    }
    extractor, _ = _extractor([_payload_response(payload)], tmp=tmp_path)

    result = extractor.extract(POST)

    assert [draft.stance.value for draft in result.drafts] == ["SHORT", "LONG"]


@pytest.mark.parametrize(
    "content",
    [
        "这不是 JSON，只是模型的解释",
        '{"opinions": "想要一个数组" }',
        '{"no_opinion": false}',
        '{"opinions": [{"stance": "LONG"}]',  # 括号不闭合
    ],
)
def test_unparseable_content_yields_parse_error_diagnostic(
    tmp_path: Path, content: str
) -> None:
    extractor, _ = _extractor([_ok(content)], tmp=tmp_path)

    result = extractor.extract(POST)

    assert result.drafts == ()
    assert _codes(result) == [DiagnosticCode.LLM_PARSE_ERROR.value]
    assert extractor.stats()["parse_failures"] == 1


def test_long_text_is_truncated_with_warning(tmp_path: Path) -> None:
    extractor, _ = _extractor([_payload_response(VALID_PAYLOAD)], tmp=tmp_path)

    result = extractor.extract("黄金做多。" + "字" * 5000)

    assert any("截断" in warning for warning in result.warnings)
    assert len(result.drafts) == 1


# ---------------------------------------------------------------------------
# 失败降级 vs 系统性故障
# ---------------------------------------------------------------------------
def test_server_error_degrades_to_diagnostic_and_is_cached(tmp_path: Path) -> None:
    """★ 5xx 重试耗尽 → 降级诊断 + 失败写缓存（第二次不再联网）。"""
    script = [httpx.Response(500), httpx.Response(500)]  # 首次 + 1 次重试
    extractor, transport = _extractor(script, tmp=tmp_path, max_retries=1)

    first = extractor.extract(POST)
    second = extractor.extract(POST)

    assert transport.calls == 2  # 只有第一次真的发了请求
    assert _codes(first) == [DiagnosticCode.LLM_API_ERROR.value]
    assert _codes(second) == [DiagnosticCode.LLM_API_ERROR.value]
    assert "llm-source=cache-error" in second.warnings
    assert extractor.stats()["api_failures"] == 1


def test_auth_error_is_raised_not_degraded(tmp_path: Path) -> None:
    """★ 401 是系统性故障：必须抛出，不能产出 200 条"看起来很差"的假结果。"""
    extractor, _ = _extractor([httpx.Response(401, json={"error": {}})], tmp=tmp_path)

    with pytest.raises(LLMAuthError):
        extractor.extract(POST)


def test_missing_api_key_raises_config_error_when_auto(tmp_path: Path) -> None:
    extractor = _no_key_extractor(tmp_path, cache_mode="auto")

    with pytest.raises(LLMConfigError):
        extractor.extract(POST)


def test_transport_error_is_retried_by_client(tmp_path: Path) -> None:
    script = [httpx.ReadTimeout("t1"), _payload_response(VALID_PAYLOAD)]
    extractor, transport = _extractor(script, tmp=tmp_path)

    result = extractor.extract(POST)

    assert transport.calls == 2
    assert len(result.drafts) == 1
    assert any(warning.startswith("llm-attempts=2") for warning in result.warnings)


def test_raw_json_is_preserved_for_audit(tmp_path: Path) -> None:
    """原始输出必须落盘（人工比对模型到底返回了什么）。"""
    extractor, _ = _extractor([_payload_response(VALID_PAYLOAD)], tmp=tmp_path)

    extractor.extract(POST)

    files = list(tmp_path.rglob("*.json"))
    assert len(files) == 1
    entry = json.loads(files[0].read_text(encoding="utf-8"))
    assert entry["status"] == "ok"
    assert json.loads(entry["raw_content"])["opinions"][0]["stance"] == "LONG"
    assert entry["usage"]["prompt_tokens"] == 900


# ---------------------------------------------------------------------------
# 缓存语义（复跑零成本的关键）
# ---------------------------------------------------------------------------
def test_second_call_hits_cache_without_network(tmp_path: Path) -> None:
    """★ 同一文本第二次必须零请求（readonly 复跑的可行性基础）。"""
    extractor, transport = _extractor([_payload_response(VALID_PAYLOAD)], tmp=tmp_path)

    first = extractor.extract(POST)
    second = extractor.extract(POST)

    assert transport.calls == 1
    assert [draft.stance.value for draft in first.drafts] == ["LONG"]
    assert [draft.stance.value for draft in second.drafts] == ["LONG"]
    assert "llm-source=cache" in second.warnings
    stats = extractor.stats()
    assert stats["posts_via_api"] == 1
    assert stats["posts_via_cache"] == 1


def test_has_media_changes_cache_key(tmp_path: Path) -> None:
    """带图 / 不带图是两条不同输入（Prompt 里的 has_media 不同）→ 必须各付费一次。"""
    extractor, transport = _extractor(
        [_payload_response(VALID_PAYLOAD), _payload_response(VALID_PAYLOAD)], tmp=tmp_path
    )

    extractor.extract(POST, has_media=False)
    extractor.extract(POST, has_media=True)

    assert transport.calls == 2


def test_prompt_version_change_invalidates_cache(tmp_path: Path) -> None:
    """★ 改 Prompt 必须自动失效，绝不能复用旧答案。"""
    payloads = [_payload_response(VALID_PAYLOAD), _payload_response(VALID_PAYLOAD)]
    v1, transport = _extractor(payloads, tmp=tmp_path, prompt_version="opinion-prompt-v1")
    v1.extract(POST)
    v2 = LLMOpinionExtractor(
        client=DeepSeekClient(FAKE_KEY, model="deepseek-chat", transport=transport),
        cache_dir=tmp_path,
        prompt_version="opinion-prompt-v2",
    )

    v2.extract(POST)

    assert transport.calls == 2


def test_default_prompt_version_matches_prompt_module(tmp_path: Path) -> None:
    extractor, _ = _extractor(tmp=tmp_path)

    assert extractor.prompt_version == PROMPT_VERSION


def test_refresh_mode_calls_api_again_and_overwrites(tmp_path: Path) -> None:
    seed, _ = _extractor([_payload_response(VALID_PAYLOAD)], tmp=tmp_path)
    seed.extract(POST)

    upd = _payload_response({"opinions": [{"stance": "SHORT", "take_profit": 2300}]})
    again, transport = _extractor([upd], tmp=tmp_path, cache_mode="refresh")
    result = again.extract(POST)

    assert transport.calls == 1  # 忽略旧缓存，重新付费一次
    assert result.drafts[0].stance.value == "SHORT"
    reread, _ = _extractor(tmp=tmp_path)
    assert reread.extract(POST).drafts[0].stance.value == "SHORT"


def test_readonly_replay_needs_no_api_key(tmp_path: Path) -> None:
    """★ readonly 复跑：命中缓存、零请求、**完全不读 .env 里的 Key**。"""
    seed, _ = _extractor([_payload_response(VALID_PAYLOAD)], tmp=tmp_path)
    seed.extract(POST)

    replay = _no_key_extractor(tmp_path, cache_mode="readonly")
    result = replay.extract(POST)

    assert len(result.drafts) == 1
    assert "llm-source=cache" in result.warnings
    assert replay.stats()["client"] is None  # 从未构造客户端 → 从未联网


def test_readonly_miss_raises_instead_of_calling_api(tmp_path: Path) -> None:
    replay = _no_key_extractor(tmp_path, cache_mode="readonly")

    with pytest.raises(LLMError) as excinfo:
        replay.extract(POST)

    assert "readonly" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 协议与统计
# ---------------------------------------------------------------------------
def test_extractor_satisfies_opinion_extractor_protocol(tmp_path: Path) -> None:
    extractor, _ = _extractor(tmp=tmp_path)

    assert isinstance(extractor, OpinionExtractor)
    assert extractor.parser_version == DEFAULT_PARSER_VERSION
    assert len(DEFAULT_PARSER_VERSION) <= 50  # author_opinions.parser_version 上限


def test_parser_version_can_be_overridden(tmp_path: Path) -> None:
    extractor, _ = _extractor(
        [_payload_response(VALID_PAYLOAD)], tmp=tmp_path, parser_version="llm-deepseek-v2"
    )

    assert extractor.extract(POST).drafts[0].parser_version == "llm-deepseek-v2"


def test_stats_are_printable_without_secrets(tmp_path: Path) -> None:
    extractor, _ = _extractor([_payload_response(VALID_PAYLOAD)], tmp=tmp_path)
    extractor.extract(POST)

    text = json.dumps(extractor.stats(), ensure_ascii=False)

    assert FAKE_KEY not in text
    assert json.loads(text)["model"] == DEFAULT_MODEL


# ---------------------------------------------------------------------------
# Token 台账与费用估算
# ---------------------------------------------------------------------------
def test_token_ledger_separates_api_and_cached(tmp_path: Path) -> None:
    """★ 费用台账：本次真实调用的 token 与"复用缓存"的 token 必须分开记。"""
    seed, _ = _extractor([_payload_response(VALID_PAYLOAD)], tmp=tmp_path)
    seed.extract(POST)
    api_usage = seed.stats()["usage"]["api"]

    # 单测 mock 里写死 prompt_tokens=900 / completion_tokens=80
    assert api_usage["posts"] == 1
    assert api_usage["prompt_tokens"] == 900
    assert api_usage["completion_tokens"] == 80
    assert seed.stats()["usage"]["cached"]["posts"] == 0

    replay = _no_key_extractor(tmp_path, cache_mode="readonly")
    replay.extract(POST)
    usage = replay.stats()["usage"]

    assert usage["api"]["posts"] == 0  # 本次没花一分钱
    assert usage["cached"]["posts"] == 1
    assert usage["cached"]["prompt_tokens"] == 900
    assert usage["api_estimated_cost_usd_peak"] == 0.0
    # 全量口径仍要能回答"这套样本一共烧了多少"
    assert usage["full_set_estimated_cost_usd_peak"] is not None
    assert usage["full_set_estimated_cost_usd_peak"] > 0


def test_thinking_mode_is_disabled_by_default(tmp_path: Path) -> None:
    """新一代模型默认开启思考模式 → 抽取器必须显式关掉（省钱、省 max_tokens）。"""
    extractor, _ = _extractor([_payload_response(VALID_PAYLOAD)], tmp=tmp_path)

    assert extractor.thinking_mode == "disabled"
    assert extractor._ensure_client().thinking_mode == "disabled"


def test_usage_ledger_tolerates_cache_entries_without_token_fields(tmp_path: Path) -> None:
    """旧缓存（`usage` 里没有 cache_hit/miss 分项）不得让台账统计崩掉。"""
    seed, _ = _extractor([_payload_response(VALID_PAYLOAD)], tmp=tmp_path)
    seed.extract(POST)
    entry_path = next(iter(seed.cache.directory.rglob("*.json")))
    payload = json.loads(entry_path.read_text(encoding="utf-8"))
    payload["usage"] = {"model": DEFAULT_MODEL, "prompt_tokens": 10}  # 缺字段的旧记录
    entry_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    replay = _no_key_extractor(tmp_path, cache_mode="readonly")
    replay.extract(POST)

    cached = replay.stats()["usage"]["cached"]
    assert cached["prompt_tokens"] == 10
    assert cached["completion_tokens"] == 0
