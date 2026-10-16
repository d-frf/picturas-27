from media import finite_number


def validate_segments(items, duration):
    if not isinstance(items, list) or not items:
        raise ValueError("Não foi detetada fala ou a lista de legendas está vazia.")
    if len(items) > 500:
        raise ValueError("O máximo é 500 segmentos.")
    result, previous = [], 0
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Segmento inválido.")
        start = finite_number(item.get("start"), 0, duration, "Início da legenda")
        end = finite_number(item.get("end"), 0, duration, "Fim da legenda")
        text = item.get("text")
        if start >= end or start < previous or not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError("As legendas devem ter texto, tempos válidos, ordem e ausência de sobreposição.")
        text = "\n".join(line.strip() for line in text.replace("\r", "").split("\n") if line.strip())
        result.append({"start": start, "end": end, "text": text})
        previous = end
    return result


def srt_timestamp(seconds):
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def to_srt(segments):
    return "\n\n".join(f"{i}\n{srt_timestamp(s['start'])} --> {srt_timestamp(s['end'])}\n{s['text']}"
                       for i, s in enumerate(segments, 1)) + "\n"
