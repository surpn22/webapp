"""Draws the test documents with Chromium, which shapes Thai correctly
(Pillow on this Mac has no raqm, so it misplaces Thai vowels and tone marks).

Writes to OUT_DIR:
  thai.png, english.png, mixed_table.png   one page each, as a screenshot
  scan.jpg                                 the Thai page tilted, noisy and JPEG-compressed, like a scan
  three_pages.pdf                          the three pages as a real (text) PDF

Needs Playwright with Chromium (not part of the app):
  python -m venv /tmp/pw && /tmp/pw/bin/pip install playwright pillow numpy && /tmp/pw/bin/playwright install chromium
  /tmp/pw/bin/python tests/make_fixtures.py tests/fixtures
"""
import io
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter
from playwright.sync_api import sync_playwright

THAI = """
<h2 style="text-align:center">พระราชบัญญัติส่งเสริมและรักษาคุณภาพสิ่งแวดล้อมแห่งชาติ<br>พ.ศ. ๒๕๓๕</h2>
<p><b>มาตรา ๔</b> ในพระราชบัญญัตินี้</p>
<p>“สิ่งแวดล้อม” หมายความว่า สิ่งต่าง ๆ ที่มีลักษณะทางกายภาพและชีวภาพที่อยู่รอบตัวมนุษย์
ซึ่งเกิดขึ้นโดยธรรมชาติและสิ่งที่มนุษย์ได้ทำขึ้น</p>
<p>“มลพิษ” หมายความว่า ของเสีย วัตถุอันตราย และมลสารอื่น ๆ รวมทั้งกาก ตะกอน หรือสิ่งตกค้าง
จากสิ่งเหล่านั้น ที่ถูกปล่อยทิ้งจากแหล่งกำเนิดมลพิษ หรือที่มีอยู่ในสิ่งแวดล้อมตามธรรมชาติ</p>
<p><b>มาตรา ๙๖</b> ผู้ใดฝ่าฝืนต้องระวางโทษจำคุกไม่เกินหนึ่งปี หรือปรับไม่เกินหนึ่งแสนบาท หรือทั้งจำทั้งปรับ</p>
"""
ENGLISH = """
<h2 style="text-align:center">ENHANCEMENT AND CONSERVATION OF NATIONAL<br>ENVIRONMENTAL QUALITY ACT, B.E. 2535</h2>
<p><b>Section 4.</b> In this Act:</p>
<p>“Environment” means natural things which form the physical and biological conditions surrounding
man, and man-made things.</p>
<p>“Pollution” means waste, hazardous substances and other pollutants, including residues, sediments or
remnants of such matters, which are discharged from point sources of pollution.</p>
<p><b>Section 96.</b> Any person who violates this provision shall be liable to imprisonment for a term
not exceeding one year or a fine not exceeding 100,000 Baht, or both.</p>
"""
MIXED = """
<h2>รายงานคุณภาพอากาศ (Air Quality Report) ประจำเดือนตุลาคม 2569</h2>
<p>จัดทำโดย กรมควบคุมมลพิษ (Pollution Control Department) ค่า PM2.5 เฉลี่ย 24 ชั่วโมง
ตามมาตรฐานต้องไม่เกิน 37.5 µg/m³</p>
<table>
<tr><th>สถานี (Station)</th><th>PM2.5</th><th>PM10</th><th>สถานะ (Status)</th></tr>
<tr><td>กรุงเทพฯ Bangkok</td><td>42.1</td><td>68.0</td><td>เกินมาตรฐาน</td></tr>
<tr><td>เชียงใหม่ Chiang Mai</td><td>55.3</td><td>81.2</td><td>เกินมาตรฐาน</td></tr>
<tr><td>ภูเก็ต Phuket</td><td>12.4</td><td>25.7</td><td>ดี (Good)</td></tr>
</table>
<p>หมายเหตุ: ข้อมูลเบื้องต้น อาจมีการปรับปรุง (Preliminary data, subject to revision)</p>
"""
CSS = """
@page { size: A4; margin: 0 }
body { margin: 0; font-family: 'Sarabun', 'Thonburi', 'Sukhumvit Set', sans-serif; font-size: 17pt;
       line-height: 1.7; color: #111; background: white; }
.page { width: 210mm; height: 297mm; padding: 22mm 22mm; box-sizing: border-box; page-break-after: always; }
h2 { font-size: 19pt; margin: 0 0 10mm; }
table { border-collapse: collapse; width: 100%; margin: 6mm 0; }
td, th { border: 1.5px solid #111; padding: 2mm 3mm; text-align: left; font-weight: normal; }
"""


def html(*bodies):
    return f"<!doctype html><meta charset=utf-8><style>{CSS}</style>" + "".join(
        f"<div class=page>{b}</div>" for b in bodies)


def scanned(img: Image.Image) -> bytes:
    """Grey, a little tilted and blurred, speckled, JPEG at quality 70."""
    g = img.convert("L").rotate(1.2, resample=Image.BICUBIC, expand=False, fillcolor=255)
    g = g.filter(ImageFilter.GaussianBlur(0.7))
    a = np.asarray(g).astype(np.int16)
    rng = np.random.default_rng(7)
    a = np.clip(a + rng.normal(0, 10, a.shape), 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(a).save(buf, "JPEG", quality=70)
    return buf.getvalue()


out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch()
    # A4 at 200 dpi, as the scripts draw PDF pages
    page = browser.new_page(viewport={"width": 794, "height": 1123}, device_scale_factor=200 / 96)
    for name, body in (("thai", THAI), ("english", ENGLISH), ("mixed_table", MIXED)):
        page.set_content(html(body))
        page.screenshot(path=str(out / f"{name}.png"), full_page=False)
    page.set_content(html(THAI, ENGLISH, MIXED))
    page.pdf(path=str(out / "three_pages.pdf"), format="A4", print_background=True)
    browser.close()
(out / "scan.jpg").write_bytes(scanned(Image.open(out / "thai.png")))
print("wrote", sorted(p.name for p in out.iterdir()))
