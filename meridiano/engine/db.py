import psycopg
from psycopg.rows import dict_row
from . import config


def connect():
    return psycopg.connect(config.database_url(), row_factory=dict_row, autocommit=True,
                           options="-c client_encoding=UTF8")


def init_schema(conn):
    import pathlib
    sql = (pathlib.Path(__file__).parent.parent / "sql" / "schema.sql").read_text()
    conn.execute(sql)


def cash_cents(conn):
    r = conn.execute(
        "SELECT COALESCE(SUM(CASE kind WHEN 'receita' THEN amount_cents ELSE -amount_cents END),0) AS c FROM ledger"
    ).fetchone()
    return int(r["c"])
