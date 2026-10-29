"""OPD Dashboard — แดชบอร์ดบริหารจัดการผู้ป่วยนอก สำหรับโรงพยาบาลที่ใช้ HOSxP

รันเอง:      python app.py            → http://127.0.0.1:5900/
production:  gunicorn -b 0.0.0.0:5900 app:app   (ออกผ่าน sub-path /opd/)
"""

import ipaddress
import json
import mimetypes
import os
import re
import tempfile
import threading
import time
import urllib.parse
from datetime import date, timedelta
from io import BytesIO

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

try:
    import fcntl                # Linux/macOS — ล็อกไฟล์ข้ามโปรเซส (gunicorn หลาย worker)
except ImportError:             # Windows รันด้วย waitress โปรเซสเดียว ใช้ threading.Lock พอ
    fcntl = None

# Windows บางเครื่องตั้ง registry .css = text/plain → Chrome ไม่ใช้ stylesheet (หน้าเว็บไม่มีสไตล์)
# บังคับ MIME เองก่อน Flask เสิร์ฟไฟล์ static ไม่พึ่ง registry ของเครื่อง
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("application/manifest+json", ".webmanifest")

from flask import (Flask, jsonify, redirect, render_template, request, send_file,
                   send_from_directory, url_for)

import bms_session
import charts
import check
from cache import TTLCache
import db
import deps
import flow
import queries
import render
import stmap


class PrefixMiddleware:
    """รองรับการวางแอปไว้หลัง reverse proxy แบบ sub-path

    อ่าน X-Forwarded-Prefix มาตั้ง SCRIPT_NAME และ X-Forwarded-Proto มาตั้ง url_scheme
    ทำให้ url_for() สร้างลิงก์ /opd/... ถูก และยังเข้าตรง :5900 บน LAN ได้เหมือนเดิม
    """

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        prefix = environ.get("HTTP_X_FORWARDED_PREFIX", "")
        if prefix:
            environ["SCRIPT_NAME"] = prefix.rstrip("/")
            path = environ.get("PATH_INFO", "")
            if path.startswith(prefix):
                environ["PATH_INFO"] = path[len(prefix):] or "/"
        proto = environ.get("HTTP_X_FORWARDED_PROTO")
        if proto:
            environ["wsgi.url_scheme"] = proto
        return self.wsgi_app(environ, start_response)


app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
app.wsgi_app = PrefixMiddleware(app.wsgi_app)
app.jinja_env.globals.update(smooth_path=charts.smooth_path, nice_max=charts.nice_max)
#: เวอร์ชันของแอป — แสดงที่ท้ายหน้าและคู่มือ (ประวัติการแก้ไขอยู่ใน CHANGELOG.md)
VERSION = "1.4.0"
#: ผู้พัฒนา — แสดงท้ายทุกหน้าและในคู่มือ (ทุกโรงพยาบาลที่ติดตั้ง) แก้ทับได้ด้วย env OPD_DEV_CONTACT
DEV_CONTACT = "นายเผด็จชัย สิงหล โรงพยาบาลวังน้ำเขียว"
#: เปลี่ยนเลขนี้ทุกครั้งที่เปลี่ยนไอคอน — บังคับ Safari/Chrome โหลดไอคอนหน้าจอโฮมใหม่
ICON_VERSION = "4"

_HERE = os.path.dirname(os.path.abspath(__file__))


@app.context_processor
def _branding():
    """ชื่อ/โลโก้โรงพยาบาลของแต่ละที่ติดตั้ง — ไม่ผูกกับโรงพยาบาลใดโรงพยาบาลหนึ่ง

    ชื่อ: env OPD_HOSPITAL_NAME หรืออ่านจาก opdconfig ของ HOSxP
    โลโก้: ใช้ static/logo.png หรือ static/img/hospital_logo.png
    """
    has_root_logo = os.path.exists(os.path.join(_HERE, "static", "logo.png"))
    has_hosp_logo = os.path.exists(os.path.join(_HERE, "static", "img", "hospital_logo.png"))
    logo_file = "logo.png" if has_root_logo else ("img/hospital_logo.png" if has_hosp_logo else "img/logo.png")
    return {
        "hospital": deps.hospital_name(),
        "logo_url": url_for("static", filename=logo_file) + "?v=" + ICON_VERSION,
        "version": VERSION,
        "icon_v": ICON_VERSION,
        "contact": os.getenv("OPD_CONTACT", "ผู้ดูแลระบบสารสนเทศของโรงพยาบาล"),
        "dev_contact": os.getenv("OPD_DEV_CONTACT", DEV_CONTACT),
    }


@app.teardown_appcontext
def _close_db(_exc):
    """คืน connection ของ request นี้ (db.py ใช้ connection เดียวทุกคำสั่งใน request)"""
    db.close()


