import hashlib
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from threading import RLock
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3
import jwt
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, Response
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from starlette.concurrency import run_in_threadpool

from media import probe, speed_command, filter_chain, color_command, finite_number, preview_frame, transition_command, thumbnail_frame
from subtitles import validate_segments, to_srt

log = logging.getLogger("video")
ROOT = Path(os.getenv("VIDEO_DATA", "/data"))
ROOT.mkdir(parents=True, exist_ok=True)
db = MongoClient(os.getenv("PROJECTS_MONGO", "mongodb://projects_mongoDB:27018"))["project"]
users = MongoClient(os.getenv("USERS_MONGO", "mongodb://users_mongoDB:27019"))["user"].users
assets, jobs, quotas = db.video_assets, db.video_jobs, db.video_quotas
subtitles = db.video_subtitles
thumbnails = db.video_thumbnails
s3 = boto3.client("s3", endpoint_url=os.getenv("S3_ENDPOINT", "http://seaweedfs:9000"),
                  aws_access_key_id=os.getenv("S3_ACCESS_KEY", "admin"),
                  aws_secret_access_key=os.getenv("S3_SECRET_KEY", "admin123"), region_name="us-east-1")
BUCKET = "picturas-video"
executor = ThreadPoolExecutor(max_workers=1)
ACTIVE = ["queued", "processing"]
# One service process in this MVP: serialize publication, request admission and
# deletion so a version cannot disappear while another request reserves it.
MUTATIONS = RLock()


def now():
    return datetime.now(timezone.utc).isoformat()


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def publish_upload(owner, project, aid, path, name, meta, digest):
    with MUTATIONS:
        existing = assets.find_one({"project_id": project, "kind": "original", "sha256": digest,
                                    "published": True, "deleting": {"$ne": True}})
        if existing:
            return {**existing, "reused_upload": True}
        key = f"{owner}/{project}/{aid}.mp4"
        stored = False
        try:
            s3.upload_file(str(path), BUCKET, key, ExtraArgs={"ContentType": "video/mp4"})
            stored = True
            doc = {"_id": aid, "owner_id": owner, "project_id": project,
                   "name": Path(name or "vídeo.mp4").name, "key": key, "metadata": meta,
                   "sha256": digest, "kind": "original", "created": now(),
                   "published": True, "active_job": None}
            assets.replace_one({"_id": aid, "kind": "uploading"}, doc)
            return doc
        except Exception:
            if stored:
                s3.delete_object(Bucket=BUCKET, Key=key)
            raise


def quota_key(uid):
    return uid + ":" + datetime.now(ZoneInfo("Europe/Lisbon")).date().isoformat()


def release_quota(job, success=False):
    if job.get("quota_key"):
        update = {"$pull": {"reserved": job["_id"]}}
        if not success:
            update["$inc"] = {"used": -1}
        # Membership makes refunds idempotent, even when cancellation and worker cleanup race.
        quotas.update_one({"_id": job["quota_key"], "reserved": job["_id"]}, update)


def unlock(job):
    assets.update_many({"active_job": job["_id"]}, {"$set": {"active_job": None}})


def recover_jobs(project_ids=None):
    scope = {"project_id": {"$in": project_ids}} if project_ids is not None else {}
    # The service deliberately uses one worker process. A restart invalidates all its old work.
    for job in jobs.find({**scope, "state": {"$in": ACTIVE}}):
        jobs.update_one({"_id": job["_id"]}, {"$set": {"state": "failed", "error": "Serviço reiniciado; repita o pedido."}})
        release_quota(job)
        unlock(job)
    # Repair a crash between completion and publication, without refunding a completed operation.
    for collection, result_field in [(assets, "result_id"), (subtitles, "subtitle_id")]:
        for result in collection.find({**scope, "published": False}):
            job = jobs.find_one({"_id": result.get("job_id")})
            if job and job["state"] == "completed" and job.get(result_field) == result["_id"]:
                collection.update_one({"_id": result["_id"]}, {"$set": {"published": True}})
                release_quota(job, True)
            else:
                if result.get("key"):
                    s3.delete_object(Bucket=BUCKET, Key=result["key"])
                collection.delete_one({"_id": result["_id"]})
                (ROOT / (result["_id"] + ".mp4")).unlink(missing_ok=True)
    for asset in assets.find({**scope, "active_job": {"$ne": None}}):
        job = jobs.find_one({"_id": asset["active_job"]})
        if not job or job["state"] not in ACTIVE:
            assets.update_one({"_id": asset["_id"]}, {"$set": {"active_job": None}})
    qscope = {"_id": {"$in": [j["quota_key"] for j in jobs.find(scope) if j.get("quota_key")]}} if project_ids is not None else {}
    for quota in quotas.find(qscope):
        for jid in quota.get("reserved", []):
            job = jobs.find_one({"_id": jid})
            if not job or job["state"] not in ACTIVE:
                release_quota({"_id": jid, "quota_key": quota["_id"]}, bool(job and job["state"] == "completed"))


