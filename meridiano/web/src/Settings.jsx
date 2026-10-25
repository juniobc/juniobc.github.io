import { useEffect, useState } from 'react';
import { admin, api, getRecovery, getToken, setRecovery } from './api.js';

const when = (t) => new Date(t).toLocaleString('pt-BR');
const REDIRECT_HELP = [
  'Abra console.cloud.google.com → APIs e serviços → Credenciais.',
  'Criar credenciais → ID do cliente OAuth → Aplicativo da Web.',
  'Em URIs de redirecionamento autorizados, coloque o endereço abaixo.',
  'Copie o ID do cliente e a chave secreta e cole aqui. Os escopos pedidos são só openid e e-mail.',
];

function GoogleCard({ me, reload }) {
  const [f, setF] = useState({ client_id: '', client_secret: '', owner_email: me.dono || '' });
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true); setMsg('');
    try {
      const r = await fetch('api/auth/google/setup', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Token': getToken() }, body: JSON.stringify(f) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof j.detail === 'string' ? j.detail : 'Confira os campos.');
      setF({ client_id: '', client_secret: '', owner_email: me.dono || '' }); setMsg('Pronto. Agora é só entrar com o Google.'); reload();
    } catch (e) { setMsg(e.message); } finally { setBusy(false); }
  };
  return (
    <section className="card">
      <b>Ativar login com Google</b>
      <ol>{REDIRECT_HELP.map((x) => <li key={x}>{x}</li>)}</ol>
      <p><code style={{ wordBreak: 'break-all' }}>{me.redirect_uri}</code></p>
      <input placeholder="ID do cliente" value={f.client_id} onChange={(e) => setF({ ...f, client_id: e.target.value })} style={{ width: '100%', marginBottom: 6 }} />
      <input type="password" autoComplete="off" placeholder="Chave secreta do cliente" value={f.client_secret} onChange={(e) => setF({ ...f, client_secret: e.target.value })} style={{ width: '100%', marginBottom: 6 }} />
      <input type="email" placeholder="E-mail do dono" value={f.owner_email} onChange={(e) => setF({ ...f, owner_email: e.target.value })} style={{ width: '100%', marginBottom: 6 }} />
      <button disabled={busy || !f.client_id || !f.client_secret} onClick={save}>{busy ? 'Salvando...' : 'Salvar'}</button>
      {msg && <p className="note" role="status">{msg}</p>}
      <p className="note">Este cadastro só vale uma vez. A chave secreta vai para um arquivo protegido no servidor e não volta para a tela.</p>
    </section>
  );
}

function SecretRow({ title, status, children, busy, can, onSave, onTest, onRemove, canSave, note }) {
  return (
    <div style={{ marginBottom: 12 }}>
      <b>{title}</b> <small>{status}</small>
      {children}
      <div style={{ marginTop: 6 }}>
        <button disabled={busy || !can || !canSave} onClick={onSave}>Salvar</button>{' '}
        {onTest && <button disabled={busy || !can} onClick={onTest}>Testar</button>}{' '}
        {onRemove && <button disabled={busy || !can} onClick={onRemove}>Remover</button>}
      </div>
      {note && <p className="note">{note}</p>}
    </div>
  );
}

