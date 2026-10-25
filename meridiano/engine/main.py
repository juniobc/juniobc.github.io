"""Motor: N trabalhadores em paralelo, cada um roda o turno de um funcionário por vez, em rodízio.
Nunca dois turnos do mesmo funcionário ao mesmo tempo. Um 429 do provedor pausa todos com recuo exponencial."""
import logging
import threading
import time

from . import config, db, llm, agent, owner_chat, seed, pacing, plan, secrets_store

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

    def dispatch_owner(self, conn, run=None, wait=False):
        """Mensagem do dono: turno imediato do destinatário (sem esperar o rodízio), até OWNER_TURNS_PER_DAY por dia.
        Nunca dois turnos do mesmo funcionário ao mesmo tempo; o que não coube no limite fica no contexto do próximo turno normal."""
        if time.time() < self.pause_until:
            return 0
        left = config.OWNER_TURNS_PER_DAY - owner_chat.extra_turns_today(conn)
        threads = []
        for emp, ids in owner_chat.dispatch_plan(conn):
            if left <= 0:
                break
            with self.lock:
                if emp["id"] in self.running:
                    continue                      # em turno agora: tenta de novo na próxima volta
                self.running[emp["id"]] = emp["name"]
            owner_chat.log_turn(conn, emp["id"], ids)
            left -= 1
            t = threading.Thread(target=self._extra_turn, args=(emp, run or agent.run_turn), daemon=True, name=f"dono-{emp['slug']}")
            t.start()
            threads.append(t)
        if wait:
            for t in threads:
                t.join()
        return len(threads)

    def _extra_turn(self, emp, run):
        conn = db.connect()
        try:
            run(conn, emp)
            conn.execute("UPDATE engine_status SET turn_count=turn_count+1 WHERE id=1")
            pacing.beat_turn(conn, emp["id"])
        except llm.RateLimited:
            with self.lock:
                self.backoff = min(900, self.backoff * 2 or 60)
                self.pause_until = time.time() + self.backoff
            log.warning("429 do provedor no turno do dono; todos esperam %ss", self.backoff)
        except Exception as e:
            log.exception("erro no turno do dono de %s: %s", emp["slug"], e)
        finally:
            self.release(emp["id"])
            conn.close()

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
            try:
                self.dispatch_owner(conn)     # mensagens do dono: turno imediato
            except Exception as e:
                log.exception("erro ao despachar mensagem do dono: %s", e)
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
