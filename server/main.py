"""HTTP API and the built web page. Run: python -m server.main (from webapp/)."""
from __future__ import annotations

import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel

from . import db, jobs
from .engines import page_count, render_pdf_page

WEB_DIST = Path(__file__).resolve().parents[1] / "web" / "dist"
ALLOWED = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}
MODES = ("text", "markdown")


@asynccontextmanager
async def lifespan(app):
    db.init()
    jobs.bus.bind(asyncio.get_running_loop())
    jobs.start_workers()
    yield


app = FastAPI(lifespan=lifespan)


class FolderIn(BaseModel):
    name: str = ""
    kind: str = "chat"               # chat: holds files and results; folder: groups chats
    parent_id: int | None = None     # the folder a chat sits in


class FolderPatch(BaseModel):
    name: str | None = None
    parent_id: int | None = None     # sent as null to take a chat out of its folder


class ModeIn(BaseModel):
    mode: str


def _folder_or_404(conn, folder_id: int) -> dict:
    f = db.row(conn.execute("SELECT * FROM folders WHERE id=?", (folder_id,)).fetchone())
    if f is None:
        raise HTTPException(404, "ไม่พบโฟลเดอร์หรือแชต")
    return f


def _chat_or_404(conn, chat_id: int) -> dict:
    f = _folder_or_404(conn, chat_id)
    if f["kind"] != "chat":
        raise HTTPException(400, "แนบไฟล์ได้ในแชตเท่านั้น")
    return f


def _check_parent(conn, parent_id: int | None):
    if parent_id is None:
        return
    p = _folder_or_404(conn, parent_id)
    if p["kind"] != "folder":
        raise HTTPException(400, "ย้ายแชตไปได้เฉพาะโฟลเดอร์")


def _clean_name(name: str, kind: str = "folder") -> str:
    name = name.strip()
    if not name:
        if kind == "chat":
            return "แชตใหม่"
        raise HTTPException(400, "กรุณาตั้งชื่อโฟลเดอร์")
    return name[:120]


FOLDER_SELECT = """
    SELECT f.*,
      (SELECT COUNT(*) FROM documents d WHERE d.folder_id=f.id
          OR d.folder_id IN (SELECT c.id FROM folders c WHERE c.parent_id=f.id)) AS documents,
      (SELECT COUNT(*) FROM jobs j WHERE (j.folder_id=f.id
          OR j.folder_id IN (SELECT c.id FROM folders c WHERE c.parent_id=f.id))
          AND j.status IN ('queued','running')) AS active,
      COALESCE((SELECT MAX(m.created_at) FROM messages m WHERE m.folder_id=f.id), f.created_at) AS updated_at
    FROM folders f"""


def _job_full(conn, job_id: int) -> dict | None:
    j = jobs.job_state(conn, job_id)
    if j is None:
        return None
    j["document"] = db.row(conn.execute(
        "SELECT id, filename, page_count, size FROM documents WHERE id=?", (j["document_id"],)).fetchone())
    pages = []
    for p in conn.execute("SELECT * FROM pages WHERE job_id=? ORDER BY page_no", (job_id,)):
        p = dict(p)
        p["meta"] = json.loads(p["meta"]) if p["meta"] else None
        pages.append(p)
    j["pages"] = pages
    return j


# -- folders ------------------------------------------------------------------