export default function Settings({ me, reqs, llmKeys, reload, goto }) {
  const [mp, setMp] = useState(null);
  const [store, setStore] = useState(null);
  const [events, setEvents] = useState([]);
  const [f, setF] = useState({ token: '', nome: '', email: '' });
  const [keys, setKeys] = useState({});
  const [other, setOther] = useState({ name: '', base: '', key: '' });
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);
  const [rec, setRec] = useState(getRecovery());
  const can = !!me?.logado || !!getRecovery();

  const refresh = () => {
    api('secrets/mp-token').then(setMp).catch(() => {});
    api('store/settings').then(setStore).catch(() => {});
    api('auth/events').then(setEvents).catch(() => {});
  };
  useEffect(() => { refresh(); const id = setInterval(refresh, 8000); return () => clearInterval(id); }, []);
  const run = async (fn, ok) => {
    setBusy(true); setMsg('');
    try { const r = await fn(); setMsg(typeof ok === 'function' ? ok(r) : ok || 'Feito.'); refresh(); reload(); } catch (e) { setMsg(e.message); } finally { setBusy(false); }
  };
  if (!me) return <p className="note">Carregando…</p>;
  const openReqs = reqs.filter((r) => !r.done);

  return (
    <>
      {!me.google_configurado && <GoogleCard me={me} reload={reload} />}
      <section className="card">
        <b>Acesso de administrador</b>
        {me.logado
          ? <p>Conectado como <b>{me.email}</b>. <button disabled={busy} onClick={() => run(() => admin('POST', 'auth/logout'), 'Você saiu.')}>Sair</button></p>
          : me.google_configurado
            ? <p><a href="api/auth/google/start"><button type="button">Entrar com Google</button></a></p>
            : <p className="note">Cadastre as credenciais do Google acima para liberar o login.</p>}
        {msg && <p className="note" role="status">{msg}</p>}
      </section>

      {openReqs.length > 0 && <section className="card"><b>Pedidos para você</b>
        {openReqs.map((r) => (
          <p key={r.id}>{r.body}{' '}
            {r.body.startsWith('Preciso do access token do Mercado Pago') && <button onClick={() => document.getElementById('cartao-mp')?.scrollIntoView({ behavior: 'smooth' })}>Ir ao cartão Mercado Pago</button>}</p>))}
      </section>}

      <section className="card" id="cartao-mp">
        <b>Mercado Pago</b>
        <SecretRow title="Access Token (produção)" can={can} busy={busy} canSave={!!(f.token || (f.nome && f.email))}
          status={!mp ? '…' : !mp.configurado ? '· não configurado' : mp.verificado ? '· configurado e verificado' : `· configurado, não verificado${mp.detalhe ? ` (${mp.detalhe})` : ''}`}
          onSave={() => run(async () => {
            const feitos = [];
            if (f.nome.trim() || f.email.trim()) { await admin('POST', 'store/settings', { seller_name: f.nome.trim(), contact_email: f.email.trim() }); feitos.push('identificação da loja'); }
            if (f.token.trim()) { const r = await admin('POST', 'secrets/mp-token', { access_token: f.token.trim() }); feitos.push(`token (${r.verificado ? 'confirmado' : 'ainda não confirmado'})`); }
            setF({ token: '', nome: '', email: '' });
            return feitos.join(' e ');
          }, (r) => `Recebi: ${r}. O valor não aparece mais aqui.`)}
          onTest={() => run(() => admin('POST', 'secrets/mp-token/test'), (r) => (r.verificado ? 'O Mercado Pago aceitou o token.' : `Não confirmado: ${r.detalhe || r.problema || 'tente de novo'}`))}
          onRemove={() => run(() => admin('DELETE', 'secrets/mp-token'), 'Token removido.')}
          note={mp?.modo === 'teste' ? 'Este é um token de teste: não recebe pagamentos reais. Use o Access Token de produção (começa com APP_USR-).' : 'No Mercado Pago: Suas integrações → Checkout Pro → Credenciais de produção → Access Token (APP_USR-…).'}>
          <input type="password" autoComplete="off" placeholder="Access Token (APP_USR-...)" value={f.token} onChange={(e) => setF({ ...f, token: e.target.value })} style={{ width: '100%', marginTop: 6 }} />
        </SecretRow>
        <b>Vendedor</b> <small>{store?.pronta ? `· ${store.seller_name} · ${store.contact_email}` : '· não identificado'}</small>
        <input autoComplete="name" placeholder="Seu nome completo (aparece na loja)" value={f.nome} onChange={(e) => setF({ ...f, nome: e.target.value })} style={{ width: '100%', margin: '6px 0' }} />
        <input type="email" autoComplete="email" placeholder="E-mail de contato (aparece na loja)" value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} style={{ width: '100%' }} />
        <p className="note">O token vai para um arquivo protegido no servidor e nunca é mostrado de volta. Nenhum agente tem acesso. O nome e o e-mail ficam públicos na loja. Sacar o dinheiro continua sendo com você.</p>
      </section>

      <section className="card">
        <b>Chaves de modelos (LLM)</b>
        {(llmKeys || []).filter((k) => k.base_url).map((k) => (
          <SecretRow key={k.key_name} title={k.nome} can={can} busy={busy} canSave={!!keys[k.key_name]}
            status={k.configurada ? `· configurada (${k.fonte})` : '· sem chave'}
            onSave={() => run(async () => { await admin('POST', `llm/keys/${k.key_name}`, { key: keys[k.key_name].trim() }); setKeys({ ...keys, [k.key_name]: '' }); }, `Chave ${k.nome} guardada.`)}
            onTest={() => run(() => admin('POST', `llm/keys/${k.key_name}/test`, {}), (r) => `${k.nome}: ${r.mensagem}`)}
            onRemove={k.fonte === 'arquivo' ? () => run(() => admin('DELETE', `llm/keys/${k.key_name}`), `Chave ${k.nome} removida.`) : null}>
            <input type="password" autoComplete="off" placeholder={`Chave ${k.nome}`} value={keys[k.key_name] ?? ''} onChange={(e) => setKeys({ ...keys, [k.key_name]: e.target.value })} style={{ width: '100%', marginTop: 6 }} />
          </SecretRow>))}
        <SecretRow title="Outro compatível" can={can} busy={busy} canSave={!!(other.name && other.key)} status=""
          onSave={() => run(async () => { await admin('POST', `llm/keys/${other.name.trim()}`, { key: other.key.trim() }); setOther({ ...other, key: '' }); }, 'Chave guardada.')}
          onTest={() => run(() => admin('POST', `llm/keys/${other.name.trim()}/test`, { base_url: other.base.trim() || null }), (r) => r.mensagem)}
          note="O endereço precisa ser de um provedor da lista permitida (Nous, OpenRouter, OpenAI, Anthropic).">
          <input placeholder="Nome (letras minúsculas, números, - e _)" value={other.name} onChange={(e) => setOther({ ...other, name: e.target.value })} style={{ width: '100%', margin: '6px 0' }} />
          <input placeholder="Endereço da API (https://...)" value={other.base} onChange={(e) => setOther({ ...other, base: e.target.value })} style={{ width: '100%', marginBottom: 6 }} />
          <input type="password" autoComplete="off" placeholder="Chave" value={other.key} onChange={(e) => setOther({ ...other, key: e.target.value })} style={{ width: '100%' }} />
        </SecretRow>
        <p className="note">A chave vai direto para um arquivo protegido no servidor e nunca volta para a tela nem chega aos agentes. Depois de guardar, ligue os modelos pagos na aba Modelos.</p>
      </section>

      <section className="card">
        <b>Segurança</b>
        <details><summary>Entrar com o token de recuperação do servidor</summary>
          <input type="password" autoComplete="off" placeholder="MERIDIANO_ADMIN_TOKEN" value={rec} onChange={(e) => { setRec(e.target.value); setRecovery(e.target.value.trim()); }} style={{ width: '100%', margin: '6px 0' }} />
          <p className="note">Só para emergência (se o login do Google falhar). Fica apenas nesta aba do navegador. {me.recuperacao_por_token ? '' : 'Este servidor não tem token de recuperação configurado.'}</p>
        </details>
        <p className="note">Últimas tentativas de acesso:</p>
        <ul className="feed">{events.slice(0, 8).map((e, i) => <li key={i}><small>{when(e.em)}</small> {e.tipo}{e.email ? ` · ${e.email}` : ''}{e.detalhe ? ` · ${e.detalhe}` : ''}</li>)}
          {!events.length && <li className="note">Nenhum registro.</li>}</ul>
      </section>
    </>
  );
}
