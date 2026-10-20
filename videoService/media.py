"""Media operations use argument arrays: filenames and parameters never enter a shell."""
import json
import math
import subprocess
from fractions import Fraction

# Get metadata from video file
def probe(path, validate_input=False):

    # Verifica se o ficheiro é inválido ou está corrompido
    result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                             "-of", "json", str(path)], capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError("Ficheiro de vídeo inválido ou danificado.")

    info = json.loads(result.stdout)
    videos = [s for s in info["streams"] if s["codec_type"] == "video"]
    audios = [s for s in info["streams"] if s["codec_type"] == "audio"]
    
    # Verficação de existência de apenas uma faixa de áudio no pedido
    if len(videos) != 1 or len(audios) > 1:
        raise ValueError("É necessário um vídeo com, no máximo, uma faixa de áudio.")

    v = videos[0]
    duration = float(info["format"].get("duration", 0))
    fps = float(Fraction(v.get("avg_frame_rate", "0/1")))
    width, height = v["width"], v["height"]
    

    rotation = int(next((d.get("rotation", 0) for d in v.get("side_data_list", [])
                         if "rotation" in d), 0)) % 360
    if rotation in (90, 270):
        width, height = height, width
    
    if not math.isfinite(duration) or duration <= 0 or not math.isfinite(fps) or fps <= 0:
        raise ValueError("Duração ou taxa de fotogramas inválida.")

    if validate_input:
        brands = {"isom", "iso2", "iso4", "iso5", "iso6", "mp41", "mp42", "avc1", "M4V ", "MSNV", "dash"}

        # Validar todas as restrições e regras de negócio relativas ao vídeo
        if info["format"].get("tags", {}).get("major_brand") not in brands or v["codec_name"] != "h264":
            raise ValueError("O MVP aceita MP4 com vídeo H.264.")
        if audios and audios[0]["codec_name"] != "aac":
            raise ValueError("A faixa de áudio deve usar AAC.")
        if duration > 120.000001 or fps > 30.000001:
            raise ValueError("Limites: 120 segundos e 30 fotogramas/s.")
        if not ((width <= 1280 and height <= 720) or (width <= 720 and height <= 1280)):
            raise ValueError("Resolução máxima: 1280 × 720 ou 720 × 1280.")
        
        # Comparing nominal FPS alone misses some VFR files; examine actual presentation times.
        frames = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                 "-show_entries", "frame=best_effort_timestamp_time", "-of", "json",
                                 str(path)], capture_output=True, timeout=60, check=True)
        
        times = [float(f["best_effort_timestamp_time"]) for f in json.loads(frames.stdout)["frames"]
                 if "best_effort_timestamp_time" in f]
        
        if len(times) > 2 and any(abs((b - a) - 1 / fps) > .002 for a, b in zip(times, times[1:])):
            raise ValueError("O MVP requer taxa de fotogramas constante.")
        
        v["nb_frames"] = len(times)
    
    return {"duration": duration, "fps": fps, "width": width, "height": height,
            "audio": bool(audios), "size": int(info["format"].get("size", 0)),
            "frames": int(v.get("nb_frames", 0) or 0)}


# Obter o comando de alteração da velocidade do vídeo
def speed_command(source, output, meta, factor):
    command = ["-i", str(source), "-map", "0:v:0", "-vf", f"setpts=(PTS-STARTPTS)/{factor}",
               "-r", str(meta["fps"]), "-c:v", "libx264", "-preset", "fast", "-crf", "20",
               "-pix_fmt", "yuv420p"]
    if meta["audio"]:
        command += ["-map", "0:a:0", "-af", f"asetpts=PTS-STARTPTS,atempo={factor}", "-c:a", "aac"]
    return command + ["-movflags", "+faststart", str(output)]