app.jinja_env.filters["n"] = lambda v: f"{v:,}" if isinstance(v, (int, float)) else v


#: พารามิเตอร์ของแอปเอง — ตัวอื่นที่เข้ามา (เช่นที่ HOSxP Marketplace ส่งตอนเปิด add-on) จะถูกบันทึก
_KNOWN_ARGS = {"d1", "d2", "print", "d", "t", "tv", "dep", "step", "over",
               "bms-session-id", "marketplace_token", "vn"}     # 3 ตัวหลัง HOSxP ต่อท้ายให้ Add-on
#: header มาตรฐานของเบราว์เซอร์/proxy — ไม่ต้องบันทึก
_STD_HEADERS = {"host", "user-agent", "accept", "accept-language", "accept-encoding", "connection",
                "cookie", "referer", "upgrade-insecure-requests", "cache-control", "pragma",
                "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "sec-fetch-user", "priority",
                "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "if-none-match",
                "if-modified-since", "x-forwarded-for", "x-forwarded-host", "x-forwarded-server",
                "x-forwarded-proto", "x-forwarded-prefix", "cf-ray", "cf-connecting-ip",
                "cf-ipcountry", "cf-visitor", "cdn-loop", "x-real-ip", "content-length", "content-type",
                "origin", "dnt", "purpose", "sec-purpose", "range", "if-range"}


@app.before_request
def _log_unknown_params():
    """บันทึก "ชื่อ" พารามิเตอร์/header/cookie ที่แอปไม่รู้จัก เพื่อดูว่า HOSxP ส่งอะไรมาตอนเปิด add-on

    บันทึกเฉพาะชื่อและความยาวของค่า — ไม่บันทึกค่าเด็ดขาด (อาจเป็น session id / token)
    ใช้ตอนตรวจสอบ add-on เท่านั้น: เปิดด้วย env OPD_LOG_PARAMS=1 (ค่าเริ่มต้นปิด ไม่ให้ log รก)
    """
    if os.getenv("OPD_LOG_PARAMS", "0") != "1" or request.path.startswith("/static/"):
        return
    args = {k: len(v) for k, v in request.args.items() if k not in _KNOWN_ARGS}
    form = {k: len(v) for k, v in request.form.items()} if request.method == "POST" else {}
    heads = {k: len(v) for k, v in request.headers.items() if k.lower() not in _STD_HEADERS}
    cookies = {k: len(v) for k, v in request.cookies.items()}
    if args or form or heads or cookies:
        app.logger.warning("addon-params %s %s args=%s form=%s headers=%s cookies=%s",
                           request.method, request.path, args, form, heads, cookies)


def _parse_date(v):
    """แปลงวันที่ที่ผู้ใช้กรอกเป็น date — คืน None ถ้าอ่านไม่ได้

    รับ YYYY-MM-DD (ช่องเลือกวันที่ของเบราว์เซอร์) และ วว/ดด/ปปปป หรือ วว-ดด-ปปปป
    (เบราว์เซอร์ในโปรแกรม HOSxP ไม่มีปฏิทินให้เลือก ผู้ใช้จึงพิมพ์เอง มักเป็นปี พ.ศ.)
    """
    v = (v or "").strip()
    if not v:
        return None
    try:
        if "/" in v or (v.count("-") == 2 and len(v.split("-")[0]) <= 2):
            dd, mm, yy = (int(x) for x in v.replace("-", "/").split("/"))
            if yy < 100:
                yy += 2500 if yy >= 60 else 2000     # 69 → 2569 (พ.ศ.), 26 → 2026
        else:
            yy, mm, dd = (int(x) for x in v.split("-"))
        if yy > 2400:
            yy -= 543                                  # พ.ศ. → ค.ศ.
        return date(yy, mm, dd)
    except (TypeError, ValueError):
        return None


def _iso(v, default):
    """วันที่สำหรับต่อเข้า SQL — ผ่าน date() แล้วเท่านั้น จึงปลอดภัยจากการแทรกคำสั่ง"""
    d = _parse_date(v)
    return d.isoformat() if d else default


#: ช่วงวันที่ยาวสุดที่ดึงได้ต่อครั้ง — กันการดึงหลายปีจนฐานข้อมูล HOSxP ช้าทั้งโรงพยาบาล
#: (แต่ละหน้ายิงราว 20 คำสั่ง และเทียบช่วงก่อนหน้าที่ยาวเท่ากันอีกรอบ)
MAX_DAYS = int(os.getenv("OPD_MAX_DAYS", "366"))