@asynccontextmanager
async def lifespan(app):
    for attempt in range(30):
        try:
            db.command("ping")
            if not any(b["Name"] == BUCKET for b in s3.list_buckets()["Buckets"]):
                s3.create_bucket(Bucket=BUCKET)
            break
        except Exception:
            if attempt == 29:
                raise
            time.sleep(2)
    assets.create_index([("project_id", 1), ("published", 1)])
    assets.create_index([("project_id", 1), ("kind", 1), ("sha256", 1)])
    jobs.create_index([("project_id", 1), ("created", -1)])
    jobs.create_index([("project_id", 1), ("fingerprint", 1), ("state", 1)])
    recover_jobs()
    # Upload placeholders belong to this single process. After a restart no
    # request can still complete them; remove any unpublished storage residue.
    for asset in assets.find({"kind": "uploading"}):
        s3.delete_object(Bucket=BUCKET, Key=asset["key"])
        (ROOT / (asset["_id"] + ".mp4")).unlink(missing_ok=True)
        assets.delete_one({"_id": asset["_id"]})
    db.projects.update_many({"video_deleting": True}, {"$unset": {"video_deleting": ""}})
    for asset in assets.find({"deleting": True}):
        try:
            remove_asset(asset["_id"], asset["project_id"])
        except Exception:
            log.exception("Deletion remains pending for %s", asset["_id"])
    yield
    executor.shutdown(wait=True, cancel_futures=True)


app = FastAPI(lifespan=lifespan)


def authorize(request, owner, project, edit=False, allow_anonymous=False, deleting=False):
    try:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise ValueError()
        payload = jwt.decode(header[7:], os.environ["JWT_SECRET_KEY"], algorithms=["HS256"],
                             options={"require": ["exp", "id"]})
        user = users.find_one({"_id": ObjectId(payload["id"])})
    except (jwt.PyJWTError, ValueError, TypeError, KeyError, InvalidId):
        raise HTTPException(401, "Sessão inválida ou expirada.")
    if not user or user.get("type") not in (("anonymous", "free", "premium") if allow_anonymous else ("free", "premium")):
        raise HTTPException(403, "O vídeo requer um perfil registado ou premium.")
    try:
        doc = db.projects.find_one({"_id": ObjectId(project), "user_id": ObjectId(owner)})
    except Exception:
        raise HTTPException(404, "Projeto não encontrado.")
    if not doc:
        raise HTTPException(404, "Projeto não encontrado.")
    if doc.get("video_deleting") and not deleting:
        raise HTTPException(409, "O projeto está a ser eliminado. Aguarde e atualize a página.")
    can_edit = str(user["_id"]) == owner
    if not can_edit:
        link = next((s for s in doc.get("sharedLinks", []) if s["id"] == request.query_params.get("share")
                     and not s.get("revoked")), None)
        if not link:
            raise HTTPException(403, "Sem acesso ao projeto.")
        can_edit = link["permission"] == "edit"
    if edit and not can_edit:
        raise HTTPException(403, "O link permite apenas leitura.")
    return user, can_edit


