"""Run inference in a child process so cancelling also stops model loading/inference."""
import json
import os
from pathlib import Path
import sys
from faster_whisper import WhisperModel
from media import probe


if __name__ == "__main__":
    source, output, progress = map(Path, sys.argv[1:4])
    duration = probe(source)["duration"]
    model = WhisperModel(os.getenv("WHISPER_MODEL", "small"), device="cpu", compute_type="int8",
                         cpu_threads=2, download_root=os.getenv("WHISPER_CACHE", "/data/models"))
    segments, _ = model.transcribe(str(source), language="pt", beam_size=5,
                                  vad_filter=True, condition_on_previous_text=False)
    result = []
    for segment in segments:
        # Model timestamps are rounded; clip the generated draft to the actual media bounds.
        start = max(result[-1]["end"] if result else 0, segment.start)
        end = min(duration, segment.end)
        if end > start and segment.text.strip() and segment.no_speech_prob <= .6 and segment.avg_logprob >= -1:
            result.append({"start": start, "end": end, "text": segment.text.strip()})
        progress.write_text(f"out_time_us={int(segment.end * 1e6)}\n")
    output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
