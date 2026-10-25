import { useEffect, useRef, useState } from 'react';
import { getCsrf, getToken } from './api.js';

const SR = typeof window !== 'undefined' ? window.SpeechRecognition || window.webkitSpeechRecognition : null;
const TTS = typeof window !== 'undefined' && 'speechSynthesis' in window && typeof window.SpeechSynthesisUtterance !== 'undefined';
const lsGet = (k) => { try { return localStorage.getItem(k) || ''; } catch { return ''; } };
const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch {} };
const hora = (t) => new Date(t).toLocaleString('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });

async function enviar(to, body) {
  const r = await fetch('api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Token': getToken(), 'X-CSRF': getCsrf() }, body: JSON.stringify({ to, body }) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof j.detail === 'string' ? j.detail : 'Não foi possível enviar.');
  return j;
}

export default function Chat({ chat, emps, reload }) {
  const [to, setTo] = useState(lsGet('mchat_to') || 'presidente');
  const [text, setText] = useState('');
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);
  const [ouvindo, setOuvindo] = useState(false);
  const [voz, setVoz] = useState(lsGet('mvoz') === '1' && TTS);
  const spoken = useRef(null);       // id da última resposta já lida em voz alta
  const rec = useRef(null);
  const fim = useRef(null);
  const msgs = chat?.mensagens || [];

  // lê em voz alta só respostas NOVAS, depois que o interruptor foi ligado
  useEffect(() => {
    const ultimaResposta = [...msgs].reverse().find((m) => m.de === 'empresa');
    if (spoken.current === null) { spoken.current = ultimaResposta ? ultimaResposta.id : 0; return; }
    if (!voz || !TTS) { if (ultimaResposta) spoken.current = ultimaResposta.id; return; }
    msgs.filter((m) => m.de === 'empresa' && m.id > spoken.current).forEach((m) => {
      const u = new window.SpeechSynthesisUtterance(`${m.nome}: ${m.texto}`);
      u.lang = 'pt-BR';
      window.speechSynthesis.speak(u);
      spoken.current = m.id;
    });
  }, [chat, voz]);
  useEffect(() => { fim.current?.scrollIntoView({ block: 'end' }); }, [msgs.length]);

  const alternarVoz = (on) => {
    setVoz(on); lsSet('mvoz', on ? '1' : '0');
    if (!on && TTS) window.speechSynthesis.cancel();
  };
  const microfone = () => {
    if (!SR) return;
    if (ouvindo && rec.current) { rec.current.stop(); return; }
    const r = new SR();
    r.lang = 'pt-BR'; r.interimResults = false; r.continuous = false;
    r.onresult = (e) => {
      const t = Array.from(e.results).map((x) => x[0].transcript).join(' ').trim();
      if (t) setText((old) => (old ? old + ' ' : '') + t);
    };
    r.onerror = (e) => setMsg(e.error === 'not-allowed' || e.error === 'service-not-allowed' ? 'O navegador bloqueou o microfone. Libere a permissão e tente de novo.' : e.error === 'no-speech' ? 'Não ouvi nada. Tente de novo.' : 'Não consegui ouvir agora. Você pode digitar.');
    r.onend = () => setOuvindo(false);
    rec.current = r; setMsg(''); setOuvindo(true);
    try { r.start(); } catch { setOuvindo(false); }
  };
  const enviarMsg = async () => {
    setBusy(true); setMsg('');
    try { await enviar(to, text.trim()); setText(''); reload(); lsSet('mchat_to', to); } catch (e) { setMsg(e.message); } finally { setBusy(false); }
  };
  const sorted = [...emps].sort((a, b) => (/presidente|ceo/i.test(b.role) - /presidente|ceo/i.test(a.role)) || a.id - b.id);

  return (
    <>
      <p className="note">Fale com a empresa. A mensagem entra no topo do contexto de quem recebe e dispara um turno na hora ({chat ? `${chat.turnos_extras_hoje} de ${chat.limite_turnos_extras}` : '…'} turnos extras hoje). Ordens e sugestões valem; as regras de segurança da empresa continuam.</p>
      <ul className="feed" aria-live="polite" style={{ maxHeight: 360, overflowY: 'auto' }}>
        {msgs.map((m) => (
          <li key={m.id} style={{ textAlign: m.de === 'dono' ? 'right' : 'left' }}>
            <small>{m.de === 'dono' ? `Você → ${m.para}` : `${m.nome} (${m.cargo})`} · {hora(m.em)}</small><br />
            {m.texto}
            {m.de === 'dono' && <><br /><small>{m.recebida_por.length ? `lida por ${m.recebida_por.join(', ')}` : 'aguardando o turno de quem recebe'}</small></>}
          </li>))}
        {!msgs.length && <li className="note">Nenhuma mensagem ainda. Escreva ou fale abaixo.</li>}
        <li ref={fim} style={{ border: 0, padding: 0 }} />
      </ul>
      <section className="card">
        <label className="note" htmlFor="chat-para">Para</label>
        <select id="chat-para" value={to} onChange={(e) => setTo(e.target.value)} style={{ width: '100%', margin: '4px 0 8px', padding: 10, borderRadius: 8 }}>
          <option value="todos">Todos (Presidente e CEO respondem na hora)</option>
          {sorted.map((e) => <option key={e.slug} value={e.slug}>{e.name} · {e.role}</option>)}
        </select>
        <textarea aria-label="Mensagem" rows={3} maxLength={2000} placeholder="Escreva ou use o microfone" value={text} onChange={(e) => setText(e.target.value)}
          style={{ width: '100%', padding: 10, borderRadius: 8, border: 0, font: 'inherit' }} />
        <div style={{ marginTop: 8, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <button disabled={busy || !text.trim()} onClick={enviarMsg}>{busy ? 'Enviando...' : 'Enviar'}</button>
          {SR && <button type="button" onClick={microfone} aria-pressed={ouvindo}>{ouvindo ? '■ Parar' : '🎤 Falar'}</button>}
        </div>
        {!SR && <p className="note">Este navegador não tem reconhecimento de voz (use o Chrome no Android). Você pode digitar normalmente.</p>}
        {TTS
          ? <label style={{ display: 'block', marginTop: 8 }}><input type="checkbox" checked={voz} onChange={(e) => alternarVoz(e.target.checked)} /> Ler respostas em voz alta</label>
          : <p className="note">Este navegador não consegue ler em voz alta. As respostas continuam aparecendo no texto.</p>}
        {msg && <p className="note" role="alert">{msg}</p>}
        <p className="note">A voz funciona só no navegador do seu celular: nenhum áudio é gravado nem enviado ao servidor da empresa (o reconhecimento de fala do Chrome é feito pelo próprio navegador).</p>
      </section>
    </>
  );
}
