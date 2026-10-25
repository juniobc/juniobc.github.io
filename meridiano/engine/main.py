"""Motor: N trabalhadores em paralelo, cada um roda o turno de um funcionário por vez, em rodízio.
Nunca dois turnos do mesmo funcionário ao mesmo tempo. Um 429 do provedor pausa todos com recuo exponencial."""
import logging
import threading
import time

from . import config, db, llm, agent, seed, pacing, plan, secrets_store

log = logging.getLogger("meridiano")


class Engine:
    def __init__(self, workers=None):
        self.workers = max(1, workers or config.WORKERS)
        self.lock = threading.Lock()
        self.running = {}            # id do funcionário -> nome, de quem está em turno agora
        self.last = 0                # último id sorteado (rodízio)
        self.backoff = 0             # recuo do 429, compartilhado
        self.pause_until = 0.0
        self.reason = "nenhum"
        self.next_at = {}            # trabalhador -> quando tenta de novo
        self.stop = threading.Event()

    def claim(self, conn):
        """Próximo funcionário em rodízio que não está em turno. None se todos estão ocupados."""
        with self.lock:
            rows = conn.execute("SELECT * FROM employees WHERE active ORDER BY id").fetchall()
            ordered = [r for r in rows if r["id"] > self.last] + [r for r in rows if r["id"] <= self.last]
            for r in ordered:
                if r["id"] not in self.running:
                    self.running[r["id"]] = r["name"]
                    self.last = r["id"]
                    return r
            return None

    def release(self, emp_id):
        with self.lock:
            self.running.pop(emp_id, None)

    def step(self, conn, run=None):
        """Um turno: sorteia, roda, registra. Devolve (rodou, segundos até o próximo turno deste trabalhador, motivo)."""
        run = run or agent.run_turn
        now = time.time()
        if now < self.pause_until:                       # 429 em andamento: todos esperam
            return False, self.pause_until - now, "429"
        me = self.claim(conn)
        if not me:
            return False, 5, "nenhum"
        errored, done = False, None
        try:
            run(conn, me)
            done = me["id"]
            with self.lock:
                self.backoff = 0
        except llm.RateLimited:
            with self.lock:
                self.backoff = min(900, self.backoff * 2 or 60)
                self.pause_until = time.time() + self.backoff
            log.warning("429 do provedor; todos esperam %ss (não conta como falha)", self.backoff)
        except Exception as e:
            errored = True
            log.exception("erro no turno de %s: %s", me["slug"], e)
        finally:
            self.release(me["id"])
        if done:
            conn.execute("UPDATE engine_status SET turn_count=turn_count+1 WHERE id=1")
        with self.lock:
            backoff = self.backoff
        delay, reason = pacing.next_delay(conn, backoff, errored)
        self.reason = reason
        pacing.beat_turn(conn, done)
        return bool(done), delay, reason

    def worker(self, n):
        conn = db.connect()
        while not self.stop.is_set():
            ran, delay, reason = self.step(conn)
            self.next_at[n] = time.time() + delay
            self.stop.wait(delay)

    def snapshot(self):
        with self.lock:
            return sorted(self.running.values())

    def housekeeping(self, conn, once=False):
        """Batimento do motor, reverificação do isolamento (5 min) e do token (60 s)."""
        next_gate = time.time() + config.GATE_CHECK_SECONDS
        next_secrets = time.time() + secrets_store.REVERIFY_SECONDS
        while not self.stop.is_set():
            if time.time() >= next_gate:     # qualquer falha desliga as ferramentas
                pacing.record_isolation(conn)
                next_gate = time.time() + config.GATE_CHECK_SECONDS
            if time.time() >= next_secrets:  # token por arquivo: lê sem reiniciar e reverifica a cada 60 s
                secrets_store.refresh(conn)
                next_secrets = time.time() + secrets_store.REVERIFY_SECONDS
            nxt = min(self.next_at.values()) - time.time() if self.next_at else 5
            pacing.beat_workers(conn, self.workers, self.snapshot(), max(0, int(nxt)) if self.next_at else 5, self.reason)
            if once:
                return
            self.stop.wait(5)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    conn = db.connect()
    db.init_schema(conn)
    seed.seed(conn)
    seed.refresh_instructions(conn)
    plan.ensure(conn)
    secrets_store.refresh(conn)
    g = pacing.record_isolation(conn)
    log.info("ferramentas: %s", g["reason"])
    conn.execute("UPDATE employees SET model=%s WHERE model = ANY(%s)", (config.DEFAULT_ROTATION[0], list(config.AVOID)))
    log.info("limites efetivos: %s chamadas, %s tokens por dia; %s trabalhadores em paralelo", config.DAILY_CALL_LIMIT, config.DAILY_TOKEN_LIMIT, config.WORKERS)
    eng = Engine()
    for n in range(eng.workers):
        threading.Thread(target=eng.worker, args=(n,), daemon=True, name=f"trabalhador-{n}").start()
        time.sleep(2)   # escalona a largada para não bater no provedor todos juntos
    eng.housekeeping(conn)


if __name__ == "__main__":
    main()
