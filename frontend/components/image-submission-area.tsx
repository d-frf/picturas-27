import { useRef, useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { HoverCard, HoverCardTrigger, HoverCardContent } from "./ui/hover-card";
import { FileImage, FileVideo, Files, X } from "lucide-react";
import AppDropzone from "./app-dropzone";
import ImagePreview from "./image-preview";
import { isVideoFile, mergeUploadFiles } from "@/lib/upload-files";

export default function ImageSubmissionArea({ files, onDrop, onPreparingChange, allowVideos = false, videosOnly = false, disabled = false, inputLabel = "Imagens a adicionar" }: {
  files: File[]; onDrop: (files: File[]) => void; allowVideos?: boolean;
  videosOnly?: boolean; disabled?: boolean; inputLabel?: string; onPreparingChange?: (busy: boolean) => void;
}) {
  const [preparing, setPreparing] = useState(false);
  const [error, setError] = useState("");
  const reading = useRef(false);

  async function handleDrop(incoming: File[]) {
    if (disabled || reading.current) return;
    reading.current = true; setPreparing(true); onPreparingChange?.(true); setError("");
    try {
      // The parent owns the complete selection. Appending a second batch
      // must not leave thumbnails that will never be sent to the server.
      onDrop(await mergeUploadFiles(files, incoming, allowVideos || videosOnly));
    } catch (e) { setError((e as Error).message); }
    finally { reading.current = false; setPreparing(false); onPreparingChange?.(false); }
  }

  return <div className="space-y-2" aria-busy={preparing}>
    <AppDropzone onDrop={(incoming) => void handleDrop(incoming)} allowVideos={allowVideos} videosOnly={videosOnly} disabled={disabled || preparing} inputLabel={inputLabel}>
      <div className={`w-full space-y-4 ${files.length ? "min-h-32" : "py-10"}`}>
        {!files.length && <Files size={60} className="mx-auto" />}
        <p className="font-medium">{videosOnly ? "Arraste vídeos MP4 para aqui" : allowVideos ? "Arraste imagens, vídeos MP4 ou um ZIP de imagens" : "Arraste imagens ou um ZIP de imagens"}</p>
        {files.length > 0 && <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 max-h-64 overflow-y-auto p-1">
          {files.map((file, index) => <HoverCard key={`${file.name}-${index}`}>
            <HoverCardTrigger asChild>
              <Card className="relative min-w-0">
                <CardContent className="p-3">
                  {isVideoFile(file) ? <FileVideo className="mx-auto" /> : <FileImage className="mx-auto" />}
                  <p className="text-xs truncate mt-2" title={file.name}>{file.name}</p>
                  <button type="button" aria-label={`Retirar ficheiro ${file.name}`} disabled={disabled || preparing} className="absolute top-1 right-1 p-1 text-foreground/60" onClick={(event) => {
                    event.stopPropagation(); onDrop(files.filter((_, position) => position !== index));
                  }}><X size={14} /></button>
                </CardContent>
              </Card>
            </HoverCardTrigger>
            {!isVideoFile(file) && <HoverCardContent className="h-48 w-64 z-50"><ImagePreview file={file} /></HoverCardContent>}
          </HoverCard>)}
        </div>}
        <Button type="button" variant="outline" className="text-foreground" disabled={disabled || preparing}>Escolher ficheiros</Button>
      </div>
    </AppDropzone>
    {preparing && <p role="status">A preparar ficheiros…</p>}
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
  </div>;
}
