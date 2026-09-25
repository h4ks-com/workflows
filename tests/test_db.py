from pathlib import Path

from sqlalchemy import Column
from sqlalchemy import Integer
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import text

from workflows.db import _add_column_sql
from workflows.db import _add_missing_columns
from workflows.db import connect


def test_add_column_sql_keeps_the_server_default() -> None:
    engine = create_engine("sqlite://")
    column = Column("tries", Integer, server_default="0")
    assert (
        _add_column_sql(engine, "jobs", column)
        == "ALTER TABLE jobs ADD COLUMN tries INTEGER DEFAULT '0'"
    )


def test_add_missing_columns_adds_nullable_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE users (id INTEGER PRIMARY KEY, logto_sub TEXT, username TEXT)")
        )

    _add_missing_columns(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("users")}
    assert {"free_credits", "free_day", "paid_credits", "created_at"} <= columns
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO users (id, logto_sub, username) VALUES (1, 's', 'a')"))
    engine.dispose()


def test_add_missing_columns_is_a_no_op_when_up_to_date(tmp_path: Path) -> None:
    engine = connect(f"sqlite:///{tmp_path}/db.db")

    _add_missing_columns(engine)

    assert "removed_at" in {column["name"] for column in inspect(engine).get_columns("jobs")}
    engine.dispose()


def test_connect_is_idempotent(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path}/db.db"
    connect(url).dispose()
    connect(url).dispose()
