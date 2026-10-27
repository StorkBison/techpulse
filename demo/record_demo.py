"""Record a narrated walkthrough video of TechPulse.

    python demo/record_demo.py            # -> demo/out/techpulse-demo.mp4

Starts the app on a fresh database, synthesises narration (edge-tts), drives a real
browser through slides + the live dashboard with Playwright's video recorder, then
muxes narration onto the recording with ffmpeg (bundled by imageio-ffmpeg).
"""

import asyncio
import contextlib
import hashlib
import os
import re
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import edge_tts
import httpx
import imageio_ffmpeg
from playwright.async_api import Page, async_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "demo" / "out"
SLIDES = (ROOT / "demo" / "slides.html").as_uri()
PORT = 8765
APP = f"http://127.0.0.1:{PORT}"
VOICE = os.getenv("DEMO_VOICE", "en-US-AndrewNeural")
VIEWPORT = {"width": 1600, "height": 900}
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
PY = sys.executable

# Injected into every page: a visible cursor with click ripple, and a subtitle bar.
OVERLAY_JS = """
(() => {
  if (window.__overlay) return; window.__overlay = true;
  const css = `
    #__cursor{position:fixed;left:0;top:0;width:26px;height:26px;z-index:2147483647;pointer-events:none;
      transform:translate(-3px,-2px);transition:transform .02s;filter:drop-shadow(0 2px 3px rgba(0,0,0,.5))}
    .__ripple{position:fixed;width:34px;height:34px;margin:-17px 0 0 -17px;border-radius:50%;
      border:3px solid #3987e5;z-index:2147483646;pointer-events:none;animation:__rp .5s ease-out forwards}
    @keyframes __rp{from{opacity:1;transform:scale(.3)}to{opacity:0;transform:scale(1.6)}}
    #__caption{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);max-width:1250px;width:max-content;
      padding:10px 22px;border-radius:10px;background:rgba(10,10,10,.82);color:#fff;z-index:2147483645;
      font:500 24px/1.4 "Segoe UI",system-ui,sans-serif;text-align:center;pointer-events:none;
      transition:opacity .25s;opacity:0}`;
  const add = () => {
    const s = document.createElement('style'); s.textContent = css; document.head.appendChild(s);
    const c = document.createElement('div'); c.id = '__cursor';
    c.innerHTML = '<svg viewBox="0 0 24 24" width="26" height="26"><path d="M3 2l7 19 2.6-7.4L20 11z" fill="#fff" stroke="#111" stroke-width="1.5" stroke-linejoin="round"/></svg>';
    const p = window.__lastPos || {x: 800, y: 450};
    c.style.left = p.x + 'px'; c.style.top = p.y + 'px';
    document.body.appendChild(c);
    const cap = document.createElement('div'); cap.id = '__caption'; document.body.appendChild(cap);
    addEventListener('mousemove', e => { c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; }, true);
    addEventListener('mousedown', e => {
      const r = document.createElement('div'); r.className = '__ripple';
      r.style.left = e.clientX + 'px'; r.style.top = e.clientY + 'px';
      document.body.appendChild(r); setTimeout(() => r.remove(), 600);
    }, true);
  };
  document.readyState === 'loading' ? addEventListener('DOMContentLoaded', add) : add();
  window.__caption = (t) => { const el = document.getElementById('__caption'); if (!el) return;
    if (t) { el.textContent = t; el.style.opacity = 1; } else { el.style.opacity = 0; } };
})();
"""


@dataclass
class Scene:
    name: str
    narration: str
    action: Callable[[Page], Awaitable[None]]
    audio: Path | None = None
    duration: float = 0.0


# --- Browser helpers ---------------------------------------------------------

_pos = {"x": 800.0, "y": 450.0}


async def move(page: Page, x: float, y: float, steps: int = 28) -> None:
    await page.mouse.move(x, y, steps=steps)
    _pos.update(x=x, y=y)
    await page.evaluate("p => window.__lastPos = p", _pos)


