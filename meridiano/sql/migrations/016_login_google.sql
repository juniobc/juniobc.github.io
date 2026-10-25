-- Login do dono com Google (OIDC), sessões de administrador e registro de tentativas. Aditiva.
CREATE TABLE IF NOT EXISTS oauth_states (
  state      TEXT PRIMARY KEY,
  nonce      TEXT NOT NULL,
  verifier   TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS admin_sessions (
  id         BIGSERIAL PRIMARY KEY,
  token_hash TEXT UNIQUE NOT NULL,          -- só o hash do cookie fica no banco
  csrf       TEXT NOT NULL,                 -- ligado à sessão; vai no cabeçalho X-CSRF
  email      TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_events (
  id     BIGSERIAL PRIMARY KEY,
  at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  kind   TEXT NOT NULL,                     -- login_ok | login_negado | state_invalido | token_invalido | sessao_encerrada | recuperacao_falha | configuracao_google ...
  email  TEXT,
  detail TEXT NOT NULL DEFAULT ''
);