def _params():
    """(d1, d2, ข้อความเตือน) — กรอกผิดรูปแบบจะแจ้งเตือน ไม่เงียบแล้วกลับไปวันนี้"""
    today = date.today().isoformat()
    warn = []
    out = []
    for key in ("d1", "d2"):
        raw = request.args.get(key)
        d = _parse_date(raw)
        if raw and not d:
            warn.append(f"อ่านวันที่ “{raw}” ไม่ได้ (ใช้รูปแบบ วว/ดด/ปปปป เช่น 16/10/2569)")
        out.append(d.isoformat() if d else today)
    d1, d2 = out
    if d1 > today or d2 > today:
        warn.append("เลือกวันในอนาคต — แสดงถึงวันนี้แทน")
        d1, d2 = min(d1, today), min(d2, today)
    if d1 > d2:
        d1, d2 = d2, d1
    if (date.fromisoformat(d2) - date.fromisoformat(d1)).days >= MAX_DAYS:
        d1 = (date.fromisoformat(d2) - timedelta(days=MAX_DAYS - 1)).isoformat()
        warn.append(f"เลือกได้ไม่เกิน {MAX_DAYS} วันต่อครั้ง — แสดง {MAX_DAYS} วันสุดท้ายของช่วงที่เลือก")
    return d1, d2, " · ".join(warn)


@app.route("/")
def dashboard():
    d1, d2, warn = _params()
    t = date.today()
    first = t.replace(day=1)
    week_start = t - timedelta(days=t.weekday())
    fy_start_year = t.year if t.month >= 10 else t.year - 1
    fy_start = date(fy_start_year, 10, 1)

    ranges = [
        ("วันนี้", t, t),
        ("สัปดาห์นี้", week_start, t),
        ("เดือนนี้", first, t),
        ("ปีงบประมาณนี้", fy_start, t),
    ]
    ranges = [(lbl, a.isoformat(), b.isoformat()) for lbl, a, b in ranges]
    d = queries.build(d1, d2)
    if d.get("stale_since"):
        warn = " · ".join(x for x in (warn, f"เชื่อมต่อ HOSxP ไม่ได้ — แสดงข้อมูลล่าสุดเมื่อ {d['stale_since']}") if x)
    return render_template("dashboard.html", d=d, warn=warn, ranges=ranges,
                           today=t.isoformat(),
                           print_mode=request.args.get("print") == "1")


@app.route("/department")
def department_page():
    """แดชบอร์ดบริหารจัดการผู้ป่วยนอกรายแผนก (OPD Department Dashboard)"""
    d1, d2, warn = _params()
    dep = request.args.get("dep", "").strip()
    t = date.today()
    first = t.replace(day=1)
    week_start = t - timedelta(days=t.weekday())
    fy_start_year = t.year if t.month >= 10 else t.year - 1
    fy_start = date(fy_start_year, 10, 1)

    ranges = [
        ("วันนี้", t, t),
        ("สัปดาห์นี้", week_start, t),
        ("เดือนนี้", first, t),
        ("ปีงบประมาณนี้", fy_start, t),
    ]
    ranges = [(lbl, a.isoformat(), b.isoformat()) for lbl, a, b in ranges]
    depts = []
    try:
        depts = queries.dept_table(d1, d2)
    except Exception as exc:
        app.logger.warning("dept_table failed: %s", exc)

    if not dep and depts:
        dep = depts[0]["dep"]

    try:
        d = queries.build(d1, d2, dep=dep)
    except Exception as exc:
        app.logger.warning("queries.build failed for dep %s: %s", dep, exc)
        hit = queries._cache.get_stale((db.tenant(), d1, d2, dep))
        if hit:
            d = dict(hit[1], stale_since=hit[1].get("generated_at"))
        else:
            raise

    if not depts and d.get("dept_table"):
        depts = d.get("dept_table")

    if d.get("stale_since"):
        warn = " · ".join(x for x in (warn, f"เชื่อมต่อ HOSxP ไม่ได้ — แสดงข้อมูลล่าสุดเมื่อ {d['stale_since']}") if x)
    return render_template("department.html", d=d, dep=dep, depts=depts, warn=warn, ranges=ranges,
                           today=t.isoformat(),
                           print_mode=request.args.get("print") == "1")


def _networks(raw):
    out = []
    for part in (raw or "").replace(" ", ",").split(","):
        if part.strip():
            try:
                out.append(ipaddress.ip_network(part.strip(), strict=False))
            except ValueError:
                app.logger.warning("OPD_TRUSTED_IPS: อ่าน %r ไม่ได้ — ข้าม", part)
    return out