async def hover(page: Page, selector: str, pause: float = 0.6) -> None:
    box = await page.locator(selector).first.bounding_box()
    if box:
        await move(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    await asyncio.sleep(pause)


async def click(page: Page, selector: str, pause: float = 0.5) -> None:
    await hover(page, selector, 0.25)
    await page.mouse.click(_pos["x"], _pos["y"])
    await asyncio.sleep(pause)


async def goto(page: Page, url: str) -> None:
    await page.goto(url, wait_until="networkidle")
    await page.evaluate("p => { window.__lastPos = p }", _pos)
    await page.mouse.move(_pos["x"], _pos["y"])


async def slide(page: Page, sid: str) -> None:
    if page.url.startswith(SLIDES.split("#")[0]):
        await page.evaluate(f"location.hash = '{sid}'")
    else:
        await goto(page, f"{SLIDES}#{sid}")
    await asyncio.sleep(0.6)


async def smooth_scroll(page: Page, dy: int, steps: int = 30, delay: float = 0.04) -> None:
    for _ in range(steps):
        await page.mouse.wheel(0, dy / steps)
        await asyncio.sleep(delay)


async def wait_pipeline_idle(timeout: float = 150) -> dict:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            stats = (await client.get(f"{APP}/api/stats")).json()
            if not stats["pipeline_busy"] and stats["last_run"]:
                return stats
            await asyncio.sleep(1)
    raise TimeoutError("pipeline run did not finish")


# --- Scene actions -------------------------------------------------------------


async def a_title(page):
    await slide(page, "title")
    await move(page, 1200, 700, steps=60)


async def a_arch(page):
    await slide(page, "arch")
    for i in range(1, 6):
        await hover(page, f".arch .node:nth-child({i})", 2.6)
    for i in range(1, 4):
        await hover(page, f".under .node:nth-child({i})", 1.6)


async def a_dashboard(page):
    await goto(page, APP)
    await asyncio.sleep(1.5)
    for sel in (".meta .chip:nth-child(1)", ".meta .chip:nth-child(2)", ".meta .chip:nth-child(3)"):
        await hover(page, sel, 1.8)
    await hover(page, ".kpis", 1.5)
    await hover(page, "#run-btn", 0.5)


async def a_run(page):
    await click(page, "#run-btn", 1.0)
    await hover(page, "#pipeline-status", 2.0)
    await wait_pipeline_idle()
    await asyncio.sleep(2.5)  # let the dashboard poll and render


async def a_results(page):
    await hover(page, "#k-total", 1.2)
    await hover(page, "#k-avg", 1.2)
    await hover(page, ".story:nth-child(1) .score", 1.5)
    await hover(page, ".story:nth-child(1) .summary", 1.5)
    await move(page, 600, 600)
    await smooth_scroll(page, 900, steps=40)
    await asyncio.sleep(1.5)
    await smooth_scroll(page, -900, steps=30)


async def a_filters(page):
    await click(page, "#f-q")
    await page.locator("#f-q").press_sequentially("AI", delay=180)
    await asyncio.sleep(2.2)
    await page.locator("#f-q").fill("")
    await page.locator("#f-q").dispatch_event("input")
    await hover(page, "#f-category", 0.3)
    await page.select_option("#f-category", "AI/ML")
    await asyncio.sleep(2.2)
    await page.select_option("#f-category", "")
    await hover(page, "#f-min", 0.3)
    await page.select_option("#f-min", "5")
    await asyncio.sleep(2.0)
    await hover(page, "#f-sort", 0.3)
    await page.select_option("#f-sort", "points")
    await asyncio.sleep(2.2)
    await page.select_option("#f-min", "1")
    await page.select_option("#f-sort", "relevance")


async def a_side(page):
    for i in range(1, 4):
        await hover(page, f"#cat-chart .hbar:nth-child({i}) .fill", 1.6)
    await hover(page, "#runs tr:nth-child(1)", 2.0)


async def a_docs(page):
    await goto(page, f"{APP}/docs")
    await asyncio.sleep(1.5)
    await click(page, "#operations-stories-list_stories_api_stories_get .opblock-summary", 1.2)
    await click(page, "#operations-stories-list_stories_api_stories_get .try-out__btn", 1.0)
    field = page.locator(
        "#operations-stories-list_stories_api_stories_get tr[data-param-name='min_relevance'] input"
    )
    await hover(
        page,
        "#operations-stories-list_stories_api_stories_get tr[data-param-name='min_relevance'] input",
        0.3,
    )
    await field.fill("")
    await field.press_sequentially("6", delay=150)
    await page.locator(
        "#operations-stories-list_stories_api_stories_get tr[data-param-name='limit'] input"
    ).fill("5")
    await click(page, "#operations-stories-list_stories_api_stories_get .execute", 1.5)
    await move(page, 800, 600)
    await smooth_scroll(page, 700, steps=40, delay=0.05)


PYTEST_TEXT = ""


def a_slide(sid: str, hovers: tuple[str, ...] = ()):
    async def _action(page):
        await slide(page, sid)
        if sid == "tests":
            await page.evaluate(
                "t => { const el = document.getElementById('pytest-output'); "
                "el.innerHTML = t.replace(/&/g,'&amp;').replace(/</g,'&lt;')"
                ".replace(/PASSED/g, '<span class=pass>PASSED</span>'); }",
                PYTEST_TEXT,
            )
        for sel in hovers:
            await hover(page, sel, 2.5)

    return _action


# --- Pipeline ------------------------------------------------------------------


def build_scenes(enricher: str, test_count: int) -> list[Scene]:
    if enricher.startswith("claude"):
        enricher_note = (
            "Enrichment is running on Claude, using the model configured in the environment."
        )
    else:
        enricher_note = (
            "For this recording no Claude API key is configured, so the app automatically "
            "falls back to its offline keyword enricher. With a key set, the same pipeline uses Claude."
        )
    return [
        Scene("title",
              "This is TechPulse, an automation project that covers the whole stack: browser automation, "
              "AI integration, a back-end API, data storage and DevOps. Hacker News publishes hundreds "
              "of stories a day, and most aren't relevant to me. TechPulse watches it on a schedule, "
              "scores every story against my interests, and alerts me only when something matters.",
              a_title),
        Scene("architecture",
              "Here's the architecture. A scheduler triggers the pipeline every thirty minutes, or you can "
              "start it on demand through the API. Playwright drives a headless browser to scrape the "
              "front pages. Results are upserted into PostgreSQL, so re-runs never create duplicates. "
              "New stories go to Claude in batches to be classified, scored and summarised, and anything "
              "above the alert threshold is posted to Slack, exactly once. Everything is served by a "
              "FastAPI back end and a live dashboard.",
              a_arch),
        Scene("dashboard",
              "This is the dashboard, running locally against an empty database. The chips at the top "
              f"show the live configuration: the enricher, the scraper source, and the alert channel. "
              f"{enricher_note}",
              a_dashboard),
        Scene("run",
              "I'll click Run pipeline now. The API starts the run as a background task and returns "
              "immediately. Behind the scenes, Playwright launches a headless browser, opens two pages "
              "of Hacker News, and extracts the rank, title, points, author and comment count for every "
              "story. If a page times out, it retries with exponential backoff. Meanwhile the dashboard "
              "polls the API, so the status updates live.",
              a_run),
        Scene("results",
              "And it's done. Every story has been scraped, stored and enriched. The tiles show the totals "
              "and the average relevance. Each story card has its score, category, a summary, tags, "
              "points and comments. Stories above the threshold are marked as alerted, which means they "
              "were pushed to the alert channel. In this local run, that's the application log.",
              a_results),
        Scene("filters",
              "The filters use the same REST API. I can search, narrow to one category, set a minimum "
              "relevance, or sort by points to see what's trending.",
              a_filters),
        Scene("side",
              "On the right is a breakdown by category, and the run history. Every run is recorded with "
              "its trigger, status, counts and duration, so a failure shows up here instead of "
              "disappearing silently.",
              a_side),
        Scene("docs",
              "Because it's built on FastAPI, the API documents itself. I'll call the stories endpoint "
              "with a minimum relevance of six and run it. The response is typed, validated JSON, the "
              "same data the dashboard uses, ready for any other tool.",
              a_docs),
        Scene("code-scraper",
              "Now the code. The scraper runs one extraction script inside the page that turns each "
              "story row into a typed record. Page loads are retried with exponential backoff, and "
              "images, fonts and stylesheets are blocked to save time. The same code also runs against "
              "a saved HTML page, so the tests never depend on the live site.",
              a_slide("code-scraper", (".cols pre", ".points li:nth-child(4)"))),
        Scene("code-ai",
              "For enrichment, the output is defined as Pydantic models: a category from a fixed list, "
              "a relevance score from one to ten, a summary, and tags. Claude's structured outputs "
              "return data that already matches that schema, so there's no fragile JSON parsing. "
              "Stories go in batches to keep costs down, refusals are handled, and any IDs the model "
              "invents are filtered out.",
              a_slide("code-ai", (".cols pre", ".points li:nth-child(1)", ".points li:nth-child(3)"))),
        Scene("code-pipeline",
              "The pipeline is built to run unattended. A lock stops the scheduler and the API from "
              "running at the same time. Upserts are idempotent. If the AI step fails, stories stay "
              "pending and are retried on the next run, and each story is alerted only once.",
              a_slide("code-pipeline", (".cols pre", ".points li:nth-child(3)"))),
        Scene("tests",
              f"For quality, there are {test_count} automated tests. They cover the enrichers with a "
              "mocked Claude client, the pipeline's failure handling and concurrency, the API, and a real "
              "browser run against the saved page. GitHub Actions runs linting, the tests, a Docker "
              "build and a smoke test of the running container.",
              a_slide("tests", ("#pytest-output",))),
        Scene("docker",
              "Deployment is one command. Docker Compose starts the app and PostgreSQL with health "
              "checks, the container runs as a non-root user with the browser built in, and all "
              "configuration comes from validated environment variables.",
              a_slide("docker", (".cols pre",))),
        Scene("outro",
              "That's TechPulse: scheduled browser automation, AI enrichment with typed outputs, a "
              "clean API and dashboard, and a tested, containerised delivery pipeline. It's easy to "
              "extend with more sources or a daily digest. Thanks for watching.",
              a_slide("outro")),
    ]  # fmt: skip


def media_duration(path: Path) -> float:
    out = subprocess.run([FFMPEG, "-i", str(path)], capture_output=True, text=True).stderr
    h, m, s = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out).groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def _audio_ok(scene: Scene) -> bool:
    """The TTS stream can end early without an error; reject clips far shorter than the text."""
    if not (scene.audio.exists() and scene.audio.stat().st_size > 1000):
        return False
    expected = len(scene.narration.split()) / 2.9  # ~175 wpm upper bound
    if media_duration(scene.audio) < 0.75 * expected:
        print(f"  truncated clip for {scene.name}, regenerating")
        scene.audio.unlink()
        return False
    return True