# Validar se o valor é um número dentro do limite
def finite_number(value, low, high, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{label}: valor entre {low} e {high}.")
    return value

# Obter cadeia de filtros em texto
def filter_chain(items):
    
    if not isinstance(items, list) or len(items) > 8:
        raise ValueError("Escolha até oito filtros, pela ordem de aplicação.")

    expressions = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Filtro inválido.")
        name, value = item.get("name"), item.get("value")
        if name == "brightness":
            value = finite_number(value, -100, 100, "Brilho")
            # Existing image brightness uses a multiplicative factor, with 1 as neutral.
            factor = 1 + value / 100
            if value != 0: expressions.append(f"colorchannelmixer=rr={factor}:gg={factor}:bb={factor}")
        elif name == "contrast":
            value = finite_number(value, .5, 2, "Contraste")
            if value != 1: expressions.append(f"eq=contrast={value}")
        elif name == "saturation":
            value = finite_number(value, 0, 2, "Saturação")
            if value != 1: expressions.append(f"eq=saturation={value}")
        elif name == "grayscale" and isinstance(value, bool):
            if value: expressions.append("hue=s=0")
        else:
            raise ValueError("Filtro desconhecido ou parâmetro inválido.")
    return ",".join(expressions)


# Obter comando de aplicação de cadeia de filtros
def color_command(source, output, chain):
    return ["-i", str(source), "-map", "0:v:0", "-map", "0:a:0?", "-vf", chain,
            "-fps_mode", "passthrough", "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(output)]


# Obter frame de uma certa timestamp
def preview_frame(source, output, timestamp, chain=""):
    # Seek after input for an accurate frame; no video version or quota is created.
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-threads", "2",
                    "-filter_threads", "1", "-i", str(source), "-ss", str(timestamp),
                    "-frames:v", "1", "-vf", chain or "null", "-q:v", "2", str(output)],
                   capture_output=True, check=True, timeout=30)

    if not output.exists() or not output.stat().st_size:
        raise ValueError("Não foi possível extrair este fotograma.")


# Aplicar uma transição entre dois vídeos
def transition_command(first, second, output, a, b, overlap):
    
    width, height, fps = a["width"], a["height"], a["fps"]

    normalize = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                 f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                 f"format=yuv420p,setpts=PTS-STARTPTS,fps={fps}")

    graph = [f"[0:v]{normalize}[v0]", f"[1:v]{normalize}[v1]",
             f"[v0][v1]xfade=transition=fade:duration={overlap}:offset={a['duration'] - overlap}[v]"]
    args = ["-i", str(first), "-i", str(second)]
    audio = a["audio"] or b["audio"]
    if audio:
        for i, meta in enumerate([a, b]):
            if meta["audio"]:
                graph.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS,"
                             f"apad,atrim=duration={meta['duration']}[a{i}]")
            else:
                # Silence occupies the missing track's exact duration; it must not shorten the composition.
                graph.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={meta['duration']}[a{i}]")
        graph.append(f"[a0][a1]acrossfade=d={overlap}:c1=tri:c2=tri[a]")
    args += ["-filter_complex", ";".join(graph), "-map", "[v]"]
    if audio:
        args += ["-map", "[a]", "-c:a", "aac"]
    return args + ["-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
                   "-movflags", "+faststart", str(output)]


# Obter frame para thumbnail
def thumbnail_frame(source, output, meta, timestamp):
    
    timestamp = finite_number(timestamp, 0, meta["duration"], "Instante da miniatura")
    
    if timestamp >= meta["duration"]:
        raise ValueError("O instante deve ser anterior ao fim do vídeo.")

    count = meta["frames"] or math.ceil(meta["duration"] * meta["fps"])
    
    frame = min(count - 1, math.floor(timestamp * meta["fps"] + .5))
    
    # Selecting by decoded frame index avoids keyframe-seek rounding and guarantees the nearest frame.
    chain = (f"select=eq(n\\,{frame}),scale=320:180:force_original_aspect_ratio=decrease,"
             "pad=320:180:(ow-iw)/2:(oh-ih)/2:black,setsar=1")
    
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-threads", "2", "-filter_threads", "1",
                    "-i", str(source), "-vf", chain, "-frames:v", "1", "-q:v", "2", str(output)],
                   capture_output=True, check=True, timeout=60)
    
    if not output.exists() or not output.stat().st_size:
        raise ValueError("Não foi possível gerar a miniatura.")
    
    return {"frame": frame, "timestamp": frame / meta["fps"], "width": 320, "height": 180}
