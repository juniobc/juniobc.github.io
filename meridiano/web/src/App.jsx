import { useEffect, useState } from 'react';
import Office from './Office.jsx';
import { api, post, getToken, setToken } from './api.js';

const brl = (n) => 'R$ ' + Number(n).toFixed(2).replace('.', ',');
const TABS = ['Escritório', 'Tarefas', 'Produtos', 'Caixa'];

function Login({ onDone, error }) {
  const [v, setV] = useState('');
  return (
    <div className="wrap"><h1>Meridiano Digital</h1>
      {error && <p className="note" style={{ color: '#ff8a80' }}>{error}</p>}
      <p className="note">Cole o token de acesso para ver a empresa.</p>
      <input value={v} onChange={(e) => setV(e.target.value)} placeholder="token" type="password" />{' '}
      <button onClick={() => { setToken(v.trim()); onDone(); }}>Entrar</button></div>
  );
}

export default function App() {
  const [auth, setAuth] = useState(!!getToken());
  const [d, setD] = useState({ state: null, emps: [], msgs: [], tasks: [], prods: [], ledger: [], reqs: [], acts: [], hist: [] });
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
        const [state, emps, msgs, tasks, prods, ledger, reqs, acts, hist] = await Promise.all(
          ['state', 'employees', 'messages?limit=40', 'tasks', 'products', 'ledger', 'owner-requests', 'activity?limit=30', 'history'].map(api));
        if (on) { setD({ state, emps, msgs, tasks, prods, ledger, reqs, acts, hist }); setErr(''); }
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
          {d.reqs.filter((r) => !r.done).map((r) => (
            <p key={r.id}>{r.body} <small>({r.from_name})</small>{' '}
              <button onClick={() => post(`owner-requests/${r.id}/done`).catch(() => {})}>Feito</button></p>))}
        </section>)}

      {tab === 0 && <>
        <div className="card" aria-live="polite"><b>Agora:</b>{' '}
          {nowAct ? <>{nowAct.name}{nowAct.target_name ? ` → ${nowAct.target_name}` : ''}: {nowAct.text || nowAct.kind} <small>({ago(nowAct.started_at)})</small></> : 'ninguém agiu nos últimos 3 minutos.'}</div>
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
        <li key={p.id}><b>{p.name}</b> <small>({p.status}{p.price_cents ? ` · ${brl(p.price_cents / 100)}` : ' · sem preço'} · {p.content.length} caracteres)</small><br />{p.description}
          {p.content && <details><summary>Ver conteúdo</summary><pre style={{ whiteSpace: 'pre-wrap' }}>{p.content}</pre></details>}</li>))}
        {!d.prods.length && <li className="note">Nenhum produto ainda.</li>}</ul>}

      {tab === 3 && <>
        <p className="note">O caixa começa em R$ 0,00 e só muda quando o dono registra um valor realmente recebido. Falas dos agentes nunca contam.</p>
        <ul className="feed">{d.ledger.map((l) => (
          <li key={l.id}><b>{l.kind === 'receita' ? '+' : '−'} {brl(l.amount_cents / 100)}</b> · {l.description}<br />
            <small>{new Date(l.created_at).toLocaleString('pt-BR')} · {l.source}</small></li>))}
          {!d.ledger.length && <li className="note">Nenhum lançamento. Caixa: R$ 0,00.</li>}</ul>
      </>}
    </div>
  );
}