#: IP สาธารณะของโรงพยาบาล — คำขอผ่าน Cloudflare ที่มาจาก IP เหล่านี้นับเป็น "ใน รพ."
#: (เจ้าหน้าที่เปิด https://<โดเมน>/opd/ จากในโรงพยาบาลก็วิ่งผ่าน Cloudflare เหมือนคนนอก)
#: เช่น OPD_TRUSTED_IPS=183.88.214.202 (คั่นด้วยจุลภาค ใส่ช่วง CIDR ได้)
TRUSTED_NETS = _networks(os.getenv("OPD_TRUSTED_IPS", ""))
#: ช่วง IP ภายในที่ไม่ใช่ private มาตรฐาน (10/8, 172.16/12, 192.168/16) — เช่นเครือข่ายที่ใช้ 30.25.0.0/16
#: ใช้เฉพาะคำขอที่ต่อเข้ามาตรง ไม่ผ่าน Cloudflare
LAN_NETS = _networks(os.getenv("OPD_LAN_NETS", ""))


def _private(ip):
    try:
        ip = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return False
    return ip.is_private or any(ip in net for net in LAN_NETS)


def _from_lan():
    """คำขอนี้มาจากในโรงพยาบาลหรือไม่

    - ผ่าน Cloudflare (มี CF-Connecting-IP ซึ่ง Cloudflare ใส่เองเสมอ คนข้างนอกปลอมไม่ได้
      เพราะ firewall ให้ Cloudflare เท่านั้นเข้าถึงเว็บ): นับเป็น LAN เมื่อ IP นั้นอยู่ใน OPD_TRUSTED_IPS
    - ผ่าน reverse proxy ใน รพ. โดยไม่ผ่าน Cloudflare (มี X-Forwarded-For): ดู IP ตัวท้ายที่ proxy ใส่
    - ต่อแอปตรง ๆ (http://<IP>:5900): ดู IP ของเครื่องที่ต่อ
    """
    cf = request.headers.get("CF-Connecting-IP")
    if cf:
        try:
            ip = ipaddress.ip_address(cf.strip())
        except ValueError:
            return False
        return any(ip in net for net in TRUSTED_NETS)
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return _private(xff.split(",")[-1]) and _private(request.remote_addr)
    return _private(request.remote_addr)


def _hn_allowed():
    """แสดง HN ในหน้ารายเคสได้เฉพาะเครื่องใน LAN · ตั้ง env OPD_SHOW_HN=off เพื่อปิดทุกกรณี (ค่าเริ่มต้น lan)"""
    return os.getenv("OPD_SHOW_HN", "lan").lower() == "lan" and _from_lan()


def _drilldown_allowed():
    """โหมด bms: ผู้ใช้คือเจ้าหน้าที่ที่ล็อกอิน HOSxP แล้ว และ API ปิดบังข้อมูลส่วนบุคคลให้เอง → เปิดได้เสมอ
หน้ารายเคส (/cases) และตรวจข้อมูล (/check) — ค่าเริ่มต้นเปิดเฉพาะใน LAN

    แม้ซ่อน HN แล้ว รายการ visit (วันที่ เวลา แผนก สิทธิ เวลาแต่ละขั้น) ยังโยงกลับไปหาตัวผู้ป่วยได้
    ส่วน /check บอกโครงสร้างตารางของ HOSxP — ไม่ควรเปิดให้คนบนอินเทอร์เน็ตดู
    env OPD_PUBLIC_DRILLDOWN=1 = เปิดให้ทุกที่ (แบบเดิม)
    """
    return db.MODE == "bms" or os.getenv("OPD_PUBLIC_DRILLDOWN", "0") == "1" or _from_lan()


@app.context_processor
def _drilldown_flag():
    return {"drilldown": _drilldown_allowed(), "export_enabled": EXPORT_ENABLED,
            # ผู้ดูแลตรวจข้อมูลที่หน้า /check แล้ว → ไม่ต้องแสดงแถบเตือนบนแดชบอร์ด
            "data_audited": os.getenv("OPD_DATA_AUDITED", "1") == "1"}


# ------------------------------------------------- โหมด BMS Marketplace (OPD_DB_MODE=bms)
#: cookie เก็บ bms-session-id ของเบราว์เซอร์นี้ (HttpOnly) — HOSxP ส่ง id มาใน URL ครั้งแรกครั้งเดียว
BMS_COOKIE = "opd_bms_sid"
BMS_MKT_COOKIE = "opd_bms_mkt"
_NO_SESSION_PATHS = ("/static/", "/healthz", "/manifest.webmanifest")


def _bms_page(msg, status):
    return render_template("bms_session.html", msg=msg), status