def asset_for(asset_id, project):
    if not isinstance(asset_id, str) or len(asset_id) != 32 or any(c not in "0123456789abcdef" for c in asset_id):
        raise HTTPException(422, "Identificador de vídeo inválido.")
    doc = assets.find_one({"_id": asset_id, "project_id": project, "published": True})
    if not doc:
        raise HTTPException(404, "Vídeo não encontrado neste projeto.")
    if doc.get("deleting"):
        raise HTTPException(409, "Esta versão tem uma eliminação pendente. Repita a eliminação.")
    return doc


def local_file(asset):
    path = ROOT / (asset["_id"] + ".mp4")
    if not path.exists():
        temp = ROOT / (uuid.uuid4().hex + ".download")
        try:
            s3.download_file(BUCKET, asset["key"], str(temp))
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
    return path


@app.get("/health")
def health():
    db.command("ping")
    return {"status": "ok"}


@app.get("/{owner}/{project}/videos")
def listing(owner: str, project: str, request: Request):
    user, can_edit = authorize(request, owner, project)
    quota = quotas.find_one({"_id": quota_key(str(user["_id"]))}) or {}
    return {"assets": list(assets.find({"project_id": project, "published": True}).sort("created", 1)),
            "jobs": list(jobs.find({"project_id": project}).sort("created", -1).limit(30)),
            "can_edit": can_edit, "profile": user["type"],
            "remaining": None if user["type"] == "premium" else max(0, 5 - quota.get("used", 0))}


@app.post("/{owner}/{project}/videos", status_code=201)
async def upload(owner: str, project: str, request: Request, file: UploadFile = File(...)):
    authorize(request, owner, project, True)
    aid = uuid.uuid4().hex
    path = ROOT / (aid + ".mp4")
    with MUTATIONS:
        authorize(request, owner, project, True)
        # The deletion guard also sees uploads that have not been published yet.
        assets.insert_one({"_id": aid, "owner_id": owner, "project_id": project,
                           "kind": "uploading", "published": False,
                           "key": f"{owner}/{project}/{aid}.mp4"})
    try:
        size, digest = 0, hashlib.sha256()
        with path.open("wb") as dest:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 100_000_000:
                    raise HTTPException(413, "O limite por vídeo é 100 MB.")
                dest.write(chunk)
                digest.update(chunk)
        meta = await run_in_threadpool(probe, path, True)
        return await run_in_threadpool(publish_upload, owner, project, aid, path,
                                       file.filename, meta, digest.hexdigest())
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    finally:
        await file.close()
        if not assets.find_one({"_id": aid, "published": True}):
            assets.delete_one({"_id": aid, "kind": "uploading"})
            path.unlink(missing_ok=True)


@app.get("/{owner}/{project}/videos/{aid}/file")
def download(owner: str, project: str, aid: str, request: Request):
    authorize(request, owner, project)
    asset = asset_for(aid, project)
    return FileResponse(local_file(asset), media_type="video/mp4", filename=asset["name"])


@app.post("/{owner}/{project}/videos/{aid}/jobs", status_code=202)
async def start_job(owner: str, project: str, aid: str, request: Request):
    user, _ = authorize(request, owner, project, True)
    try:
        params = await request.json()
    except ValueError:
        raise HTTPException(422, "Pedido JSON inválido.")
    if not isinstance(params, dict):
        raise HTTPException(422, "Pedido JSON inválido.")
    return await run_in_threadpool(enqueue_job, owner, project, aid, user, params)


def enqueue_job(owner, project, aid, user, params):
    with MUTATIONS:
        parent = db.projects.find_one({"_id": ObjectId(project), "user_id": ObjectId(owner)})
        if not parent or parent.get("video_deleting"):
            raise HTTPException(409, "O projeto foi removido ou está a ser eliminado.")
        return admit_job(owner, project, aid, user, params)


