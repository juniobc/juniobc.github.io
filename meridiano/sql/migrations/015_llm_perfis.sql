-- Perfis de LLM por funcionário, custo real por chamada e modo contingência. Aditiva.
CREATE TABLE IF NOT EXISTS llm_profiles (
  id           SERIAL PRIMARY KEY,
  label        TEXT UNIQUE NOT NULL,
  provider     TEXT NOT NULL CHECK (provider IN ('openai_compat','anthropic')),
  base_url     TEXT NOT NULL,
  model        TEXT NOT NULL,
  key_name     TEXT NOT NULL,                 -- arquivo ~/meridiano/secrets/llm-<key_name>; a chave nunca fica no banco
  price_in_per_m  NUMERIC(10,4) NOT NULL DEFAULT 0,   -- USD por milhão de tokens
  price_out_per_m NUMERIC(10,4) NOT NULL DEFAULT 0,
  supports_tools BOOLEAN NOT NULL DEFAULT TRUE,
  is_free      BOOLEAN NOT NULL DEFAULT FALSE,
  enabled      BOOLEAN NOT NULL DEFAULT FALSE,
  daily_usd_cap NUMERIC(10,2),
  notes        TEXT NOT NULL DEFAULT '',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS llm_profiles_endpoint ON llm_profiles(base_url, model);

CREATE TABLE IF NOT EXISTS llm_settings (
  id             INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  contingency    BOOLEAN NOT NULL DEFAULT TRUE,        -- ligado: só perfis grátis, mesmo que o agente esteja configurado num pago
  global_usd_cap NUMERIC(10,2) NOT NULL DEFAULT 2.00   -- teto de gasto por dia somando todos os perfis pagos
);
INSERT INTO llm_settings(id) VALUES (1) ON CONFLICT DO NOTHING;

ALTER TABLE employees ADD COLUMN IF NOT EXISTS llm_profile_id INTEGER REFERENCES llm_profiles(id);
ALTER TABLE usage_log ADD COLUMN IF NOT EXISTS profile_id INTEGER REFERENCES llm_profiles(id);
ALTER TABLE usage_log ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(12,6) NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS usage_profile_day ON usage_log(profile_id, created_at);

-- Os 8 modelos grátis da Nous já em uso (os dois que falham nos testes ficam desligados).
INSERT INTO llm_profiles(label,provider,base_url,model,key_name,is_free,enabled,notes) VALUES
 ('Nous · laguna-s-2.1 (grátis)','openai_compat','https://inference-api.nousresearch.com/v1','poolside/laguna-s-2.1:free','nous',true,true,''),
 ('Nous · laguna-xs-2.1 (grátis)','openai_compat','https://inference-api.nousresearch.com/v1','poolside/laguna-xs-2.1:free','nous',true,false,'gasta o limite só pensando e devolve vazio'),
 ('Nous · solar-mini4 (grátis)','openai_compat','https://inference-api.nousresearch.com/v1','upstage/solar-mini4:free','nous',true,true,''),
 ('Nous · longcat-2.0 (grátis)','openai_compat','https://inference-api.nousresearch.com/v1','meituan/longcat-2.0:free','nous',true,true,''),
 ('Nous · longcat-2.5-preview (grátis)','openai_compat','https://inference-api.nousresearch.com/v1','meituan/longcat-2.5-preview:free','nous',true,true,''),
 ('Nous · step-3.7-flash (grátis)','openai_compat','https://inference-api.nousresearch.com/v1','stepfun/step-3.7-flash:free','nous',true,false,'devolveu erro 400 num teste simples'),
 ('Nous · beam-1.0 (grátis)','openai_compat','https://inference-api.nousresearch.com/v1','reflection/beam-1.0-501b-a23b:free','nous',true,true,''),
 ('Nous · ornith-1.5 (grátis)','openai_compat','https://inference-api.nousresearch.com/v1','ornith/ornith-1.5-122b:free','nous',true,true,'')
ON CONFLICT DO NOTHING;

-- Pagos úteis, DESLIGADOS. Preço em USD por milhão de tokens (entrada/saída). Ligar exige chave, limite diário e sair do modo contingência.
INSERT INTO llm_profiles(label,provider,base_url,model,key_name,price_in_per_m,price_out_per_m,is_free,enabled,daily_usd_cap,notes) VALUES
 ('Nous · Claude Haiku 5.5','openai_compat','https://inference-api.nousresearch.com/v1','anthropic/claude-haiku-5.5','nous',0.10,0.50,false,false,1.00,'cobra do saldo da Nous'),
 ('Nous · Claude Sonnet 5.5','openai_compat','https://inference-api.nousresearch.com/v1','anthropic/claude-sonnet-5.5','nous',2.00,10.00,false,false,1.00,'cobra do saldo da Nous'),
 ('Nous · Claude Opus 5.5','openai_compat','https://inference-api.nousresearch.com/v1','anthropic/claude-opus-5.5','nous',4.00,20.00,false,false,1.00,'cobra do saldo da Nous'),
 ('Nous · GPT-6 Luna','openai_compat','https://inference-api.nousresearch.com/v1','openai/gpt-6-luna','nous',0.10,0.50,false,false,1.00,'cobra do saldo da Nous'),
 ('Nous · GPT-6.1 Sol','openai_compat','https://inference-api.nousresearch.com/v1','openai/gpt-6.1-sol','nous',2.00,10.00,false,false,1.00,'cobra do saldo da Nous'),
 ('Anthropic direta · Haiku 5.5','anthropic','https://api.anthropic.com/v1','claude-haiku-5-5','anthropic',0.10,0.50,false,false,1.00,'precisa de chave em llm-anthropic; confirme o preço no painel da Anthropic'),
 ('Anthropic direta · Sonnet 5.5','anthropic','https://api.anthropic.com/v1','claude-sonnet-5-5','anthropic',2.00,10.00,false,false,1.00,'precisa de chave em llm-anthropic; confirme o preço no painel da Anthropic'),
 ('Anthropic direta · Opus 5.5','anthropic','https://api.anthropic.com/v1','claude-opus-5-5','anthropic',4.00,20.00,false,false,1.00,'precisa de chave em llm-anthropic; confirme o preço no painel da Anthropic'),
 ('OpenAI direta · GPT-6 Luna','openai_compat','https://api.openai.com/v1','gpt-6-luna','openai',0.10,0.50,false,false,1.00,'precisa de chave em llm-openai; confirme id e preço no painel da OpenAI'),
 ('OpenAI direta · GPT-6.1 Sol','openai_compat','https://api.openai.com/v1','gpt-6.1-sol','openai',2.00,10.00,false,false,1.00,'precisa de chave em llm-openai; confirme id e preço no painel da OpenAI')
ON CONFLICT DO NOTHING;

UPDATE employees e SET llm_profile_id = p.id FROM llm_profiles p
 WHERE e.llm_profile_id IS NULL AND p.model = e.model AND p.is_free AND p.base_url LIKE 'https://inference-api.nousresearch.com/%';