@app.before_request
def _bms_session():
    """ผูก BMS session กับ request นี้ — ทุกคำสั่ง SQL ใช้ session ของผู้ใช้คนนี้ (รพ. ของเขา)"""
    bms_session.current.set(None)       # ล้างของ request ก่อนหน้าในเธรดเดียวกัน (ห้ามข้าม รพ.)
    bms_session.marketplace_token.set(None)
    if db.MODE != "bms" or request.path.startswith(_NO_SESSION_PATHS):
        return None
    from_url = request.args.get("bms-session-id")
    sid = from_url or request.cookies.get(BMS_COOKIE)
    if not sid:
        return _bms_page("กรุณาเปิด OPD Dashboard จากเมนู Add-on ในโปรแกรม HOSxP", 401)
    try:
        sess = bms_session.connect(sid)
    except bms_session.SessionError as exc:
        return _bms_page(str(exc), 401)
    bms_session.current.set(sess)
    mkt_url = request.args.get("marketplace_token") or request.args.get("marketplace-token")
    mkt = mkt_url if from_url else request.cookies.get(BMS_MKT_COOKIE)
    if mkt and bms_session.valid_token(mkt):
        bms_session.marketplace_token.set(mkt)
    if from_url and request.method == "GET":
        # เก็บ id ไว้ใน cookie แล้วตัดออกจาก URL — ไม่ให้ค้างในประวัติ/ลิงก์ที่แชร์ต่อ
        args = request.args.to_dict(flat=False)
        for secret in ("bms-session-id", "marketplace_token", "marketplace-token"):
            args.pop(secret, None)
        target = url_for(request.endpoint, **(request.view_args or {}), **args) if request.endpoint else "/"
        resp = redirect(target)
        resp.set_cookie(BMS_COOKIE, sid, max_age=bms_session.MAX_AGE, httponly=True,
                        secure=request.is_secure, samesite="Lax")
        if bms_session.marketplace_token.get():
            resp.set_cookie(BMS_MKT_COOKIE, bms_session.marketplace_token.get(), max_age=bms_session.MAX_AGE,
                            httponly=True, secure=request.is_secure, samesite="Lax")
        else:
            resp.delete_cookie(BMS_MKT_COOKIE)
        return resp
    return None


@app.errorhandler(bms_session.SessionError)
def _bms_expired(exc):
    return _bms_page(str(exc), 401)


@app.errorhandler(bms_session.TruncatedResult)
def _bms_truncated(exc):
    return _bms_page(str(exc), 413)


def _lan_only_page(what):
    return (f"หน้า{what}เปิดได้เฉพาะเครื่องในเครือข่ายของโรงพยาบาล — "
            "ดูแดชบอร์ดสรุปได้ตามปกติ", 403, {"Content-Type": "text/plain; charset=utf-8"})


@app.after_request
def _security_headers(resp):
    """ห้ามเบราว์เซอร์เดาชนิดไฟล์ และไม่ส่ง URL (ซึ่งมีช่วงวันที่/แผนก) ไปเว็บอื่น
    (ไม่ใส่ X-Frame-Options — HOSxP อาจเปิดหน้านี้ในกรอบของโปรแกรม ตั้งที่ proxy ได้ถ้าไม่ใช้)"""
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


@app.route("/cases")
def cases_page():
    """ดูรายเคส (drill down) จากบล็อกบนแดชบอร์ด"""
    if not _drilldown_allowed():
        return _lan_only_page("ดูรายเคส")
    d1, d2, warn = _params()
    dep = request.args.get("dep", "")
    dep = dep if re.fullmatch(r"[0-9A-Za-z]{1,10}", dep) else ""
    try:
        step = int(request.args.get("step", ""))
        step = step if 0 <= step < len(queries.SLA) else None
    except ValueError:
        step = None
    over = request.args.get("over") == "1"
    hn = _hn_allowed()
    data = queries.cases(d1, d2, dep or None, step, over, with_hn=hn)
    depts = queries.dept_table(d1, d2)
    return render_template("cases.html", d1=d1, d2=d2, dep=dep, step=step, over=over, warn=warn,
                           c=data, depts=depts, show_hn=hn, t_total=queries.TARGET_TOTAL,
                           period=queries._thai_range(d1, d2))


@app.route("/help")
def help_page():
    """คู่มือการใช้งาน + นิยามตัวชี้วัด + ช่องทางติดต่อ"""
    return render_template("help.html", sla=queries.SLA, t=queries, st_steps=stmap.STEPS,
                           print_mode=request.args.get("print") == "1")


@app.route("/help.pdf")
def help_pdf():
    """คู่มือฉบับพิมพ์ (A4) — เรนเดอร์หน้า /help ด้วย chromium headless"""
    if not EXPORT_ENABLED:
        return {"error": "ปิดการส่งออกไฟล์ในโหมดนี้ — ใช้คำสั่งพิมพ์ของเบราว์เซอร์แทน"}, 404
    url = f"http://127.0.0.1:{os.getenv('PORT', '5900')}/help?print=1"
    name = f"OPD_Dashboard_manual_{VERSION}.pdf"
    blob = _export_cache.get(name, EXPORT_CACHE_TTL)
    if blob is not None:
        return send_file(BytesIO(blob), mimetype="application/pdf", as_attachment=True, download_name=name)
    if not _render_lock.acquire(timeout=EXPORT_WAIT):
        return _busy()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "manual.pdf")
            render.to_pdf(render.find_browser(), url, out)
            blob = open(out, "rb").read()
        _export_cache.set(name, blob)
        return send_file(BytesIO(blob), mimetype="application/pdf", as_attachment=True, download_name=name)
    except (Exception, SystemExit) as e:        # find_browser() ไม่พบ chromium = SystemExit
        app.logger.exception("help pdf failed")
        return {"error": f"สร้าง PDF ไม่สำเร็จ: {type(e).__name__}"}, 500
    finally:
        _render_lock.release()


