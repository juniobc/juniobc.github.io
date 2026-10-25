-- Pedidos e pagamentos. Nenhum dado pessoal do comprador é guardado (LGPD): só o id do pagamento e os valores.
CREATE TABLE IF NOT EXISTS orders (
  id            SERIAL PRIMARY KEY,
  token         TEXT UNIQUE NOT NULL,                 -- aleatório, é a chave da página do pedido (e do download, na etapa seguinte)
  product_id    INTEGER NOT NULL REFERENCES products(id),
  amount_cents  BIGINT NOT NULL CHECK (amount_cents > 0),   -- preço no momento da compra
  status        TEXT NOT NULL DEFAULT 'criado' CHECK (status IN ('criado','aguardando','pago','recusado','cancelado','reembolsado','chargeback')),
  preference_id TEXT,
  payment_id    TEXT,
  test          BOOLEAN NOT NULL DEFAULT FALSE,       -- pagamento de teste: nunca entra no caixa
  gross_cents   BIGINT,
  fee_cents     BIGINT,
  net_cents     BIGINT,
  refunded_cents BIGINT NOT NULL DEFAULT 0,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  paid_at       TIMESTAMPTZ,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS orders_status ON orders(status, created_at);

-- Auditoria de tudo que o Mercado Pago avisou e do que o sistema decidiu.
CREATE TABLE IF NOT EXISTS payment_events (
  id          BIGSERIAL PRIMARY KEY,
  payment_id  TEXT NOT NULL,
  order_id    INTEGER REFERENCES orders(id),
  mp_status   TEXT NOT NULL DEFAULT '',
  outcome     TEXT NOT NULL,
  note        TEXT NOT NULL DEFAULT '',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS payment_events_pid ON payment_events(payment_id);
