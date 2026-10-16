"use client";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Asset, assetLabel, processingNotice, toWebVtt } from "./presentation";

export type Caption = { start: number; end: number; text: string };
type SubtitleVersion = { _id: string; segments: Caption[] };

export type ControlProps = {
  aid: string; duration: number; fps: number; canRun: boolean;
  api: (path: string, init?: RequestInit) => Promise<Response>;
  perform: (task: () => Promise<void>) => Promise<void>;
  notify?: (message: string) => void;
  getCurrentTime?: () => number;
};
type Filter = { name: string; value: number | boolean };
const names: Record<string, string> = { brightness: "Brilho", contrast: "Contraste", saturation: "Saturação", grayscale: "Preto e branco" };

export function ColorControls({ aid, duration, fps, canRun, api, perform, notify, getCurrentTime }: ControlProps) {
  const [filters, setFilters] = useState<Filter[]>([{ name: "brightness", value: 0 }]);
  const [timestamp, setTimestamp] = useState(0);
  const [image, setImage] = useState("");
  useEffect(() => () => { if (image) URL.revokeObjectURL(image); }, [image]);
  useEffect(() => { setTimestamp(0); setImage(""); }, [aid]);
  const body = (params: unknown): RequestInit => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(params) });
  return <section className="border rounded-lg p-4 space-y-3">
    <h2 className="text-xl font-medium">Cor e filtros</h2>
    <p className="text-muted-foreground">Ajuste o aspeto do vídeo. Combine até oito filtros pela ordem pretendida.</p>
    {filters.map((filter, i) => <div key={i} className="flex flex-wrap gap-2 items-center rounded-lg border p-3">
      <span>{i + 1}.</span>
      <select aria-label={`Filtro ${i + 1}`} className="border rounded p-2 bg-background" value={filter.name} onChange={(e) => {
        const name = e.target.value;
        setFilters(filters.map((f, n) => n === i ? { name, value: name === "grayscale" ? true : name === "brightness" ? 0 : 1 } : f));
      }}>{Object.entries(names).map(([key, name]) => <option key={key} value={key}>{name}</option>)}</select>
      {filter.name === "grayscale" ? <label><input type="checkbox" checked={Boolean(filter.value)} onChange={(e) => setFilters(filters.map((f, n) => n === i ? { ...f, value: e.target.checked } : f))} /> Ativar</label> :
        <input aria-label={`Valor do filtro ${i + 1}`} className="border rounded p-2 bg-background w-28" type="number" value={Number(filter.value)} step={filter.name === "brightness" ? 1 : .1} min={filter.name === "brightness" ? -100 : filter.name === "contrast" ? .5 : 0} max={filter.name === "brightness" ? 100 : 2} onChange={(e) => setFilters(filters.map((f, n) => n === i ? { ...f, value: Number(e.target.value) } : f))} />}
      {filter.name !== "grayscale" && <input aria-label={`Ajustar ${names[filter.name]} ${i + 1}`} type="range" className="w-full accent-primary" value={Number(filter.value)} step={filter.name === "brightness" ? 1 : .1} min={filter.name === "brightness" ? -100 : filter.name === "contrast" ? .5 : 0} max={filter.name === "brightness" ? 100 : 2} onChange={(e) => setFilters(filters.map((f, n) => n === i ? { ...f, value: Number(e.target.value) } : f))} />}
      <Button size="sm" variant="outline" disabled={i === 0} onClick={() => { const next = [...filters]; [next[i - 1], next[i]] = [next[i], next[i - 1]]; setFilters(next); }}>Subir</Button>
      <Button size="sm" variant="outline" disabled={i === filters.length - 1} onClick={() => { const next = [...filters]; [next[i + 1], next[i]] = [next[i], next[i + 1]]; setFilters(next); }}>Descer</Button>
      <Button size="sm" variant="ghost" onClick={() => setFilters(filters.filter((_, n) => n !== i))}>Remover</Button>
    </div>)}
    <Button variant="outline" disabled={filters.length >= 8} onClick={() => setFilters([...filters, { name: "brightness", value: 0 }])}>Adicionar filtro</Button>
    <label className="block">Instante para pré-visualização (s) <input className="border rounded p-2 bg-background w-28" type="number" min={0} max={Math.max(0, duration - 1 / fps)} step={.1} value={timestamp} onChange={(e) => setTimestamp(Number(e.target.value))} /></label>
    {getCurrentTime && <Button size="sm" variant="outline" onClick={() => setTimestamp(Math.max(0, Math.min(getCurrentTime(), duration - 1 / fps)))}>Usar instante do leitor</Button>}
    <div className="flex flex-wrap gap-3">
      <Button variant="outline" disabled={!canRun} onClick={() => void perform(async () => {
        const blob = await (await api(`/${aid}/preview`, body({ filters, timestamp }))).blob();
        setImage(URL.createObjectURL(blob));
      })}>Pré-visualizar fotograma</Button>
      <Button disabled={!canRun} onClick={() => void perform(async () => {
        const result = await (await api(`/${aid}/jobs`, body({ operation: "color", filters }))).json();
        notify?.(processingNotice(result));
      })}>Aplicar ao vídeo</Button>
    </div>
    {image && <img src={image} alt="Pré-visualização dos filtros pela ordem escolhida" className="max-h-64 rounded" />}
  </section>;
}

