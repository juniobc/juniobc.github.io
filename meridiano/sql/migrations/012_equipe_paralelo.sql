-- Equipe: entrega mensurável por funcionário, revisões de equipe e turnos em paralelo.
ALTER TABLE employees ADD COLUMN IF NOT EXISTS deliverable TEXT NOT NULL DEFAULT '';
ALTER TABLE engine_status ADD COLUMN IF NOT EXISTS turn_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE engine_status ADD COLUMN IF NOT EXISTS workers_total INTEGER NOT NULL DEFAULT 0;
ALTER TABLE engine_status ADD COLUMN IF NOT EXISTS workers_running INTEGER NOT NULL DEFAULT 0;
ALTER TABLE engine_status ADD COLUMN IF NOT EXISTS running_names TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS team_reviews (
  id          SERIAL PRIMARY KEY,
  turn_no     INTEGER NOT NULL,
  by_id       INTEGER REFERENCES employees(id),
  headcount   INTEGER NOT NULL,
  open_tasks  INTEGER NOT NULL,
  summary     TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
