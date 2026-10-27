import argparse
import asyncio
import logging

import uvicorn

from techpulse.api import build_pipeline
from techpulse.config import get_settings
from techpulse.db import Database


def main() -> None:
    parser = argparse.ArgumentParser(prog="techpulse", description="TechPulse automation")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Run the API, dashboard and scheduler")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("run-once", help="Run the pipeline once and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )

    if args.command == "serve":
        uvicorn.run("techpulse.main:app", host=args.host, port=args.port)
        return

    settings = get_settings()
    db = Database(settings.database_url)
    db.create_all()
    run = asyncio.run(build_pipeline(settings, db).run("cli"))
    print(
        f"Run #{run.id} {run.status}: scraped={run.scraped} new={run.new} "
        f"enriched={run.enriched} alerts={run.alerts}"
        + (f" error={run.error}" if run.error else "")
    )
    raise SystemExit(0 if run.status == "success" else 1)


if __name__ == "__main__":
    main()
