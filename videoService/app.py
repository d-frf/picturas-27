import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from config import BUCKET, ROOT, assets, db, executor, jobs, s3
from routes import router
from services import recover_jobs, remove_asset

log = logging.getLogger("video")

@asynccontextmanager
async def lifespan(app: FastAPI):
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
app.include_router(router)