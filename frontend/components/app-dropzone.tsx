"use client";

import { useToast } from "@/hooks/use-toast";
import React, { useState } from "react";
import Dropzone from "react-dropzone";

interface CustomDropzoneProps {
  onDrop: (acceptedFiles: File[]) => void;
  children?: React.ReactNode;
  allowVideos?: boolean;
  inputLabel?: string;
  videosOnly?: boolean;
  disabled?: boolean;
}

export default function AppDropzone({ onDrop, children, allowVideos = false, inputLabel, videosOnly = false, disabled = false }: CustomDropzoneProps) {
  const [isDragging, setIsDragging] = useState<boolean>(false);
  const { toast } = useToast();

  return (
    <div className="h-full w-full flex flex-col">
      <Dropzone
        disabled={disabled}
        onDrop={onDrop}
        onDragEnter={() => setIsDragging(true)}
        onDragLeave={() => setIsDragging(false)}
        onDropAccepted={() => setIsDragging(false)}
        onDropRejected={() => {
          setIsDragging(false);
          toast({
            title: "Invalid file type!",
            description: videosOnly ? "Escolha vídeos MP4." : allowVideos ? "Escolha imagens PNG/JPEG, vídeo MP4 ou um ZIP de imagens." : "Please upload a .png, .jpeg, .jpg, or .zip file",
            variant: "destructive",
          });
        }}
        accept={videosOnly ? { "video/mp4": [".mp4"] } : {
          "image/png": [".png"],
          "image/jpeg": [".jpeg"],
          "image/jpg": [".jpg"],
          "application/zip": [".zip"],
          ...(allowVideos ? { "video/mp4": [".mp4"] } : {}),
        }}
      >
        {({ getRootProps, getInputProps }) => (
          <div
            {...getRootProps()}
            className={`flex h-full w-full ${disabled ? "cursor-not-allowed opacity-60" : "cursor-pointer"} items-center justify-center rounded-xl border-2 border-dashed ${
              isDragging
                ? "border-primary bg-primary/10 text-primary"
                : "border-neutral-400 hover:bg-sidebar text-neutral-400"
            } p-4 text-center transition-colors`}
          >
            <input {...getInputProps()} aria-label={inputLabel} />
            {children}
          </div>
        )}
      </Dropzone>
    </div>
  );
}