@app.get("/api/folders")
def list_folders():
    with db.tx() as conn:
        rows = conn.execute(FOLDER_SELECT + " ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]


def _one(conn, folder_id: int) -> dict:
    return db.row(conn.execute(FOLDER_SELECT + " WHERE f.id=?", (folder_id,)).fetchone())


@app.post("/api/folders")
def create_folder(body: FolderIn):
    if body.kind not in ("chat", "folder"):
        raise HTTPException(400, "ชนิดไม่ถูกต้อง")
    with db.tx() as conn:
        parent = body.parent_id if body.kind == "chat" else None
        _check_parent(conn, parent)
        cur = conn.execute("INSERT INTO folders(id, name, kind, parent_id, created_at) VALUES (?,?,?,?,?)",
                           (db.next_id(conn, "folders"), _clean_name(body.name, body.kind), body.kind, parent, db.now()))
        f = _one(conn, cur.lastrowid)
    jobs.bus.publish({"kind": "folders"})
    return f


@app.patch("/api/folders/{folder_id}")
def update_folder(folder_id: int, body: FolderPatch):
    with db.tx() as conn:
        f = _folder_or_404(conn, folder_id)
        if body.name is not None:
            conn.execute("UPDATE folders SET name=? WHERE id=?", (_clean_name(body.name), folder_id))
        if "parent_id" in body.model_fields_set:
            if f["kind"] != "chat":
                raise HTTPException(400, "ย้ายได้เฉพาะแชต")
            _check_parent(conn, body.parent_id)
            conn.execute("UPDATE folders SET parent_id=? WHERE id=?", (body.parent_id, folder_id))
        out = _one(conn, folder_id)
    jobs.bus.publish({"kind": "folders"})
    return out


@app.delete("/api/folders/{folder_id}")
def delete_folder(folder_id: int):
    """Deleting a folder deletes the chats in it too."""
    with db.tx() as conn:
        _folder_or_404(conn, folder_id)
        ids = [folder_id] + [r[0] for r in conn.execute("SELECT id FROM folders WHERE parent_id=?", (folder_id,))]
        marks = ",".join("?" * len(ids))
        active = [r[0] for r in conn.execute(
            f"SELECT id FROM jobs WHERE folder_id IN ({marks}) AND status IN ('queued','running')", ids)]
    for job_id in active:
        jobs.cancel(job_id)
    with db.tx() as conn:
        conn.execute(f"DELETE FROM folders WHERE id IN ({marks})", ids)
    for i in ids:
        shutil.rmtree(jobs.folder_dir(i), ignore_errors=True)
    jobs.bus.publish({"kind": "folders"})
    return {"ok": True}


# -- messages, uploads, jobs ----------------------------------------------------

@app.get("/api/folders/{folder_id}/messages")
def list_messages(folder_id: int):
    with db.tx() as conn:
        _chat_or_404(conn, folder_id)
        out = []
        for m in conn.execute("SELECT * FROM messages WHERE folder_id=? ORDER BY id", (folder_id,)):
            m = dict(m)
            m["payload"] = json.loads(m["payload"])
            if m["type"] == "ocr_result":
                m["job"] = _job_full(conn, m["payload"]["job_id"])
                if m["job"] is None:
                    continue
            out.append(m)
        return out


def _new_job(conn, folder_id: int, doc: dict, mode: str) -> int:
    cur = conn.execute(
        "INSERT INTO jobs(id, folder_id, document_id, mode, status, page_total, created_at) VALUES (?,?,?,?,?,?,?)",
        (db.next_id(conn, "jobs"), folder_id, doc["id"], mode, "queued", doc["page_count"], db.now()))
    job_id = cur.lastrowid
    db.add_message(conn, folder_id, "assistant", "ocr_result", {"job_id": job_id})
    return job_id


@app.post("/api/folders/{folder_id}/upload")
async def upload(folder_id: int, files: list[UploadFile] = File(...), mode: str = Form("text")):
    if mode not in MODES:
        raise HTTPException(400, "โหมดไม่ถูกต้อง")
    with db.tx() as conn:
        _chat_or_404(conn, folder_id)
    target = jobs.folder_dir(folder_id)
    target.mkdir(parents=True, exist_ok=True)
    accepted, rejected = [], []
    for f in files:
        name = Path(f.filename or "file").name
        ext = Path(name).suffix.lower()
        if ext not in ALLOWED:
            rejected.append({"filename": name, "reason": "ชนิดไฟล์นี้ยังไม่รองรับ (รับ PDF, PNG, JPG, TIFF, WEBP)"})
            continue
        with db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO documents(id, folder_id, filename, stored_name, page_count, size, created_at)"
                " VALUES (?,?,?,?,?,?,?)", (db.next_id(conn, "documents"), folder_id, name, "", 0, 0, db.now()))
            doc_id = cur.lastrowid
        # stored under a plain ASCII name: the original name stays in the database
        stored = f"{doc_id}{ext}"
        path = target / stored
        with open(path, "wb") as out:
            while chunk := await f.read(1 << 20):
                out.write(chunk)   # the file is kept byte for byte as uploaded
        try:
            if ext == ".pdf":
                pages = page_count(path)
                if pages < 1:
                    raise ValueError("ไม่มีหน้า")
            else:
                with Image.open(path) as im:
                    im.verify()
                pages = 1
        except Exception:
            path.unlink(missing_ok=True)
            with db.tx() as conn:
                conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            rejected.append({"filename": name, "reason": "เปิดไฟล์ไม่ได้ ไฟล์อาจเสียหรือมีรหัสผ่าน"})
            continue
        with db.tx() as conn:
            conn.execute("UPDATE documents SET stored_name=?, page_count=?, size=? WHERE id=?",
                         (stored, pages, path.stat().st_size, doc_id))
            accepted.append(db.row(conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()))

    job_ids = []
    with db.tx() as conn:
        db.add_message(conn, folder_id, "user", "upload", {
            "mode": mode,
            "files": [{"document_id": d["id"], "filename": d["filename"], "pages": d["page_count"]} for d in accepted],
            "rejected": rejected})
        for d in accepted:
            job_ids.append(_new_job(conn, folder_id, d, mode))
    for job_id in job_ids:
        jobs.enqueue(job_id, mode)
    jobs.bus.publish({"kind": "messages", "folder_id": folder_id})
    return {"jobs": job_ids, "rejected": rejected}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int):
    with db.tx() as conn:
        j = _job_full(conn, job_id)
    if j is None:
        raise HTTPException(404, "ไม่พบงาน")
    return j


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: int):
    if jobs.cancel(job_id) is None:
        raise HTTPException(404, "ไม่พบงาน")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: int):
    """Reads only the pages that have no result yet."""
    with db.tx() as conn:
        j = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if j is None:
            raise HTTPException(404, "ไม่พบงาน")
        if j["status"] not in ("interrupted", "cancelled", "failed"):
            raise HTTPException(409, "งานนี้ไม่ได้หยุดอยู่")
        conn.execute("UPDATE jobs SET status='queued', error=NULL, finished_at=NULL WHERE id=?", (job_id,))
        mode = j["mode"]
    jobs.enqueue(job_id, mode)
    jobs._emit(job_id, "job")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retry-failed")
