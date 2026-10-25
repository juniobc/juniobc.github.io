"""Loop do motor: um funcionário por tick, em rodízio."""
import logging
import time
from . import config, db, llm, agent, seed

log = logging.getLogger("meridiano")


def pick(conn, last_id):
    rows = conn.execute("SELECT * FROM employees WHERE active ORDER BY id").fetchall()
    for r in rows:
        if r["id"] > last_id:
            return r
    return rows[0] if rows else None


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    conn = db.connect()
    db.init_schema(conn)
    seed.seed(conn)
    last = 0
    while True:
        me = pick(conn, last)
        if me:
            last = me["id"]
            try:
                agent.run_turn(conn, me)
            except llm.BudgetExceeded as e:
                log.warning("%s", e)
                time.sleep(600)
            except Exception as e:
                log.exception("erro no turno de %s: %s", me["slug"], e)
        time.sleep(config.TICK_SECONDS)


if __name__ == "__main__":
    main()
