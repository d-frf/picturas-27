import hashlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import RLock

import boto3
from pymongo import MongoClient

log = logging.getLogger("video")

ROOT = Path(os.getenv("VIDEO_DATA", "/data"))
ROOT.mkdir(parents=True, exist_ok=True)

# Database connections
db = MongoClient(os.getenv("PROJECTS_MONGO", "mongodb://projects_mongoDB:27018"))["project"]
users = MongoClient(os.getenv("USERS_MONGO", "mongodb://users_mongoDB:27019"))["user"].users

assets = db.video_assets
jobs = db.video_jobs
quotas = db.video_quotas
subtitles = db.video_subtitles
thumbnails = db.video_thumbnails

# S3 Client Configuration
s3 = boto3.client(
    "s3",
    endpoint_url=os.getenv("S3_ENDPOINT", "http://seaweedfs:9000"),
    aws_access_key_id=os.getenv("S3_ACCESS_KEY", "admin"),
    aws_secret_access_key=os.getenv("S3_SECRET_KEY", "admin123"),
    region_name="us-east-1"
)
BUCKET = "picturas-video"

executor = ThreadPoolExecutor(max_workers=1)
ACTIVE = ["queued", "processing"]
MUTATIONS = RLock()