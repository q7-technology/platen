"""Platen offers Postgres, SQL Server, MySQL and SQLite. They do not all spell
"give me the first N rows" the same way."""

from __future__ import annotations

import pytest

from app.datasources import limited


@pytest.mark.parametrize("dialect", ["postgresql", "mysql", "sqlite"])
def test_most_databases_take_a_limit_on_the_end(dialect):
    sql = limited("select * from cartons", 25, dialect)

    assert sql.lower().endswith("limit 25")
    assert "select * from (select * from cartons)" in sql.lower()


def test_sql_server_puts_it_in_front_instead():
    """SQL Server has no LIMIT. A query preview there would have failed on
    every single template."""
    sql = limited("select * from cartons", 25, "mssql")

    assert "limit" not in sql.lower()
    assert sql.lower().startswith("select top 25 *")
    assert "(select * from cartons)" in sql.lower()


def test_no_rows_at_all_works_on_both_shapes():
    assert limited("select 1", 0, "postgresql").lower().endswith("limit 0")
    assert limited("select 1", 0, "mssql").lower().startswith("select top 0")


def test_the_limit_is_a_number_and_cannot_be_anything_else():
    with pytest.raises((ValueError, TypeError)):
        limited("select 1", "25; drop table cartons", "postgresql")
