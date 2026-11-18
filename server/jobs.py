"""Job queues: one worker thread per model, one page at a time, results saved page by page."""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
import traceback
from pathlib import Path

from . import db
from .engines import Cancelled, MarkdownEngine, TextEngine


class EventBus:
    """Fans worker events out to every open SSE connection."""

    def __init__(self):
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def bind(self, loop):
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=1000)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subs.discard(q)

    def publish(self, event: dict):
        if self._loop is None:
            return
        data = json.dumps(event, ensure_ascii=False)

        def deliver():
            with self._lock:
                subs = list(self._subs)
            for q in subs:
                try:
                    q.put_nowait(data)
                except asyncio.QueueFull:
                    pass
        self._loop.call_soon_threadsafe(deliver)


bus = EventBus()
engines = {"text": TextEngine(), "markdown": MarkdownEngine()}
_queues = {"text": queue.Queue(), "markdown": queue.Queue()}
_cancel: set[int] = set()
_cancel_lock = threading.Lock()
busy = {"text": None, "markdown": None}   # job id each worker is on


def folder_dir(folder_id: int) -> Path:
    return db.DATA_DIR / "folders" / str(folder_id)


def job_state(conn, job_id: int) -> dict | None:
    j = db.row(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    if j is None:
        return None
    j["pages_done"] = conn.execute("SELECT COUNT(*) FROM pages WHERE job_id=?", (job_id,)).fetchone()[0]
    j["pages_failed"] = conn.execute(
        "SELECT COUNT(*) FROM pages WHERE job_id=? AND status!='ok'", (job_id,)).fetchone()[0]
    return j


def _emit(job_id: int, kind: str, **extra):
    with db.tx() as conn:
        j = job_state(conn, job_id)
    if j:
        bus.publish({"kind": kind, "job": j, **extra})


def enqueue(job_id: int, mode: str):
    with _cancel_lock:
        _cancel.discard(job_id)
    _queues[mode].put(job_id)


def cancel(job_id: int) -> str | None:
    """Queued jobs stop now; a running job stops within moments (OCR Markdown is cut off mid-page,
    OCR Text pages take under a second) and the unfinished page is not saved."""
    with db.tx() as conn:
        j = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if j is None:
            return None
        if j["status"] in ("queued", "interrupted"):
            conn.execute("UPDATE jobs SET status='cancelled', finished_at=? WHERE id=?", (db.now(), job_id))
    with _cancel_lock:
        _cancel.add(job_id)
    _emit(job_id, "job")
    return "ok"


def _cancelled(job_id: int) -> bool:
    with _cancel_lock:
        return job_id in _cancel


def _run(job_id: int, mode: str):
    with db.tx() as conn:
        j = db.row(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        if j is None or j["status"] != "queued":
            return
        doc = db.row(conn.execute("SELECT * FROM documents WHERE id=?", (j["document_id"],)).fetchone())
        done = {r[0] for r in conn.execute("SELECT page_no FROM pages WHERE job_id=?", (job_id,))}
        conn.execute("UPDATE jobs SET status='running', started_at=COALESCE(started_at, ?) WHERE id=?",
                     (db.now(), job_id))
    _emit(job_id, "job")
    path = folder_dir(j["folder_id"]) / doc["stored_name"]
    engine = engines[mode]
    announce_ready = mode == "markdown" and not engine.loaded
    if announce_ready:
        bus.publish({"kind": "model", "mode": "markdown", "state": "loading", "job_id": job_id})

    for number in range(1, j["page_total"] + 1):
        if _cancelled(job_id):
            with db.tx() as conn:
                conn.execute("UPDATE jobs SET status='cancelled', finished_at=? WHERE id=?", (db.now(), job_id))
            _emit(job_id, "job")
            return
        if number in done:
            continue
        bus.publish({"kind": "page_start", "job_id": job_id, "folder_id": j["folder_id"], "page_no": number})
        start = time.perf_counter()
        meta, error = None, None
        try:
            r = engine.read(path, number, should_stop=lambda: _cancelled(job_id))
            content = r["content"]
            status = "incomplete" if r.get("incomplete") else "ok"
            if mode == "text":
                meta = {"lines": r["lines"], "dropped": r["dropped"]}
        except Cancelled:
            continue  # the check at the top of the loop marks the job cancelled
        except Exception as e:  # a bad page must not stop the rest
            traceback.print_exc()
            content, status, error = "", "failed", f"{type(e).__name__}: {e}"
        seconds = time.perf_counter() - start
        if _cancelled(job_id):
            continue  # cancelled while this page was read: drop it, as the user asked to stop
        try:
            with db.tx() as conn:
                still = conn.execute("SELECT created_at FROM jobs WHERE id=?", (job_id,)).fetchone()
                if still is None or still[0] != j["created_at"]:
                    return  # the job or its chat was deleted meanwhile
                cur = conn.execute(
                    "INSERT INTO pages(job_id, document_id, page_no, mode, status, content, meta, error, seconds)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (job_id, doc["id"], number, mode, status, content,
                     json.dumps(meta, ensure_ascii=False) if meta else None, error, seconds))
                page = db.row(conn.execute("SELECT * FROM pages WHERE id=?", (cur.lastrowid,)).fetchone())
        except Exception:
            return  # the job or its folder was deleted meanwhile
        if announce_ready and engine.loaded:
            bus.publish({"kind": "model", "mode": "markdown", "state": "ready"})
            announce_ready = False
        _emit(job_id, "page", page=page)
        print(f"job {job_id} {doc['filename']} page {number} ({mode}): {seconds:.1f} s {status}", flush=True)

    with db.tx() as conn:
        status = "cancelled" if _cancelled(job_id) else "done"   # cancelled on its last page
        conn.execute("UPDATE jobs SET status=?, finished_at=? WHERE id=?", (status, db.now(), job_id))
    _emit(job_id, "job")


def _worker(mode: str):
    while True:
        job_id = _queues[mode].get()
        busy[mode] = job_id
        try:
            _run(job_id, mode)
        except Exception as e:
            traceback.print_exc()
            try:
                with db.tx() as conn:
                    conn.execute("UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=?",
                                 (f"{type(e).__name__}: {e}", db.now(), job_id))
                _emit(job_id, "job")
            except Exception:
                pass
        finally:
            busy[mode] = None
            with _cancel_lock:
                _cancel.discard(job_id)


def start_workers():
    # Jobs that were queued or running when the app last stopped are marked interrupted;
    # the user resumes them, and only the pages without a result are read.
    with db.tx() as conn:
        conn.execute("UPDATE jobs SET status='interrupted' WHERE status IN ('queued', 'running')")
    for mode in ("text", "markdown"):
        threading.Thread(target=_worker, args=(mode,), daemon=True, name=f"ocr-{mode}").start()
