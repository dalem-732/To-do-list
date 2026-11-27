import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

_engine: Engine | None = None
_Session: sessionmaker[Session] | None = None


class Base(DeclarativeBase):
    pass


def normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def make_engine(url: str) -> Engine:
    normalized = normalize_database_url(url)
    if normalized.startswith("sqlite"):
        kwargs: dict = {"connect_args": {"check_same_thread": False}}
        if ":memory:" in normalized:
            kwargs["poolclass"] = StaticPool
        return create_engine(normalized, **kwargs)
    return create_engine(normalized, pool_pre_ping=True)


def init_db(url: str | None = None) -> None:
    global _engine, _Session
    raw = url if url is not None else os.environ.get("DATABASE_URL")
    if not raw:
        raise RuntimeError("DATABASE_URL is not set")
    if _engine is not None:
        _engine.dispose()
    _engine = make_engine(raw)
    _Session = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    from app import models  # noqa: F401

    _run_migrations(_engine)
    _backfill_secrets()


def _run_migrations(engine: Engine) -> None:
    migrations = [
        ("001_current_schema", _migrate_current_schema),
        ("002_user_sessions", _migrate_user_sessions),
    ]
    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(741995201)"))
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version VARCHAR(64) PRIMARY KEY, applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
        )
        applied = {row[0] for row in connection.execute(text("SELECT version FROM schema_migrations"))}
        for version, migration in migrations:
            if version in applied:
                continue
            migration(connection)
            connection.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:version)"),
                {"version": version},
            )


def _migrate_current_schema(connection: Connection) -> None:
    Base.metadata.create_all(connection)
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    dialect = connection.dialect.name

    if "tasks" in tables:
        columns = {column["name"] for column in inspector.get_columns("tasks")}
        uuid_type = "UUID" if dialect == "postgresql" else "CHAR(32)"
        if "user_id" not in columns:
            connection.execute(text(f"ALTER TABLE tasks ADD COLUMN user_id {uuid_type}"))
        if "guest_id" not in columns:
            connection.execute(text(f"ALTER TABLE tasks ADD COLUMN guest_id {uuid_type}"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_tasks_user_id ON tasks (user_id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_tasks_guest_id ON tasks (guest_id)"))
        if dialect == "postgresql":
            connection.execute(text("ALTER TABLE tasks ALTER COLUMN title TYPE VARCHAR(2048)"))

    if "users" in tables:
        columns = {column["name"] for column in inspector.get_columns("users")}
        if "login_enc" not in columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN login_enc VARCHAR(512)"))
        if dialect == "postgresql":
            connection.execute(text("ALTER TABLE users ALTER COLUMN login TYPE VARCHAR(64)"))


def _migrate_user_sessions(connection: Connection) -> None:
    from app.models import UserSession

    UserSession.__table__.create(connection, checkfirst=True)
    inspector = inspect(connection)
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "session_version" not in columns:
        connection.execute(text("ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0"))


def _session_factory() -> sessionmaker[Session]:
    if _Session is None:
        init_db()
    if _Session is None:
        raise RuntimeError("Database is not initialized")
    return _Session


def _backfill_secrets() -> None:
    from sqlalchemy import or_, select

    from app.auth import normalize_login
    from app.crypto import encrypt_text, login_lookup
    from app.models import Task, User

    session = _session_factory()()
    try:
        changed = False
        for user in session.scalars(select(User).where(or_(User.login_enc.is_(None), User.login_enc == ""))).all():
            plain = user.login.strip()
            user.login_enc = encrypt_text(plain)
            user.login = login_lookup(normalize_login(plain))
            changed = True
        for task in session.scalars(select(Task).where(Task.title.not_like("enc1:%"))).all():
            task.title = encrypt_text(task.title)
            changed = True
        if changed:
            session.commit()
        else:
            session.rollback()
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    session = _session_factory()()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    yield from get_session()


def check_db() -> None:
    if _engine is None:
        init_db()
    if _engine is None:
        raise RuntimeError("Database is not initialized")
    with _engine.connect() as connection:
        connection.execute(text("SELECT 1"))
