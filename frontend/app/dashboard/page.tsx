"use client";

import AppDropzone from "@/components/app-dropzone";
import NewProjectDialog from "@/components/dashboard-sidebar/new-project-dialog";
import { mergeUploadFiles } from "@/lib/upload-files";
import { useToast } from "@/hooks/use-toast";
import { Files } from "lucide-react";
import { useState } from "react";

export default function Dashboard() {
  const [files, setFiles] = useState<File[]>([]);
  const [preparing, setPreparing] = useState(false);
  const { toast } = useToast();

  const handleDrop = async (acceptedFiles: File[]) => {
    if (preparing) return;
    setPreparing(true);
    try { setFiles(await mergeUploadFiles([], acceptedFiles, true)); }
    catch (e) { toast({ title: "Não foi possível preparar os ficheiros", description: (e as Error).message, variant: "destructive" }); }
    finally { setPreparing(false); }
  };

  return (
    <div className="p-8 h-full">
      <NewProjectDialog files={files} setFiles={setFiles} trigger={false}>
        <AppDropzone onDrop={handleDrop} allowVideos disabled={preparing} inputLabel="Ficheiros para novo projeto">
          <div className="flex flex-col gap-4 items-center justify-center max-w-[20rem]">
            <Files size={100} />
            <p className="text-2xl font-medium">
              Selecione um projeto ou carregue imagens, vídeos MP4 ou um ZIP de imagens para criar um novo.
            </p>
          </div>
        </AppDropzone>
      </NewProjectDialog>
    </div>
  );
}
