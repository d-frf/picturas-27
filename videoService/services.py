import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from config import BUCKET, MUTATIONS, ROOT, ACTIVE, assets, jobs, quotas, s3, subtitles, thumbnails
from media import filter_chain, finite_number, thumbnail_frame, probe

def now():
    return datetime.now(timezone.utc).isoformat()

def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()

def local_file(asset: dict) -> Path:
    path = ROOT / (asset["_id"] + ".mp4")
    
    # 1. Se o ficheiro não existe, faz download do S3
    if not path.exists():
        _download_from_s3(asset, path)

    # 2. Validar integridade com o ffprobe
    try:
        probe_meta = probe(path)
        # Se probe() devolver um dicionário/resultado vazio em vez de lançar exceção:
        if not probe_meta:
            raise ValueError("Metadados inválidos ou vazios.")
    except Exception:
        # Se detetar corrupção na cache local, apaga o ficheiro local corrompido
        path.unlink(missing_ok=True)
        
        # Tenta re-descarregar uma vez para verificar se o original no S3 está saudável
        try:
            _download_from_s3(asset, path)
            probe(path) # Re-valida
        except Exception:
            # Se continuar a falhar, limpa novamente e lança a exceção final
            path.unlink(missing_ok=True)
            raise ValueError("O ficheiro de vídeo está corrompido no armazenamento original ou é inválido.")

    return path

def _download_from_s3(asset: dict, destination: Path) -> None:
    temp = ROOT / (uuid.uuid4().hex + ".download")
    try:
        s3.download_file(BUCKET, asset["key"], str(temp))
        temp.replace(destination)
    finally:
        temp.unlink(missing_ok=True)

def asset_for(asset_id, project):
    if not isinstance(asset_id, str) or len(asset_id) != 32 or any(c not in "0123456789abcdef" for c in asset_id):
        raise HTTPException(422, "Identificador de vídeo inválido.")
    doc = assets.find_one({"_id": asset_id, "project_id": project, "published": True})
    if not doc:
        raise HTTPException(404, "Vídeo não encontrado neste projeto.")
    if doc.get("deleting"):
        raise HTTPException(409, "Esta versão tem uma eliminação pendente. Repita a eliminação.")
    return doc

def quota_key(uid):
    return uid + ":" + datetime.now(ZoneInfo("Europe/Lisbon")).date().isoformat()

def release_quota(job, success=False):
    if job.get("quota_key"):
        update = {"$pull": {"reserved": job["_id"]}}
        if not success:
            update["$inc"] = {"used": -1}
        quotas.update_one({"_id": job["quota_key"], "reserved": job["_id"]}, update)

def unlock(job):
    assets.update_many({"active_job": job["_id"]}, {"$set": {"active_job": None}})

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

def recover_jobs(project_ids=None):
    scope = {"project_id": {"$in": project_ids}} if project_ids is not None else {}
    for job in jobs.find({**scope, "state": {"$in": ACTIVE}}):
        jobs.update_one({"_id": job["_id"]}, {"$set": {"state": "failed", "error": "Serviço reiniciado; repita o pedido."}})
        release_quota(job)
        unlock(job)

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

def remove_asset(aid, project):
    with MUTATIONS:
        asset = assets.find_one({"_id": aid, "project_id": project, "published": True})
        if not asset:
            raise HTTPException(404, "Vídeo não encontrado neste projeto.")
        if asset.get("active_job") or jobs.find_one({"project_id": project, "input_ids": aid, "state": {"$in": ACTIVE}}):
            raise HTTPException(409, "Aguarde ou cancele o processamento antes de apagar esta versão.")
        if assets.find_one({"project_id": project, "_id": {"$ne": aid}, "$or": [{"source_id": aid}, {"input_ids": aid}]}):
            raise HTTPException(409, "Apague primeiro as versões que dependem deste vídeo.")

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
            raise HTTPException(503, "Eliminação incompleta. A versão está bloqueada; repita a eliminação para terminar a limpeza.")