async def synthesize(scenes: list[Scene]) -> None:
    """Sequential with retries (the TTS service throttles bursts); cached by text hash."""
    for i, scene in enumerate(scenes):
        digest = hashlib.sha1(f"{VOICE}|{scene.narration}".encode()).hexdigest()[:10]
        scene.audio = OUT / "voice" / f"{i:02d}_{scene.name}_{digest}.mp3"
        scene.audio.parent.mkdir(exist_ok=True)
        attempt = 0
        while not _audio_ok(scene):
            attempt += 1
            if attempt > 5:
                raise RuntimeError(f"TTS failed for scene {scene.name}")
            try:
                tts = edge_tts.Communicate(scene.narration, VOICE, rate="+4%")
                await tts.save(str(scene.audio))
            except Exception as exc:
                print(f"  TTS retry {attempt} for {scene.name}: {exc}")
                scene.audio.unlink(missing_ok=True)
                await asyncio.sleep(2 * attempt)
        scene.duration = media_duration(scene.audio)
    total = sum(s.duration for s in scenes)
    print(f"Narration: {len(scenes)} clips, {total:.0f}s")


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


async def run_captions(page: Page, scene: Scene) -> None:
    parts = sentences(scene.narration)
    total_chars = sum(len(p) for p in parts)
    for part in parts:
        with contextlib.suppress(Exception):  # page mid-navigation; next sentence will land
            await page.evaluate("t => window.__caption && window.__caption(t)", part)
        await asyncio.sleep(scene.duration * len(part) / total_chars)
    with contextlib.suppress(Exception):
        await page.evaluate("() => window.__caption && window.__caption('')")


