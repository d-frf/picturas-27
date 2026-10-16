export type Parameters = {
  operation: string; factor?: number; overlap?: number; second_id?: string;
  filters?: { name: string; value: number | boolean }[];
};

export type Asset = {
  _id: string; name: string; kind: string; source_id?: string; input_ids?: string[];
  active_job?: string; thumbnail_id?: string; deleting?: boolean; created: string;
  operation_params?: Parameters;
  metadata: { duration: number; width: number; height: number; fps: number; audio: boolean };
};

export type Job = {
  _id: string; asset_id: string; asset_name?: string; input_names?: string[]; params: Parameters;
  created: string; state: string; progress: number; error?: string; message?: string;
  result_id?: string; subtitle_id?: string; result_deleted?: boolean; asset_deleted?: boolean;
};

export type Library = { assets: Asset[]; jobs: Job[]; can_edit: boolean; profile: string; remaining: number | null };

export function operationLabel(params?: Parameters) {
  if (!params) return "Processamento anterior";
  if (params.operation === "speed") return `Velocidade ${params.factor}×`;
  if (params.operation === "transition") return `Transição de ${params.overlap} s`;
  if (params.operation === "subtitles") return "Legendas automáticas em português";
  const labels: Record<string, string> = { brightness: "Brilho", contrast: "Contraste", saturation: "Saturação", grayscale: "Preto e branco" };
  if (params.operation === "color") return "Cor: " + (params.filters || []).map((f) => `${labels[f.name] || f.name}${typeof f.value === "number" ? " " + f.value : ""}`).join(" → ");
  return "Processamento de vídeo";
}

export function assetLabel(asset: Asset) {
  return `${asset.name} · ${asset.kind === "original" ? "Original" : operationLabel(asset.operation_params)}${asset.deleting ? " · Eliminação pendente" : ""}`;
}

export function processingNotice(result: { noop?: boolean; reused?: boolean; reused_pending?: boolean }) {
  if (result.noop) return "Sem alterações: não foi criado um processamento nem consumida quota.";
  if (result.reused) return "Este pedido já tinha um resultado. Foi reutilizado sem nova operação nem consumo de quota; pode abri-lo em Processamentos.";
  if (result.reused_pending) return "Este pedido já está em curso. Pode acompanhá-lo em Processamentos; não foi criada outra operação.";
  return "Pedido colocado em fila. Acompanhe-o em Processamentos e abra o resultado quando terminar.";
}

export function toWebVtt(items: { start: number; end: number; text: string }[]) {
  const timestamp = (seconds: number) => new Date(Math.round(seconds * 1000)).toISOString().slice(11, 23);
  const escape = (text: string) => text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return "WEBVTT\n\n" + items.map((s) => `${timestamp(s.start)} --> ${timestamp(s.end)}\n${escape(s.text)}`).join("\n\n") + "\n";
}