export function TransitionControls({ aid, duration, canRun, api, perform, notify, clips }: ControlProps & { clips: Asset[] }) {
  const [secondId, setSecondId] = useState("");
  const [overlap, setOverlap] = useState(.5);
  const second = clips.find((c) => c._id === secondId && c._id !== aid);
  const maximum = second ? Math.min(duration, second.metadata.duration) / 2 : 0;
  return <section className="border rounded-lg p-4 space-y-3">
    <h2 className="text-xl font-medium">Transição entre dois clips</h2>
    <p className="text-muted-foreground">O vídeo aberto no leitor fica primeiro. Escolha o segundo clip e durante quanto tempo se sobrepõem.</p>
    {!clips.some((c) => c._id !== aid) && <p className="rounded-lg bg-muted p-3 text-sm">Adicione outro vídeo através do botão Adicionar vídeo para criar uma transição.</p>}
    <label className="block">Segundo clip <select aria-label="Segundo clip" className="border rounded p-2 bg-background w-full" value={secondId} onChange={(e) => setSecondId(e.target.value)}>
      <option value="">Selecione um segundo clip</option>
      {clips.filter((c) => c._id !== aid).map((c) => <option key={c._id} value={c._id}>{assetLabel(c)}</option>)}
    </select></label>
    <label className="block">Duração da transição (s) <input className="border rounded p-2 bg-background w-28" type="number" min={.001} max={maximum || undefined} step={.1} value={overlap} onChange={(e) => setOverlap(Number(e.target.value))} /></label>
    {second && <p>Máximo: {maximum.toFixed(2)} s · Resultado previsto: {(duration + second.metadata.duration - overlap).toFixed(2)} s.</p>}
    <Button disabled={!canRun || !second || overlap <= 0 || overlap > maximum} onClick={() => void perform(async () => {
      const result = await (await api(`/${aid}/jobs`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ operation: "transition", second_id: secondId, overlap }) })).json();
      notify?.(processingNotice(result));
    })}>Criar vídeo com transição</Button>
  </section>;
}