def start_server(db_path: Path) -> subprocess.Popen:
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
        "SCRAPER_PAGES": "2",
        "SCRAPER_TIMEOUT_MS": "30000",
        "ALERT_THRESHOLD": os.getenv("ALERT_THRESHOLD", "7"),
        "SCHEDULE_INTERVAL_MINUTES": "30",
        "BROWSER_CHANNEL": os.getenv("BROWSER_CHANNEL", "chrome"),
    }
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "techpulse.main:app", "--port", str(PORT), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=(OUT / "server.log").open("w"), stderr=subprocess.STDOUT,
    )  # fmt: skip
    for _ in range(60):
        try:
            if httpx.get(f"{APP}/health").status_code == 200:
                return proc
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    proc.kill()
    raise RuntimeError("server did not start - see demo/out/server.log")


def pytest_output() -> tuple[str, int]:
    res = subprocess.run(
        [PY, "-m", "pytest", "-v", "-p", "no:cacheprovider", "--color=no"],
        cwd=ROOT, capture_output=True, text=True,
        env={**os.environ, "BROWSER_CHANNEL": os.getenv("BROWSER_CHANNEL", "chrome")},
    )  # fmt: skip
    lines = [ln for ln in res.stdout.splitlines() if "PASSED" in ln or "FAILED" in ln]
    summary = res.stdout.strip().splitlines()[-1]
    shown = [re.sub(r"\s+\[\s*\d+%\]$", "", ln) for ln in lines]
    return "$ pytest -v\n" + "\n".join(shown) + "\n\n" + summary, len(lines)


