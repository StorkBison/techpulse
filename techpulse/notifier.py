import logging

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from techpulse.models import Story

log = logging.getLogger(__name__)


def format_alert(story: Story) -> str:
    return (
        f":rotating_light: *{story.relevance}/10* - <{story.url}|{story.title}>\n"
        f"_{story.category}_ · {story.points} points · {story.comments} comments\n"
        f"> {story.summary}"
    )


class Notifier:
    """Sends high-relevance stories to Slack, or logs them when no webhook is set."""

    def __init__(self, slack_webhook_url: str | None = None):
        self.webhook = slack_webhook_url

    @property
    def channel(self) -> str:
        return "slack" if self.webhook else "log"

    def send(self, stories: list[Story]) -> int:
        for story in stories:
            text = format_alert(story)
            if self.webhook:
                self._post(text)
            else:
                log.info("ALERT %s", text.replace("\n", " | "))
        return len(stories)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
    def _post(self, text: str) -> None:
        httpx.post(self.webhook, json={"text": text}, timeout=10).raise_for_status()