export function SubtitleControls({ aid, duration, canRun, api, perform, notify, premium, hasAudio, signal, onCaptions }: ControlProps & { premium: boolean; hasAudio: boolean; signal: string; onCaptions: (items: Caption[]) => void }) {
  const [active, setActive] = useState<SubtitleVersion | null>(null);
  const [version, setVersion] = useState<SubtitleVersion | null>(null);
  const [segments, setSegments] = useState<Caption[]>([]);
  const [draft, setDraft] = useState(false);
  const [loadError, setLoadError] = useState("");
  useEffect(() => {
    let alive = true;
    setActive(null); setVersion(null); setSegments([]); onCaptions([]); setLoadError("");
    api(`/${aid}/subtitles`).then((r) => r.json()).then((data) => {
      if (!alive) return;
      setActive(data.active); setVersion(data.draft || data.active);
      setSegments((data.draft || data.active)?.segments || []); setDraft(!!data.draft);
      onCaptions((data.draft || data.active)?.segments || []);
    }).catch((e) => alive && setLoadError(e.message));
    return () => { alive = false; };
  }, [aid, api, signal, onCaptions]);
  function update(i: number, key: keyof Caption, value: string | number) {
    setSegments(segments.map((s, n) => n === i ? { ...s, [key]: value } : s));
  }
  return <section className="border rounded-lg p-4 space-y-3">
    <h2 className="text-xl font-medium">Legendas automáticas</h2>
    <p>Transcrição local em português, seguida de revisão. As legendas são guardadas separadamente do vídeo. O primeiro pedido pode demorar mais enquanto o modelo é descarregado.</p>
    {!premium && <p>A geração e edição requerem perfil premium.</p>}
    {!hasAudio && <p>Este vídeo não tem áudio; não é possível gerar legendas automáticas.</p>}
    {loadError && <p role="alert" className="text-destructive">{loadError}</p>}
    <Button disabled={!canRun || !premium || !hasAudio} onClick={() => {
      if (active && !window.confirm("Criar um novo rascunho? As legendas atuais só serão substituídas após rever e guardar.")) return;
      void perform(async () => { await api(`/${aid}/jobs`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ operation: "subtitles", replace_confirmed: !!active }) }); notify?.("Rascunho de legendas solicitado. Acompanhe o pedido em Processamentos e reveja-o antes de guardar."); });
    }}>Gerar rascunho de legendas</Button>
    {version && <>
      <p>{draft ? "Pré-visualização do rascunho por rever. As legendas anteriores continuam guardadas até confirmar a substituição." : "Legendas guardadas. Pode corrigir o texto e os tempos."}</p>
      <div className="space-y-3 max-h-80 overflow-y-auto">
        {segments.map((segment, i) => <div key={i} className="border rounded p-3 space-y-2">
          <div className="flex flex-wrap gap-2"><span>{i + 1}.</span>
            <label>Início (s) <input aria-label={`Início da legenda ${i + 1}`} disabled={!premium || !canRun} className="border rounded p-1 bg-background w-24" type="number" step={.01} min={0} max={duration} value={segment.start} onChange={(e) => update(i, "start", Number(e.target.value))} /></label>
            <label>Fim (s) <input aria-label={`Fim da legenda ${i + 1}`} disabled={!premium || !canRun} className="border rounded p-1 bg-background w-24" type="number" step={.01} min={0} max={duration} value={segment.end} onChange={(e) => update(i, "end", Number(e.target.value))} /></label>
          </div>
          <textarea aria-label={`Texto da legenda ${i + 1}`} disabled={!premium || !canRun} className="border rounded p-2 bg-background w-full" value={segment.text} maxLength={2000} onChange={(e) => update(i, "text", e.target.value)} />
          {premium && <Button variant="outline" disabled={!canRun} onClick={() => setSegments(segments.filter((_, n) => n !== i))}>Remover segmento</Button>}
        </div>)}
      </div>
      <Button variant="outline" onClick={() => void perform(async () => {
        let previous = 0;
        for (const s of segments) {
          if (!s.text.trim() || !Number.isFinite(s.start) || !Number.isFinite(s.end) || s.start < previous || s.start < 0 || s.start >= s.end || s.end > duration) throw new Error("Corrija o texto e os tempos antes de pré-visualizar.");
          previous = s.end;
        }
        onCaptions(segments);
      })}>Pré-visualizar estas legendas</Button>
      {active && <Button variant="outline" onClick={() => onCaptions(active.segments)}>Ver legendas guardadas</Button>}
      {premium && <div className="flex flex-wrap gap-3">
        <Button variant="outline" disabled={!canRun || segments.length >= 500} onClick={() => { const start = segments.at(-1)?.end || 0; setSegments([...segments, { start, end: Math.min(duration, start + 1), text: "" }]); }}>Adicionar segmento</Button>
        <Button disabled={!canRun} onClick={() => {
          if (active && active._id !== version._id && !window.confirm("Substituir as legendas ativas pelas legendas revistas?")) return;
          void perform(async () => {
            const doc = await (await api(`/${aid}/subtitles/${version._id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ segments, expected_active_id: active?._id || null, replace_confirmed: !!active }) })).json();
            setActive(doc); setVersion(doc); setDraft(false); onCaptions(doc.segments);
            notify?.("Legendas revistas guardadas. Pode descarregá-las em SRT ou VTT.");
          });
        }}>Guardar legendas revistas</Button>
      </div>}
    </>}
    {active && <Button variant="outline" onClick={() => void perform(async () => {
      const blob = await (await api(`/${aid}/subtitles.srt`)).blob(); const url = URL.createObjectURL(blob);
      const link = document.createElement("a"); link.href = url; link.download = "legendas.srt"; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    })}>Descarregar SRT</Button>}
    {active && <Button variant="outline" onClick={() => {
      const url = URL.createObjectURL(new Blob([toWebVtt(active.segments)], { type: "text/vtt;charset=utf-8" }));
      const link = document.createElement("a"); link.href = url; link.download = "legendas.vtt"; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    }}>Descarregar VTT</Button>}
  </section>;
}

export function ThumbnailControls({ aid, duration, canRun, api, perform, notify, currentId, canEdit, getCurrentTime }: ControlProps & { currentId?: string; canEdit: boolean }) {
  const [timestamp, setTimestamp] = useState(0);
  const [preview, setPreview] = useState("");
  const [current, setCurrent] = useState("");
  const [loadError, setLoadError] = useState("");
  useEffect(() => () => { if (preview) URL.revokeObjectURL(preview); }, [preview]);
  useEffect(() => { setTimestamp(0); setPreview(""); }, [aid]);
  useEffect(() => {
    let alive = true; let url = ""; setCurrent(""); setLoadError("");
    if (currentId) api(`/${aid}/thumbnail/file`).then((r) => r.blob()).then((blob) => {
      if (alive) { url = URL.createObjectURL(blob); setCurrent(url); }
    }).catch((e) => alive && setLoadError(e.message));
    return () => { alive = false; if (url) URL.revokeObjectURL(url); };
  }, [aid, currentId, api]);
  return <section className="border rounded-lg p-4 space-y-3">
    <h2 className="text-xl font-medium">Miniatura</h2>
    <p className="text-muted-foreground">Escolha uma imagem de apresentação para o vídeo. Pause o leitor no momento pretendido e use esse instante.</p>
    <p className="text-xs text-muted-foreground">JPEG · 320 × 180 · não consome quota</p>
    {loadError && <p role="alert" className="text-destructive">{loadError}</p>}
    {current && <div><p>Miniatura atual</p><img src={current} alt="Miniatura atual do vídeo" width={320} height={180} /></div>}
    {canEdit && <>
      <label className="block">Instante (s) <input className="border rounded p-2 bg-background w-28" type="number" min={0} max={duration} step={.01} value={timestamp} onChange={(e) => { setTimestamp(Number(e.target.value)); setPreview(""); }} /></label>
      {getCurrentTime && <Button size="sm" variant="outline" onClick={() => { setTimestamp(Math.min(duration, getCurrentTime())); setPreview(""); }}>Usar instante do leitor</Button>}
      <Button variant="outline" disabled={!canRun} onClick={() => void perform(async () => {
        const blob = await (await api(`/${aid}/thumbnail-preview`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ timestamp }) })).blob();
        setPreview(URL.createObjectURL(blob));
      })}>Pré-visualizar miniatura</Button>
      {preview && <div className="space-y-2"><img src={preview} alt="Nova miniatura proposta" width={320} height={180} />
        <Button disabled={!canRun} onClick={() => {
          if (currentId && !window.confirm("Substituir a miniatura atual por esta miniatura?")) return;
          void perform(async () => { await api(`/${aid}/thumbnail`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ timestamp, expected_thumbnail_id: currentId || null, replace_confirmed: !!currentId }) }); setPreview(""); notify?.("Miniatura guardada e disponível para download."); });
        }}>{currentId ? "Substituir miniatura" : "Guardar miniatura"}</Button>
      </div>}
    </>}
    {current && <Button variant="outline" onClick={() => void perform(async () => {
      const blob = await (await api(`/${aid}/thumbnail/file`)).blob(); const url = URL.createObjectURL(blob);
      const link = document.createElement("a"); link.href = url; link.download = "miniatura.jpg"; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    })}>Descarregar miniatura</Button>}
  </section>;
}