async def record(scenes: list[Scene]) -> tuple[Path, list[float]]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel=os.getenv("BROWSER_CHANNEL", "chrome"))
        context = await browser.new_context(
            viewport=VIEWPORT, record_video_dir=str(OUT / "raw"), record_video_size=VIEWPORT,
            color_scheme="dark",
        )  # fmt: skip
        await context.add_init_script(OVERLAY_JS)
        page = await context.new_page()
        t0 = time.monotonic()
        await goto(page, f"{SLIDES}#title")
        await asyncio.sleep(0.8)

        starts = []
        for scene in scenes:
            start = time.monotonic() - t0
            starts.append(start)
            print(f"  [{start:6.1f}s] {scene.name} ({scene.duration:.1f}s narration)")
            captions = asyncio.create_task(run_captions(page, scene))
            await asyncio.gather(scene.action(page), asyncio.sleep(scene.duration))
            await captions
            await asyncio.sleep(0.7)
        await asyncio.sleep(1.5)
        video_path = Path(await page.video.path())
        await context.close()
        await browser.close()
    return video_path, starts


def mux(video: Path, scenes: list[Scene], starts: list[float], out: Path) -> None:
    args = [FFMPEG, "-y", "-i", str(video)]
    for s in scenes:
        args += ["-i", str(s.audio)]
    delays = "".join(
        f"[{i + 1}:a]adelay={int(st * 1000)}:all=1[a{i}];" for i, st in enumerate(starts)
    )
    mix = "".join(f"[a{i}]" for i in range(len(scenes)))
    filt = (
        f"{delays}{mix}amix=inputs={len(scenes)}:normalize=0,loudnorm=I=-16:TP=-1.5[aout];"
        "[0:v]scale=1920:1080:flags=lanczos,fps=30,format=yuv420p[vout]"
    )
    args += [
        "-filter_complex", filt, "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "160k", "-shortest", str(out),
    ]  # fmt: skip
    subprocess.run(args, check=True, capture_output=True)


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for f in (OUT / "raw").glob("*.webm"):
        f.unlink()
    db_path = OUT / "demo.db"
    db_path.unlink(missing_ok=True)

    print("Running test suite for the tests slide...")
    global PYTEST_TEXT
    PYTEST_TEXT, test_count = pytest_output()
    print(f"  {test_count} tests")

    print("Starting app...")
    server = start_server(db_path)
    try:
        enricher = httpx.get(f"{APP}/api/stats").json()["enricher"]
        print(f"  enricher: {enricher}")
        scenes = build_scenes(enricher, test_count)
        print("Synthesising narration...")
        await synthesize(scenes)
        print("Recording...")
        video, starts = await record(scenes)
    finally:
        server.terminate()

    out = OUT / "techpulse-demo.mp4"
    print("Muxing...")
    mux(video, scenes, starts, out)
    print(f"Done: {out} ({media_duration(out):.0f}s)")


if __name__ == "__main__":
    asyncio.run(main())