def retry_failed(job_id: int):
    """Drops pages that failed or stopped early, then reads them again."""
    with db.tx() as conn:
        j = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if j is None:
            raise HTTPException(404, "ไม่พบงาน")
        if j["status"] in ("queued", "running"):
            raise HTTPException(409, "งานนี้ยังทำอยู่")
        conn.execute("DELETE FROM pages WHERE job_id=? AND status!='ok'", (job_id,))
        conn.execute("UPDATE jobs SET status='queued', error=NULL, finished_at=NULL WHERE id=?", (job_id,))
        mode = j["mode"]
    jobs.enqueue(job_id, mode)
    jobs._emit(job_id, "job")
    return {"ok": True}


@app.post("/api/documents/{doc_id}/rerun")
def rerun(doc_id: int, body: ModeIn):
    if body.mode not in MODES:
        raise HTTPException(400, "โหมดไม่ถูกต้อง")
    with db.tx() as conn:
        d = db.row(conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())
        if d is None:
            raise HTTPException(404, "ไม่พบไฟล์")
        job_id = _new_job(conn, d["folder_id"], d, body.mode)
    jobs.enqueue(job_id, body.mode)
    jobs.bus.publish({"kind": "messages", "folder_id": d["folder_id"]})
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}/download")
def download(job_id: int, fmt: str = "md"):
    if fmt not in ("md", "txt"):
        raise HTTPException(400, "รูปแบบไม่ถูกต้อง")
    with db.tx() as conn:
        j = _job_full(conn, job_id)
    if j is None:
        raise HTTPException(404, "ไม่พบงาน")
    # the scripts join pages with one blank line; the content is the model's own output
    body = "\n\n".join(p["content"] for p in j["pages"])
    name = f"{j['document']['filename']}.{fmt}"
    return Response(
        body.encode("utf-8"),
        media_type=("text/markdown" if fmt == "md" else "text/plain") + "; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=\"download.{fmt}\"; filename*=UTF-8''{quote(name)}"})


def _doc_or_404(doc_id: int) -> dict:
    with db.tx() as conn:
        d = db.row(conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())
    if d is None:
        raise HTTPException(404, "ไม่พบไฟล์")
    return d