def admit_job(owner, project, aid, user, params):
    asset = asset_for(aid, project)
    operation = params.get("operation")
    if operation not in ("speed", "color", "transition", "subtitles"):
        raise HTTPException(422, "Operação desconhecida.")
    duration = asset["metadata"]["duration"]
    if operation == "speed":
        factor = params.get("factor")
        if isinstance(factor, bool) or factor not in (.5, 1, 1.5, 2):
            raise HTTPException(422, "Escolha 0,5×, 1×, 1,5× ou 2×.")
        if factor == 1:
            return {"noop": True, "asset_id": aid}
        duration /= factor
    elif operation == "color":
        try:
            chain = filter_chain(params.get("filters"))
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if not chain:
            return {"noop": True, "asset_id": aid}
    elif operation == "transition":
        second = asset_for(params.get("second_id"), project)
        if second["_id"] == aid:
            raise HTTPException(422, "Selecione dois clips distintos.")
        try:
            overlap = finite_number(params.get("overlap"), .001,
                                    min(duration, second["metadata"]["duration"]) / 2, "Duração da transição")
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        duration += second["metadata"]["duration"] - overlap
    elif operation == "subtitles":
        if user["type"] != "premium":
            raise HTTPException(403, "A geração de legendas requer perfil premium.")
        if not asset["metadata"]["audio"]:
            raise HTTPException(422, "O vídeo não tem faixa de áudio.")
        if asset.get("subtitles_id") and params.get("replace_confirmed") is not True:
            raise HTTPException(409, "Confirme a criação de um novo rascunho para este vídeo.")
    if duration > 240.05:
        raise HTTPException(422, "O resultado excederia 240 segundos.")
    inputs = [asset] + ([second] if operation == "transition" else [])
    fingerprint = None
    if operation != "subtitles":
        recipe = {"operation": operation}
        if operation == "speed":
            recipe["factor"] = float(factor)
        elif operation == "color":
            # The validated FFmpeg expression represents the effective ordered
            # filters, including removal of neutral choices.
            recipe["filters"] = filter_chain([
                {"name": item["name"], "value": float(item["value"]) if item["name"] != "grayscale" else item["value"]}
                for item in params["filters"]
            ])
        else:
            recipe["overlap"] = float(overlap)
        for source in inputs:
            if not source.get("sha256"):
                source["sha256"] = file_digest(local_file(source))
                assets.update_one({"_id": source["_id"]}, {"$set": {"sha256": source["sha256"]}})
        payload = {"processor": "video-mvp-v1", "inputs": [{"id": a["_id"], "sha256": a["sha256"]} for a in inputs], **recipe}
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        previous = jobs.find_one({"project_id": project, "fingerprint": fingerprint, "state": "completed"}, sort=[("created", -1)])
        if previous and assets.find_one({"_id": previous.get("result_id"), "published": True, "deleting": {"$ne": True}}):
            return {**previous, "reused": True}
        pending = jobs.find_one({"project_id": project, "fingerprint": fingerprint, "state": {"$in": ACTIVE}})
        if pending:
            return {**pending, "reused_pending": True}
    jid = uuid.uuid4().hex
    job = {"_id": jid, "project_id": project, "owner_id": owner, "user_id": str(user["_id"]),
           "asset_id": aid, "params": params, "expected_duration": duration, "created": now(),
           "state": "queued", "progress": 0, "error": None, "quota_key": None,
           "input_ids": [a["_id"] for a in inputs], "input_names": [a["name"] for a in inputs],
           "asset_name": asset["name"], "fingerprint": fingerprint}
    if not assets.find_one_and_update({"_id": aid, "active_job": None, "published": True, "deleting": {"$ne": True}}, {"$set": {"active_job": jid}}):
        raise HTTPException(409, "Já existe uma operação neste vídeo.")
    try:
        if operation == "transition" and not assets.find_one_and_update(
                {"_id": params["second_id"], "active_job": None, "published": True, "deleting": {"$ne": True}}, {"$set": {"active_job": jid}}):
            raise HTTPException(409, "Já existe uma operação no segundo clip.")
        if user["type"] != "premium":
            key = quota_key(str(user["_id"]))
            try:
                quotas.update_one({"_id": key}, {"$setOnInsert": {"used": 0, "reserved": []}}, upsert=True)
            except DuplicateKeyError:
                pass
            if not quotas.find_one_and_update({"_id": key, "used": {"$lt": 5}},
                                              {"$inc": {"used": 1}, "$addToSet": {"reserved": jid}}):
                raise HTTPException(429, "A quota diária de cinco operações foi atingida.")
            job["quota_key"] = key
        jobs.insert_one(job)
        executor.submit(run_job, jid)
    except Exception:
        release_quota(job)
        unlock(job)
        raise
    return job


