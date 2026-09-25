from datetime import UTC
from datetime import date
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import JsonValue
from sqlalchemy import JSON
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import DefaultClause
from sqlalchemy import Dialect
from sqlalchemy import Engine
from sqlalchemy import ForeignKey
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import make_url
from sqlalchemy import text
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import Session
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import registry
from sqlalchemy.orm import relationship
from sqlalchemy.orm import sessionmaker
from sqlalchemy.types import TypeDecorator

type JsonObject = dict[str, JsonValue]


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, _dialect: Dialect) -> datetime | None:
        return value.astimezone(UTC).replace(tzinfo=None) if value else None

    def process_result_value(self, value: datetime | None, _dialect: Dialect) -> datetime | None:
        return value.replace(tzinfo=UTC) if value else None


class JobStatus(StrEnum):
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LedgerKind(StrEnum):
    FREE_GRANT = "free_grant"
    TOPUP = "topup"
    RESERVE = "reserve"
    CAPTURE = "capture"
    REFUND = "refund"
    ADMIN_ADJUST = "admin_adjust"


class Base(DeclarativeBase):
    registry = registry(type_annotation_map={datetime: UTCDateTime, JsonObject: JSON})


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    logto_sub: Mapped[str] = mapped_column(unique=True)
    username: Mapped[str] = mapped_column(unique=True)
    free_credits: Mapped[int] = mapped_column(default=0)
    free_day: Mapped[date | None]
    paid_credits: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ExternalIdentity(Base):
    __tablename__ = "external_identities"

    id: Mapped[int] = mapped_column(primary_key=True)
    identity: Mapped[str] = mapped_column(unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    user: Mapped[User] = relationship()


class LinkRequest(Base):
    __tablename__ = "link_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    identity: Mapped[str]
    token_hash: Mapped[str] = mapped_column(unique=True)
    used: Mapped[bool] = mapped_column(default=False)
    expires_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(index=True)
    params: Mapped[JsonObject]
    status: Mapped[str] = mapped_column(index=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    quote: Mapped[int]
    estimate_seconds: Mapped[int]
    reserved_free: Mapped[int] = mapped_column(default=0)
    reserved_paid: Mapped[int] = mapped_column(default=0)
    identity: Mapped[str | None]
    webhook: Mapped[JsonObject | None]
    progress_step: Mapped[str | None]
    progress_done: Mapped[int | None]
    progress_total: Mapped[int | None]
    result: Mapped[JsonObject | None]
    error: Mapped[str | None]
    callback_token_hash: Mapped[str | None]
    confirm_token_hash: Mapped[str | None] = mapped_column(unique=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    queued_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    last_event_at: Mapped[datetime | None]
    removed_at: Mapped[datetime | None]

    owner: Mapped[User | None] = relationship(lazy="joined")
    events: Mapped[list[JobEvent]] = relationship(order_by="JobEvent.id")


class JobEvent(Base):
    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    kind: Mapped[str]
    data: Mapped[JsonObject]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(unique=True)
    signing_key: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str]
    free_delta: Mapped[int] = mapped_column(default=0)
    paid_delta: Mapped[int] = mapped_column(default=0)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"))
    beans_txn_id: Mapped[str | None] = mapped_column(unique=True)
    note: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


def _add_missing_columns(engine: Engine) -> None:
    # create_all only creates tables that don't exist yet, so a column added to a model
    # after the production database was created needs its own additive migration.
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table in Base.metadata.tables.values():
            if table.name not in existing_tables:
                continue
            existing_columns = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                connection.execute(text(_add_column_sql(engine, table.name, column)))


def _add_column_sql[T](engine: Engine, table_name: str, column: Column[T]) -> str:
    column_type = column.type.compile(dialect=engine.dialect)
    sql = f"ALTER TABLE {table_name} ADD COLUMN {column.name} {column_type}"
    if isinstance(column.server_default, DefaultClause):
        default = column.server_default.arg
        sql += f" DEFAULT '{default}'" if isinstance(default, str) else f" DEFAULT {default}"
    return sql


def connect(database_url: str) -> Engine:
    database = make_url(database_url).database
    if database:
        Path(database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    return engine


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