@app.get("/api/documents/{doc_id}/pages/{number}/image")
def page_image(doc_id: int, number: int):
    """A picture of the page for checking by eye. Only for viewing: OCR never uses it."""
    d = _doc_or_404(doc_id)
    if not 1 <= number <= d["page_count"]:
        raise HTTPException(404, "ไม่มีหน้านี้")
    folder = jobs.folder_dir(d["folder_id"])
    cache = folder / "previews" / f"{doc_id}_{number}.png"
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        src = folder / d["stored_name"]
        if src.suffix.lower() == ".pdf":
            im = render_pdf_page(src, number, scale=150 / 72)
        else:
            im = Image.open(src)
            im.seek(0)
            im = im.convert("RGB")
        im.thumbnail((2000, 2000))
        im.save(cache)
    return FileResponse(cache, media_type="image/png")


@app.get("/api/documents/{doc_id}/file")
def original_file(doc_id: int):
    d = _doc_or_404(doc_id)
    return FileResponse(jobs.folder_dir(d["folder_id"]) / d["stored_name"], filename=d["filename"])


# -- search ---------------------------------------------------------------------

def _snippet(text: str, q: str, width: int = 70) -> dict:
    i = text.lower().find(q.lower())
    if i < 0:
        return {"before": text[:width * 2], "match": "", "after": ""}
    a = max(0, i - width)
    b = min(len(text), i + len(q) + width)
    return {"before": ("…" if a else "") + text[a:i], "match": text[i:i + len(q)],
            "after": text[i + len(q):b] + ("…" if b < len(text) else "")}


@app.get("/api/folders/{folder_id}/search")
def search(folder_id: int, q: str):
    q = q.strip()
    if not q:
        return []
    with db.tx() as conn:
        _folder_or_404(conn, folder_id)
        base = """SELECT p.id, p.job_id, p.page_no, p.mode, p.content, d.filename, d.id AS document_id,
                         j.folder_id AS chat_id
                  FROM pages p JOIN jobs j ON j.id=p.job_id JOIN documents d ON d.id=p.document_id
                  WHERE (j.folder_id=? OR j.folder_id IN (SELECT id FROM folders WHERE parent_id=?)) AND """
        if len(q) >= 3:
            # one quoted phrase; trigram matches it anywhere, ASCII without regard to case
            phrase = '"' + q.replace('"', '""') + '"'
            rows = conn.execute(base + "p.id IN (SELECT rowid FROM pages_fts WHERE pages_fts MATCH ?)"
                                " ORDER BY d.filename, p.mode, p.page_no LIMIT 200", (folder_id, folder_id, phrase)).fetchall()
        else:
            # trigram needs three characters; shorter words are looked up directly
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            rows = conn.execute(base + "p.content LIKE ? ESCAPE '\\' ORDER BY d.filename, p.mode, p.page_no LIMIT 200",
                                (folder_id, folder_id, like)).fetchall()
    return [{"page_id": r["id"], "job_id": r["job_id"], "page_no": r["page_no"], "mode": r["mode"],
             "filename": r["filename"], "document_id": r["document_id"], "chat_id": r["chat_id"],
             "snippet": _snippet(r["content"], q)} for r in rows]


# -- status and live events -------------------------------------------------------

@app.get("/api/status")
def status():
    return {"markdown_loaded": jobs.engines["markdown"].loaded, "busy": jobs.busy}


@app.post("/api/models/markdown/unload")
def unload_markdown():
    if jobs.busy["markdown"] is not None or not jobs._queues["markdown"].empty():
        raise HTTPException(409, "โมเดลกำลังทำงานอยู่ ปล่อยได้เมื่อไม่มีงาน OCR Markdown ค้างอยู่")
    freed = jobs.engines["markdown"].unload()
    jobs.bus.publish({"kind": "model", "mode": "markdown", "state": "unloaded"})
    return {"freed": freed}


@app.get("/api/events")
async def events():
    q = jobs.bus.subscribe()

    async def stream():
        try:
            yield "retry: 2000\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            jobs.bus.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if WEB_DIST.exists():
    app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")


if __name__ == "__main__":
    import uvicorn
    import os
    # The browser keeps /api/events open, so a graceful shutdown could wait forever while the
    # OCR worker threads go on writing. Give connections two seconds, then end the process for sure.
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning", timeout_graceful_shutdown=2)
    os._exit(0)
