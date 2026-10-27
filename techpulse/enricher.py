"""AI enrichment: classify, score and summarise stories.

`ClaudeEnricher` sends a whole batch to Claude in one request and gets back
schema-validated JSON (structured outputs). `KeywordEnricher` is a deterministic
offline fallback so the pipeline works without an API key (local dev, CI, demos).
"""

import json
import logging
import re
from typing import Literal, Protocol

import anthropic
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

Category = Literal[
    "AI/ML", "Programming", "DevOps/Cloud", "Security", "Hardware",
    "Science", "Business", "Design", "Other",
]  # fmt: skip
CATEGORIES: tuple[str, ...] = Category.__args__  # type: ignore[attr-defined]


class StoryInput(BaseModel):
    hn_id: int
    title: str
    domain: str | None = None


class StoryInsight(BaseModel):
    hn_id: int
    category: Category
    relevance: int = Field(ge=1, le=10, description="1-10 relevance to the reader's interests")
    summary: str = Field(description="One sentence on why this matters to the reader")
    tags: list[str] = Field(description="2-4 short lowercase topic tags")


class InsightBatch(BaseModel):
    insights: list[StoryInsight]


class Enricher(Protocol):
    name: str

    def enrich(self, stories: list[StoryInput]) -> list[StoryInsight]: ...


SYSTEM_PROMPT = """You curate a tech news feed for one engineer.
Their interests: {interests}.

For every story you receive, return one insight:
- category: the best-fitting category.
- relevance: 1-10. 9-10 = directly about their interests and likely actionable;
  5-6 = loosely related; 1-3 = unrelated to their interests.
- summary: one plain sentence (max 25 words) on what it is and why it matters to them.
  You only see the title and domain - do not invent details beyond what they imply.
- tags: 2-4 short lowercase tags.
Return exactly one insight per input hn_id."""


class ClaudeEnricher:
    def __init__(self, model: str, interests: list[str], client: anthropic.Anthropic | None = None):
        self.model = model
        self.interests = interests
        self.client = client or anthropic.Anthropic()
        self.name = f"claude:{model}"

    def enrich(self, stories: list[StoryInput]) -> list[StoryInsight]:
        if not stories:
            return []
        payload = json.dumps([s.model_dump() for s in stories], ensure_ascii=False)
        response = self.client.beta.messages.parse(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT.format(interests=", ".join(self.interests)),
            messages=[{"role": "user", "content": f"Stories:\n{payload}"}],
            output_format=InsightBatch,
            # Simple classification: low effort keeps it fast and cheap.
            output_config={"effort": "low"},
            # If a request is ever declined, let the API retry on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise RuntimeError(f"Claude declined the batch (request {response._request_id})")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("Claude response truncated - reduce batch size")

        wanted = {s.hn_id for s in stories}
        insights = [i for i in response.parsed_output.insights if i.hn_id in wanted]
        log.info(
            "Claude enriched %d/%d stories (in=%d out=%d tokens)",
            len(insights), len(stories),
            response.usage.input_tokens, response.usage.output_tokens,
        )  # fmt: skip
        return insights


# --- Offline fallback -------------------------------------------------------

_RULES: list[tuple[str, tuple[str, ...]]] = [
    (
        "AI/ML",
        (
            "ai",
            "llm",
            "gpt",
            "claude",
            "model",
            "neural",
            "agent",
            "openai",
            "anthropic",
            "ml",
            "inference",
            "transformer",
            "diffusion",
        ),
    ),
    (
        "Security",
        (
            "security",
            "vulnerab",
            "exploit",
            "cve",
            "breach",
            "malware",
            "encrypt",
            "privacy",
            "hack",
            "auth",
            "password",
        ),
    ),
    (
        "DevOps/Cloud",
        (
            "kubernetes",
            "k8s",
            "docker",
            "aws",
            "cloud",
            "devops",
            "terraform",
            "deploy",
            "server",
            "linux",
            "postgres",
            "database",
            "infra",
        ),
    ),
    (
        "Programming",
        (
            "rust",
            "python",
            "javascript",
            "typescript",
            "golang",
            "go ",
            "compiler",
            "programming",
            "code",
            "api",
            "library",
            "framework",
            "git",
            "sql",
            "lisp",
            "c++",
            "zig",
            "open source",
            "show hn",
        ),
    ),
    (
        "Hardware",
        (
            "chip",
            "cpu",
            "gpu",
            "hardware",
            "nintendo",
            "3ds",
            "arm",
            "risc",
            "battery",
            "robot",
            "fpga",
            "semiconductor",
        ),
    ),
    (
        "Science",
        (
            "physics",
            "biology",
            "space",
            "nasa",
            "climate",
            "quantum",
            "research",
            "study",
            "scientists",
            "math",
        ),
    ),
    (
        "Business",
        (
            "startup",
            "funding",
            "acquire",
            "ipo",
            "layoff",
            "revenue",
            "ceo",
            "market",
            "company",
            "billion",
        ),
    ),
    ("Design", ("design", "ui", "ux", "font", "typography", "css")),
]


_CORE = {"AI/ML", "DevOps/Cloud", "Programming", "Security"}


def _normalize(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9+#]+", " ", text.lower()) + " "


def _matches(keyword: str, text: str) -> bool:
    keyword = keyword.strip()
    if " " in keyword:
        return f" {keyword} " in text
    if len(keyword) >= 5:  # allow stems: "vulnerab" -> "vulnerability"
        return any(word.startswith(keyword) for word in text.split())
    return f" {keyword} " in text


class KeywordEnricher:
    """Deterministic, dependency-free heuristic used when no API key is configured."""

    name = "keyword-fallback"

    def __init__(self, interests: list[str]):
        self.interest_terms = {
            t for i in interests for t in _normalize(i.replace("/", " ")).split() if len(t) > 1
        }

    def enrich(self, stories: list[StoryInput]) -> list[StoryInsight]:
        return [self._one(s) for s in stories]

    def _one(self, story: StoryInput) -> StoryInsight:
        text = _normalize(f"{story.title} {story.domain or ''}")
        hits = {cat: [kw.strip() for kw in kws if _matches(kw, text)] for cat, kws in _RULES}
        category, matched = max(hits.items(), key=lambda kv: len(kv[1]))
        if not matched:
            category = "Other"
        interest_hits = sum(1 for t in self.interest_terms if _matches(t, text))
        relevance = 2 + 2 * interest_hits + (2 if category in _CORE else 0)
        tags = [kw for kws in hits.values() for kw in kws][:4]
        return StoryInsight(
            hn_id=story.hn_id,
            category=category,
            relevance=max(1, min(10, relevance)),
            summary=(
                f"Offline keyword match ({', '.join(tags)}) - set ANTHROPIC_API_KEY "
                "for AI summaries."
                if tags
                else "No keyword match - set ANTHROPIC_API_KEY for AI summaries."
            ),
            tags=tags or [category.lower().split("/")[0]],
        )


def build_enricher(api_key: str | None, model: str, interests: list[str]) -> Enricher:
    if api_key:
        return ClaudeEnricher(model, interests, anthropic.Anthropic(api_key=api_key))
    log.warning("ANTHROPIC_API_KEY not set - using offline keyword enricher")
    return KeywordEnricher(interests)