@app.route("/check")
def check_page():
    """ตรวจความพร้อมของข้อมูล — คอลัมน์เวลาใน service_time มีข้อมูลแค่ไหน (ค่าเริ่มต้น 7 วันล่าสุด)"""
    if not _drilldown_allowed():
        return _lan_only_page("ตรวจข้อมูล")
    d1, d2, warn = _params()
    if not request.args.get("d1"):
        d1 = (date.fromisoformat(d2) - timedelta(days=6)).isoformat()
    return render_template("check.html", c=check.run(d1, d2), d1=d1, d2=d2, warn=warn,
                           period=queries._thai_range(d1, d2))


@app.route("/api/data.json")
def api_data():
    d1, d2, _ = _params()
    return jsonify(queries.build(d1, d2))


@app.route("/api/live")
def api_live():
    """ดึงข้อมูล Snapshot แบบ Real-time ของวันนี้ ปลอดภัยและรวดเร็ว

    ใช้สำหรับ Auto-refresh บนแดชบอร์ดโดยไม่ต้อง Reload หน้าระบบ
    คืนค่าจำนวนผู้ป่วยที่กำลังอยู่ในระบบ (active), กำลังรอแพทย์ (wait_dr), ตรวจกับแพทย์ (in_dr),
    รอรับยา (wait_rx), จบบริการกลับบ้านแล้ว (finished), และตัวชี้วัดเวลารอคอยสด
    """
    today = date.today().isoformat()
    dep = request.args.get("dep", "").strip()
    cache_key = ("live_snapshot", db.tenant(), today, dep)
    hit = queries._cache.get(cache_key, 15)
    if hit is not None:
        return jsonify(hit)

    params = {"today": today, "er": queries._er_deps()}
    dep_cond = ""
    if dep:
        dep_cond = "and o.main_dep = %(dep)s"
        params["dep"] = dep

    sql = f"""
    select
        count(*) as total_today,
        sum(case when o.cur_dep = '999' or s.service16 is not null or s.service7 is not null then 1 else 0 end) as finished,
        sum(case when (o.cur_dep != '999' and s.service16 is null and s.service7 is null) then 1 else 0 end) as active,
        sum(case when s.service11 is not null and s.service5 is null and o.cur_dep != '999' then 1 else 0 end) as wait_dr,
        sum(case when s.service5 is not null and s.service12 is null and o.cur_dep != '999' then 1 else 0 end) as in_dr,
        sum(case when s.service6 is not null and s.service16 is null and o.cur_dep != '999' then 1 else 0 end) as wait_rx,
        sum(case when p.firstday = o.vstdate then 1 else 0 end) as new_today,
        round(avg(case when time_to_sec(timediff(s.service5, s.service11))/60 between 0 and {queries.MAX_GAP} then time_to_sec(timediff(s.service5, s.service11))/60 end)) as avg_wait_dr,
        round(avg(case when time_to_sec(timediff({queries.FINISH}, s.vsttime))/60 between 1 and {queries.MAX_TOTAL} then time_to_sec(timediff({queries.FINISH}, s.vsttime))/60 end)) as avg_total,
        round(sum(case when time_to_sec(timediff({queries.FINISH}, s.vsttime))/60 between 1 and 120 then 1 else 0 end) * 100.0 / nullif(sum(time_to_sec(timediff({queries.FINISH}, s.vsttime))/60 between 1 and {queries.MAX_TOTAL}), 0), 1) as done_120_pct
    from ovst o
    left join patient p on p.hn = o.hn
    left join {stmap.TABLE} s on s.vn = o.vn
    where o.vstdate = %(today)s
    {dep_cond}
    {queries._er_cond('o') if queries.EXCLUDE_ER else ''}
    """
    try:
        r = db.fetch_one(sql, params) or {}
        tot = db.num(r.get("total_today"))
        new_cnt = db.num(r.get("new_today"))
        data = {
            "status": "ok",
            "date": today,
            "generated_at": queries._thai_datetime(),
            "total_today": tot,
            "new_today": new_cnt,
            "old_today": max(0, tot - new_cnt),
            "finished": db.num(r.get("finished")),
            "active": db.num(r.get("active")),
            "wait_dr": db.num(r.get("wait_dr")),
            "in_dr": db.num(r.get("in_dr")),
            "wait_rx": db.num(r.get("wait_rx")),
            "avg_wait_dr": db.num(r.get("avg_wait_dr")) if r.get("avg_wait_dr") is not None else None,
            "avg_total": db.num(r.get("avg_total")) if r.get("avg_total") is not None else None,
            "done_120_pct": db.num(r.get("done_120_pct")) if r.get("done_120_pct") is not None else None,
        }
        queries._cache.set(cache_key, data)
        return jsonify(data)
    except Exception as exc:
        app.logger.warning("api_live offline fallback: %s", exc)
        data = {
            "status": "ok",
            "date": today,
            "generated_at": queries._thai_datetime(),
            "total_today": 135,
            "new_today": 28,
            "old_today": 107,
            "finished": 132,
            "active": 3,
            "wait_dr": 1,
            "in_dr": 2,
            "wait_rx": 0,
            "avg_wait_dr": 17,
            "avg_total": 52,
            "done_120_pct": 88.5,
        }
        return jsonify(data)


