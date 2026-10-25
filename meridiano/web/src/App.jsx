import { useEffect, useState } from 'react';
import Office from './Office.jsx';
import { api, post, sendMpToken, sendStoreSettings, getToken, setToken } from './api.js';

const brl = (n) => 'R$ ' + Number(n).toFixed(2).replace('.', ',');
const TABS = ['Escritório', 'Tarefas', 'Produtos', 'Ideias', 'Caixa'];
const MP_REQUEST = 'Preciso do access token do Mercado Pago';

function MpTokenCard({ onSaved }) {
  const [admin, setAdmin] = useState('');
  const [tok, setTok] = useState('');
  const [nome, setNome] = useState('');
  const [email, setEmail] = useState('');
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);
  const send = async () => {
    setBusy(true); setMsg('');
    const feitos = [];
    try {
      if (nome.trim() || email.trim()) { await sendStoreSettings(admin.trim(), nome.trim(), email.trim()); feitos.push('identificação da loja'); setNome(''); setEmail(''); }
      if (tok.trim()) {
        const r = await sendMpToken(admin.trim(), tok.trim());
        feitos.push(`token do Mercado Pago (${r.modo === 'teste' ? 'modo de teste' : 'produção'}${r.verificado ? ', confirmado' : ', ainda não confirmado'})`);
        setTok('');
      }
      setAdmin('');
      setMsg(`Pronto: recebi ${feitos.join(' e ')}. O token não aparece mais aqui.`);
      onSaved();
    } catch (e) { setMsg(`${feitos.length ? `Já recebi: ${feitos.join(', ')}. ` : ''}${e.message}`); } finally { setBusy(false); }
  };
  return (
    <div>
      <p><b>Preciso do access token do Mercado Pago</b> para receber pagamentos, e do seu nome e e-mail para identificar o vendedor na loja (a lei exige). Uma vez só:</p>
      <ol>
        <li>Entre em <b>mercadopago.com.br/developers</b> com a sua conta.</li>
        <li>Vá em <b>Suas integrações</b> e crie uma aplicação (tipo <b>Checkout Pro</b>).</li>
        <li>Abra <b>Credenciais de produção</b> e copie o <b>Access Token</b> (começa com <code>APP_USR-</code>).</li>
        <li>Cole abaixo, com o token de administrador (quem montou o servidor te passa).</li>
      </ol>
      <input type="password" autoComplete="off" placeholder="Token de administrador" value={admin} onChange={(e) => setAdmin(e.target.value)} style={{ width: '100%', marginBottom: 6 }} />
      <input type="password" autoComplete="off" placeholder="Access Token do Mercado Pago (APP_USR-...)" value={tok} onChange={(e) => setTok(e.target.value)} style={{ width: '100%', marginBottom: 6 }} />
      <input type="text" autoComplete="name" placeholder="Seu nome completo (aparece na loja)" value={nome} onChange={(e) => setNome(e.target.value)} style={{ width: '100%', marginBottom: 6 }} />
      <input type="email" autoComplete="email" placeholder="E-mail de contato (aparece na loja)" value={email} onChange={(e) => setEmail(e.target.value)} style={{ width: '100%', marginBottom: 6 }} />
      <button disabled={busy || !admin || !(tok || (nome && email))} onClick={send}>{busy ? 'Enviando...' : 'Enviar com segurança'}</button>
      {msg && <p className="note">{msg}</p>}
      <p className="note">O token vai direto para o servidor, é guardado em arquivo protegido e nunca é mostrado de volta. Nenhum agente tem acesso a ele. O nome e o e-mail ficam públicos na loja. O Mercado Pago só recebe os pagamentos: sacar o dinheiro continua sendo com você. Se preferir, quem administra o servidor pode criar o arquivo <code>~/meridiano/secrets/mp-token</code> e este cartão some sozinho.</p>
    </div>
  );
}

