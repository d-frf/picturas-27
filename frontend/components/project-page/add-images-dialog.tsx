"use client";

import { useEffect, useState } from "react";
import {
  Dialog,
  DialogTrigger,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import ImageSubmissionArea from "../image-submission-area";
import { LoaderCircle, Plus } from "lucide-react";
import { useAddProjectImages } from "@/lib/mutations/projects";
import { useProjectInfo } from "@/providers/project-provider";
import { useSession } from "@/providers/session-provider";
import { useToast } from "@/hooks/use-toast";
import { getErrorMessage } from "@/lib/error-messages";

export function AddImagesDialog() {
  const [open, setOpen] = useState(false);
  const [imageFiles, setImageFiles] = useState<File[]>([]);
  const [preparing, setPreparing] = useState(false);
  const { toast } = useToast();

  const { _id: pid, version} = useProjectInfo();
  const session = useSession();

  const addImages = useAddProjectImages(
    session.user._id,
    pid as string,
    session.token,
  );

  useEffect(() => { if (!open) setImageFiles([]); }, [open]);

  function handleAdd() {
    addImages.mutate(
      {
        images: imageFiles,
        projectVersion: version,
      },
      {
        onSuccess: () => {
          toast({
            title: "Images added successfully.",
          });
          setOpen(false);
        },
        onError: (error) => {
          const { title, description } = getErrorMessage("project-upload", error);
          toast({
            title,
            description,
            variant: "destructive",
          });
        },
      },
    );
  }

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!addImages.isPending && !preparing) setOpen(next); }}>
      <DialogTrigger asChild>
        <Button className="inline-flex" variant="outline">
          <Plus /> Add Images
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-[425px]">
        <DialogHeader>
          <DialogTitle>Add Images</DialogTitle>
          <DialogDescription>
            Add more images to your project.
          </DialogDescription>
        </DialogHeader>
        <ImageSubmissionArea key={String(open)} files={imageFiles} onDrop={setImageFiles} disabled={addImages.isPending} onPreparingChange={setPreparing} />
        <DialogFooter>
          <Button
            onClick={handleAdd}
            disabled={imageFiles.length === 0 || addImages.isPending || preparing}
            className="inline-flex items-center gap-1"
          >
            <span>Add</span>
            {addImages.isPending && (
              <LoaderCircle className="size-[1em] animate-spin" />
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