def remove_asset(aid, project):
    with MUTATIONS:
        asset = assets.find_one({"_id": aid, "project_id": project, "published": True})
        if not asset:
            raise HTTPException(404, "Vídeo não encontrado neste projeto.")
        if asset.get("active_job") or jobs.find_one({"project_id": project, "input_ids": aid, "state": {"$in": ACTIVE}}):
            raise HTTPException(409, "Aguarde ou cancele o processamento antes de apagar esta versão.")
        if assets.find_one({"project_id": project, "_id": {"$ne": aid}, "$or": [{"source_id": aid}, {"input_ids": aid}]}):
            raise HTTPException(409, "Apague primeiro as versões que dependem deste vídeo.")
        # Storage deletion is not a Mongo/S3 transaction. Keep a visible retry
        # marker until every object is removed; the version cannot be reused.
        assets.update_one({"_id": aid}, {"$set": {"deleting": True}})
        images = list(thumbnails.find({"asset_id": aid, "project_id": project}))
        try:
            for key in [asset["key"]] + [image["key"] for image in images]:
                s3.delete_object(Bucket=BUCKET, Key=key)
            (ROOT / (aid + ".mp4")).unlink(missing_ok=True)
            thumbnails.delete_many({"asset_id": aid, "project_id": project})
            subtitles.delete_many({"asset_id": aid, "project_id": project})
            jobs.update_many({"project_id": project, "result_id": aid},
                             {"$unset": {"result_id": ""}, "$set": {"result_deleted": True}})
            jobs.update_many({"project_id": project, "asset_id": aid},
                             {"$unset": {"subtitle_id": ""}, "$set": {"asset_deleted": True}})
            assets.delete_one({"_id": aid, "project_id": project})
        except Exception:
            log.exception("Deletion failed for %s", aid)
            raise HTTPException(503, "Eliminação incompleta. A versão está bloqueada; repita a eliminação para terminar a limpeza.")


@app.delete("/{owner}/{project}/videos/{aid}", status_code=204)
async def delete_version(owner: str, project: str, aid: str, request: Request):
    authorize(request, owner, project, True)
    if not isinstance(aid, str) or len(aid) != 32 or any(c not in "0123456789abcdef" for c in aid):
        raise HTTPException(422, "Identificador de vídeo inválido.")
    await run_in_threadpool(remove_asset, aid, project)
    return Response(status_code=204)


@app.post("/{owner}/{project}/videos/deletion-check")
def lock_empty_project(owner: str, project: str, request: Request):
    with MUTATIONS:
        authorize(request, owner, project, True, allow_anonymous=True, deleting=True)
        if assets.find_one({"project_id": project}) or jobs.find_one({"project_id": project, "state": {"$in": ACTIVE}}):
            raise HTTPException(409, "Apague primeiro as versões de vídeo do projeto, incluindo as eliminações pendentes, antes de eliminar o projeto ou a conta.")
        db.projects.update_one({"_id": ObjectId(project)}, {"$set": {"video_deleting": True}})
        return {"empty": True}


@app.delete("/{owner}/{project}/videos/deletion-check/release", status_code=204)
def unlock_empty_project(owner: str, project: str, request: Request):
    with MUTATIONS:
        authorize(request, owner, project, True, allow_anonymous=True, deleting=True)
        db.projects.update_one({"_id": ObjectId(project)}, {"$unset": {"video_deleting": ""}})
    return Response(status_code=204)


