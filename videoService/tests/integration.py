"""End-to-end tests against the local gateway and real FFmpeg/MongoDB/SeaweedFS.

Run inside the service: python tests/integration.py
Fixtures are isolated and removed afterwards; existing projects are never modified.
"""
import array
import hashlib
import json
import io
import math
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import struct
import time
import unittest
import urllib.error
import urllib.request
import uuid
import zipfile
import zlib
from unittest.mock import patch
from botocore.exceptions import ClientError
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config as service
from services import quota_key,recover_jobs,remove_asset
from media import probe
from bson import ObjectId
import jwt
from fastapi import HTTPException

BASE = "http://api_gateway:8000"


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.user_ids, self.project_ids = [], []
        self.uid, self.token, self.pid = self.identity("premium")
        self.sample = self.root / "sample.mp4"
        self.make_video(self.sample)
        self.aid = self.upload(self.sample)["_id"]

    def tearDown(self):
        for a in service.assets.find({"project_id": {"$in": self.project_ids}}):
            service.s3.delete_object(Bucket=service.BUCKET, Key=a["key"])
            (service.ROOT / (a["_id"] + ".mp4")).unlink(missing_ok=True)
        service.assets.delete_many({"project_id": {"$in": self.project_ids}})
        service.jobs.delete_many({"project_id": {"$in": self.project_ids}})
        service.subtitles.delete_many({"project_id": {"$in": self.project_ids}})
        for thumb in service.thumbnails.find({"project_id": {"$in": self.project_ids}}):
            service.s3.delete_object(Bucket=service.BUCKET, Key=thumb["key"])
        service.thumbnails.delete_many({"project_id": {"$in": self.project_ids}})
        service.db.projects.delete_many({"_id": {"$in": [ObjectId(p) for p in self.project_ids]}})
        service.users.delete_many({"_id": {"$in": [ObjectId(u) for u in self.user_ids]}})
        service.quotas.delete_many({"_id": {"$in": [quota_key(u) for u in self.user_ids]}})
        self.temp.cleanup()

    def identity(self, profile):
        uid, pid = ObjectId(), ObjectId()
        service.users.insert_one({"_id": uid, "name": "VIDEO INTEGRATION FIXTURE", "type": profile, "operations": []})
        service.db.projects.insert_one({"_id": pid, "user_id": uid, "name": "VIDEO INTEGRATION FIXTURE",
                                        "imgs": [], "tools": [], "version": 0, "sharedLinks": []})
        self.user_ids.append(str(uid)); self.project_ids.append(str(pid))
        token = jwt.encode({"id": str(uid), "exp": int(time.time()) + 3600}, os.environ.get("JWT_SECRET_KEY",'lisan_al_gaib'), algorithm="HS256")
        return str(uid), token, str(pid)

    def api(self, path="", method="GET", data=None, token=None, owner=None, pid=None, expected=200, raw=False, headers=None):
        url = BASE + f"/projects/{owner or self.uid}/{pid or self.pid}/videos" + path
        head = {"Authorization": "Bearer " + (token or self.token), **(headers or {})}
        if isinstance(data, dict):
            data = json.dumps(data).encode(); head["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=head)
        try:
            response = urllib.request.urlopen(request, timeout=180)
        except urllib.error.HTTPError as error:
            response = error
        payload = response.read()
        if isinstance(expected, tuple):
            self.assertIn(response.status, expected, payload[:1000])
        else:
            self.assertEqual(response.status, expected, payload[:1000])
        return payload if raw else json.loads(payload)

    def make_video(self, path, duration=4, audio=True, color="testsrc2", resolution="320x180", fps=30, codec="libx264"):
        source = f"{color}=size={resolution}:rate={fps}:duration={duration}" if color == "testsrc2" else f"color=c={color}:s={resolution}:r={fps}:d={duration}"
        args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", source]
        if audio:
            args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={duration}", "-c:a", "aac"]
        args += ["-c:v", codec, "-threads", "2", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path)]
        subprocess.run(args, check=True, timeout=90)

    def upload(self, path, **kwargs):
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\nContent-Type: video/mp4\r\n\r\n").encode()
        body += path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        expected = kwargs.pop("expected", 201)
        return self.api(method="POST", data=body, headers={"Content-Type": "multipart/form-data; boundary=" + boundary}, expected=expected, **kwargs)

    def job(self, params, aid=None, **kwargs):
        return self.api(f"/{aid or self.aid}/jobs", "POST", params, expected=202, **kwargs)

    def wait(self, jid, expected="completed"):
        end = time.monotonic() + 120
        while time.monotonic() < end:
            job = service.jobs.find_one({"_id": jid})
            if job["state"] not in service.ACTIVE:
                # A cancellation is acknowledged before FFmpeg stops. Wait for release as well.
                locked = service.assets.count_documents({"active_job": jid})
                if not locked:
                    self.assertEqual(job["state"], expected, job)
                    return job
            time.sleep(.2)
        self.fail("Job timeout: " + jid)

    def result_path(self, job):
        path = self.root / (job["_id"] + ".mp4")
        path.write_bytes(self.api(f"/{job['result_id']}/file", raw=True))
        return path

    def test_speed_and_original(self):
        digest = hashlib.sha256(self.sample.read_bytes()).hexdigest()
        for factor in [.5, 1.5, 2]:
            job = self.wait(self.job({"operation": "speed", "factor": factor})["_id"])
            path = self.result_path(job)
            meta = probe(path)
            self.assertLessEqual(abs(meta["duration"] - 4 / factor), .1)
            self.assertTrue(meta["audio"])
            # A440 remains A440 after atempo: count sign crossings in decoded PCM.
            pcm = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "48000", "-f", "f32le", "-"])
            samples = array.array("f"); samples.frombytes(pcm)
            # Measure steady-state pitch, excluding AAC padding and atempo start/end windows.
            samples = samples[len(samples) // 4: 3 * len(samples) // 4]
            frequency = sum(a < 0 <= b for a, b in zip(samples, samples[1:])) / (len(samples) / 48000)
            self.assertLess(abs(frequency - 440), 8)
        self.assertEqual(hashlib.sha256(self.api(f"/{self.aid}/file", raw=True)).hexdigest(), digest)
        before = len(self.api()["jobs"])
        self.assertTrue(self.job({"operation": "speed", "factor": 1})["noop"])
        self.assertEqual(len(self.api()["jobs"]), before)
        self.api(f"/{self.aid}/jobs", "POST", {"operation": "speed", "factor": 99}, expected=422)

    def test_no_audio_and_validation(self):
        silent = self.root / "silent.mp4"; self.make_video(silent, audio=False)
        aid = self.upload(silent)["_id"]
        result = self.wait(self.job({"operation": "speed", "factor": 2}, aid)["_id"])
        self.assertFalse(probe(self.result_path(result))["audio"])
        invalid = self.root / "invalid.mp4"; invalid.write_text("this is not video")
        self.upload(invalid, expected=422)
        wrong = self.root / "wrong.mp4"; self.make_video(wrong, codec="mpeg4")
        boundary = "wrongformat"
        body = f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="wrong.mp4"\r\n\r\n'.encode() + wrong.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        self.api(method="POST", data=body, headers={"Content-Type": "multipart/form-data; boundary=" + boundary}, expected=422)

    def test_access_and_quota(self):
        self.api(headers={"Authorization": ""}, expected=401)
        source = service.assets.find_one({"_id": self.aid})
        try:
            response = urllib.request.urlopen("http://seaweedfs:9000/" + service.BUCKET + "/" + source["key"])
        except urllib.error.HTTPError as denied:
            response = denied
        self.assertIn(response.status, (401, 403), "The video bucket must not allow anonymous object reads")
        response.close()
        uid, token, pid = self.identity("free")
        self.api(token=token, expected=401)
        self.api("?share=invalid", token=token, expected=403)
        service.db.projects.update_one({"_id": ObjectId(self.pid)}, {"$set": {"sharedLinks": [{"id": "test-read", "permission": "read", "revoked": False}]}})
        self.api("?share=test-read", token=token)
        self.api(f"/{self.aid}/jobs?share=test-read", "POST", {"operation": "speed", "factor": 2}, token=token, expected=403)
        auid, atoken, _ = self.identity("anonymous")
        self.api(token=atoken, owner=auid, expected=403)
        aid = self.upload(self.sample, token=token, owner=uid, pid=pid)["_id"]
        for value in [10, 20, 30, 40, 50]:
            started = self.job({"operation": "color", "filters": [{"name": "brightness", "value": value}]}, aid, token=token, owner=uid, pid=pid)
            self.wait(started["_id"])
        self.assertEqual(self.api(token=token, owner=uid, pid=pid)["remaining"], 0)
        self.api(f"/{aid}/jobs", "POST", {"operation": "speed", "factor": .5}, token=token, owner=uid, pid=pid, expected=429)

    def test_color_order_preview_and_audio(self):
        filters = [{"name": "brightness", "value": 20}, {"name": "contrast", "value": 2}, {"name": "grayscale", "value": True}]
        previews = []
        for chain in [filters, list(reversed(filters))]:
            image = self.api(f"/{self.aid}/preview", "POST", {"filters": chain, "timestamp": 1.5}, raw=True)
            self.assertTrue(image.startswith(b"\xff\xd8"))
            previews.append(image)
        self.assertNotEqual(previews[0], previews[1])
        self.assertEqual(len(self.api()["jobs"]), 0)
        job = self.wait(self.job({"operation": "color", "filters": filters})["_id"])
        path = self.result_path(job)
        original, result = probe(self.sample), probe(path)
        for key in ["duration", "frames", "width", "height", "fps", "audio"]:
            self.assertEqual(original[key], result[key])
        original_audio = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(self.sample), "-map", "0:a:0", "-c", "copy", "-f", "adts", "-"])
        output_audio = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-c", "copy", "-f", "adts", "-"])
        self.assertEqual(original_audio, output_audio)
        self.assertTrue(self.job({"operation": "color", "filters": [{"name": "contrast", "value": 1}]})["noop"])
        self.api(f"/{self.aid}/jobs", "POST", {"operation": "color", "filters": [{"name": "brightness", "value": 101}]}, expected=422)

    def test_video_brightness_maps_existing_factor(self):
        grey = self.root / "grey.mp4"; self.make_video(grey, color="0x646464")
        aid = self.upload(grey)["_id"]
        result = self.result_path(self.wait(self.job({"operation": "color", "filters": [{"name": "brightness", "value": 50}]}, aid)["_id"]))
        def pixel(path):
            return subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])[:3]
        original, changed = pixel(grey), pixel(result)
        for a, b in zip(original, changed):
            self.assertLessEqual(abs(b - a * 1.5), 4)

    def test_cancel_and_failure_refund(self):

        uid, token, pid = self.identity("free")
        
        large = self.root / "large.mp4"; self.make_video(large, duration=30, resolution="1280x720")
        aid = self.upload(large, token=token, owner=uid, pid=pid)["_id"]

        job = self.job({"operation": "speed", "factor": .5}, aid, token=token, owner=uid, pid=pid)
        
        self.api(f"/{aid}/jobs", "POST", {"operation": "speed", "factor": 2}, token=token, owner=uid, pid=pid, expected=409)
        self.api(f"/{aid}", "DELETE", token=token, owner=uid, pid=pid, expected=409)
        self.api(f"/jobs/{job['_id']}/cancel", "POST", token=token, owner=uid, pid=pid)
        
        self.wait(job["_id"], "cancelled")
        
        self.assertEqual(self.api(token=token, owner=uid, pid=pid)["remaining"], 5)
        
        self.assertFalse(service.assets.find_one({"job_id": job["_id"], "published": True}))
        
        # Damage only this fixture, including S3: a damaged cache alone is recoverable.
        stored = service.assets.find_one({"_id": aid})
        cached = service.ROOT / (aid + ".mp4"); cached.write_bytes(b"damaged")
        service.s3.put_object(Bucket=service.BUCKET, Key=stored["key"], Body=b"damaged", ContentType="video/mp4")
        try:
            failed = self.job({"operation": "speed", "factor": 2}, aid, token=token, owner=uid, pid=pid)
            self.wait(failed["_id"], "failed")
            self.assertFalse(service.assets.find_one({"job_id": failed["_id"], "published": True}))
        finally:
            service.s3.upload_file(str(large), service.BUCKET, stored["key"], ExtraArgs={"ContentType": "video/mp4"})
            cached.unlink(missing_ok=True)
        
        self.assertEqual(self.api(token=token, owner=uid, pid=pid)["remaining"], 5)
        self.assertEqual(hashlib.sha256(self.api(f"/{aid}/file", token=token, owner=uid, pid=pid, raw=True)).hexdigest(), hashlib.sha256(large.read_bytes()).hexdigest())

        # The same cache damage must succeed when S3 still contains a healthy original.
        cached.write_bytes(b"damaged")
        recovered = self.wait(self.job({"operation": "speed", "factor": 2}, aid, token=token, owner=uid, pid=pid)["_id"])
        self.assertTrue(service.assets.find_one({"_id": recovered["result_id"], "published": True}))
        self.assertEqual(self.api(token=token, owner=uid, pid=pid)["remaining"], 4)
        self.assertEqual(hashlib.sha256(self.api(f"/{aid}/file", token=token, owner=uid, pid=pid, raw=True)).hexdigest(), hashlib.sha256(large.read_bytes()).hexdigest())

    def test_transition_normalization_and_silence(self):
        red = self.root / "red.mp4"; blue = self.root / "blue.mp4"
        self.make_video(red, color="red")
        self.make_video(blue, duration=3, color="blue", resolution="160x160", fps=15, audio=False)
        first, second = self.upload(red)["_id"], self.upload(blue)["_id"]
        job = self.wait(self.job({"operation": "transition", "second_id": second, "overlap": .5}, first)["_id"])
        result = self.result_path(job); meta = probe(result)
        self.assertLessEqual(abs(meta["duration"] - 6.5), .1)
        self.assertEqual((meta["width"], meta["height"], meta["fps"]), (320, 180, 30))
        self.assertTrue(meta["audio"])
        pixel = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(result), "-ss", "3.75", "-vf", "crop=2:2:158:88", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
        self.assertGreater(pixel[0], 40); self.assertGreater(pixel[2], 40)
        self.api(f"/{first}/jobs", "POST", {"operation": "transition", "second_id": second, "overlap": 2}, expected=422)
        self.api(f"/{first}/jobs", "POST", {"operation": "transition", "second_id": first, "overlap": .5}, expected=422)
        self.assertEqual(hashlib.sha256(self.api(f"/{first}/file", raw=True)).digest(), hashlib.sha256(red.read_bytes()).digest())

    @unittest.skipUnless(shutil.which("espeak-ng"), "Use the test image for actual Portuguese speech transcription")
    def test_subtitles_real_speech_review_and_srt(self):
        wave = self.root / "speech.wav"; video = self.root / "speech.mp4"
        subprocess.run(["espeak-ng", "-v", "pt-pt", "-s", "140", "-w", str(wave),
                        "Olá, este é um teste de legendas automáticas em português."], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=green:s=320x180:r=30", "-i", str(wave), "-shortest", "-c:v", "libx264", "-threads", "2", "-c:a", "aac", str(video)], check=True)
        aid = self.upload(video)["_id"]
        job = self.wait(self.job({"operation": "subtitles"}, aid)["_id"])
        self.assertIn("subtitle_id", job)
        data = self.api(f"/{aid}/subtitles")
        self.assertIsNone(data["active"])
        segments = data["draft"]["segments"]
        self.assertTrue(segments)
        self.assertTrue(any("teste" in s["text"].lower() for s in segments), segments)
        segments[0]["text"] = "Texto revisto pelo utilizador."
        saved = self.api(f"/{aid}/subtitles/{data['draft']['_id']}", "PUT",
                         {"segments": segments, "expected_active_id": None})
        self.assertEqual(self.api(f"/{aid}/subtitles")["active"]["_id"], saved["_id"])
        srt = self.api(f"/{aid}/subtitles.srt", raw=True).decode()
        self.assertIn(" --> ", srt); self.assertIn("Texto revisto pelo utilizador.", srt)
        self.api(f"/{aid}/jobs", "POST", {"operation": "subtitles"}, expected=409)
        self.api(f"/{aid}/subtitles/{saved['_id']}", "PUT", {"segments": [{"start": 1, "end": .5, "text": "bad"}], "expected_active_id": saved["_id"]}, expected=422)
        self.api(f"/{aid}/subtitles/{saved['_id']}", "PUT", {"segments": segments, "expected_active_id": None}, expected=409)
        self.assertEqual(self.api(f"/{aid}/subtitles")["active"]["_id"], saved["_id"])

    def test_subtitles_profiles_and_absent_speech(self):
        uid, token, pid = self.identity("free")
        aid = self.upload(self.sample, token=token, owner=uid, pid=pid)["_id"]
        self.api(f"/{aid}/jobs", "POST", {"operation": "subtitles"}, token=token, owner=uid, pid=pid, expected=403)
        silent = self.root / "silent.mp4"; self.make_video(silent, audio=False)
        aid = self.upload(silent)["_id"]
        self.api(f"/{aid}/jobs", "POST", {"operation": "subtitles"}, expected=422)
        finished = self.wait(self.job({"operation": "subtitles"})["_id"])
        self.assertIn("fala", finished["message"])
        self.assertFalse(service.subtitles.find_one({"asset_id": self.aid, "published": True}))

    def test_thumbnail_nearest_frame_and_safe_replace(self):
        uid, token, pid = self.identity("free")
        square = self.root / "square.mp4"; self.make_video(square, color="red", resolution="160x160")
        aid = self.upload(square, token=token, owner=uid, pid=pid)["_id"]
        preview = self.api(f"/{aid}/thumbnail-preview", "POST", {"timestamp": .051}, token=token, owner=uid, pid=pid, raw=True)
        self.assertTrue(preview.startswith(b"\xff\xd8"))
        doc = self.api(f"/{aid}/thumbnail", "POST", {"timestamp": .051, "expected_thumbnail_id": None}, token=token, owner=uid, pid=pid, expected=201)
        self.assertEqual(doc["frame"], 2)
        self.assertAlmostEqual(doc["timestamp"], 2 / 30)
        image = self.api(f"/{aid}/thumbnail/file", token=token, owner=uid, pid=pid, raw=True)
        self.assertEqual(image, preview)
        path = self.root / "thumb.jpg"; path.write_bytes(image)
        info = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "json", str(path)]))
        self.assertEqual((info["streams"][0]["width"], info["streams"][0]["height"]), (320, 180))
        pixel = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
        self.assertLess(max(pixel[:3]), 10)  # Square image is centred with black side bars.
        centre = (90 * 320 + 160) * 3; self.assertGreater(pixel[centre], 200)
        self.api(f"/{aid}/thumbnail", "POST", {"timestamp": 1, "expected_thumbnail_id": doc["_id"]}, token=token, owner=uid, pid=pid, expected=409)
        self.api(f"/{aid}/thumbnail", "POST", {"timestamp": 99, "expected_thumbnail_id": doc["_id"], "replace_confirmed": True}, token=token, owner=uid, pid=pid, expected=422)
        self.assertEqual(self.api(f"/{aid}/thumbnail/file", token=token, owner=uid, pid=pid, raw=True), image)
        changed = self.api(f"/{aid}/thumbnail", "POST", {"timestamp": 1, "expected_thumbnail_id": doc["_id"], "replace_confirmed": True}, token=token, owner=uid, pid=pid, expected=201)
        self.assertNotEqual(changed["_id"], doc["_id"])
        self.assertEqual(self.api(token=token, owner=uid, pid=pid)["remaining"], 5)
        self.assertEqual(len(self.api(token=token, owner=uid, pid=pid)["jobs"]), 0)

    def test_shared_edit_upload_and_revocation(self):
        _, token, other = self.identity("free")
        service.db.projects.update_one({"_id": ObjectId(self.pid)}, {"$set": {"sharedLinks": [{"id": "test-edit", "permission": "edit", "revoked": False}]}})
        boundary = uuid.uuid4().hex
        body = f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="shared.mp4"\r\n\r\n'.encode() + self.sample.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        doc = self.api("?share=test-edit", "POST", body, token=token, headers={"Content-Type": "multipart/form-data; boundary=" + boundary}, expected=201)
        self.assertEqual(doc["owner_id"], self.uid)
        self.api(f"/{self.aid}/jobs", "POST", {"operation": "transition", "second_id": {"$ne": None}, "overlap": .5}, expected=422)
        foreign = self.upload(self.sample, pid=other, owner=self.user_ids[-1], token=token)["_id"]
        self.api(f"/{self.aid}/jobs", "POST", {"operation": "transition", "second_id": foreign, "overlap": .5}, expected=404)
        service.db.projects.update_one({"_id": ObjectId(self.pid)}, {"$set": {"sharedLinks.0.revoked": True}})
        self.api("?share=test-edit", token=token, expected=403)

    def test_concurrent_quota_reservations(self):
        uid, token, pid = self.identity("free")
        ids = []
        for index in range(6):
            path = self.root / f"concurrent-{index}.mp4"
            self.make_video(path, color=["red", "blue", "green", "yellow", "white", "black"][index])
            ids.append(self.upload(path, token=token, owner=uid, pid=pid)["_id"])
        def submit(aid):
            return self.api(f"/{aid}/jobs", "POST", {"operation": "speed", "factor": .5}, token=token, owner=uid, pid=pid, expected=(202, 429))
        with ThreadPoolExecutor(max_workers=6) as clients:
            results = list(clients.map(submit, ids))
        accepted = [r for r in results if "_id" in r]
        self.assertEqual(len(accepted), 5)
        for job in accepted:
            self.wait(job["_id"])
        self.assertEqual(self.api(token=token, owner=uid, pid=pid)["remaining"], 0)
        self.assertEqual(service.users.find_one({"_id": ObjectId(uid)})["operations"], [])

    def test_empty_project_accepts_video_without_images(self):
        request = urllib.request.Request(BASE + f"/projects/{self.uid}",
            data=json.dumps({"name": "VIDEO INTEGRATION FIXTURE"}).encode(), method="POST",
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 201)
            project = json.load(response)
        self.project_ids.append(project["_id"])
        self.assertEqual(project["imgs"], [])
        self.assertEqual(self.api(pid=project["_id"])["assets"], [])
        asset = self.upload(self.sample, pid=project["_id"])
        result = self.wait(self.job({"operation": "speed", "factor": 2}, asset["_id"], pid=project["_id"])["_id"])
        self.assertTrue(result["result_id"])
        self.assertEqual(service.db.projects.find_one({"_id": ObjectId(project["_id"])})["imgs"], [])

    def test_project_and_account_deletion_preserve_video_until_removed(self):
        def remove(path, token=self.token, version=None):
            headers = {"Authorization": "Bearer " + token}
            if version is not None:
                headers["X-Project-Version"] = str(version)
            try:
                response = urllib.request.urlopen(urllib.request.Request(BASE + path, method="DELETE", headers=headers))
            except urllib.error.HTTPError as error:
                response = error
            status, body = response.status, response.read()
            response.close()
            return status, body
        self.assertEqual(remove(f"/projects/{self.uid}/{self.pid}", version=0)[0], 409)
        self.assertEqual(remove(f"/users/{self.uid}")[0], 409)
        self.assertTrue(service.users.find_one({"_id": ObjectId(self.uid)}))
        self.assertTrue(service.db.projects.find_one({"_id": ObjectId(self.pid)}))
        self.assertFalse(service.db.projects.find_one({"_id": ObjectId(self.pid)}).get("video_deleting"))
        self.assertEqual(hashlib.sha256(self.api(f"/{self.aid}/file", raw=True)).hexdigest(), hashlib.sha256(self.sample.read_bytes()).hexdigest())
        self.api(f"/{self.aid}", "DELETE", expected=204, raw=True)
        self.assertEqual(remove(f"/projects/{self.uid}/{self.pid}", version=0)[0], 204)
        self.assertEqual(remove(f"/users/{self.uid}")[0], 204)
        # The guard also permits the existing anonymous image-project lifecycle.
        anon, token, pid = self.identity("anonymous")
        self.assertEqual(remove(f"/projects/{anon}/{pid}", token=token, version=0)[0], 204)

    def test_repeated_upload_is_scoped_and_does_not_create_copies(self):
        with ThreadPoolExecutor(max_workers=3) as clients:
            copies = list(clients.map(lambda _: self.upload(self.sample), range(3)))
        self.assertTrue(all(doc["_id"] == self.aid and doc["reused_upload"] for doc in copies))
        self.assertEqual(len(self.api()["assets"]), 1)
        self.assertEqual(len(self.api()["jobs"]), 0)
        self.assertEqual(copies[0]["sha256"], hashlib.sha256(self.sample.read_bytes()).hexdigest())
        uid, token, pid = self.identity("free")
        separate = self.upload(self.sample, owner=uid, token=token, pid=pid)
        self.assertNotEqual(separate["_id"], self.aid)
        self.assertNotIn("reused_upload", separate)

    def test_repeat_requests_reuse_result_without_quota(self):
        uid, token, pid = self.identity("free")
        aid = self.upload(self.sample, token=token, owner=uid, pid=pid)["_id"]
        def submit(_):
            return self.job({"operation": "speed", "factor": .5}, aid, token=token, owner=uid, pid=pid)
        with ThreadPoolExecutor(max_workers=4) as clients:
            responses = list(clients.map(submit, range(4)))
        self.assertEqual(len({item["_id"] for item in responses}), 1)
        first = self.wait(responses[0]["_id"])
        repeated = submit(None)
        self.assertTrue(repeated["reused"])
        self.assertEqual(repeated["result_id"], first["result_id"])
        library = self.api(token=token, owner=uid, pid=pid)
        self.assertEqual(library["remaining"], 4)
        self.assertEqual(len(library["jobs"]), 1)
        self.assertEqual(library["jobs"][0]["input_names"], ["sample.mp4"])
        # A different input version is a new request, even with the same factor.
        second = self.wait(self.job({"operation": "speed", "factor": .5}, first["result_id"], token=token, owner=uid, pid=pid)["_id"])
        self.assertNotEqual(second["_id"], first["_id"])
        result_bytes = self.api(f"/{second['result_id']}/file", token=token, owner=uid, pid=pid, raw=True)
        self.assertEqual(service.assets.find_one({"_id": second["result_id"]})["sha256"], hashlib.sha256(result_bytes).hexdigest())
        colored = self.wait(self.job({"operation": "color", "filters": [{"name": "brightness", "value": 25}, {"name": "contrast", "value": 1}]}, aid, token=token, owner=uid, pid=pid)["_id"])
        equivalent = self.job({"operation": "color", "filters": [{"name": "brightness", "value": 25.0}]}, aid, token=token, owner=uid, pid=pid)
        self.assertTrue(equivalent["reused"])
        self.assertEqual(equivalent["result_id"], colored["result_id"])
        self.assertEqual(self.api(token=token, owner=uid, pid=pid)["remaining"], 2)

    def test_delete_version_cleans_storage_and_preserves_dependencies(self):
        job = self.wait(self.job({"operation": "speed", "factor": 2})["_id"])
        aid = job["result_id"]
        keys = [service.assets.find_one({"_id": aid})["key"]]
        thumb = self.api(f"/{aid}/thumbnail", "POST", {"timestamp": 0}, expected=201)
        keys.append(thumb["key"])
        other = self.api(f"/{aid}/thumbnail", "POST", {"timestamp": 1, "replace_confirmed": True, "expected_thumbnail_id": thumb["_id"]}, expected=201)
        keys.append(other["key"])
        sid = uuid.uuid4().hex
        service.subtitles.insert_one({"_id": sid, "project_id": self.pid, "asset_id": aid,
            "segments": [{"start": 0, "end": 1, "text": "Deletion fixture"}], "published": True, "draft": False})
        service.assets.update_one({"_id": aid}, {"$set": {"subtitles_id": sid}})
        self.api(f"/{self.aid}", "DELETE", expected=409)
        _, read_token, _ = self.identity("free")
        service.db.projects.update_one({"_id": ObjectId(self.pid)}, {"$set": {"sharedLinks": [{"id": "delete-read", "permission": "read", "revoked": False}]}})
        self.api(f"/{aid}?share=delete-read", "DELETE", token=read_token, expected=403)
        self.api(f"/{aid}", "DELETE", expected=204, raw=True)
        self.assertFalse(service.assets.find_one({"_id": aid}))
        self.assertFalse(service.subtitles.find_one({"asset_id": aid}))
        self.assertFalse(service.thumbnails.find_one({"asset_id": aid}))
        self.assertFalse((service.ROOT / (aid + ".mp4")).exists())
        for key in keys:
            with self.assertRaises(ClientError) as missing:
                service.s3.head_object(Bucket=service.BUCKET, Key=key)
            self.assertEqual(missing.exception.response["ResponseMetadata"]["HTTPStatusCode"], 404)
        self.api(f"/{aid}/file", expected=404)
        historical = next(j for j in self.api()["jobs"] if j["_id"] == job["_id"])
        self.assertTrue(historical["result_deleted"])
        self.assertNotIn("result_id", historical)
        # Deleted results are never reused; a new request can create a replacement.
        replacement = self.wait(self.job({"operation": "speed", "factor": 2})["_id"])
        self.assertNotEqual(replacement["result_id"], aid)
        self.api(f"/{replacement['result_id']}", "DELETE", expected=204, raw=True)
        self.api(f"/{self.aid}", "DELETE", expected=204, raw=True)
        self.assertEqual(self.api()["assets"], [])
        self.assertNotEqual(self.upload(self.sample)["_id"], self.aid)

    def test_incomplete_deletion_is_blocked_and_can_be_retried(self):
        thumb = self.api(f"/{self.aid}/thumbnail", "POST", {"timestamp": 0}, expected=201)
        original_delete = service.s3.delete_object
        calls = 0
        def interrupted(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("Injected storage interruption on fixture thumbnail")
            return original_delete(**kwargs)
        with patch.object(service.s3, "delete_object", side_effect=interrupted):
            with self.assertRaises(HTTPException) as failure:
                remove_asset(self.aid, self.pid)
            self.assertEqual(failure.exception.status_code, 503)
        self.assertTrue(self.api()["assets"][0]["deleting"])
        self.api(f"/{self.aid}/file", expected=409)
        self.api(f"/{self.aid}/jobs", "POST", {"operation": "speed", "factor": 2}, expected=409)
        self.api(f"/{self.aid}/thumbnail", "POST", {"timestamp": 0}, expected=409)
        self.api(f"/{self.aid}", "DELETE", expected=204, raw=True)
        self.assertEqual(self.api()["assets"], [])
        with self.assertRaises(ClientError) as missing:
            service.s3.head_object(Bucket=service.BUCKET, Key=thumb["key"])
        self.assertEqual(missing.exception.response["ResponseMetadata"]["HTTPStatusCode"], 404)

    def test_recovery_of_interrupted_and_completed_jobs(self):
        pending, completed, orphan = [uuid.uuid4().hex for _ in range(3)]
        key = quota_key(self.uid)
        service.quotas.insert_one({"_id": key, "used": 3, "reserved": [pending, completed, orphan]})
        for jid, state in [(pending, "processing"), (completed, "completed")]:
            service.jobs.insert_one({"_id": jid, "project_id": self.pid, "quota_key": key, "state": state})
        service.assets.update_one({"_id": self.aid}, {"$set": {"active_job": pending}})
        rid = uuid.uuid4().hex
        result = service.assets.find_one({"_id": self.aid})
        result.update({"_id": rid, "job_id": completed, "published": False, "active_job": orphan})
        service.assets.insert_one(result)
        service.jobs.update_one({"_id": completed}, {"$set": {"result_id": rid}})
        recover_jobs([self.pid])
        self.assertEqual(service.jobs.find_one({"_id": pending})["state"], "failed")
        self.assertIsNone(service.assets.find_one({"_id": self.aid})["active_job"])
        self.assertTrue(service.assets.find_one({"_id": rid})["published"])
        self.assertIsNone(service.assets.find_one({"_id": rid})["active_job"])
        self.assertEqual(service.quotas.find_one({"_id": key})["used"], 1)
        recover_jobs([self.pid])
        self.assertEqual(service.quotas.find_one({"_id": key})["used"], 1)

    def test_input_limits_and_variable_fps(self):
        for name, kwargs in [("duration", {"duration": 121}), ("resolution", {"duration": 1, "resolution": "1920x1080"}), ("fps", {"duration": 1, "fps": 60})]:
            path = self.root / (name + ".mp4")
            self.make_video(path, audio=False, **kwargs)
            self.upload(path, expected=422)
        vfr = self.root / "vfr.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(self.sample), "-vf", "select=not(eq(n\\,5))", "-fps_mode", "vfr", "-c:v", "libx264", "-threads", "2", "-an", str(vfr)], check=True)
        self.upload(vfr, expected=422)
        oversize = self.root / "oversize.mp4"
        with oversize.open("wb") as f:
            f.truncate(100_000_001)
        self.upload(oversize, expected=413)

    def test_speed_audiovisual_event_sync(self):
        path = self.root / "event.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=160x90:r=30:d=4",
                        "-f", "lavfi", "-i", "aevalsrc=if(lt(abs(t-1)\\,0.01)\\,0.8*sin(2*PI*440*t)\\,0):s=48000:d=4",
                        "-vf", "drawbox=color=white:t=fill:enable=gte(t\\,1)", "-c:v", "libx264", "-threads", "2", "-c:a", "aac", str(path)], check=True)
        aid = self.upload(path)["_id"]
        for factor in [.5, 1.5, 2]:
            result = self.result_path(self.wait(self.job({"operation": "speed", "factor": factor}, aid)["_id"]))
            frames = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(result), "-an", "-f", "rawvideo", "-pix_fmt", "gray", "-"])
            visual = next(i for i, pixel in enumerate(frames[::160 * 90]) if pixel > 200) / 30
            decoded = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(result), "-vn", "-ac", "1", "-ar", "48000", "-f", "f32le", "-"])
            samples = array.array("f"); samples.frombytes(decoded)
            audible = next(i for i, sample in enumerate(samples) if abs(sample) > .05) / 48000
            self.assertLessEqual(abs(visual - audible), .1)
            self.assertLessEqual(abs(visual - 1 / factor), .1)

    def legacy(self, path, method="GET", data=None, expected=200, raw=False, headers=None):
        head = {"Authorization": "Bearer " + self.token, **(headers or {})}
        if isinstance(data, dict):
            head["Content-Type"] = "application/json"; data = json.dumps(data).encode()
        try:
            response = urllib.request.urlopen(urllib.request.Request(BASE + path, data=data, method=method, headers=head), timeout=90)
        except urllib.error.HTTPError as error:
            response = error
        payload = response.read(); self.assertEqual(response.status, expected, payload[:1000])
        return payload if raw else json.loads(payload) if payload else None

    def test_existing_image_editor_regression(self):
        project = self.legacy(f"/projects/{self.uid}", "POST", {"name": "VIDEO IMAGE REGRESSION FIXTURE"}, expected=201)
        pid = project["_id"]; self.project_ids.append(pid)
        prefix = f"/projects/{self.uid}/{pid}"
        def version():
            return {"X-Project-Version": str(service.db.projects.find_one({"_id": ObjectId(pid)})["version"])}
        def chunk(name, data):
            return struct.pack("!I", len(data)) + name + data + struct.pack("!I", zlib.crc32(name + data))
        png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!IIBBBBB", 16, 16, 8, 2, 0, 0, 0))
        png += chunk(b"IDAT", zlib.compress((b"\x00" + bytes([60, 120, 180]) * 16) * 16)) + chunk(b"IEND", b"")
        boundary = uuid.uuid4().hex
        body = f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="regression.png"\r\nContent-Type: image/png\r\n\r\n'.encode() + png + f"\r\n--{boundary}--\r\n".encode()
        try:
            self.legacy(prefix + "/img", "POST", body, expected=204, headers={**version(), "Content-Type": "multipart/form-data; boundary=" + boundary})
            self.legacy(prefix + "/tool", "POST", {"procedure": "brightness", "params": {"brightness": 1.5}}, expected=201, raw=True, headers=version())
            self.legacy(prefix + "/process", "POST", {}, expected=201, headers=version())
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                current = service.db.projects.find_one({"_id": ObjectId(pid)})
                if current.get("committedToken", 0) > 0:
                    break
                time.sleep(.2)
            self.assertGreater(current.get("committedToken", 0), 0)
            result = self.legacy(prefix + "/process", raw=True)
            with zipfile.ZipFile(io.BytesIO(result)) as zipped:
                self.assertEqual(len(zipped.namelist()), 1)
                output = zipped.read(zipped.namelist()[0])
            pixels = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], input=output, capture_output=True, check=True).stdout
            self.assertEqual(list(pixels[:3]), [90, 180, 255])
            self.assertEqual(len(self.api()["assets"]), 1)
        finally:
            self.legacy(prefix, "DELETE", expected=204, headers=version())


if __name__ == "__main__":
    unittest.main(verbosity=2)
