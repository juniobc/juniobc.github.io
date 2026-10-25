"""Migrações SQL numeradas (sql/migrations/NNN_nome.sql), aplicadas uma vez, em ordem."""
import pathlib

DIR = pathlib.Path(__file__).parent.parent / "sql" / "migrations"
LOCK = 727001  # API e motor sobem juntos: uma só aplica por vez


def migrate(conn):
    conn.execute("SELECT pg_advisory_lock(%s)", (LOCK,))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
        applied = []
        for f in sorted(DIR.glob("*.sql")):
            if f.name in done:
                continue
            with conn.transaction():
                conn.execute(f.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations(name) VALUES (%s)", (f.name,))
            applied.append(f.name)
        return applied
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK,))


if __name__ == "__main__":
    from . import db
    print("migrações aplicadas:", migrate(db.connect()) or "nenhuma")
