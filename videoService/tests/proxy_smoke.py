"""Check the public local HTTPS entry point without creating application data."""

import re
import ssl
import urllib.error
import urllib.request
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[2]
    # Trust only the project's development certificate for this local check.
    context = ssl.create_default_context(cafile=str(root / "certs/selfsigned.crt"))

    def check(path, expected):
        try:
            with urllib.request.urlopen(
                "https://localhost:8080" + path, context=context, timeout=15
            ) as response:
                status, body = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, body = error.code, error.read()
        if status != expected:
            raise AssertionError(f"{path}: expected {expected}, received {status}")
        print(f"{path}: {status}")
        return body

    home = check("/", 200)
    scripts = re.findall(rb'<script[^>]+src="([^\"]+)"', home)
    asset = next(src.decode() for src in scripts if src.startswith(b"/_next/static/"))
    check(asset, 200)
    check("/dashboard/smoke/video", 200)
    # A denied request confirms that the API is reachable and enforces auth.
    check("/api-gateway/projects/smoke/smoke/videos", 401)
    check("/socket.io/?EIO=4&transport=polling", 200)
    check("/s3-console/", 200)


if __name__ == "__main__":
    main()
