-- Uma linha com o que o motor está fazendo, para o painel (/api/engine).
CREATE TABLE IF NOT EXISTS engine_status (
  id                   INTEGER PRIMARY KEY CHECK (id = 1),
  heartbeat_at         TIMESTAMPTZ,
  last_turn_at         TIMESTAMPTZ,
  last_turn_employee_id INTEGER REFERENCES employees(id),
  current_employee_id  INTEGER REFERENCES employees(id),
  next_turn_at         TIMESTAMPTZ,
  sleep_s              INTEGER NOT NULL DEFAULT 0,
  wait_reason          TEXT NOT NULL DEFAULT 'nenhum'
);
INSERT INTO engine_status(id) VALUES (1) ON CONFLICT DO NOTHING;
