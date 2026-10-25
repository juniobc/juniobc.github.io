"""Loop do motor: um funcionário por turno, em rodízio, em cadência constante."""
import logging
import time
from . import config, db, llm, agent, seed, pacing, plan

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
    seed.refresh_instructions(conn)
    plan.ensure(conn)
    pacing.record_isolation(conn)
    conn.execute("UPDATE employees SET model=%s WHERE model = ANY(%s)", (config.DEFAULT_ROTATION[0], list(config.AVOID)))
    log.info("limites efetivos: %s chamadas, %s tokens por dia", config.DAILY_CALL_LIMIT, config.DAILY_TOKEN_LIMIT)
    last = 0
    backoff = 0
    while True:
        me = pick(conn, last)
        errored = False
        done = None
        if me:
            last = me["id"]
            pacing.beat(conn, current=me["id"], delay=config.TICK_SECONDS)
            try:
                agent.run_turn(conn, me)
                done = me["id"]
                backoff = 0
            except llm.RateLimited:
                backoff = min(900, backoff * 2 or 60)
                log.warning("429 do provedor; esperando %ss (não conta como falha)", backoff)
            except Exception as e:
                errored = True
                log.exception("erro no turno de %s: %s", me["slug"], e)
        delay, reason = pacing.next_delay(conn, backoff, errored)
        pacing.beat(conn, last_turn=done, delay=delay, reason=reason)
        time.sleep(delay)


if __name__ == "__main__":
    main()
