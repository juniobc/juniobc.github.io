ALTER TABLE employees ADD COLUMN IF NOT EXISTS fired_at TIMESTAMPTZ;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS fire_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE employees ADD COLUMN IF NOT EXISTS fired_by INTEGER REFERENCES employees(id);

-- O que cada funcionário realmente fez, gravado a partir das tools chamadas.
CREATE TABLE IF NOT EXISTS activity (
  id          BIGSERIAL PRIMARY KEY,
  employee_id INTEGER NOT NULL REFERENCES employees(id),
  kind        TEXT NOT NULL CHECK (kind IN ('pesquisando','escrevendo_produto','conversando','reunião','parado','contratando','demitindo','planejando')),
  text        TEXT NOT NULL DEFAULT '',
  target_id   INTEGER REFERENCES employees(id),
  started_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS activity_emp ON activity(employee_id, id DESC);
CREATE INDEX IF NOT EXISTS activity_recent ON activity(id DESC);