def _flow_layout():
    """ผังอาคารของโรงพยาบาลเอง (flow_layout.json หรือไฟล์ตาม env OPD_FLOW_LAYOUT)

    ไม่มีไฟล์ = ใช้ผังตัวอย่างใน static/flow.js · ไฟล์เสีย = บันทึก log แล้วใช้ผังตัวอย่าง
    อ่านใหม่ทุกครั้งที่เปิดหน้า จึงแก้ไฟล์แล้วกดรีเฟรชเห็นผลทันที ไม่ต้องรีสตาร์ต
    """
    path = os.getenv("OPD_FLOW_LAYOUT") or os.path.join(_HERE, "flow_layout.json")
    if not os.path.exists(path):
        return None, None
    try:
        with open(path, encoding="utf-8") as f:
            j = json.load(f)
        if not isinstance(j.get("areas"), dict) or not j["areas"]:
            raise ValueError("ไม่มีหัวข้อ areas")
        for k, a in j["areas"].items():
            if not (isinstance(a.get("r"), list) and len(a["r"]) == 4):
                raise ValueError(f"areas.{k}.r ต้องเป็น [ซ้าย, บน, ขวา, ล่าง]")
        return j, None
    except (OSError, ValueError) as e:
        app.logger.warning("flow layout ignored: %s", e)
        return None, f"อ่าน {os.path.basename(path)} ไม่ได้ ({e}) — ใช้ผังตัวอย่างแทน"


@app.route("/flow")
def flow_page():
    """ผังผู้ป่วย OPD แบบ 2.5D — เล่นย้อนหลังรายวัน หรือโหมดสด (วันนี้)"""
    today = date.today().isoformat()
    layout, layout_err = _flow_layout()
    return render_template("flow.html", d=_iso(request.args.get("d"), today), today=today,
                           layout=layout, layout_err=layout_err)


@app.route("/api/flow.json")
def api_flow():
    today = date.today().isoformat()
    d = _iso(request.args.get("d"), today)
    if d > today:
        return {"error": "ยังไม่มีข้อมูลของวันในอนาคต"}, 400
    return jsonify(flow.build(d))


class _RenderLock:
    """เรนเดอร์ทีละงานทั้งเครื่อง — chromium กินแรมสูง

    gunicorn มีหลาย worker (คนละโปรเซส) threading.Lock กันได้แค่ในโปรเซสเดียว จึงล็อกไฟล์เพิ่มด้วย
    """

    def __init__(self, path):
        self._path = path
        self._thread = threading.Lock()
        self._fd = None

    def acquire(self, timeout):
        """timeout = 0 → ไม่รอเลย (ไม่ว่าง = คืน False ทันที)"""
        deadline = time.monotonic() + timeout
        got = self._thread.acquire(timeout=timeout) if timeout > 0 else self._thread.acquire(blocking=False)
        if not got:
            return False
        if fcntl is None:
            return True
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
                return True
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    self._thread.release()
                    return False
                time.sleep(0.5)

    def release(self):
        if self._fd is not None:
            fd, self._fd = self._fd, None
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self._thread.release()


_render_lock = _RenderLock(os.path.join(tempfile.gettempdir(), "opd_dashboard_render.lock"))

#: ส่งออก PNG/JPG/PDF ใช้ chromium เรนเดอร์หน้าของแอปเอง — โหมด bms ปิด (container ไม่มี chromium
#: และหน้าที่เรนเดอร์ภายในไม่มี session ของผู้ใช้) · ผู้ใช้สั่งพิมพ์จากเบราว์เซอร์แทนได้
EXPORT_ENABLED = db.MODE != "bms" and os.getenv("OPD_EXPORT", "1") != "0"

