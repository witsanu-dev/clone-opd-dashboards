"""เชื่อมต่อ HOSxP ผ่าน BMS Session API — ใช้เมื่อรันเป็น Add-on บน BMS Marketplace (OPD_DB_MODE=bms)

ขั้นตอน (BMS-SESSION-FOR-DEV v2.0 ของ BMS)
  1. HOSxP เปิดแอปพร้อม ?bms-session-id=<id>
  2. แลก id เป็นข้อมูลการเชื่อมต่อ: GET https://hosxp.net/phapi/PasteJSON?Action=GET&code=<id>
     → result.user_info.bms_url (tunnel ไป HOSxP ของ รพ. นั้น), bms_session_code (Bearer token),
       hospital_code, bms_database_type — ไม่มีชื่อ รพ. (location คือที่ทำงานของผู้ใช้) ดูชื่อจากตาราง hospcode
  3. ทุกคำสั่ง SQL: POST {bms_url}/api/sql  Authorization: Bearer <bms_session_code>
     body {"sql": ..., "app": ...} → {"MessageCode": 200, "data": [...], "truncated": false, ...}

ข้อจำกัดของ /api/sql: อ่านอย่างเดียว (SELECT/WITH/SHOW…), ห้ามตาราง opduser/opdconfig/sys_var/user_var/user_jwt,
ไม่เกิน 20 ตารางต่อคำสั่ง, ผลลัพธ์ใหญ่เกินจะถูกตัด (truncated), ข้อมูลส่วนบุคคลอาจถูกแทนด้วย [MASKED]

แอปนี้รันบน cluster ของ BMS ใช้ได้หลายโรงพยาบาลพร้อมกัน — ทุกอย่างผูกกับ session ของ request ปัจจุบัน
(contextvar) ห้ามเก็บ token ลง log หรือ URL
"""

import os
import re
import threading
import time
from urllib.parse import urlsplit

try:
    import contextvars
except ImportError:
    class _ContextVar:
        def __init__(self, name, default=None):
            self._name = name
            self._default = default
            self._local = threading.local()

        def get(self, default=None):
            return getattr(self._local, "val", self._default if default is None else default)

        def set(self, val):
            self._local.val = val

    class _ContextVarsModule:
        ContextVar = _ContextVar

    contextvars = _ContextVarsModule()

try:
    from dataclasses import dataclass
except ImportError:
    def dataclass(cls=None, **kwargs):
        def _wrap(c):
            def __init__(self, *args, **kw):
                annotations = getattr(c, "__annotations__", {})
                for k, v in zip(annotations.keys(), args):
                    setattr(self, k, v)
                for k, v in kw.items():
                    setattr(self, k, v)
            c.__init__ = __init__
            return c
        return _wrap(cls) if cls is not None else _wrap

import requests

from cache import TTLCache

PASTE_URL = os.getenv("OPD_BMS_PASTE_URL", "https://hosxp.net/phapi/PasteJSON")
APP_ID = os.getenv("OPD_BMS_APP", "OPD.Dashboard")
#: ส่งคำสั่งไปได้เฉพาะ tunnel ของ HOSxP — กันการปลอมข้อมูล session ให้เซิร์ฟเวอร์นี้ยิงไปที่อื่น (SSRF)
ALLOWED_HOST_SUFFIXES = tuple(s.strip().lower() for s in
                              os.getenv("OPD_BMS_ALLOWED_HOSTS", ".hosxp.net").split(",") if s.strip())
QUERY_TIMEOUT = int(os.getenv("OPD_DB_TIMEOUT", "60"))
#: HOSxP (ทดสอบ 22 ต.ค. 69: MariaDB 10.6, รพ.วังน้ำเขียว) ตัดผลลัพธ์ที่ ~1 MB แบบเงียบ ๆ — ไม่ส่ง truncated
#: (ขอ 5,000 แถว ได้ 4,993 แถว response 1,053,672 byte) → response ใหญ่เกินค่านี้ถือว่าถูกตัด
TRUNCATE_GUARD_BYTES = int(os.getenv("OPD_BMS_TRUNCATE_GUARD", "1000000"))
#: อายุ session ที่ใช้จริง — เอกสารบอก 3–10 ชม. ยึดค่าที่สั้นกว่า
MAX_AGE = 3 * 3600

_ID_RX = re.compile(r"[A-Za-z0-9-]{8,100}")


class SessionError(Exception):
    """ใช้ session นี้ไม่ได้ (ไม่มี/หมดอายุ/ไม่รองรับ) — ให้ผู้ใช้เปิดแอปใหม่จาก HOSxP"""


class TruncatedResult(RuntimeError):
    """/api/sql ตัดผลลัพธ์เพราะใหญ่เกิน — ห้ามเอาไปคำนวณยอดต่อ"""


@dataclass(frozen=True)
class Session:
    id: str
    url: str
    token: str
    hospital_code: str
    db_type: str
    expires: float

    def __repr__(self):                 # กัน token หลุดลง log/traceback
        return f"Session(hospital={self.hospital_code!r}, url={self.url!r})"


