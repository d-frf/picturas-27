import hashlib
import json
import subprocess
import uuid
from pathlib import Path

from bson import ObjectId
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from starlette.concurrency import run_in_threadpool

from auth import authorize
from config import (BUCKET, MUTATIONS, ROOT, ACTIVE, assets, db, executor, jobs,
                    quotas, s3, subtitles, thumbnails)
from media import (color_command, filter_chain, finite_number, preview_frame, probe,
                   thumbnail_frame)
from services import (asset_for, file_digest, local_file, now, publish_upload,
                      quota_key, release_quota, remove_asset, unlock)
from subtitles import validate_segments, to_srt
from jobs.runner import run_job

router = APIRouter()

@router.get("/health")
def health():
    db.command("ping")
    return {"status": "ok"}

@router.get("/{owner}/{project}/videos")
def listing(owner: str, project: str, request: Request):
    user, can_edit = authorize(request, owner, project)
    quota = quotas.find_one({"_id": quota_key(str(user["_id"]))}) or {}
    return {
        "assets": list(assets.find({"project_id": project, "published": True}).sort("created", 1)),
        "jobs": list(jobs.find({"project_id": project}).sort("created", -1).limit(30)),
        "can_edit": can_edit, "profile": user["type"],
        "remaining": None if user["type"] == "premium" else max(0, 5 - quota.get("used", 0))
    }

@router.post("/{owner}/{project}/videos", status_code=201)
async def upload(owner: str, project: str, request: Request, file: UploadFile = File(...)):
    authorize(request, owner, project, True)
    aid = uuid.uuid4().hex
    path = ROOT / (aid + ".mp4")
    with MUTATIONS:
        authorize(request, owner, project, True)
        assets.insert_one({
            "_id": aid, "owner_id": owner, "project_id": project,
            "kind": "uploading", "published": False,
            "key": f"{owner}/{project}/{aid}.mp4"
        })
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
        return await run_in_threadpool(publish_upload, owner, project, aid, path, file.filename, meta, digest.hexdigest())
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    finally:
        await file.close()
        if not assets.find_one({"_id": aid, "published": True}):
            assets.delete_one({"_id": aid, "kind": "uploading"})
            path.unlink(missing_ok=True)

@router.get("/{owner}/{project}/videos/{aid}/file")
def download(owner: str, project: str, aid: str, request: Request):
    authorize(request, owner, project)
    asset = asset_for(aid, project)
    return FileResponse(local_file(asset), media_type="video/mp4", filename=asset["name"])

@router.post("/{owner}/{project}/videos/{aid}/jobs", status_code=202)
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
            raise HTTPException(422, "Escolha 0,5x, 1,5x ou 2x.")
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
    job = {
        "_id": jid, "project_id": project, "owner_id": owner, "user_id": str(user["_id"]),
        "asset_id": aid, "params": params, "expected_duration": duration, "created": now(),
        "state": "queued", "progress": 0, "error": None, "quota_key": None,
        "input_ids": [a["_id"] for a in inputs], "input_names": [a["name"] for a in inputs],
        "asset_name": asset["name"], "fingerprint": fingerprint
    }
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

@router.delete("/{owner}/{project}/videos/{aid}", status_code=204)
async def delete_version(owner: str, project: str, aid: str, request: Request):
    authorize(request, owner, project, True)
    if not isinstance(aid, str) or len(aid) != 32 or any(c not in "0123456789abcdef" for c in aid):
        raise HTTPException(422, "Identificador de vídeo inválido.")
    await run_in_threadpool(remove_asset, aid, project)
    return Response(status_code=204)