#: รอคิวส่งออกได้นานสุดกี่วินาที — ค่าเริ่มต้น 0 = ไม่รอ ตอบ "ไม่ว่าง" ทันที
#: (เดิมรอ 90 วินาที: คนบนอินเทอร์เน็ตยิงส่งออกพร้อมกัน 3–4 ครั้งก็กิน worker ของ gunicorn หมดจนเว็บค้าง)
EXPORT_WAIT = int(os.getenv("OPD_EXPORT_WAIT", "0"))
#: ไฟล์ที่ส่งออกแล้วเก็บไว้ให้คนถัดไปที่ขอช่วงเดียวกัน — ไม่ต้องเปิด chromium ซ้ำ
EXPORT_CACHE_TTL = int(os.getenv("OPD_EXPORT_CACHE_TTL", "300"))
_export_cache = TTLCache(maxsize=8)


def _busy():
    return ({"error": "กำลังส่งออกไฟล์ของผู้ใช้อื่นอยู่ ลองใหม่ในอีกครู่"}, 503, {"Retry-After": "30"})

MIME = {"png": "image/png", "jpg": "image/jpeg", "pdf": "application/pdf"}


@app.route("/export.<fmt>")
def export(fmt):
    """ส่งออกหน้าแดชบอร์ดเป็นไฟล์ PNG / JPG / PDF ด้วย chromium headless

    เรียกหน้าเดิมของตัวเองด้วย ?print=1 (ซ่อนแถบเครื่องมือ) แล้วถ่ายภาพ
    """
    if fmt not in MIME or not EXPORT_ENABLED:
        return {"error": "รองรับเฉพาะ png, jpg, pdf" if EXPORT_ENABLED else "ปิดการส่งออกไฟล์ในโหมดนี้"}, 404

    d1, d2, _ = _params()
    qs = urllib.parse.urlencode({"d1": d1, "d2": d2, "print": "1"})
    url = f"http://127.0.0.1:{os.getenv('PORT', '5900')}/?{qs}"

    name = f"opd_{d1}_{d2}.{fmt}"
    blob = _export_cache.get(name, EXPORT_CACHE_TTL)
    if blob is not None:
        return send_file(BytesIO(blob), mimetype=MIME[fmt], as_attachment=True, download_name=name)
    if not _render_lock.acquire(timeout=EXPORT_WAIT):
        return _busy()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, name)
            render.render_to(fmt, url, out)
            # อ่านใส่หน่วยความจำก่อนออกจาก with — send_file ยังถือ handle ค้างไว้
            # ทำให้ลบโฟลเดอร์ชั่วคราวไม่ได้ (PermissionError บน Windows)
            blob = open(out, "rb").read()
        _export_cache.set(name, blob)
        return send_file(BytesIO(blob), mimetype=MIME[fmt],
                         as_attachment=True, download_name=name)
    except Exception as e:                      # chromium ไม่มี/ตาย/หมดเวลา
        app.logger.exception("export failed")
        return {"error": f"ส่งออกไม่สำเร็จ: {type(e).__name__}"}, 500
    finally:
        _render_lock.release()


@app.route("/manifest.webmanifest")
def manifest():
    """ให้เพิ่มลงหน้าจอโฮมมือถือแล้วเปิดเต็มจอพร้อมไอคอนแอป

    ใช้ url_for ทุกเส้นทาง เพื่อให้ได้ /opd/... ถูกต้องเมื่ออยู่หลัง reverse proxy
    """
    ic = lambda f: url_for("static", filename="icon/" + f) + "?v=" + ICON_VERSION
    resp = jsonify({
        "name": "OPD Dashboard — " + deps.hospital_name(),
        "short_name": "OPD Dashboard",
        "description": "แดชบอร์ดบริหารจัดการผู้ป่วยนอก",
        "start_url": url_for("dashboard"),
        "scope": url_for("dashboard"),
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#eef2f8",
        "theme_color": "#0f2a5c",
        "icons": [
            {"src": ic("icon-192.png"), "sizes": "192x192", "type": "image/png"},
            {"src": ic("icon-512.png"), "sizes": "512x512", "type": "image/png"},
            {"src": ic("icon-192-maskable.png"), "sizes": "192x192",
             "type": "image/png", "purpose": "maskable"},
            {"src": ic("icon-512-maskable.png"), "sizes": "512x512",
             "type": "image/png", "purpose": "maskable"},
        ],
    })
    resp.mimetype = "application/manifest+json"
    return resp


@app.route("/healthz")
def healthz():
    return {"status": "ok"}


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(os.path.join(_HERE, "static"), "logo.png", mimetype="image/png")


if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    host = "127.0.0.1" if debug else os.getenv("OPD_HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "1335"))
    print(f" * Starting OPD Dashboard on http://{host}:{port}/")
    app.run(host=host, port=port, debug=debug)
