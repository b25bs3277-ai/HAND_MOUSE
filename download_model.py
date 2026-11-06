"""
download_model.py
-----------------
Only needed if you use a NEW mediapipe (0.10.30 or later, or 1.x), which no
longer ships the built-in hand model.

Downloads Google's hand model ONCE (about 7.5 MB) to:
    models/hand_landmarker.task
After that, the app works with no internet. build.bat also packs this file
into the .exe automatically when it exists.

Run:   python download_model.py
"""

import hashlib
import os
import sys
import urllib.request

URL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
       "hand_landmarker/float16/1/hand_landmarker.task")
EXPECTED_SHA256 = "fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1"

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "models", "hand_landmarker.task")


def main():
    os.makedirs(os.path.dirname(TARGET), exist_ok=True)
    if os.path.exists(TARGET) and os.path.getsize(TARGET) > 1_000_000:
        print("Model already there:", TARGET)
        return
    print("Downloading", URL)
    tmp = TARGET + ".part"
    try:
        with urllib.request.urlopen(URL, timeout=60) as response, open(tmp, "wb") as out:
            total = int(response.headers.get("Content-Length", 0))
            done = 0
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r  {done * 100 // total}%", end="")
        print()
    except Exception as e:
        print("Download failed:", e)
        print("Download it in a browser from the URL above and save it as", TARGET)
        sys.exit(1)

    with open(tmp, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    if digest != EXPECTED_SHA256:
        print("Warning: the file checksum is different from the one we expected.")
        print("It may be a newer model version. It will still be used.")
    os.replace(tmp, TARGET)
    print("Saved:", TARGET)


if __name__ == "__main__":
    main()
