"""Checks that the app's output is the original scripts' output, character for character.

1. Runs ocr_text.py and ocr_markdown.py on each test file (results cached in tests/out/baseline).
2. Sends the same files through the running app, in the matching mode, and downloads the result.
3. Compares the two byte for byte.

Usage (the app must be running: ./start.sh):
  ../.venv/bin/python tests/accuracy_test.py [--fresh] [--modes text,markdown] [files ...]
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PY = ROOT / ".venv" / "bin" / "python"
SCRIPTS = {"text": (ROOT / "ocr_text.py", ".txt"), "markdown": (ROOT / "ocr_markdown.py", ".md")}
APP = "http://127.0.0.1:8765"


def http(method, path, data=None, headers=None):
    req = urllib.request.Request(APP + path, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    return json.loads(http(method, path, data, {"Content-Type": "application/json"} if data else None))


def upload(folder_id, path: Path, mode: str):
    boundary = uuid.uuid4().hex
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"mode\"\r\n\r\n{mode}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{path.name}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n".encode() + path.read_bytes() + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    return json.loads(http("POST", f"/api/folders/{folder_id}/upload", b"".join(parts),
                           {"Content-Type": f"multipart/form-data; boundary={boundary}"}))


def baseline(path: Path, mode: str, fresh: bool) -> bytes:
    script, ext = SCRIPTS[mode]
    out_dir = HERE / "out" / "baseline" / mode
    out_dir.mkdir(parents=True, exist_ok=True)
    result = out_dir / (path.name + ext)
    if fresh or not result.exists():
        shutil.copy(path, out_dir / path.name)
        print(f"  running {script.name} on {path.name} …", flush=True)
        subprocess.run([str(PY), str(script), path.name], cwd=out_dir, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.read_bytes()


def first_difference(a: str, b: str) -> str:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return f"at character {i}: script {a[max(0, i - 20):i + 20]!r} / app {b[max(0, i - 20):i + 20]!r}"
    return f"lengths differ: script {len(a)} characters, app {len(b)}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--fresh", action="store_true", help="re-run the original scripts")
    ap.add_argument("--modes", default="text,markdown")
    args = ap.parse_args()
    files = [Path(f).resolve() for f in args.files] or sorted((HERE / "fixtures").glob("*.*"))
    modes = args.modes.split(",")

    folder = api("POST", "/api/folders", {"name": f"ทดสอบความแม่นยำ {time.strftime('%H:%M:%S')}"})
    print(f"app folder {folder['id']}: {folder['name']}")
    results = []
    for mode in modes:
        for path in files:
            expected = baseline(path, mode, args.fresh)
            job_id = upload(folder["id"], path, mode)["jobs"][0]
            start = time.time()
            while True:
                job = api("GET", f"/api/jobs/{job_id}")
                if job["status"] not in ("queued", "running"):
                    break
                time.sleep(1)
            got = http("GET", f"/api/jobs/{job_id}/download?fmt={'md' if mode == 'markdown' else 'txt'}")
            same = got == expected
            seconds = [p["seconds"] for p in job["pages"] if p["seconds"]]
            line = (f"{'PASS' if same else 'FAIL'}  {mode:8} {path.name:20} {job['status']:9} "
                    f"{len(expected.decode()):5} chars  {sum(seconds) / max(len(seconds), 1):5.1f} s/page (app)")
            if not same:
                line += "\n      " + first_difference(expected.decode(), got.decode())
            print(line, flush=True)
            results.append(same)
    print(f"\n{sum(results)}/{len(results)} identical to the original scripts")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