@app.post("/{owner}/{project}/videos/jobs/{jid}/cancel")
def cancel(owner: str, project: str, jid: str, request: Request):
    authorize(request, owner, project, True)
    job = jobs.find_one_and_update({"_id": jid, "project_id": project, "state": {"$in": ACTIVE}},
                                  {"$set": {"state": "cancelled", "error": None}},
                                  return_document=ReturnDocument.AFTER)
    if not job:
        raise HTTPException(409, "O pedido já terminou ou não existe.")
    release_quota(job)
    # The worker releases the asset only after stopping FFmpeg, avoiding overlapping jobs.
    return job


@app.get("/{owner}/{project}/videos/{aid}/subtitles")
def get_subtitles(owner: str, project: str, aid: str, request: Request):
    authorize(request, owner, project)
    source = asset_for(aid, project)
    return {"active": subtitles.find_one({"_id": source.get("subtitles_id"), "published": True}),
            "draft": subtitles.find_one({"asset_id": aid, "draft": True, "archived": {"$ne": True},
                                          "published": True}, sort=[("created", -1)])}


@app.put("/{owner}/{project}/videos/{aid}/subtitles/{sid}")
async def save_subtitles(owner: str, project: str, aid: str, sid: str, request: Request):
    user, _ = authorize(request, owner, project, True)
    if user["type"] != "premium":
        raise HTTPException(403, "A edição de legendas requer perfil premium.")
    try:
        params = await request.json()
    except ValueError:
        raise HTTPException(422, "Pedido JSON inválido.")
    return await run_in_threadpool(save_reviewed_subtitles, project, aid, sid, params)


def save_reviewed_subtitles(project, aid, sid, params):
    with MUTATIONS:
        return save_reviewed_subtitles_locked(project, aid, sid, params)


def save_reviewed_subtitles_locked(project, aid, sid, params):
    source = asset_for(aid, project)
    old = subtitles.find_one({"_id": sid, "asset_id": aid, "published": True})
    if not old:
        raise HTTPException(404, "Versão de legendas não encontrada.")
    try:
        segments = validate_segments(params.get("segments"), source["metadata"]["duration"])
    except (ValueError, AttributeError) as exc:
        raise HTTPException(422, str(exc))
    if source.get("subtitles_id") and source["subtitles_id"] != sid and params.get("replace_confirmed") is not True:
        raise HTTPException(409, "Confirme a substituição das legendas ativas.")
    doc = {"_id": uuid.uuid4().hex, "asset_id": aid, "project_id": project,
           "segments": segments, "created": now(), "draft": False, "published": True, "source_id": sid}
    subtitles.insert_one(doc)
    changed = assets.update_one({"_id": aid, "subtitles_id": params.get("expected_active_id")},
                                {"$set": {"subtitles_id": doc["_id"]}}).matched_count
    if not changed:
        subtitles.delete_one({"_id": doc["_id"]})
        raise HTTPException(409, "As legendas mudaram; atualize antes de guardar.")
    if old["draft"]:
        subtitles.update_one({"_id": sid}, {"$set": {"archived": True}})
    return doc


