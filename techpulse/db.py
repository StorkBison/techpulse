from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        db_path = url.split("///", 1)[-1]
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url, pool_pre_ping=True)


class Database:
    """Thin wrapper so the app, CLI and tests can each own an engine."""

    def __init__(self, url: str):
        self.engine = make_engine(url)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        from techpulse import models  # noqa: F401  (register tables)

        Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        return self.session_factory()

    def session_dependency(self) -> Iterator[Session]:
        with self.session() as s:
            yield s
