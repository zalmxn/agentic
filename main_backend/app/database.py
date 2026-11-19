from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def create_sqlite_engine(database_url: str) -> Engine:
    connect_args = {}
    if database_url.startswith("sqlite"):
        connect_args = {
            "check_same_thread": False,
            "timeout": 15.0,
            "isolation_level": None,  # explicit transaction control for BEGIN IMMEDIATE
        }

    engine = create_engine(
        database_url,
        connect_args=connect_args,
        pool_pre_ping=True,
        future=True,
    )

    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL;")
                cursor.execute("PRAGMA foreign_keys=ON;")
                cursor.execute("PRAGMA busy_timeout=5000;")
                cursor.execute("PRAGMA synchronous=NORMAL;")
            finally:
                cursor.close()

    return engine


settings = get_settings()
engine = create_sqlite_engine(settings.database_url)
SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def immediate_transaction(db: Session) -> Generator[Session, None, None]:
    """
    Execute an atomic write transaction using SQLite's BEGIN IMMEDIATE.
    This acquires the RESERVED write lock at transaction start, serializing
    concurrent booking writes and preventing double-booking race conditions
    (SQLite's equivalent of PostgreSQL SELECT ... FOR UPDATE).
    """
    if db.in_transaction():
        db.rollback()
    dialect_name = db.bind.dialect.name if db.bind is not None else ""
    if dialect_name == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
