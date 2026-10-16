import { extractZipImages } from "./utils";

export const isVideoFile = (file: File) => file.type === "video/mp4" || /\.mp4$/i.test(file.name);
const isImageFile = (file: File) => /^image\/(png|jpe?g)$/.test(file.type) || /\.(png|jpe?g)$/i.test(file.name);

async function sameContent(left: File, right: File) {
  if (left === right) return true;
  if (left.size !== right.size) return false;
  // Compare only same-name candidates, in bounded chunks: do not load two
  // possible 100 MB videos into memory just to deduplicate a selection.
  for (let offset = 0; offset < left.size; offset += 1024 * 1024) {
    const [a, b] = await Promise.all([left.slice(offset, offset + 1024 * 1024).arrayBuffer(), right.slice(offset, offset + 1024 * 1024).arrayBuffer()]);
    const first = new Uint8Array(a), second = new Uint8Array(b);
    if (first.some((byte, index) => byte !== second[index])) return false;
  }
  return true;
}

export async function mergeUploadFiles(previous: File[], incoming: File[], allowVideos = false) {
  const expanded: File[] = [];
  for (const file of incoming) {
    if (file.type === "application/zip" || /\.zip$/i.test(file.name)) {
      let images: File[];
      try { images = await extractZipImages(file); }
      catch { throw new Error(`Não foi possível abrir o ZIP ${file.name}.`); }
      if (!images.length) throw new Error(`O ZIP ${file.name} não contém imagens PNG ou JPEG.`);
      expanded.push(...images);
    } else if (isImageFile(file) || (allowVideos && isVideoFile(file))) expanded.push(file);
  }
  const merged = [...previous];
  for (let file of expanded) {
    const basename = file.name.replace(/^.*[\\/]/, "");
    if (basename !== file.name) file = new File([file], basename, { type: file.type, lastModified: file.lastModified });
    let duplicate = false;
    for (const selected of merged.filter((candidate) => candidate.name === file.name && candidate.size === file.size)) {
      if (await sameContent(selected, file)) { duplicate = true; break; }
    }
    if (duplicate) continue;
    // Image storage requires unique names. Preserve the last extension and
    // MIME type, including names such as foto.ferias.2026.png.
    if (!isVideoFile(file)) {
      const dot = file.name.lastIndexOf(".");
      const stem = dot > 0 ? file.name.slice(0, dot) : file.name;
      const extension = dot > 0 ? file.name.slice(dot) : "";
      let name = file.name, count = 1;
      while (merged.some((selected) => selected.name === name)) name = `${stem} (${count++})${extension}`;
      if (name !== file.name) file = new File([file], name, { type: file.type, lastModified: file.lastModified });
    }
    merged.push(file);
  }
  return merged;
}
