from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from techpulse.enricher import (
    CATEGORIES,
    ClaudeEnricher,
    InsightBatch,
    KeywordEnricher,
    StoryInput,
    StoryInsight,
    build_enricher,
)


@pytest.fixture
def keyword():
    return KeywordEnricher(["AI/LLMs", "Python", "security"])


def test_keyword_enricher_scores_interesting_stories_higher(keyword):
    ai, bike = keyword.enrich(
        [
            StoryInput(hn_id=1, title="Running LLM agents in Python"),
            StoryInput(hn_id=2, title="The history of the bicycle"),
        ]
    )
    assert ai.category == "AI/ML"
    assert bike.category == "Other"
    assert ai.relevance > bike.relevance
    assert 1 <= bike.relevance <= 10 and 1 <= ai.relevance <= 10


def test_keyword_enricher_matches_word_stems(keyword):
    (insight,) = keyword.enrich([StoryInput(hn_id=1, title="Critical vulnerability in OpenSSH")])
    assert insight.category == "Security"
    assert "vulnerab" in insight.tags


def test_keyword_enricher_always_returns_valid_category(keyword):
    out = keyword.enrich([StoryInput(hn_id=i, title=t) for i, t in enumerate(["", "???", "x"])])
    assert all(i.category in CATEGORIES for i in out)


def _fake_response(insights, stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        parsed_output=InsightBatch(insights=insights),
        usage=SimpleNamespace(input_tokens=100, output_tokens=50),
        _request_id="req_test",
    )


def test_claude_enricher_sends_structured_request_and_filters_unknown_ids():
    client = MagicMock()
    client.beta.messages.parse.return_value = _fake_response(
        [
            StoryInsight(hn_id=1, category="AI/ML", relevance=9, summary="s", tags=["ai"]),
            StoryInsight(hn_id=999, category="Other", relevance=1, summary="hallucinated", tags=[]),
        ]
    )
    enricher = ClaudeEnricher("claude-opus-5-5", ["AI"], client=client)

    out = enricher.enrich([StoryInput(hn_id=1, title="LLMs")])

    assert [i.hn_id for i in out] == [1]
    kwargs = client.beta.messages.parse.call_args.kwargs
    assert kwargs["model"] == "claude-opus-5-5"
    assert kwargs["output_format"] is InsightBatch
    assert kwargs["fallbacks"] == "default"
    assert "AI" in kwargs["system"]


def test_claude_enricher_raises_on_refusal():
    client = MagicMock()
    client.beta.messages.parse.return_value = _fake_response([], stop_reason="refusal")
    with pytest.raises(RuntimeError, match="declined"):
        ClaudeEnricher("m", [], client=client).enrich([StoryInput(hn_id=1, title="t")])


def test_claude_enricher_skips_api_for_empty_batch():
    client = MagicMock()
    assert ClaudeEnricher("m", [], client=client).enrich([]) == []
    client.beta.messages.parse.assert_not_called()


def test_build_enricher_falls_back_without_key():
    assert isinstance(build_enricher(None, "m", ["AI"]), KeywordEnricher)
    assert isinstance(build_enricher("sk-test", "m", ["AI"]), ClaudeEnricher)
