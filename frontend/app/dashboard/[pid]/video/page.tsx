"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useSearchParams } from "next/navigation";
import Link from "next/link";
import { useSession } from "@/providers/session-provider";
import { Button } from "@/components/ui/button";
import { Caption, ColorControls, TransitionControls, SubtitleControls, ThumbnailControls } from "./controls";
import { Asset, Library, assetLabel, operationLabel, processingNotice, toWebVtt } from "./presentation";
import AppDropzone from "@/components/app-dropzone";
import { isVideoFile } from "@/lib/upload-files";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { SidebarTrigger } from "@/components/ui/sidebar";
import { ArrowLeft, Captions, Check, Download, Film, Gauge, History, ImageIcon, Layers, LoaderCircle, Palette, Plus, RefreshCw, Trash2, Upload, X } from "lucide-react";

const tools = [
  { id: "speed", label: "Velocidade", icon: Gauge },
  { id: "color", label: "Cor e filtros", icon: Palette },
  { id: "transition", label: "Transições", icon: Layers },
  { id: "subtitles", label: "Legendas", icon: Captions },
  { id: "thumbnail", label: "Miniatura", icon: ImageIcon },
] as const;
type Tool = typeof tools[number]["id"];

export default function VideoPage() {
  const session = useSession();
  const { pid } = useParams<{ pid: string }>();
  const search = useSearchParams();
  const owner = search.get("owner") || session.user._id;
  const share = search.get("share");
  const suffix = share ? `?share=${encodeURIComponent(share)}` : "";
  const base = `/api-gateway/projects/${owner}/${pid}/videos`;
  const [library, setLibrary] = useState<Library | null>(null);
  const [selected, setSelected] = useState("");
  const [factor, setFactor] = useState("1.5");
  const [preview, setPreview] = useState("");
  const [captions, setCaptions] = useState<Caption[]>([]);
  const [track, setTrack] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [tool, setTool] = useState<Tool | null>("speed");
  const [historyOpen, setHistoryOpen] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [dragging, setDragging] = useState(false);
  const player = useRef<HTMLVideoElement>(null);
  const asset = library?.assets.find((a) => a._id === selected);
  const active = !!library && (library.jobs.some((j) => ["queued", "processing"].includes(j.state)) || library.assets.some((a) => !!a.active_job));
  const dependencies = library?.assets.filter((a) => a.source_id === selected || a.input_ids?.includes(selected)) || [];

  const api = useCallback(async (path: string, init?: RequestInit) => {
    const response = await fetch(base + path + suffix, {
      ...init, headers: { Authorization: `Bearer ${session.token}`, ...init?.headers },
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.detail || (typeof detail === "string" ? detail : "Não foi possível concluir o pedido."));
    }
    return response;
  }, [base, suffix, session.token]);

  const refresh = useCallback(async () => {
    if (session.user.type === "anonymous") return;
    setLoading(true);
    try {
      const data: Library = await (await api("")).json();
      setLibrary(data);
      setSelected((current) => data.assets.some((a) => a._id === current) ? current : data.assets[0]?._id || "");
    } catch (e) { setError((e as Error).message); }
    finally { setLoading(false); }
  }, [api, session.user.type]);

  useEffect(() => {
    setLibrary(null); setSelected(""); setError(""); setNotice("");
    void refresh();
    const visible = () => { if (!document.hidden) void refresh(); };
    document.addEventListener("visibilitychange", visible);
    window.addEventListener("focus", visible);
    return () => {
      document.removeEventListener("visibilitychange", visible);
      window.removeEventListener("focus", visible);
    };
  }, [refresh]);

  useEffect(() => {
    if (!active) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    // Poll only while work exists and the tab is visible. Schedule after the
    // previous response, so slow requests do not overlap indefinitely.
    const schedule = () => {
      if (stopped || document.hidden) return;
      timer = setTimeout(async () => { await refresh(); schedule(); }, 2000);
    };
    const visibility = () => { clearTimeout(timer); schedule(); };
    schedule(); document.addEventListener("visibilitychange", visibility);
    return () => { stopped = true; clearTimeout(timer); document.removeEventListener("visibilitychange", visibility); };
  }, [active, refresh]);

  useEffect(() => {
    setPreview("");
    if (!selected || asset?.deleting) return;
    let alive = true;
    let url = "";
    api(`/${selected}/file`).then((r) => r.blob()).then((blob) => {
      if (alive) { url = URL.createObjectURL(blob); setPreview(url); }
    }).catch((e) => alive && setError(e.message));
    // Protected media is fetched with the session header; tokens never appear in media URLs.
    return () => { alive = false; if (url) URL.revokeObjectURL(url); };
  }, [selected, asset?.deleting, api]);

  useEffect(() => {
    const content = toWebVtt(captions);
    const url = captions.length ? URL.createObjectURL(new Blob([content], { type: "text/vtt" })) : "";
    setTrack(url);
    return () => { if (url) URL.revokeObjectURL(url); };
  }, [captions]);

  async function perform(task: () => Promise<void>) {
    setBusy(true); setError(""); setNotice("");
    try { await task(); }
    catch (e) { setError((e as Error).message); }
    finally { await refresh(); setBusy(false); }
  }

  async function download() {
    if (!asset) return;
    const blob = await (await api(`/${asset._id}/file`)).blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a"); link.href = url; link.download = asset.name; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function uploadFiles(files: File[]) {
    if (!files.length || busy) return;
    await perform(async () => {
      let uploadedCount = 0, reused = 0;
      const failures: string[] = [];
      for (const file of files) {
        try {
          if (!isVideoFile(file)) throw new Error("Escolha um vídeo MP4.");
          if (file.size > 100_000_000) throw new Error("O limite por vídeo é 100 MB.");
          const body = new FormData(); body.append("file", file);
          const uploaded: Asset & { reused_upload?: boolean } = await (await api("", { method: "POST", body })).json();
          setSelected(uploaded._id); uploadedCount++;
          if (uploaded.reused_upload) reused++;
        } catch (e) { failures.push(`${file.name}: ${(e as Error).message}`); }
      }
      // A rejected file must not silently discard the rest of a batch.
      if (uploadedCount) setNotice(files.length === 1
        ? reused ? "Este ficheiro já estava no projeto. Foi selecionado o original existente, sem criar outra cópia." : "Vídeo carregado e disponível na biblioteca."
        : `${uploadedCount} ficheiros tratados: ${uploadedCount - reused} vídeos carregados e ${reused} reutilizados.`);
      if (failures.length) setError(failures.join(" · "));
      else if (uploadedCount) setUploadOpen(false);
    });
  }

  const canRun = library?.can_edit && !!asset && !asset.active_job && !asset.deleting && !busy;
  if (session.user.type === "anonymous") return <div className="p-6 space-y-4">
    <h1 className="text-2xl font-semibold">Vídeos do projeto</h1>
    <p>O vídeo requer uma conta registada ou premium. Entre ou crie uma conta para continuar.</p>
    <Button asChild><Link href="/login">Entrar</Link></Button> <Button variant="outline" asChild><Link href="/register">Criar conta</Link></Button>
  </div>;
  // Keep tool panels mounted when hidden: changing tools must not discard a
  // subtitle draft or a prepared filter chain. The player stays independent.
  const uploadArea = <AppDropzone videosOnly disabled={busy} inputLabel="Vídeos a carregar" onDrop={(files) => void uploadFiles(files)}>
    <div className="space-y-3 py-6">
      <Upload className="mx-auto h-8 w-8 text-primary" aria-hidden="true" />
      <h2 className="font-medium text-foreground">Carregar vídeos</h2>
      <p className="text-sm">Arraste um ou vários MP4 para aqui.</p>
      <Button type="button" variant="outline" className="text-foreground" disabled={busy}>{busy ? "A carregar…" : "Escolher ficheiros"}</Button>
      <p className="text-xs">Até 100 MB · 120 s · 720p · 30 fps<br />MP4 com H.264 e áudio AAC opcional</p>
    </div>
  </AppDropzone>;

  return (
    <div className="flex h-full min-w-0 flex-col overflow-hidden bg-background" aria-label="Editor de vídeo">
      <header className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <SidebarTrigger className="lg:hidden" />
          <Button size="icon" variant="ghost" asChild title="Voltar às imagens"><Link aria-label="Voltar às imagens" href={`/dashboard/${pid}${share ? `?owner=${owner}&share=${share}` : ""}`}><ArrowLeft className="h-4 w-4" /></Link></Button>
          <h1 className="text-base font-semibold">Vídeos do projeto</h1>
          {library && <span className="hidden text-xs text-muted-foreground xl:inline">{library.profile === "premium" ? "Premium · sem limite diário" : `${library.remaining} operações disponíveis hoje`}</span>}
        </div>
        <div className="flex items-center gap-1.5">
          <Button variant="ghost" size="icon" aria-label="Atualizar biblioteca" title="Atualizar biblioteca" disabled={loading || busy} onClick={() => { setError(""); void refresh(); }}><RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} /></Button>
          <Button variant="outline" aria-label="Processamentos" onClick={() => setHistoryOpen(true)}><History className="mr-1.5 h-4 w-4" /><span className="hidden sm:inline">Processamentos</span><span className="sm:hidden">Pedidos</span>{active && <span className="ml-2 h-2 w-2 rounded-full bg-blue-500 motion-safe:animate-pulse" aria-label="Em curso" />}</Button>
          {library?.can_edit && <Button onClick={() => setUploadOpen(true)} disabled={busy}><Plus className="mr-1 h-4 w-4" /><span className="hidden sm:inline">Adicionar vídeo</span><span className="sm:hidden">Vídeo</span></Button>}
          <Button variant="outline" size="icon" aria-label="Descarregar esta versão MP4" title="Descarregar esta versão MP4" disabled={!asset || !!asset.deleting || busy} onClick={() => void perform(download)}><Download className="h-4 w-4" /></Button>
          {library?.can_edit && <Button variant="ghost" size="icon" aria-label="Apagar versão" title={dependencies.length ? "Apague primeiro as versões dependentes" : asset?.active_job ? "Aguarde ou cancele o processamento" : "Apagar versão"} disabled={!asset || busy || !!asset.active_job || dependencies.length > 0} onClick={() => {
            if (!asset || !window.confirm(`Apagar definitivamente ${asset.name}, incluindo as suas miniaturas e legendas?`)) return;
            void perform(async () => { await api(`/${asset._id}`, { method: "DELETE" }); setNotice("Versão apagada, incluindo os seus ficheiros associados."); });
          }}><Trash2 className="h-4 w-4 text-destructive" /></Button>}
        </div>
      </header>
      {error && !uploadOpen && <p role="alert" className="max-h-20 shrink-0 overflow-auto border-b bg-destructive/5 px-4 py-2 text-sm text-destructive">{error}</p>}
      {notice && <p role="status" className="max-h-20 shrink-0 overflow-auto border-b bg-primary/5 px-4 py-2 text-sm">{notice}</p>}
      {!library && <p role="status" className="p-4 text-sm">A carregar biblioteca de vídeo…</p>}
      <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
        <nav aria-label="Ferramentas de vídeo" className="flex shrink-0 gap-1 overflow-x-auto border-b bg-muted/20 p-2 lg:w-24 lg:flex-col lg:border-b-0 lg:border-r">
          {tools.map(({ id, label, icon: Icon }) => <button key={id} type="button" aria-label={`Ferramenta: ${label}`} aria-pressed={tool === id} aria-controls={`video-tool-${id}`} disabled={!asset || (!library?.can_edit && !["subtitles", "thumbnail"].includes(id))} onClick={() => setTool(tool === id ? null : id)} className={`flex min-w-[72px] shrink-0 flex-col items-center justify-center gap-1.5 rounded-lg px-2 py-3 text-[11px] font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${tool === id ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground"}`}>
            <Icon className="h-5 w-5" aria-hidden="true" /><span>{label}</span>
          </button>)}
        </nav>
        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          <section id="video-player" aria-label="Versão selecionada" className="relative flex min-h-0 flex-1 flex-col p-3 lg:p-4" onDragOver={(event) => {
            if (!event.dataTransfer.types.includes("Files")) return;
            event.preventDefault(); event.dataTransfer.dropEffect = library?.can_edit && !busy ? "copy" : "none";
            if (library?.can_edit && !busy) setDragging(true);
          }} onDragLeave={(event) => {
            if (!(event.relatedTarget instanceof Node) || !event.currentTarget.contains(event.relatedTarget)) setDragging(false);
          }} onDrop={(event) => {
            setDragging(false);
            // Nested upload boxes already handle their drop; do not send it twice.
            if (event.defaultPrevented || !event.dataTransfer.files.length) return;
            event.preventDefault();
            if (library?.can_edit && !busy) void uploadFiles(Array.from(event.dataTransfer.files));
          }}>
            {dragging && <div className="pointer-events-none absolute inset-2 z-20 flex items-center justify-center rounded-xl border-2 border-dashed border-primary bg-background/95 p-6 text-center"><div><Upload className="mx-auto mb-3 h-8 w-8 text-primary" /><p className="font-medium">Largue os MP4 para adicionar ao projeto</p><p className="mt-1 text-sm text-muted-foreground">O vídeo atual e as suas versões são conservados.</p></div></div>}
            {asset ? <>
              <div className="mb-2 flex shrink-0 flex-wrap items-center justify-between gap-2">
                <div className="min-w-0 flex-1">
                  <h2 className="truncate text-sm font-medium" title={assetLabel(asset)}>{assetLabel(asset)}</h2>
                  <p className="text-xs text-muted-foreground">{asset.metadata.duration.toFixed(2)} s · {asset.metadata.width} × {asset.metadata.height} · {asset.metadata.fps.toFixed(2)} fps · {asset.metadata.audio ? "com áudio" : "sem áudio"}</p>
                </div>
                {asset.active_job && <span role="status" className="flex items-center gap-1 text-xs text-primary"><LoaderCircle className="h-3 w-3 animate-spin" />A processar</span>}
              </div>
              <div className="relative min-h-24 flex-1 overflow-hidden rounded-xl bg-slate-950">
                {asset.deleting ? <p role="alert" className="p-5 text-sm text-white">Eliminação pendente. Volte a carregar em Apagar versão para concluir a limpeza.</p> : preview ? <video ref={player} key={preview} controls src={preview} className="absolute inset-0 h-full w-full object-contain">
                  {track && <track key={track} kind="subtitles" label="Português" srcLang="pt" src={track} default />}
                </video> : <div className="flex h-full items-center justify-center gap-2 p-5 text-sm text-slate-300"><LoaderCircle className="h-4 w-4 animate-spin" />A carregar pré-visualização…</div>}
              </div>
              <p className="mt-2 shrink-0 truncate text-xs text-muted-foreground" title={dependencies.length > 0 ? "Apague primeiro as versões dependentes se quiser remover esta versão." : undefined}>{dependencies.length > 0 ? `${dependencies.length} versões dependem desta. Apague-as primeiro para a remover.` : "Cada edição cria uma versão. O original continua disponível em baixo."}</p>
            </> : <div className="flex min-h-0 flex-1 items-center justify-center overflow-y-auto">
              <div className="w-full max-w-md space-y-3 p-2">
                <div className="text-center"><Film className="mx-auto mb-2 h-8 w-8 text-muted-foreground" /><h2 className="font-semibold">Comece pelo seu vídeo</h2><p className="text-sm text-muted-foreground">Ainda não existem vídeos. Carregue um MP4 para começar; não precisa de adicionar imagens.</p></div>
                {library?.can_edit && !uploadOpen && uploadArea}
              </div>
            </div>}
          </section>
          <section aria-label="Biblioteca de versões" className="shrink-0 border-t bg-muted/20 px-3 py-2">
            <div className="mb-2 flex items-center justify-between gap-2">
              <h2 className="text-xs font-semibold">Vídeos e versões <span className="font-normal text-muted-foreground">· {library?.assets.length || 0}{library && ` · ${library.profile === "premium" ? "Premium" : `${library.remaining} operações hoje`}`}</span></h2>
              <select aria-label="Vídeo / versão" className="max-w-[55%] rounded border bg-background px-2 py-1 text-xs" value={selected} onChange={(e) => setSelected(e.target.value)}><option value="">Selecione um vídeo</option>{library?.assets.map((a) => <option key={a._id} value={a._id}>{assetLabel(a)}</option>)}</select>
            </div>
            <div className="flex gap-2 overflow-x-auto pb-1" aria-label="Escolher versão">
              {library?.assets.map((a) => <button key={a._id} type="button" aria-label={`Selecionar versão: ${assetLabel(a)}`} aria-pressed={selected === a._id} onClick={() => setSelected(a._id)} className={`flex w-44 shrink-0 items-center gap-2 rounded-lg border p-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${selected === a._id ? "border-primary bg-primary/5 ring-1 ring-primary" : "bg-background hover:border-primary/50"}`}>
                <div className={`flex h-10 w-10 shrink-0 items-center justify-center rounded ${selected === a._id ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground"}`}>{a.active_job ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Film className="h-4 w-4" />}</div>
                <div className="min-w-0 flex-1"><p className="truncate text-xs font-medium" title={a.name}>{a.name}</p><p className="truncate text-[10px] text-muted-foreground" title={assetLabel(a)}>{a.deleting ? "Eliminação pendente" : a.kind === "original" ? "Original" : operationLabel(a.operation_params)}</p><p className="text-[10px] text-muted-foreground">{a.metadata.duration.toFixed(1)} s</p></div>
                {selected === a._id && <Check className="h-3 w-3 shrink-0 text-primary" />}
              </button>)}
            </div>
          </section>
        </div>
        {asset && <aside aria-label="Opções da ferramenta" hidden={!tool} className={`min-h-0 shrink-0 border-t bg-background lg:w-[340px] lg:border-l lg:border-t-0 xl:w-[360px] ${tool ? "flex h-[43%] flex-col lg:h-auto" : "hidden"}`}>
          <div className="flex shrink-0 items-center justify-between border-b px-4 py-2"><p className="text-xs font-medium text-muted-foreground">Opções da ferramenta</p><Button variant="ghost" size="icon" aria-label="Fechar opções da ferramenta" onClick={() => setTool(null)} className="h-7 w-7"><X className="h-4 w-4" /></Button></div>
          <div className="min-h-0 flex-1 overflow-y-auto p-3 text-sm [&_section]:border-0 [&_section]:p-1 [&_h2]:text-base [&_p]:text-sm [&_input]:max-w-full [&_img]:max-w-full">
            {!library?.can_edit && <p className="mb-3 rounded bg-muted p-3 text-sm">Este projeto foi partilhado apenas para leitura.</p>}
            <div id="video-tool-speed" hidden={tool !== "speed"}>
              <section className="space-y-4"><h2 className="font-semibold">Controlo de velocidade</h2><p className="text-muted-foreground">Acelere ou abrande o vídeo completo, mantendo a altura tonal do áudio.</p>
                <div role="group" aria-label="Velocidade" className="grid grid-cols-4 gap-1">{[.5, 1, 1.5, 2].map((f) => <Button key={f} variant={factor === String(f) ? "default" : "outline"} aria-pressed={factor === String(f)} disabled={!canRun} onClick={() => setFactor(String(f))}>{f}×</Button>)}</div>
                <div className="rounded-lg bg-muted/50 p-3"><p className="text-xs text-muted-foreground">Duração prevista</p><p className="text-xl font-semibold">{(asset.metadata.duration / Number(factor)).toFixed(2)} s</p></div>
                <Button className="w-full" disabled={!canRun} onClick={() => void perform(async () => { const result = await (await api(`/${selected}/jobs`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ operation: "speed", factor: Number(factor) }) })).json(); setNotice(processingNotice(result)); })}>Aplicar velocidade</Button>
              </section>
            </div>
            <div id="video-tool-color" hidden={tool !== "color"}>{library?.can_edit && <ColorControls aid={selected} duration={asset.metadata.duration} fps={asset.metadata.fps} canRun={!!canRun} api={api} perform={perform} notify={setNotice} getCurrentTime={() => player.current?.currentTime || 0} />}</div>
            <div id="video-tool-transition" hidden={tool !== "transition"}>{library?.can_edit && <TransitionControls aid={selected} duration={asset.metadata.duration} fps={asset.metadata.fps} canRun={!!canRun} api={api} perform={perform} notify={setNotice} clips={library.assets.filter((a) => !a.deleting)} />}</div>
            <div id="video-tool-subtitles" hidden={tool !== "subtitles"}>{library && <SubtitleControls aid={selected} duration={asset.metadata.duration} fps={asset.metadata.fps} canRun={!!canRun} api={api} perform={perform} notify={setNotice} premium={library.profile === "premium"} hasAudio={asset.metadata.audio} signal={library.jobs.filter((j) => j.asset_id === selected && j.subtitle_id).map((j) => j.subtitle_id).join(",")} onCaptions={setCaptions} />}</div>
            <div id="video-tool-thumbnail" hidden={tool !== "thumbnail"}>{library && <ThumbnailControls aid={selected} duration={asset.metadata.duration} fps={asset.metadata.fps} canRun={!!canRun} api={api} perform={perform} notify={setNotice} currentId={asset.thumbnail_id} canEdit={library.can_edit} getCurrentTime={() => player.current?.currentTime || 0} />}</div>
          </div>
        </aside>}
      </div>
      <Dialog open={uploadOpen} onOpenChange={(open) => { if (!busy) setUploadOpen(open); }}><DialogContent className="max-h-[85dvh] overflow-y-auto"><DialogHeader><DialogTitle>Adicionar vídeos</DialogTitle><DialogDescription>Escolha os ficheiros ou arraste-os para a caixa. Pode carregar vários de uma vez.</DialogDescription></DialogHeader>{library?.can_edit && uploadArea}{error && <p role="alert" className="text-sm text-destructive">{error}</p>}</DialogContent></Dialog>
      <Dialog open={historyOpen} onOpenChange={setHistoryOpen}><DialogContent className="max-h-[85dvh] max-w-2xl overflow-y-auto"><DialogHeader><DialogTitle>Processamentos {active && "em curso"}</DialogTitle><DialogDescription>Acompanhe os últimos 30 pedidos, cancele um processamento ou abra o resultado.</DialogDescription></DialogHeader>
        {library && !library.jobs.length && <div className="py-8 text-center text-sm text-muted-foreground"><History className="mx-auto mb-3 h-8 w-8" /><p>Ainda não foi pedido nenhum processamento.</p></div>}
        {library?.jobs.map((j) => <div key={j._id} className="border rounded p-3 flex flex-wrap items-center gap-3">
          <div className="min-w-0 flex-1">
            <h3 className="font-medium">{operationLabel(j.params)}</h3>
            <p>Entrada: {j.input_names?.join(" + ") || j.asset_name || library.assets.find((a) => a._id === j.asset_id)?.name || "Versão anterior"}{j.asset_deleted ? " (removida)" : ""}</p>
            <p className="text-sm text-muted-foreground">{new Date(j.created).toLocaleString("pt-PT")}</p>
            <p role="status">{({ queued: "Em fila", processing: "A processar", completed: "Concluído", failed: "Falhou", cancelled: "Cancelado" } as Record<string, string>)[j.state]} · {j.progress}%</p>
            {["queued", "processing"].includes(j.state) && <progress aria-label={`Progresso de ${operationLabel(j.params)}`} value={j.progress} max={100} className="w-full" />}
          </div>
          {j.error && <span role="alert">{j.error}</span>}
          {j.message && <span>{j.message}</span>}
          {j.result_deleted && <span>O vídeo resultante foi removido.</span>}
          {j.result_id && library.assets.some((a) => a._id === j.result_id && !a.deleting) && <Button variant="outline" onClick={() => {
            setSelected(j.result_id!); setNotice("Vídeo resultante selecionado no leitor.");
            setHistoryOpen(false);
          }}>Abrir vídeo resultante</Button>}
          {j.subtitle_id && !j.asset_deleted && <Button variant="outline" onClick={() => { setSelected(j.asset_id); setTool("subtitles"); setHistoryOpen(false); setNotice("Rascunho selecionado para revisão."); }}>Rever rascunho de legendas</Button>}
          {library.can_edit && ["queued", "processing"].includes(j.state) && <Button variant="outline" disabled={busy} onClick={() => void perform(async () => { await api(`/jobs/${j._id}/cancel`, { method: "POST" }); })}>Cancelar</Button>}
        </div>)}
      </DialogContent></Dialog>
    </div>
  );
}