@app.get("/{owner}/{project}/videos/{aid}/subtitles.srt")
def export_subtitles(owner: str, project: str, aid: str, request: Request):
    authorize(request, owner, project)
    source = asset_for(aid, project)
    doc = subtitles.find_one({"_id": source.get("subtitles_id"), "published": True})
    if not doc:
        raise HTTPException(404, "Guarde primeiro as legendas revistas.")
    return Response(to_srt(doc["segments"]), media_type="application/x-subrip; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="legendas.srt"'})


@app.post("/{owner}/{project}/videos/{aid}/thumbnail-preview")
async def preview_thumbnail(owner: str, project: str, aid: str, request: Request):
    authorize(request, owner, project)
    source = asset_for(aid, project)
    path = ROOT / (uuid.uuid4().hex + ".jpg")
    try:
        params = await request.json()
        await run_in_threadpool(thumbnail_frame, local_file(source), path, source["metadata"], params.get("timestamp", 0))
        return Response(path.read_bytes(), media_type="image/jpeg")
    except (ValueError, AttributeError) as exc:
        raise HTTPException(422, str(exc))
    except subprocess.SubprocessError:
        raise HTTPException(422, "Não foi possível gerar a miniatura; a anterior foi preservada.")
    finally:
        path.unlink(missing_ok=True)


@app.post("/{owner}/{project}/videos/{aid}/thumbnail", status_code=201)
async def save_thumbnail(owner: str, project: str, aid: str, request: Request):
    authorize(request, owner, project, True)
    try:
        params = await request.json()
        if not isinstance(params, dict):
            raise ValueError("Pedido JSON inválido.")
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return await run_in_threadpool(create_thumbnail, owner, project, aid, params)


def create_thumbnail(owner, project, aid, params):
    with MUTATIONS:
        return create_thumbnail_locked(owner, project, aid, params)


def create_thumbnail_locked(owner, project, aid, params):
    source = asset_for(aid, project)
    if source.get("thumbnail_id") and params.get("replace_confirmed") is not True:
        raise HTTPException(409, "Confirme a substituição da miniatura.")
    tid = uuid.uuid4().hex
    path = ROOT / (tid + ".jpg")
    key = f"{owner}/{project}/{tid}.jpg"
    stored, activated = False, False
    try:
        meta = thumbnail_frame(local_file(source), path, source["metadata"], params.get("timestamp", 0))
        s3.upload_file(str(path), BUCKET, key, ExtraArgs={"ContentType": "image/jpeg"})
        stored = True
        doc = {"_id": tid, "project_id": project, "asset_id": aid, "created": now(), "key": key, **meta}
        thumbnails.insert_one(doc)
        # Activate only if the client's previous version still matches. Old thumbnails remain intact.
        activated = bool(assets.update_one({"_id": aid, "thumbnail_id": params.get("expected_thumbnail_id"), "active_job": None},
                                           {"$set": {"thumbnail_id": tid, "thumbnail_timestamp": meta["timestamp"]}}).matched_count)
        if not activated:
            raise HTTPException(409, "O vídeo ou a miniatura mudou; atualize antes de guardar.")
        return doc
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except subprocess.SubprocessError:
        raise HTTPException(422, "Não foi possível gerar a miniatura; a anterior foi preservada.")
    finally:
        path.unlink(missing_ok=True)
        if not activated:
            thumbnails.delete_one({"_id": tid})
            if stored:
                s3.delete_object(Bucket=BUCKET, Key=key)


@app.get("/{owner}/{project}/videos/{aid}/thumbnail/file")
def thumbnail_file(owner: str, project: str, aid: str, request: Request):
    authorize(request, owner, project)
    source = asset_for(aid, project)
    doc = thumbnails.find_one({"_id": source.get("thumbnail_id"), "asset_id": aid})
    if not doc:
        raise HTTPException(404, "Este vídeo ainda não tem miniatura.")
    return Response(s3.get_object(Bucket=BUCKET, Key=doc["key"])["Body"].read(), media_type="image/jpeg",
                    headers={"Content-Disposition": 'attachment; filename="miniatura.jpg"'})


class Cancelled(Exception):
    pass


@app.post("/{owner}/{project}/videos/{aid}/preview")
async def color_preview(owner: str, project: str, aid: str, request: Request):
    authorize(request, owner, project)
    source = asset_for(aid, project)
    path = ROOT / (uuid.uuid4().hex + ".jpg")
    try:
        params = await request.json()
        chain = filter_chain(params.get("filters", []))
        timestamp = finite_number(params.get("timestamp", 0), 0,
                                  max(0, source["metadata"]["duration"] - 1 / source["metadata"]["fps"]), "Instante")
        await run_in_threadpool(preview_frame, local_file(source), path, timestamp, chain)
        return Response(path.read_bytes(), media_type="image/jpeg")
    except (ValueError, AttributeError) as exc:
        raise HTTPException(422, str(exc))
    except subprocess.SubprocessError:
        raise HTTPException(422, "Não foi possível gerar a pré-visualização.")
    finally:
        path.unlink(missing_ok=True)


def execute_ffmpeg(jid, arguments, duration, command=None):
    progress = ROOT / (jid + ".progress")
    stderr = ROOT / (jid + ".log")
    proc = None
    try:
        with stderr.open("wb") as err:
            proc = subprocess.Popen(command or ["ffmpeg", "-nostdin", "-y", "-v", "error", "-threads", "2",
                                     "-filter_threads", "1", "-filter_complex_threads", "1",
                                     "-progress", str(progress)] + arguments, stdout=subprocess.DEVNULL, stderr=err)
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


def run_job(jid):
    job = jobs.find_one({"_id": jid})
    output = ROOT / (jid + (".json" if job["params"]["operation"] == "subtitles" else ".output.mp4"))
    result = None
    completed = False
    try:
        if not jobs.update_one({"_id": jid, "state": "queued"}, {"$set": {"state": "processing"}}).modified_count:
            raise Cancelled()
        source = asset_for(job["asset_id"], job["project_id"])
        operation = job["params"]["operation"]
        if operation == "subtitles":
            execute_ffmpeg(jid, [], job["expected_duration"], command=[sys.executable, "transcribe.py",
                           str(local_file(source)), str(output), str(ROOT / (jid + ".progress"))])
            generated = json.loads(output.read_text())
            if not generated:
                if not jobs.update_one({"_id": jid, "state": "processing"},
                                       {"$set": {"state": "completed", "progress": 100, "message": "Não foi detetada fala."}}).modified_count:
                    raise Cancelled()
                completed = True
                release_quota(job, True)
                return
            segments = validate_segments(generated, source["metadata"]["duration"])
            sid = uuid.uuid4().hex
            draft = {"_id": sid, "asset_id": source["_id"], "project_id": job["project_id"], "created": now(),
                     "segments": segments, "draft": True, "published": False, "job_id": jid}
            subtitles.insert_one(draft)
            if not jobs.update_one({"_id": jid, "state": "processing"},
                                   {"$set": {"state": "completed", "progress": 100, "subtitle_id": sid}}).modified_count:
                subtitles.delete_one({"_id": sid})
                raise Cancelled()
            completed = True
            subtitles.update_one({"_id": sid}, {"$set": {"published": True}})
            release_quota(job, True)
            return
        if operation == "speed":
            command = speed_command(local_file(source), output, source["metadata"], job["params"]["factor"])
        elif operation == "color":
            command = color_command(local_file(source), output, filter_chain(job["params"]["filters"]))
        else:
            second = asset_for(job["params"]["second_id"], job["project_id"])
            command = transition_command(local_file(source), local_file(second), output,
                                         source["metadata"], second["metadata"], job["params"]["overlap"])
        execute_ffmpeg(jid, command, job["expected_duration"])
        meta = probe(output)
        if abs(meta["duration"] - job["expected_duration"]) > .1:
            raise ValueError("A duração do resultado não passou a validação.")
        if operation == "color" and (meta["width"] != source["metadata"]["width"] or
                                     meta["height"] != source["metadata"]["height"] or
                                     meta["frames"] != source["metadata"]["frames"] or
                                     meta["audio"] != source["metadata"]["audio"]):
            raise ValueError("O resultado não preservou a estrutura do vídeo.")
        rid = uuid.uuid4().hex
        result = {"_id": rid, "project_id": job["project_id"], "owner_id": job["owner_id"],
                  "name": Path(source["name"]).stem + {"speed": "-velocidade.mp4", "color": "-cor.mp4", "transition": "-transicao.mp4"}[operation], "kind": operation, "source_id": source["_id"],
                  "input_ids": job["input_ids"],
                  "key": f"{job['owner_id']}/{job['project_id']}/{rid}.mp4", "metadata": meta,
                  "created": now(), "published": False, "active_job": None, "job_id": jid,
                  "sha256": file_digest(output), "operation_params": job["params"]}
        s3.upload_file(str(output), BUCKET, result["key"], ExtraArgs={"ContentType": "video/mp4"})
        assets.insert_one(result)
        output.replace(ROOT / (rid + ".mp4"))
        # Cancellation wins if it was recorded before publication; no partial result becomes visible.
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
