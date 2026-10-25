-- Conversa do dono com a empresa (texto e voz no navegador). Aditiva.
CREATE TABLE IF NOT EXISTS owner_messages (
  id          BIGSERIAL PRIMARY KEY,
  direction   TEXT NOT NULL CHECK (direction IN ('dono','empresa')),
  employee_id INTEGER REFERENCES employees(id),   -- dono: destinatário (NULL = todos); empresa: quem respondeu
  body        TEXT NOT NULL,
  reply_to    BIGINT REFERENCES owner_messages(id),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS owner_messages_recent ON owner_messages(id DESC);

-- Quem já recebeu cada mensagem do dono no contexto de um turno concluído.
CREATE TABLE IF NOT EXISTS owner_message_reads (
  message_id  BIGINT NOT NULL REFERENCES owner_messages(id) ON DELETE CASCADE,
  employee_id INTEGER NOT NULL REFERENCES employees(id),
  at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (message_id, employee_id)
);

-- Turnos extras disparados por mensagem do dono (limite diário).
CREATE TABLE IF NOT EXISTS owner_turns (
  id          BIGSERIAL PRIMARY KEY,
  employee_id INTEGER NOT NULL REFERENCES employees(id),
  message_id  BIGINT NOT NULL REFERENCES owner_messages(id) ON DELETE CASCADE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (message_id, employee_id)
);