@router.post("/{owner}/{project}/videos/deletion-check")
def lock_empty_project(owner: str, project: str, request: Request):
    with MUTATIONS:
        authorize(request, owner, project, True, allow_anonymous=True, deleting=True)
        if assets.find_one({"project_id": project}) or jobs.find_one({"project_id": project, "state": {"$in": ACTIVE}}):
            raise HTTPException(409, "Apague primeiro as versões de vídeo do projeto, incluindo as eliminações pendentes, antes de eliminar o projeto ou a conta.")
        db.projects.update_one({"_id": ObjectId(project)}, {"$set": {"video_deleting": True}})
        return {"empty": True}

@router.delete("/{owner}/{project}/videos/deletion-check/release", status_code=204)
def unlock_empty_project(owner: str, project: str, request: Request):
    with MUTATIONS:
        authorize(request, owner, project, True, allow_anonymous=True, deleting=True)
        db.projects.update_one({"_id": ObjectId(project)}, {"$unset": {"video_deleting": ""}})
    return Response(status_code=204)

@router.post("/{owner}/{project}/videos/jobs/{jid}/cancel")
def cancel(owner: str, project: str, jid: str, request: Request):
    authorize(request, owner, project, True)
    job = jobs.find_one_and_update({"_id": jid, "project_id": project, "state": {"$in": ACTIVE}},
                                  {"$set": {"state": "cancelled", "error": None}},
                                  return_document=ReturnDocument.AFTER)
    if not job:
        raise HTTPException(409, "O pedido já terminou ou não existe.")
    release_quota(job)
    return job

@router.get("/{owner}/{project}/videos/{aid}/subtitles")
def get_subtitles(owner: str, project: str, aid: str, request: Request):
    authorize(request, owner, project)
    source = asset_for(aid, project)
    return {
        "active": subtitles.find_one({"_id": source.get("subtitles_id"), "published": True}),
        "draft": subtitles.find_one({"asset_id": aid, "draft": True, "archived": {"$ne": True}, "published": True}, sort=[("created", -1)])
    }

@router.put("/{owner}/{project}/videos/{aid}/subtitles/{sid}")
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
        doc = {
            "_id": uuid.uuid4().hex, "asset_id": aid, "project_id": project,
            "segments": segments, "created": now(), "draft": False, "published": True, "source_id": sid
        }
        subtitles.insert_one(doc)
        changed = assets.update_one({"_id": aid, "subtitles_id": params.get("expected_active_id")},
                                    {"$set": {"subtitles_id": doc["_id"]}}).matched_count
        if not changed:
            subtitles.delete_one({"_id": doc["_id"]})
            raise HTTPException(409, "As legendas mudaram; atualize antes de guardar.")
        if old["draft"]:
            subtitles.update_one({"_id": sid}, {"$set": {"archived": True}})
        return doc

@router.get("/{owner}/{project}/videos/{aid}/subtitles.srt")
def export_subtitles(owner: str, project: str, aid: str, request: Request):
    authorize(request, owner, project)
    source = asset_for(aid, project)
    doc = subtitles.find_one({"_id": source.get("subtitles_id"), "published": True})
    if not doc:
        raise HTTPException(404, "Guarde primeiro as legendas revistas.")
    return Response(to_srt(doc["segments"]), media_type="application/x-subrip; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="legendas.srt"'})

@router.post("/{owner}/{project}/videos/{aid}/thumbnail-preview")
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

@router.post("/{owner}/{project}/videos/{aid}/thumbnail", status_code=201)
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

@router.get("/{owner}/{project}/videos/{aid}/thumbnail/file")
def thumbnail_file(owner: str, project: str, aid: str, request: Request):
    authorize(request, owner, project)
    source = asset_for(aid, project)
    doc = thumbnails.find_one({"_id": source.get("thumbnail_id"), "asset_id": aid})
    if not doc:
        raise HTTPException(404, "Este vídeo ainda não tem miniatura.")
    return Response(s3.get_object(Bucket=BUCKET, Key=doc["key"])["Body"].read(), media_type="image/jpeg",
                    headers={"Content-Disposition": 'attachment; filename="miniatura.jpg"'})

@router.post("/{owner}/{project}/videos/{aid}/preview")
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