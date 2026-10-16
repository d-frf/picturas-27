import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Label } from "../ui/label";
import { Input } from "../ui/input";
import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { useAddProject } from "@/lib/mutations/projects";
import ImageSubmissionArea from "../image-submission-area";
import { useSession } from "@/providers/session-provider";
import { LoaderCircle } from "lucide-react";
import { useRouter } from "next/navigation";
import { getErrorMessage } from "@/lib/error-messages";
import { api } from "@/lib/axios";
import { isVideoFile } from "@/lib/upload-files";

export default function NewProjectDialog({
  files = [],
  setFiles,
  children,
  trigger = true,
}: {
  files?: File[];
  setFiles?: (files: File[]) => void;
  children: React.ReactNode;
  trigger?: boolean;
}) {
  const { toast } = useToast();
  const router = useRouter();

  const [open, setOpen] = useState<boolean>(false);
  const [name, setName] = useState<string>("");
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const imageFiles = selectedFiles.filter((file) => !isVideoFile(file));
  const videoFiles = selectedFiles.filter(isVideoFile);
  const [saving, setSaving] = useState(false);
  const [preparing, setPreparing] = useState(false);
  const [uploadError, setUploadError] = useState("");
  const [createdProjectId, setCreatedProjectId] = useState("");

  const session = useSession();
  const addProject = useAddProject(session.user._id, session.token);

  async function handleCreate() {
    if (saving || preparing || !name.trim()) return;
    if (videoFiles.some((file) => file.size > 100_000_000)) {
      setUploadError("O limite por vídeo é 100 MB. Retire o ficheiro que excede esse tamanho.");
      return;
    }
    if (videoFiles.length && session.user.type === "anonymous") return;
    setSaving(true); setUploadError("");
    try {
      // Keep the project after an upload failure: retry the remaining videos
      // in this same project instead of creating an empty project per attempt.
      let pid = createdProjectId;
      if (!pid) {
        const project = await addProject.mutateAsync({
          uid: session.user._id, token: session.token, name: name.trim(), images: imageFiles,
        });
        pid = project._id;
        setCreatedProjectId(pid);
        setSelectedFiles((pending) => pending.filter(isVideoFile));
      }
      for (const file of videoFiles) {
        const body = new FormData(); body.append("file", file);
        await api.post(`/projects/${session.user._id}/${pid}/videos`, body, {
          headers: { Authorization: `Bearer ${session.token}` },
        });
        setSelectedFiles((pending) => pending.filter((candidate) => candidate !== file));
      }
      setOpen(false);
      toast({
        title: "Projeto criado com sucesso",
        description: videoFiles.length ? "Os vídeos foram carregados no projeto." : imageFiles.length ? "As imagens foram adicionadas ao novo projeto." : "Pode agora carregar o seu vídeo.",
      });
      router.push(`/dashboard/${pid}${videoFiles.length || createdProjectId || !imageFiles.length ? "/video" : ""}`);
    } catch (error) {
      const { title, description } = getErrorMessage("project-create", error);
      setUploadError(description);
      toast({ title, description, variant: "destructive" });
    } finally { setSaving(false); }
  }

  useEffect(() => {
    if (files.length > 0) {
      setSelectedFiles(files);
      setOpen(true);
    }
  }, [files]);

  useEffect(() => {
    if (!open) {
      setName(""); setSelectedFiles([]); setUploadError(""); setCreatedProjectId("");
      if (setFiles) setFiles([]);
    }
  }, [open, setFiles]);

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!saving && !preparing) setOpen(next); }}>
      {trigger ? <DialogTrigger asChild>{children}</DialogTrigger> : children}
      <DialogContent className="max-h-[90dvh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Create New Project</DialogTitle>
        </DialogHeader>
        <Label htmlFor="project-name">Project Name</Label>
        <Input
          id="project-name"
          placeholder="Enter project name"
          value={name}
          disabled={saving || !!createdProjectId}
          onChange={(e) => setName(e.target.value)}
        />
        <ImageSubmissionArea key={String(open)} files={selectedFiles} allowVideos videosOnly={!!createdProjectId} disabled={saving} onPreparingChange={setPreparing}
          inputLabel="Ficheiros do novo projeto" onDrop={(selection) => { setSelectedFiles(selection); setUploadError(""); }} />
        <p className="text-sm text-muted-foreground">Imagens e vídeos são opcionais. Os MP4 selecionados são carregados ao criar o projeto; também pode criar um projeto vazio.</p>
        {videoFiles.length > 0 && session.user.type === "anonymous" && <p role="alert">Entre numa conta registada ou premium para carregar vídeos.</p>}
        {uploadError && <p role="alert" className="text-destructive">{uploadError}</p>}
        {createdProjectId && !saving && <p className="text-sm">O projeto já foi criado. Pode retomar o carregamento dos vídeos pendentes ou <button className="underline" onClick={() => { setOpen(false); router.push(`/dashboard/${createdProjectId}/video`); }}>abrir o projeto</button>.</p>}
        <DialogFooter>
          <Button
            onClick={() => handleCreate()}
            disabled={!name.trim() || saving || preparing || (videoFiles.length > 0 && session.user.type === "anonymous")}
            className="inline-flex items-center gap-1"
          >
            <span>{saving ? "A criar e carregar…" : createdProjectId ? "Retomar carregamento" : "Create"}</span>
            {saving && (
              <LoaderCircle className="size-[1em] animate-spin" />
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
