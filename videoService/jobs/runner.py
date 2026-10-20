import logging
import subprocess
import time

from pathlib import Path
from config import BUCKET, ROOT, ACTIVE, assets, jobs, s3, subtitles
from media import probe
from services import asset_for, file_digest, local_file, now, release_quota, unlock
from jobs.factory import JobExecutorFactory

import uuid

log = logging.getLogger("video")

class Cancelled(Exception):
    pass

def execute_ffmpeg(jid, arguments, duration, command=None):
    progress = ROOT / (jid + ".progress")
    stderr = ROOT / (jid + ".log")
    proc = None
    try:
        with stderr.open("wb") as err:
            proc = subprocess.Popen(
                command or ["ffmpeg", "-nostdin", "-y", "-v", "error", "-threads", "2",
                            "-filter_threads", "1", "-filter_complex_threads", "1",
                            "-progress", str(progress)] + arguments,
                stdout=subprocess.DEVNULL, stderr=err
            )
            started = time.monotonic()
            while proc.poll() is None:
                current = jobs.find_one({"_id": jid})
                if current["state"] == "cancelled":
                    raise Cancelled()
                if time.monotonic() - started > 900:
                    raise ValueError("O processamento excedeu o tempo limite.")
                if progress.exists():
                    values = dict(line.split("=", 1) for line in progress.read_text().splitlines() if "=" in line)
                    percent = min(99, int(float(values.get("out_time_us", "0")) / 1e6 / duration * 100))
                    jobs.update_one({"_id": jid, "state": "processing"}, {"$set": {"progress": max(0, percent)}})
                time.sleep(.2)
            if proc.returncode:
                log.error("FFmpeg %s: %s", jid, stderr.read_text()[-2000:])
                raise ValueError("Não foi possível processar o vídeo; o original foi preservado.")
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        progress.unlink(missing_ok=True)
        stderr.unlink(missing_ok=True)

class FFMpegRunner:
    """Wrapper context passed down to strategies requiring ffmpeg execution."""
    execute_ffmpeg = staticmethod(execute_ffmpeg)

def run_job(jid):
    job = jobs.find_one({"_id": jid})
    operation = job["params"]["operation"]
    output = ROOT / (jid + (".json" if operation == "subtitles" else ".output.mp4"))
    result = None
    completed = False

    try:
        if not jobs.update_one({"_id": jid, "state": "queued"}, {"$set": {"state": "processing"}}).modified_count:
            raise Cancelled()

        source = asset_for(job["asset_id"], job["project_id"])
        
        # Factory Pattern Dispatch
        executor = JobExecutorFactory.get_executor(operation)
        job_result = executor.execute(job, source, output, FFMpegRunner())

        if operation == "subtitles":
            if job_result["empty"]:
                if not jobs.update_one({"_id": jid, "state": "processing"},
                                       {"$set": {"state": "completed", "progress": 100, "message": "Não foi detetada fala."}}).modified_count:
                    raise Cancelled()
                completed = True
                release_quota(job, True)
                return

            sid = job_result["subtitle_id"]
            if not jobs.update_one({"_id": jid, "state": "processing"},
                                   {"$set": {"state": "completed", "progress": 100, "subtitle_id": sid}}).modified_count:
                subtitles.delete_one({"_id": sid})
                raise Cancelled()
            completed = True
            subtitles.update_one({"_id": sid}, {"$set": {"published": True}})
            release_quota(job, True)
            return

        # Common handling for media assets (speed, color, transition)
        meta = probe(output)
        if abs(meta["duration"] - job["expected_duration"]) > .1:
            raise ValueError("A duração do resultado não passou a validação.")

        rid = uuid.uuid4().hex
        result = {
            "_id": rid, "project_id": job["project_id"], "owner_id": job["owner_id"],
            "name": Path(source["name"]).stem + job_result["suffix"], "kind": operation, "source_id": source["_id"],
            "input_ids": job["input_ids"],
            "key": f"{job['owner_id']}/{job['project_id']}/{rid}.mp4", "metadata": meta,
            "created": now(), "published": False, "active_job": None, "job_id": jid,
            "sha256": file_digest(output), "operation_params": job["params"]
        }
        s3.upload_file(str(output), BUCKET, result["key"], ExtraArgs={"ContentType": "video/mp4"})
        assets.insert_one(result)
        output.replace(ROOT / (rid + ".mp4"))

        won = jobs.update_one({"_id": jid, "state": "processing"},
                              {"$set": {"state": "completed", "progress": 100, "result_id": rid}}).modified_count
        if not won:
            raise Cancelled()
        completed = True
        assets.update_one({"_id": rid}, {"$set": {"published": True}})
        release_quota(job, True)

    except Exception as exc:
        if not isinstance(exc, Cancelled):
            log.exception("Video job %s failed", jid)
        if result and not completed:
            assets.delete_one({"_id": result["_id"]})
            s3.delete_object(Bucket=BUCKET, Key=result["key"])
            (ROOT / (result["_id"] + ".mp4")).unlink(missing_ok=True)
        jobs.update_one({"_id": jid, "state": {"$in": ACTIVE}},
                        {"$set": {"state": "failed", "error": str(exc) if isinstance(exc, ValueError)
                                  else "Falha no processamento; pode repetir o pedido."}})
        release_quota(job, completed)
    finally:
        output.unlink(missing_ok=True)
        unlock(job)