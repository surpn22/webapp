#!/bin/bash
# เปิดแอป OCR เอกสาร: ./start.sh   (ปิดด้วย Ctrl+C)
set -e
cd "$(dirname "$0")"
PY=../.venv/bin/python
PORT=8765

# build หน้าเว็บเมื่อยังไม่เคย build หรือมีการแก้โค้ดหน้าเว็บ
if [ ! -d web/dist ] || [ -n "$(find web/src web/index.html -newer web/dist/index.html 2>/dev/null | head -1)" ]; then
  echo "กำลัง build หน้าเว็บ…"
  [ -d web/node_modules ] || (cd web && npm install --no-fund --no-audit)
  (cd web && npm run build >/dev/null)
fi

if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  echo "แอปเปิดอยู่แล้วที่ http://127.0.0.1:$PORT"
  open "http://127.0.0.1:$PORT"
  exit 0
fi

echo "เปิดแอปที่ http://127.0.0.1:$PORT  (กด Ctrl+C เพื่อปิด)"
( sleep 2; open "http://127.0.0.1:$PORT" ) &
exec "$PY" -m server.main