_sessions = TTLCache(maxsize=512)
current = contextvars.ContextVar("bms_session", default=None)
#: marketplace token ของ Add-on (HOSxP ต่อท้าย launch URL เป็น marketplace_token) — ส่งต่อไปกับ /api/sql
#: ให้ HOSxP ตัดสินสิทธิ์รายตาราง (grantedTables) · API อ่านชื่อ "marketplace-token" (ขีดกลาง)
marketplace_token = contextvars.ContextVar("bms_marketplace_token", default=None)
#: ส่ง token ใน query string ด้วย (ปกติส่งแค่ใน body — query string เสี่ยงค้างใน log ของ tunnel)
TOKEN_IN_QUERY = os.getenv("OPD_BMS_TOKEN_IN_QUERY", "0") == "1"
_TOKEN_RX = re.compile(r"[A-Za-z0-9_.:-]{4,200}")
_local = threading.local()


def _http():
    s = getattr(_local, "http", None)
    if s is None:
        s = _local.http = requests.Session()
    return s


def valid_id(session_id):
    return bool(session_id) and bool(_ID_RX.fullmatch(session_id))


def valid_token(token):
    return bool(token) and bool(_TOKEN_RX.fullmatch(token))


def _allowed_url(url):
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host.endswith(s) for s in ALLOWED_HOST_SUFFIXES)


def connect(session_id):
    """แลก bms-session-id เป็น Session (แคชไว้จนใกล้หมดอายุ)"""
    if not valid_id(session_id):
        raise SessionError("รูปแบบ bms-session-id ไม่ถูกต้อง")
    hit = _sessions.get(session_id, MAX_AGE)
    if hit is not None and hit.expires > time.time():
        return hit
    try:
        r = _http().get(PASTE_URL, params={"Action": "GET", "code": session_id}, timeout=30)
        payload = r.json()
    except (requests.RequestException, ValueError) as exc:
        raise SessionError(f"ติดต่อ hosxp.net ไม่ได้ ({type(exc).__name__})") from None
    code = payload.get("MessageCode")
    if code != 200:
        # 401 = hosxp.net ไม่ยอมรับคำขอ (token_missing/token_expired/…) — Message เป็นรหัสเหตุผล ไม่ใช่ความลับ
        reason = f" ({str(payload.get('Message'))[:40]})" if code == 401 else ""
        raise SessionError(f"session หมดอายุหรือไม่มีอยู่{reason} — เปิดแอปใหม่จาก HOSxP")
    result = payload.get("result") or {}
    info = result.get("user_info") or {}
    url = (info.get("bms_url") or "").rstrip("/")
    token = info.get("bms_session_code") or result.get("key_value") or ""
    if not token or not _allowed_url(url):
        raise SessionError("ข้อมูล session ไม่ถูกต้อง")
    db_type = (info.get("bms_database_type") or "").lower()
    if db_type and not any(k in db_type for k in ("mysql", "maria")):
        raise SessionError(f"ยังไม่รองรับฐานข้อมูล {info.get('bms_database_type')} (รองรับ MySQL/MariaDB)")
    age = min(int(result.get("expired_second") or MAX_AGE), MAX_AGE)
    sess = Session(id=session_id, url=url, token=token,
                   hospital_code=str(info.get("hospital_code") or ""),
                   db_type=db_type, expires=time.time() + age)
    _sessions.set(session_id, sess)
    return sess


def forget(session_id):
    with _sessions._lock:
        _sessions._data.pop(session_id, None)


def run_sql(sql):
    """รันคำสั่งอ่านอย่างเดียวด้วย session ของ request ปัจจุบัน → list[dict]"""
    sess = current.get()
    if sess is None:
        raise SessionError("ไม่มี BMS session — เปิดแอปจาก HOSxP")
    body = {"sql": " ".join(sql.split()), "app": APP_ID}
    params = None
    mkt = marketplace_token.get()
    if mkt:
        body["marketplace-token"] = mkt
        if TOKEN_IN_QUERY:
            params = {"marketplace-token": mkt}
    try:
        r = _http().post(f"{sess.url}/api/sql", params=params,
                         headers={"Authorization": f"Bearer {sess.token}"},
                         json=body, timeout=QUERY_TIMEOUT + 5)
    except requests.RequestException as exc:
        raise RuntimeError(f"ติดต่อ HOSxP ของโรงพยาบาลไม่ได้ ({type(exc).__name__})") from None
    if r.status_code in (401, 501):
        forget(sess.id)
        raise SessionError("session หมดอายุ — เปิดแอปใหม่จาก HOSxP")
    if r.status_code == 413:
        raise TruncatedResult("ข้อมูลแถวเดียวใหญ่เกินขนาดที่ API ส่งได้")
    try:
        payload = r.json()
    except ValueError:
        raise RuntimeError(f"HOSxP API ตอบกลับไม่ใช่ JSON (HTTP {r.status_code})") from None
    if payload.get("MessageCode") != 200:
        raise RuntimeError(f"HOSxP API {payload.get('MessageCode')}: {str(payload.get('Message'))[:300]}")
    if payload.get("truncated") or len(r.content) >= TRUNCATE_GUARD_BYTES:
        limit = payload.get("result_limit_bytes")
        size = f" (เพดาน {int(limit) // 1024:,} KB)" if str(limit or "").isdigit() and int(limit) else ""
        raise TruncatedResult(f"ข้อมูลมากเกินกว่าที่ API ส่งได้ในครั้งเดียว{size} — ลองเลือกช่วงวันที่สั้นลง")
    return payload.get("data") or []
