import json
import sys
import uuid
from pathlib import Path

from config import ROOT, subtitles
from media import color_command, filter_chain, probe, speed_command, transition_command
from services import asset_for, local_file, now
from subtitles import validate_segments


class BaseJobExecutor():
    def execute(self,job,source,output,runner):
        pass


class SubtitlesJobExecutor(BaseJobExecutor):
    def execute(self, job, source, output, runner):
        jid = job["_id"]
        runner.execute_ffmpeg(jid, [], job["expected_duration"], command=[
            sys.executable, "transcribe.py",
            str(local_file(source)), str(output), str(ROOT / (jid + ".progress"))
        ])
        generated = json.loads(output.read_text())
        if not generated:
            return {"type": "subtitles", "empty": True}

        segments = validate_segments(generated, source["metadata"]["duration"])
        sid = uuid.uuid4().hex
        draft = {
            "_id": sid, "asset_id": source["_id"], "project_id": job["project_id"],
            "created": now(), "segments": segments, "draft": True, "published": False, "job_id": jid
        }
        subtitles.insert_one(draft)
        return {"type": "subtitles", "subtitle_id": sid, "empty": False}

class SpeedJobExecutor(BaseJobExecutor):
    def execute(self, job, source, output, runner):
        command = speed_command(local_file(source), output, source["metadata"], job["params"]["factor"])
        runner.execute_ffmpeg(job["_id"], command, job["expected_duration"])
        return {"type": "video", "suffix": "-velocidade.mp4"}

class ColorJobExecutor(BaseJobExecutor):
    def execute(self, job, source, output, runner):
        command = color_command(local_file(source), output, filter_chain(job["params"]["filters"]))
        runner.execute_ffmpeg(job["_id"], command, job["expected_duration"])
        
        meta = probe(output)
        if (meta["width"] != source["metadata"]["width"] or
            meta["height"] != source["metadata"]["height"] or
            meta["frames"] != source["metadata"]["frames"] or
            meta["audio"] != source["metadata"]["audio"]):
            raise ValueError("O resultado não preservou a estrutura do vídeo.")
            
        return {"type": "video", "suffix": "-cor.mp4"}

class TransitionJobExecutor(BaseJobExecutor):
    def execute(self, job, source, output, runner):
        second = asset_for(job["params"]["second_id"], job["project_id"])
        command = transition_command(
            local_file(source), local_file(second), output,
            source["metadata"], second["metadata"], job["params"]["overlap"]
        )
        runner.execute_ffmpeg(job["_id"], command, job["expected_duration"])
        return {"type": "video", "suffix": "-transicao.mp4"}

class JobExecutorFactory:
    _executors = {
        "subtitles": SubtitlesJobExecutor(),
        "speed": SpeedJobExecutor(),
        "color": ColorJobExecutor(),
        "transition": TransitionJobExecutor(),
    }

    @classmethod
    def get_executor(cls, operation: str) -> BaseJobExecutor:
        executor = cls._executors.get(operation)
        if not executor:
            raise ValueError(f"Operação desconhecida: {operation}")
        return executor