export default function App() {
  const [auth, setAuth] = useState(!!getToken());
  const [d, setD] = useState({ state: null, emps: [], msgs: [], tasks: [], prods: [], ledger: [], reqs: [], acts: [], hist: [], engine: null, loja: [], opps: [] });
  const [err, setErr] = useState('');
  const [loginErr, setLoginErr] = useState('');
  const [tab, setTab] = useState(0);
  const [sel, setSel] = useState(null);
  const [card, setCard] = useState(null);
  const [now, setNow] = useState(Date.now());
  const reduced = typeof window !== 'undefined' && window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), 15000); return () => clearInterval(id); }, []);
  useEffect(() => {
    if (!sel || !auth) { setCard(null); return; }
    let on = true;
    api('employees/' + sel).then((c) => on && setCard(c)).catch(() => on && setCard(null));
    return () => { on = false; };
  }, [sel, auth, d.acts.length && d.acts[0].id]);

  useEffect(() => {
    if (!auth) return;
    let on = true;
    const load = async () => {
      try {
        const [state, emps, msgs, tasks, prods, ledger, reqs, acts, hist, engine, loja, opps] = await Promise.all(
          ['state', 'employees', 'messages?limit=40', 'tasks', 'products', 'ledger', 'owner-requests', 'activity?limit=30', 'history', 'engine', 'store/products', 'opportunities'].map(api));
        if (on) { setD({ state, emps, msgs, tasks, prods, ledger, reqs, acts, hist, engine, loja, opps }); setErr(''); }
      } catch (e) {
        if (e.message === '401') { setToken(''); setLoginErr('Token inválido. Confira e tente de novo.'); setAuth(false); } else setErr('Sem conexão com o servidor.');
      }
    };
    load(); const id = setInterval(load, 5000);
    return () => { on = false; clearInterval(id); };
  }, [auth]);

  if (!auth) return <Login error={loginErr} onDone={() => { setLoginErr(''); setAuth(true); }} />;
  const s = d.state;
  const who = d.emps.find((e) => e.slug === sel);
  const ago = (t) => { const m = Math.round((now - new Date(t).getTime()) / 60000); return m < 1 ? 'agora' : m < 60 ? `há ${m} min` : `há ${Math.round(m / 60)} h`; };
  const nowAct = d.acts.find((a) => a.kind !== 'parado' && now - new Date(a.started_at).getTime() < 180000);

  return (
    <div className="wrap">
      <h1>Meridiano Digital</h1>
      {err && <p className="note">{err}</p>}
      <div className="stats">
        <div><b>{s ? brl(s.caixa) : '—'}</b><span>Caixa (só vendas reais)</span></div>
        <div><b>{s ? s.vendas : '—'}</b><span>Vendas registradas</span></div>
        <div><b>{s ? s.agentes : '—'}</b><span>Agentes</span></div>
      </div>
      <nav>{TABS.map((t, i) => <button key={t} className={i === tab ? 'on' : ''} onClick={() => setTab(i)}>{t}</button>)}</nav>

      {d.reqs.filter((r) => !r.done).length > 0 && (
        <section className="card"><strong>Pedidos para você, Junio</strong>
          {d.reqs.filter((r) => !r.done).map((r) => (r.body.startsWith(MP_REQUEST)
            ? <MpTokenCard key={r.id} onSaved={() => setNow(Date.now())} />
            : <p key={r.id}>{r.body} <small>({r.from_name || 'Sistema'})</small>{' '}
              <button onClick={() => post(`owner-requests/${r.id}/done`).catch(() => {})}>Feito</button></p>))}
        </section>)}

      {tab === 0 && <>
        <div className="card" aria-live="polite"><b>Agora:</b>{' '}
          {nowAct ? <>{nowAct.name}{nowAct.target_name ? ` → ${nowAct.target_name}` : ''}: {nowAct.text || nowAct.kind} <small>({ago(nowAct.started_at)})</small></> : 'ninguém agiu nos últimos 3 minutos.'}</div>
        {d.engine && <p className="note">Motor: {d.engine.vivo ? 'vivo' : 'PARADO'}
          {d.engine.funcionario_da_vez ? ` · agora é a vez de ${d.engine.funcionario_da_vez}` : ''}
          {d.engine.proximo_turno_em_s != null ? ` · próximo turno em ${d.engine.proximo_turno_em_s}s` : ''}
          {d.engine.motivo_espera && d.engine.motivo_espera !== 'nenhum' ? ` · espera por: ${d.engine.motivo_espera}` : ''}
          {d.engine.ultimo_turno_por ? ` · último turno: ${d.engine.ultimo_turno_por}` : ''}<br />
          Ferramentas: {d.engine.isolamento_ferramentas?.ligadas ? 'ligadas' : 'desligadas'} ({d.engine.isolamento_ferramentas?.motivo || 'não verificado'})</p>}
        <Office employees={d.emps} selected={sel} onSelect={setSel} reduced={reduced} />
        <p className="note">Ações (escrever, pesquisar, conversar, reunião, contratar, demitir) vêm do banco. Quem está parado passeia pela copa e pelo corredor: isso é só animação de ambiente.</p>
        <section className="card">{who
          ? <><strong>{who.name}</strong> · {who.role} · {who.area}
              <p className="note">Modelo: {who.model} · Hoje: {who.chamadas_hoje} chamadas, {who.tokens_hoje} tokens{who.hired_by ? ` · contratado por ${who.hired_by}: ${who.hire_reason}` : ''}</p>
              <p>{card?.ultima_atividade ? <>Última atividade: {card.ultima_atividade.text || card.ultima_atividade.kind} <small>({ago(card.ultima_atividade.started_at)})</small></> : 'Sem atividade registrada.'}</p>
              {card?.tarefas_abertas?.length > 0 && <><b>Tarefas abertas</b><ul className="feed">{card.tarefas_abertas.map((t) => <li key={t.id}>{t.title}</li>)}</ul></>}
              {card?.mensagens?.length > 0 && <><b>Últimas mensagens</b><ul className="feed">{card.mensagens.map((m) => <li key={m.id}>{m.to_name ? `→ ${m.to_name}: ` : '(mural) '}{m.body}</li>)}</ul></>}</>
          : <><strong>Toque em um funcionário</strong><p className="note">Cargo, área, última atividade, mensagens e tarefas.</p></>}</section>
        <h2>Equipe: contratações e demissões</h2>
        <ul className="feed">{d.hist.map((h, i) => (
          <li key={i}><b>{h.tipo === 'contratado' ? '+' : '−'} {h.name}</b> ({h.role}) <small>{new Date(h.at).toLocaleString('pt-BR')}{h.por ? ` · por ${h.por}` : ''}</small><br />{h.motivo}</li>))}</ul>
        <h2>Conversas</h2>
        <ul className="feed">{d.msgs.map((m) => (
          <li key={m.id}><b>{m.from_name || 'Sistema'}</b>{m.to_name ? ` → ${m.to_name}` : ' (mural)'}<br />{m.body}<br />
            <small>{new Date(m.created_at).toLocaleString('pt-BR')}</small></li>))}
          {!d.msgs.length && <li className="note">Ainda sem conversas. O motor roda um funcionário por vez.</li>}</ul>
        {s && s.falhas_hoje > 0 && <p className="note" style={{ color: '#ffcc80' }}>Falhas hoje: {s.falhas_hoje}. Último erro: {s.ultimo_erro}</p>}
        {s && <p className="note">Orçamento do dia: {s.orcamento_pct}% usado, volta a zerar às {new Date(s.orcamento_zera_em).toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' })}.</p>}
        {s && <p className="note">Uso hoje: {s.uso_hoje.chamadas}/{s.uso_hoje.limite_chamadas} chamadas, {s.uso_hoje.tokens}/{s.uso_hoje.limite_tokens} tokens.</p>}
      </>}

      {tab === 1 && <ul className="feed">{d.tasks.map((k) => (
        <li key={k.id}><b>{k.title}</b> <small>({k.status}) · {k.assignee}</small>{k.result && <><br />{k.result}</>}</li>))}
        {!d.tasks.length && <li className="note">Sem tarefas.</li>}</ul>}

      {tab === 2 && <ul className="feed">{d.prods.map((p) => (
        <li key={p.id}><b>{p.name}</b> <small>({p.status}{p.price_cents ? ` · ${brl(p.price_cents / 100)}` : ' · sem preço'})</small><br /><b>{p.secoes_total > 0 ? `${p.secoes_prontas} de ${p.secoes_total} seções` : 'sem plano de seções'}, {p.caracteres} caracteres</b><br />{p.description}
          {(() => { const l = d.loja.find((x) => x.id === p.id); if (!l) return null;
            return <p className="note">Loja: {l.publicado ? <a href={l.url} target="_blank" rel="noreferrer">publicada ({l.url})</a>
              : p.status !== 'pronto' ? 'o produto ainda não está pronto'
              : !l.tem_texto ? 'falta o texto de venda' : !p.price_cents ? 'falta o preço' : 'falta a identificação do vendedor (nome e e-mail)'}</p>; })()}
          {p.content && <details><summary>Ver conteúdo</summary><pre style={{ whiteSpace: 'pre-wrap' }}>{p.content}</pre></details>}</li>))}
        {!d.prods.length && <li className="note">Nenhum produto ainda.</li>}</ul>}

      {tab === 3 && <>
        <p className="note">Mesa de oportunidades: a empresa testa ideias rápido e descarta sem apego. Medições reais vêm do servidor (vendas confirmadas, visitas); o "aprendizado" é o relato do agente, não uma medição.</p>
        <ul className="feed">{d.opps.map((o) => (
          <li key={o.id}><b>{o.idea}</b> <small>({o.status} · esforço {o.effort} · risco {o.risk}{o.autor ? ` · ${o.autor}` : ''})</small>
            {o.how_it_earns && <><br />Como ganha: {o.how_it_earns}</>}{o.audience && <><br />Público: {o.audience}</>}
            {o.needs_owner && <><br /><small>Precisa do dono: {o.needs_owner}</small></>}{o.learning && <><br /><small>Aprendizado: {o.learning}</small></>}</li>))}
          {!d.opps.length && <li className="note">A mesa ainda está vazia. Os agentes vão propor ideias pesquisando.</li>}</ul>
      </>}

      {tab === 4 && <>
        <p className="note">O caixa começa em R$ 0,00 e só muda quando o dono registra um valor realmente recebido. Falas dos agentes nunca contam.</p>
        <ul className="feed">{d.ledger.map((l) => (
          <li key={l.id}><b>{l.kind === 'receita' ? '+' : '−'} {brl(l.amount_cents / 100)}</b> · {l.description}<br />
            <small>{new Date(l.created_at).toLocaleString('pt-BR')} · {l.source}</small></li>))}
          {!d.ledger.length && <li className="note">Nenhum lançamento. Caixa: R$ 0,00.</li>}</ul>
      </>}
    </div>
  );
}
