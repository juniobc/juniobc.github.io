import { useEffect, useState } from 'react';
import Office from './Office.jsx';
import { api, getToken, setToken } from './api.js';

const brl = (n) => 'R$ ' + Number(n).toFixed(2).replace('.', ',');
const TABS = ['Escritório', 'Tarefas', 'Produtos', 'Caixa'];

function Login({ onDone }) {
  const [v, setV] = useState('');
  return (
    <div className="wrap"><h1>Meridiano Digital</h1>
      <p className="note">Cole o token de acesso para ver a empresa.</p>
      <input value={v} onChange={(e) => setV(e.target.value)} placeholder="token" type="password" />{' '}
      <button onClick={() => { setToken(v.trim()); onDone(); }}>Entrar</button></div>
  );
}

export default function App() {
  const [auth, setAuth] = useState(!!getToken());
  const [d, setD] = useState({ state: null, emps: [], msgs: [], tasks: [], prods: [], ledger: [] });
  const [err, setErr] = useState('');
  const [tab, setTab] = useState(0);
  const [sel, setSel] = useState(null);

  useEffect(() => {
    if (!auth) return;
    let on = true;
    const load = async () => {
      try {
        const [state, emps, msgs, tasks, prods, ledger] = await Promise.all(
          ['state', 'employees', 'messages?limit=40', 'tasks', 'products', 'ledger'].map(api));
        if (on) { setD({ state, emps, msgs, tasks, prods, ledger }); setErr(''); }
      } catch (e) {
        if (e.message === '401') { setToken(''); setAuth(false); } else setErr('Sem conexão com o servidor.');
      }
    };
    load(); const id = setInterval(load, 5000);
    return () => { on = false; clearInterval(id); };
  }, [auth]);

  if (!auth) return <Login onDone={() => setAuth(true)} />;
  const s = d.state, speech = {};
  d.msgs.slice().reverse().forEach((m) => { if (m.from_slug) speech[m.from_slug] = m.body; });
  const who = d.emps.find((e) => e.slug === sel);

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

      {tab === 0 && <>
        <Office employees={d.emps} speech={speech} selected={sel} onSelect={setSel} />
        <section className="card">{who
          ? <><strong>{who.name}</strong> · {who.role} · {who.area}<p>{who.status === 'working' ? (who.activity || 'Trabalhando') : 'Parado, esperando a vez'}</p>
              <p className="note">Modelo: {who.model}{who.hired_by ? ` · contratado por ${who.hired_by}: ${who.hire_reason}` : ''}</p></>
          : <><strong>Toque em um funcionário</strong><p className="note">Cargo, área e o que faz agora.</p></>}</section>
        <h2>Conversas</h2>
        <ul className="feed">{d.msgs.map((m) => (
          <li key={m.id}><b>{m.from_name || 'Sistema'}</b>{m.to_name ? ` → ${m.to_name}` : ' (mural)'}<br />{m.body}<br />
            <small>{new Date(m.created_at).toLocaleString('pt-BR')}</small></li>))}
          {!d.msgs.length && <li className="note">Ainda sem conversas. O motor roda um funcionário por vez.</li>}</ul>
        {s && <p className="note">Uso hoje: {s.uso_hoje.chamadas}/{s.uso_hoje.limite_chamadas} chamadas, {s.uso_hoje.tokens}/{s.uso_hoje.limite_tokens} tokens.</p>}
      </>}

      {tab === 1 && <ul className="feed">{d.tasks.map((k) => (
        <li key={k.id}><b>{k.title}</b> <small>({k.status}) · {k.assignee}</small>{k.result && <><br />{k.result}</>}</li>))}
        {!d.tasks.length && <li className="note">Sem tarefas.</li>}</ul>}

      {tab === 2 && <ul className="feed">{d.prods.map((p) => (
        <li key={p.id}><b>{p.name}</b><br />{p.description}<br /><small>{p.file_path}</small></li>))}
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
