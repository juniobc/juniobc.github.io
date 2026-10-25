import { useState } from 'react';
import { admin, getRecovery } from './api.js';

const usd = (n) => 'US$ ' + Number(n || 0).toFixed(4).replace(/0+$/, '').replace(/\.$/, '.00');
const price = (p) => (p.gratis ? 'grátis' : `US$ ${p.preco_entrada.toFixed(2)} / ${p.preco_saida.toFixed(2)} por milhão`);

export default function Models({ llm, reload, me }) {
  const adm = !!me?.logado || !!getRecovery();
  const [msg, setMsg] = useState('');
  const [caps, setCaps] = useState({});
  const [busy, setBusy] = useState(false);
  const run = async (fn, ok) => {
    setBusy(true); setMsg('');
    try { await fn(); setMsg(ok || 'Feito.'); reload(); } catch (e) { setMsg(e.message); } finally { setBusy(false); }
  };
  if (!llm) return <p className="note">Carregando…</p>;
  const options = llm.perfis.filter((p) => p.ligado);
  return (
    <>
      <section className="card">
        {adm ? <p className="note">Conectado como administrador. {msg}</p> : <p className="note">Para trocar modelo, ligar perfil ou desligar a contingência, entre com o Google na aba Configurações. {msg}</p>}
      </section>

      <section className="card">
        <b>Modo contingência: {llm.contingencia ? 'LIGADO' : 'desligado'}</b>
        <p className="note">{llm.contingencia
          ? 'Todos usam só modelos grátis. Nenhum modelo pago é chamado, mesmo que um funcionário esteja configurado nele (a escolha fica guardada).'
          : 'Modelos pagos liberados, cada um dentro do seu limite diário e do teto global.'}</p>
        <button disabled={busy || !adm} onClick={() => run(() => admin('POST', 'llm/contingency', { on: !llm.contingencia }), llm.contingencia ? 'Contingência desligada.' : 'Contingência ligada.')}>
          {llm.contingencia ? 'Desligar contingência' : 'Ligar contingência'}</button>
        <p className="note">Gasto de hoje: {usd(llm.gasto_hoje_usd)} de teto global {usd(llm.teto_global_usd)}.</p>
        {llm.avisos.map((a, i) => <p key={i} className="note" style={{ color: '#ffcc80' }}>{a}</p>)}
      </section>

      <h2>Funcionários</h2>
      <ul className="feed">{llm.funcionarios.map((f) => (
        <li key={f.slug}><b>{f.nome}</b> <small>({f.cargo})</small><br />
          Configurado: {f.perfil}{f.desvio ? <> · <b>usando agora: {f.efetivo}</b> <small>({f.desvio})</small></> : ''}<br />
          <small>Gasto hoje: {usd(f.gasto_hoje_usd)}</small>
          <br /><select aria-label={`Modelo de ${f.nome}`} disabled={busy || !adm} value={f.perfil_id ?? ''}
            onChange={(e) => run(() => admin('POST', `llm/employees/${f.slug}`, { profile_id: e.target.value ? Number(e.target.value) : null }), `Modelo de ${f.nome} atualizado.`)}>
            <option value="">padrão grátis (rodízio)</option>
            {options.map((p) => <option key={p.id} value={p.id}>{p.label}{p.gratis ? '' : ' (pago)'}</option>)}
          </select></li>))}</ul>

      <h2>Perfis de modelo</h2>
      <ul className="feed">{llm.perfis.map((p) => (
        <li key={p.id}><b>{p.label}</b> <small>({p.modelo} · {price(p)})</small><br />
          {p.ligado ? 'ligado' : 'desligado'} · {p.gratis ? 'grátis' : (p.chave_configurada ? 'chave configurada' : `sem chave (llm-${p.key_name})`)}
          {!p.gratis && <> · limite diário {p.limite_diario_usd != null ? usd(p.limite_diario_usd) : 'não definido'}</>}
          {p.ligado && !p.chamavel_agora && <> · <b>não é chamado agora: {p.motivo}</b></>}<br />
          <small>Hoje: {p.chamadas_hoje} chamadas, {p.tokens_hoje} tokens, {usd(p.gasto_hoje_usd)}{p.notas ? ` · ${p.notas}` : ''}</small><br />
          {!p.gratis && <input inputMode="decimal" aria-label={`Limite diário de ${p.label} em dólar`} placeholder="limite diário US$" value={caps[p.id] ?? ''}
            onChange={(e) => setCaps({ ...caps, [p.id]: e.target.value })} style={{ width: 130, marginRight: 6 }} />}
          {!p.gratis && caps[p.id] && <button disabled={busy || !adm} onClick={() => run(() => admin('PATCH', `llm/profiles/${p.id}`, { daily_usd_cap: Number(String(caps[p.id]).replace(',', '.')) }), 'Limite salvo.')}>Salvar limite</button>}{' '}
          <button disabled={busy || !adm} onClick={() => run(() => admin('PATCH', `llm/profiles/${p.id}`, { enabled: !p.ligado }), p.ligado ? 'Perfil desligado.' : 'Perfil ligado.')}>{p.ligado ? 'Desligar' : 'Ligar'}</button></li>))}</ul>

      <p className="note">As chaves dos provedores ficam na aba Configurações.</p>

      <h2>Uso de hoje por funcionário</h2>
      <ul className="feed">{llm.uso_por_agente.map((u) => <li key={u.nome}><b>{u.nome}</b>: {u.chamadas} chamadas, {u.tokens} tokens, {usd(u.custo_usd)}</li>)}
        {!llm.uso_por_agente.length && <li className="note">Sem uso hoje.</li>}</ul>
    </>
  );
}
