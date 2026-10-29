"""รวม SQL ทุกบล็อกของ OPD Dashboard (อ้างอิงสคีมา HOSxP)

ทุกฟังก์ชันคืน dict/list ที่พร้อมส่งเข้า template ตรง ๆ
ตัวเลขทุกตัวมาจากฐานข้อมูลจริง ไม่มีค่าสมมติ

เวลาแต่ละขั้นตอนมาจากตาราง `service_time` (1 แถวต่อ vn, คอลัมน์ชนิด TIME ของวันเดียวกับ vstdate)
ลำดับด้านล่างยืนยันจากข้อมูลจริง 15 ก.ย. - 15 ต.ค. 2569 (~8,500 visit, ดู README)

    vsttime    ลงทะเบียน (ห้องบัตร/Kiosk)
    service3   ส่งตรวจ
    service4   เริ่มซักประวัติ/คัดกรอง      (NCD, OPD จุดซักประวัติ, ER, ARI)
    service11  คัดกรองเสร็จ → ส่งพบแพทย์   (หน่วยเดียวกับ service4)
    service5   เริ่มตรวจ                   (ห้องตรวจโรค 2-5, ทันตกรรม, ER)
    service12  ตรวจเสร็จ                   (ทันตกรรมบันทึกแค่ตัวนี้ ไม่มี service5)
    service6   ใบสั่งยาถึงห้องจ่ายยา
    service7   ชำระเงิน                    (ห้องเก็บเงิน)
    service16  จ่ายยาเสร็จ                  (ห้องจ่ายยา)
    service20  จบบริการที่หน่วยเฉพาะ        (ทันตกรรม, กายภาพ, ปฐมภูมิ)

`ovst_service_time` ใช้ไม่ได้ — หยุดบันทึกตั้งแต่ มี.ค. 2569
โรงพยาบาลที่บันทึกลงคอลัมน์อื่น กำหนดได้ด้วย env OPD_ST_MAP (ดู stmap.py และหน้า /check)
"""

import os
import sys
from datetime import date, datetime, timedelta

import deps
import stmap
from cache import TTLCache
from db import fetch, fetch_one, num
from db import tenant as db_tenant

# ---------------------------------------------------------------- เกณฑ์

#: ช่วงเวลาระหว่างสองขั้นที่ถือว่าเป็นค่าจริง (นาที) — เกินนี้คือกดเวลาผิด/ลืมกด ตัดทิ้ง
MAX_GAP = 360
#: เวลารวมทั้ง visit ที่ถือว่าเป็นค่าจริง (นาที)
MAX_TOTAL = 600

#: ขั้นตอนในตาราง SLA: (ชื่อ, คอลัมน์เริ่ม, คอลัมน์จบ, เป้าหมายนาที)
SLA = [
    ("ลงทะเบียน", "vsttime", "service3", 10),
    ("รอคัดกรอง", "service3", "service4", 15),
    ("คัดกรอง / ซักประวัติ", "service4", "service11", 15),
    ("รอพบแพทย์", "service11", "service5", 20),
    ("ตรวจ / หัตถการ", "service5", "service12", 15),
    ("ชำระเงิน", "service12", "service7", 10),
    ("รอรับยา", "service6", "service16", 20),
]

TARGET_TOTAL = int(os.getenv("OPD_TARGET_TOTAL", "60"))      # เวลารวมต่อราย (นาที)
TARGET_WAIT_DR = 20                                            # รอพบแพทย์ (นาที) = แถว SLA
TARGET_DONE_PCT = 80                                           # % จบบริการภายใน 2 ชม.
TARGET_NOSHOW = 10                                             # % ผิดนัด
TARGET_PEAK = int(os.getenv("OPD_TARGET_PEAK", "100"))         # ผู้ป่วยต่อชั่วโมงต่อวัน

#: ช่วงชั่วโมงที่แสดงในกราฟรายชั่วโมง (นอกช่วงนี้นับรวมเป็น "นอกเวลา")
HOUR_FROM, HOUR_TO = 6, 20

#: แผนกฉุกเฉิน (ER)
ER_DEP = os.getenv("OPD_ER_DEP")
EXCLUDE_ER = os.getenv("OPD_EXCLUDE_ER", "1") == "1"


def _er_deps():
    """รหัสแผนก ER — ส่งเป็นพารามิเตอร์ `not in %(er)s` (รายการว่างใส่ '' กัน SQL `in ()`)"""
    codes = [c.strip() for c in ER_DEP.split(",")] if ER_DEP else deps.codes_in("ER")
    return [c for c in codes if c] or [""]


def _er_cond(o_alias="o"):
    if not EXCLUDE_ER:
        return ""
    return (f" and not exists (select 1 from er_regist _er where _er.vn = {o_alias}.vn)"
            f" and ifnull({o_alias}.main_dep, '') not in %(er)s")


def _st_er_cond(s_alias="s"):
    if not EXCLUDE_ER:
        return ""
    return (f" and not exists (select 1 from er_regist _er where _er.vn = {s_alias}.vn)"
            f" and ifnull({s_alias}.service5_dep, '') not in %(er)s")

#: เวลาจบบริการ = ขั้นสุดท้ายที่มีบันทึก (ไม่มียา/ไม่ต้องจ่ายเงิน ก็จบที่ตรวจเสร็จ)
FINISH = ("greatest(" + ", ".join(
    f"ifnull(s.{c},'00:00:00')"
    for c in ("service12", "service6", "service7", "service16", "service20")) + ")")


def _mins(a, b):
    return f"time_to_sec(timediff(s.{b}, s.{a}))/60"


def _gap_avg(a, b):
    m = _mins(a, b)
    return f"avg(case when {m} between 0 and {MAX_GAP} then {m} end)"


def _gap_n(a, b):
    return f"sum({_mins(a, b)} between 0 and {MAX_GAP})"


def _pct(part, whole, digits=1):
    return round(part * 100.0 / whole, digits) if whole else 0.0


def _delta(cur, prev):
    """% เปลี่ยนแปลงเทียบช่วงก่อนหน้า"""
    if not prev:
        return None
    return round((cur - prev) * 100.0 / prev, 1)


def _prev_period(d1, d2):
    span = (date.fromisoformat(d2) - date.fromisoformat(d1)).days + 1
    p2 = date.fromisoformat(d1) - timedelta(days=1)
    p1 = p2 - timedelta(days=span - 1)
    return p1.isoformat(), p2.isoformat(), span


# ------------------------------------------------- เวลาแต่ละขั้น (service_time)

def service_times(d1, d2, dep=None):
    """เวลาเฉลี่ยทุกขั้นตอน + เวลารวมต่อรายในคำสั่งเดียว"""
    cols = []
    for i, (_, a, b, _) in enumerate(SLA):
        cols.append(f"{_gap_avg(a, b)} g{i}")
        cols.append(f"{_gap_n(a, b)} n{i}")
    params = {"d1": d1, "d2": d2, "er": _er_deps()}
    join_dep = ""
    dep_cond = ""
    if dep:
        join_dep = "join ovst _o on _o.vn = s.vn"
        dep_cond = "and _o.main_dep = %(dep)s"
        params["dep"] = dep
    r = fetch_one(f"""
        select {', '.join(cols)},
          avg(case when tt between 1 and {MAX_TOTAL} then tt end) total_avg,
          sum(tt between 1 and {MAX_TOTAL}) total_n,
          sum(tt between 1 and 120) done_120
        from (select s.*, time_to_sec(timediff({FINISH}, s.vsttime))/60 tt
              from {stmap.TABLE} s
              {join_dep}
              where s.vstdate between %(d1)s and %(d2)s and s.vsttime is not null
              {dep_cond}
              {_st_er_cond('s')}) s""", params)

    rows = []
    for i, (label, _, _, target) in enumerate(SLA):
        avg = r.get(f"g{i}")
        mins = round(num(avg)) if avg is not None else None
        rows.append({"label": label, "avg": mins, "target": target,
                     "n": num(r.get(f"n{i}")),
                     "ok": None if mins is None else mins <= target})
    total_n = num(r.get("total_n"))
    return {
        "sla": rows,
        "total_avg": round(num(r.get("total_avg"))) if total_n else None,
        "total_n": total_n,
        "done_120_pct": _pct(num(r.get("done_120")), total_n),
    }


# ---------------------------------------------------------------- 1. KPI

def visit_counts(d1, d2, dep=None):
    params = {"d1": d1, "d2": d2, "er": _er_deps()}
    dep_cond = ""
    if dep:
        dep_cond = " and o.main_dep = %(dep)s"
        params["dep"] = dep
    r = fetch_one(f"""
        select count(*) total, sum(p.firstday = o.vstdate) new_pt,
          sum(exists(select 1 from er_regist _er where _er.vn = o.vn) or o.main_dep in %(er)s) er_pt
        from ovst o left join patient p on p.hn = o.hn
        where o.vstdate between %(d1)s and %(d2)s
        {dep_cond}
        {_er_cond('o') if EXCLUDE_ER else ''}""", params)
    total, new = num(r.get("total")), num(r.get("new_pt"))
    return {"total": total, "new": new, "old": total - new, "er_pt": num(r.get("er_pt"))}


def kpi_summary(d1, d2, st, dep=None):
    p1, p2, span = _prev_period(d1, d2)
    cur = visit_counts(d1, d2, dep=dep)
    prev = visit_counts(p1, p2, dep=dep)
    st_prev = service_times(p1, p2, dep=dep)
    wait_dr = next((r for r in st["sla"] if r["label"] == "รอพบแพทย์"), {"avg": None})
    return {
        "period": {"d1": d1, "d2": d2, "prev_d1": p1, "prev_d2": p2, "days": span},
        **cur,
        "per_day": round(cur["total"] / span) if span else 0,
        "total_delta": _delta(cur["total"], prev["total"]),
        "total_time": st["total_avg"],
        "total_time_delta": _delta(st["total_avg"] or 0, st_prev["total_avg"] or 0),
        "wait_dr": wait_dr["avg"],
        "done_120_pct": st["done_120_pct"],
        "done_120_prev": st_prev["done_120_pct"] if st_prev["total_n"] else None,
    }


# ----------------------------------------------- 2/9. ผู้ป่วยตามช่วงเวลา

def hourly(d1, d2, days, dep=None):
    params = {"d1": d1, "d2": d2, "appt": APPT_CODES, "er": _er_deps()}
    dep_cond = ""
    if dep:
        dep_cond = " and o.main_dep = %(dep)s"
        params["dep"] = dep
    rows = fetch(f"""
        select hour(o.vsttime) h, count(*) c, sum(o.ovstist in %(appt)s) appt
        from ovst o where o.vstdate between %(d1)s and %(d2)s and o.vsttime is not null
        {dep_cond}
        {_er_cond('o')}
        group by h""", params)
    by_h = {num(r["h"]): r for r in rows}
    out = []
    for h in range(HOUR_FROM, HOUR_TO + 1):
        r = by_h.get(h, {})
        total, appt = num(r.get("c")), num(r.get("appt"))
        out.append({"h": h, "label": f"{h:02d}:00", "range": f"{h:02d}-{h + 1:02d}",
                    "total": total, "appt": appt, "walkin": total - appt})
    off = sum(num(r["c"]) for h, r in by_h.items() if h < HOUR_FROM or h > HOUR_TO)
    peak = max(out, key=lambda x: x["total"]) if out else None
    per_day = round(peak["total"] / days) if peak and days else 0
    return {"hours": out, "off_hours": off, "peak": peak, "peak_per_day": per_day}


# -------------------------------------------- 3/6. ผู้ป่วยตามแผนก (main_dep)

#: แผนกที่ไม่ควรเป็นแผนกส่งคิวแรกของ visit (ovst.main_dep) — เจอเมื่อห้องบัตรเลือกแผนกผิด
#: ผู้ป่วยกลุ่มนี้มักยังผ่านคัดกรอง/พบแพทย์ตามปกติ จึงแยกเป็นแถว "ส่งคิวไม่ตรง" ไม่ปนกับแผนกจริง
#: ovst.ovstist ที่หมายถึง "มาตามนัด" — HOSxP มาตรฐานคือ 02 (นัดมา) แต่บาง รพ. เพิ่มรหัสเอง
#: ตรวจได้ที่หน้า /check · env OPD_APPT_CODES=02,08
APPT_CODES = [c.strip() for c in os.getenv("OPD_APPT_CODES", "02").split(",") if c.strip()] or ["02"]

MISROUTE_GROUPS = {"RX", "CASH"}
MISROUTE_CODES = {"999"}                 # 999 = กลับบ้าน
#: แจ้งในบล็อก 17 เมื่อส่งคิวไม่ตรงอย่างน้อยกี่ราย และกี่ % ของ visit
MISROUTE_MIN, MISROUTE_PCT = 10, 1.0


def _misroute(dep):
    return dep in MISROUTE_CODES or deps.group_of(dep) in MISROUTE_GROUPS


def departments(d1, d2):
    """(Top 10 แผนกส่งคิวแรก, สรุปกลุ่มส่งคิวไม่ตรง)"""
    rows = fetch(f"""
        select o.main_dep dep, k.department name, count(*) c,
          sum(s.service5 is not null or s.service12 is not null) seen,
          {_gap_avg('service11', 'service5')} wait_dr
        from ovst o
          left join kskdepartment k on k.depcode = o.main_dep
          left join {stmap.TABLE} s on s.vn = o.vn
        where o.vstdate between %(d1)s and %(d2)s
        group by o.main_dep, k.department order by c desc""", {"d1": d1, "d2": d2})
    # ค่าแทน NULL ใส่ฝั่ง Python — สตริงไทยใน SQL จะเพี้ยนเพราะ connection เป็น tis-620
    allrows = [{"dep": r["dep"], "name": r["name"] or "ไม่ระบุแผนก", "count": num(r["c"]),
                "seen": num(r["seen"]),
                "wait_dr": round(num(r["wait_dr"])) if r.get("wait_dr") is not None else None}
               for r in rows]
    mis_rows = [d for d in allrows if _misroute(d["dep"])]
    out = [d for d in allrows if not _misroute(d["dep"])][:10]
    mis = {"count": sum(d["count"] for d in mis_rows), "seen": sum(d["seen"] for d in mis_rows),
           "rows": mis_rows}
    top = max([d["count"] for d in out] + [mis["count"], 1])
    for d in out:
        d["bar"] = _pct(d["count"], top)
    mis["bar"] = _pct(mis["count"], top)
    return out, mis


# ------------------------------------------------------- 4. Patient Flow

FLOW_COLORS = ["#1565d8", "#1e8e5a", "#f5b70a", "#f47b16", "#7048c8", "#12b5d6", "#6b7280"]


def patient_flow(d1, d2, dep=None):
    params = {"d1": d1, "d2": d2, "er": _er_deps()}
    dep_cond = ""
    if dep:
        dep_cond = " and o.main_dep = %(dep)s"
        params["dep"] = dep
    r = fetch_one(f"""
        select count(*) total,
          sum(s.service4 is not null or s.service11 is not null) screened,
          sum(s.service11 is not null) sent_dr,
          sum(s.service5 is not null or s.service12 is not null) seen_dr,
          sum(s.service6 is not null or s.service16 is not null) rx,
          sum(s.service7 is not null) paid,
          sum(o.an is null and ifnull(o.ovstost, '') != '54' and s.vn is not null and {FINISH} > s.vsttime) home,
          sum(o.an is null and o.ovstost = '54') refer,
          sum(o.an is not null) admitted,
          sum((s.service5 is not null or s.service12 is not null) and s.service11 is null) direct_dr
        from ovst o left join {stmap.TABLE} s on s.vn = o.vn
        where o.vstdate between %(d1)s and %(d2)s
        {dep_cond}
        {_er_cond('o')}""", params)
    total = num(r.get("total"))
    # พบแพทย์โดยไม่มีเวลา "ส่งพบแพทย์" (เช่น ทันตกรรมไม่ผ่านจุดคัดกรอง) — แท่งพบแพทย์จึงยาวกว่าส่งพบแพทย์
    direct = []
    if num(r.get("direct_dr")):
        direct = [{"name": deps.name_of(x["dep"]) or "ไม่ระบุแผนก", "n": num(x["n"])} for x in fetch(f"""
            select coalesce(s.service5_dep, s.service12_dep) dep, count(*) n
            from ovst o join {stmap.TABLE} s on s.vn = o.vn
            where o.vstdate between %(d1)s and %(d2)s and s.service11 is null
              and (s.service5 is not null or s.service12 is not null)
              {dep_cond}
              {_er_cond('o')}
            group by 1 order by n desc limit 2""", params)]
    steps = [("ลงทะเบียน", total, "how_to_reg"),
             ("คัดกรอง", num(r.get("screened")), "vital_signs"),
             ("ส่งพบแพทย์", num(r.get("sent_dr")), "pending_actions"),
             ("พบแพทย์", num(r.get("seen_dr")), "medical_services"),
             ("ห้องยา", num(r.get("rx")), "medication"),
             ("ชำระเงิน", num(r.get("paid")), "payments"),
             ("กลับบ้าน", num(r.get("home")), "home")]
    return {"steps": [{"label": l, "value": v, "pct": round(_pct(v, total)), "icon": ic,
                       "color": FLOW_COLORS[i]} for i, (l, v, ic) in enumerate(steps)],
            "admitted": num(r.get("admitted")),
            "refer": num(r.get("refer")),
            "direct_dr": num(r.get("direct_dr")), "direct_top": direct}


# ---------------------------------------------------- 7. การนัดหมาย

def appointments(d1, d2, total, dep=None):
    """การนัดหมาย — ใช้นิยามเดียวกับบล็อก Alert ทุกตัวเลข"""
    today = date.today().isoformat()
    o_dep = " and o.main_dep = %(dep)s" if dep else ""
    a_dep = " and (a.depcode = %(dep)s or exists(select 1 from ovst _o where _o.vn = a.vn and _o.main_dep = %(dep)s))" if dep else ""
    o_came_dep = " and o.main_dep = %(dep)s" if dep else ""
    params_o = {"d1": d1, "d2": d2, "appt": APPT_CODES}
    params_a = {"d1": d1, "d2": d2, "today": today}
    if dep:
        params_o["dep"] = dep
        params_a["dep"] = dep

    appt_visits = num(fetch_one(f"""
        select count(*) c from ovst o
        where o.vstdate between %(d1)s and %(d2)s and o.ovstist in %(appt)s
        {o_dep}""", params_o).get("c"))
    r = fetch_one(f"""
        select sum(a.nextdate < %(today)s) booked,
          sum(a.nextdate < %(today)s and exists(select 1 from ovst o
              where o.hn = a.hn and o.vstdate = a.nextdate{o_came_dep})) came,
          sum(a.nextdate >= %(today)s) booked_today,
          sum(a.nextdate >= %(today)s and exists(select 1 from ovst o
              where o.hn = a.hn and o.vstdate = a.nextdate{o_came_dep})) came_today
        from oapp a where a.nextdate between %(d1)s and %(d2)s
        {a_dep}""", params_a)
    booked, came = num(r.get("booked")), num(r.get("came"))
    noshow = max(0, booked - came)
    items = [
        {"label": "มาตามนัด", "value": came, "color": "#1e8e5a"},
        {"label": "ผิดนัด (No-show)", "value": noshow, "color": "#dc3545"},
    ]
    for it in items:
        it["pct"] = _pct(it["value"], booked)
    return {"items": items, "booked": booked, "came": came, "noshow": noshow,
            "show_rate": _pct(came, booked) if booked else None,
            "noshow_rate": _pct(noshow, booked) if booked else None,
            "today_booked": num(r.get("booked_today")), "today_came": num(r.get("came_today")),
            "visits": total, "appt_visits": appt_visits, "walkin": total - appt_visits}


# ------------------------------------------- 8. โปรไฟล์ผู้ป่วย (แทนความพึงพอใจ)

PALETTE = ["#1565d8", "#1e8e5a", "#f5b70a", "#f47b16", "#7048c8"]


def patient_profile(d1, d2, total, dep=None):
    params = {"d1": d1, "d2": d2, "er": _er_deps()}
    dep_cond = ""
    if dep:
        dep_cond = " and o.main_dep = %(dep)s"
        params["dep"] = dep

    rights = fetch(f"""
        select p.name, count(*) c
        from ovst o left join pttype p on p.pttype = o.pttype
        where o.vstdate between %(d1)s and %(d2)s
        {dep_cond}
        {_er_cond('o')}
        group by p.name order by c desc limit 5""", params)
    items = [{"label": r["name"] or "ไม่ระบุสิทธิ", "value": num(r["c"]), "pct": _pct(num(r["c"]), total),
              "color": PALETTE[i % 5]} for i, r in enumerate(rights)]
    rest = max(0, total - sum(i["value"] for i in items))
    if rest > 0:
        items.append({"label": "อื่น ๆ", "value": rest, "pct": _pct(rest, total),
                      "color": "#94a3b8"})

    dx = fetch(f"""
        select d.icd10, ifnull(nullif(c.tname, ''), ifnull(c.name, d.icd10)) name, count(*) cnt
        from ovstdiag d join ovst o on o.vn = d.vn
             left join icd101 c on c.code = d.icd10
        where d.diagtype = '1' and o.vstdate between %(d1)s and %(d2)s
        {dep_cond}
        {_er_cond('o')}
        group by d.icd10, name order by cnt desc limit 5""", params)
    dx_items = [{"label": r["name"], "code": r["icd10"], "value": num(r["cnt"])} for r in dx]
    top = max((i["value"] for i in dx_items), default=1)
    for it in dx_items:
        it["bar"] = _pct(it["value"], top)

    ref_row = fetch_one(f"""
        select count(distinct o.vn) total,
               sum(case when o.ovstost = '54' then 1 else 0 end) by_ovstost,
               sum(case when ro.vn is not null then 1 else 0 end) by_referout
        from ovst o left join referout ro on ro.vn = o.vn
        where o.vstdate between %(d1)s and %(d2)s
          and (o.ovstost = '54' or ro.vn is not null)
          {dep_cond}
          {_er_cond('o')}""", params) or {}
    refer = {
        "total": num(ref_row.get("total")),
        "by_referout": num(ref_row.get("by_referout")),
        "by_ovstost": num(ref_row.get("by_ovstost")),
    }

    return {"rights": items, "dx": dx_items, "refer": refer}


# ------------------------------------------------------ 10. Alert & Action

def alerts(kpi, st, appt, hrs):
    """รายการเฝ้าระวังจากเกณฑ์ — เรียงเรื่องที่เกินเป้ามากที่สุดขึ้นก่อน"""
    out = []

    def add(issue, value, target, bad, ratio=1.0):
        out.append({"issue": issue, "value": value, "target": target,
                    "level": ("high" if ratio >= 1.5 else "mid") if bad else "ok",
                    "ratio": ratio if bad else 0})

    if kpi["total_time"] is not None:
        add("เวลารวมต่อราย", f"{kpi['total_time']} นาที", f"≤ {TARGET_TOTAL} นาที",
            kpi["total_time"] > TARGET_TOTAL, kpi["total_time"] / TARGET_TOTAL)
    for row in st["sla"]:
        if row["avg"] is not None and not row["ok"]:
            add(f"เวลา{row['label'].replace(' / ', '/')}", f"{row['avg']} นาที",
                f"≤ {row['target']} นาที", True, row["avg"] / row["target"])
    add("จบบริการภายใน 2 ชม.", f"{kpi['done_120_pct']}%", f"≥ {TARGET_DONE_PCT}%",
        kpi["done_120_pct"] < TARGET_DONE_PCT,
        TARGET_DONE_PCT / kpi["done_120_pct"] if kpi["done_120_pct"] else 2)
    if appt["noshow_rate"] is not None:
        add("อัตราผิดนัด (No-show)", f"{appt['noshow_rate']}%", f"≤ {TARGET_NOSHOW}%",
            appt["noshow_rate"] > TARGET_NOSHOW, appt["noshow_rate"] / TARGET_NOSHOW)
    if hrs["peak"]:
        add(f"ผู้ป่วยสูงสุดรายชั่วโมง ({hrs['peak']['range']} น.)",
            f"{hrs['peak_per_day']} ราย/วัน", f"≤ {TARGET_PEAK} ราย",
            hrs["peak_per_day"] > TARGET_PEAK, hrs["peak_per_day"] / TARGET_PEAK)

    out.sort(key=lambda a: -a["ratio"])
    bad = [a for a in out if a["level"] != "ok"]
    good = [a for a in out if a["level"] == "ok"]
    return (bad[:5] + good)[:6]


# ------------------------------------------- 11-15. วิเคราะห์คอขวด (Bottleneck)
#
# ใช้ติดตามผลการปรับปรุง 3 จุดที่พบจากข้อมูล 16 ก.ย. - 16 ต.ค. 2569
#   1) ผู้ป่วยมาถึง 06-08 น. แต่แพทย์เริ่มตรวจ ~08:05 และพีค 09-11 น. (นัด 08:00 เกือบทั้งหมด)
#   2) ผู้ป่วยที่ต้องเจาะ Lab ก่อนพบแพทย์ ค้างที่ขั้นคัดกรอง ~70 นาที (รอผล Lab)
#   3) รอรับยาช่วง 09-11 น. นานกว่าช่วงบ่ายเกือบเท่าตัว

#: ช่วงชั่วโมงของกราฟคอขวด (เวลาราชการ)
BN_FROM, BN_TO = 6, 16


#: เป้าหมาย Lab TAT ผู้ป่วยนอก (สั่ง → รายงานผล, นาที)
TARGET_LAB_TAT = int(os.getenv("OPD_TARGET_LAB_TAT", "60"))


def _bn_hours():
    return range(BN_FROM, BN_TO + 1)


def _hhmm(sec):
    if sec is None:
        return None
    sec = int(round(sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}"


def _level(v, target):
    """ระดับเดียวกับบล็อก Alert: เกินเป้า ≥ 1.5 เท่า = high, เกินเล็กน้อย = mid"""
    if v is None:
        return "na"
    if v <= target:
        return "ok"
    return "high" if v >= target * 1.5 else "mid"


def _avg_or_none(v):
    return round(num(v)) if v is not None else None


def doctor_flow(d1, d2, hrs, dep=None):
    """ผู้ป่วยมาถึง vs แพทย์เริ่มตรวจ รายชั่วโมง + เวลารอพบแพทย์ตามชั่วโมงที่มาถึง"""
    params = {"d1": d1, "d2": d2, "er": _er_deps()}
    join_dep = ""
    dep_cond = ""
    if dep:
        join_dep = "join ovst _o on _o.vn = s.vn"
        dep_cond = "and _o.main_dep = %(dep)s"
        params["dep"] = dep

    rows = fetch(f"""
        select 'e' k, hour(s.service5) h, count(*) n, null w
        from {stmap.TABLE} s
        {join_dep}
        where s.vstdate between %(d1)s and %(d2)s and s.service5 >= '06:00'
        {dep_cond}
        group by h
        union all
        select 'w' k, hour(s.vsttime) h, {_gap_n('service11', 'service5')} n,
          {_gap_avg('service11', 'service5')} w
        from {stmap.TABLE} s
        {join_dep}
        where s.vstdate between %(d1)s and %(d2)s and s.vsttime is not null
        {dep_cond}
        group by h""", params)
    exam = {num(r["h"]): num(r["n"]) for r in rows if r["k"] == "e"}
    wait = {num(r["h"]): r for r in rows if r["k"] == "w"}
    arr = {h["h"]: h["total"] for h in hrs["hours"]}

    hours = []
    for h in _bn_hours():
        w = wait.get(h, {})
        hours.append({"h": h, "label": f"{h:02d}", "range": f"{h:02d}-{h + 1:02d}",
                      "arrive": arr.get(h, 0), "exam": exam.get(h, 0),
                      "wait": _avg_or_none(w.get("w")), "wait_n": num(w.get("n"))})

    # แพทย์เริ่มตรวจคนแรกของวัน: เฉพาะวันจันทร์-ศุกร์ ไม่นับ ER (ตรวจทั้งคืน)
    first = fetch(f"""
        select s.vstdate d, time_to_sec(min(s.service5)) f
        from {stmap.TABLE} s
        {join_dep}
        where s.vstdate between %(d1)s and %(d2)s and s.service5 >= '06:00'
          and dayofweek(s.vstdate) between 2 and 6
          and ifnull(s.service5_dep, '') not in %(er)s
          {dep_cond}
        group by s.vstdate""", params)
    secs = sorted(num(r["f"]) for r in first if r.get("f") is not None)
    median = None
    if secs:
        m = len(secs) // 2
        median = secs[m] if len(secs) % 2 else (secs[m - 1] + secs[m]) / 2

    total_arr = sum(arr.values())
    early = sum(v for h, v in arr.items() if h < 9)      # มาถึง 06:00-08:59 น.
    waits = [x for x in hours if x["wait"] is not None and x["wait_n"] >= 5]
    worst = max(waits, key=lambda x: x["wait"]) if waits else None
    peak_exam = max(hours, key=lambda x: x["exam"]) if any(x["exam"] for x in hours) else None
    return {
        "hours": hours,
        "first_avg": _hhmm(sum(secs) / len(secs)) if secs else None,
        "first_median": _hhmm(median),
        "first_days": len(secs),
        "early_pct": _pct(early, total_arr),
        "early": early,
        "worst_wait": worst,
        "peak_exam": peak_exam,
    }


def appointment_times(d1, d2, dep=None):
    """การกระจายเวลานัด (oapp.nexttime) ของนัดที่ตกในช่วงวันที่เลือก"""
    params = {"d1": d1, "d2": d2}
    dep_cond = ""
    if dep:
        dep_cond = "and (a.depcode = %(dep)s or exists(select 1 from ovst _o where _o.vn = a.vn and _o.main_dep = %(dep)s))"
        params["dep"] = dep
    rows = fetch(f"""
        select time_format(a.nexttime, '%%H:%%i') t, count(*) c
        from oapp a where a.nextdate between %(d1)s and %(d2)s
        {dep_cond}
        group by t order by c desc""", params)
    total = sum(num(r["c"]) for r in rows)
    slots, other, unset = [], 0, 0
    for r in rows:
        t, c = r["t"], num(r["c"])
        if not t or t == "00:00":
            unset += c          # ไม่ได้ระบุเวลานัด
        elif len(slots) < 5:
            slots.append({"label": f"{t} น.", "value": c})
        else:
            other += c
    if other:
        slots.append({"label": "เวลาอื่น ๆ", "value": other, "muted": True})
    if unset:
        slots.append({"label": "ไม่ระบุเวลา", "value": unset, "muted": True})
    top = max((s["value"] for s in slots), default=1)
    for s in slots:
        s["pct"] = _pct(s["value"], total)
        s["bar"] = _pct(s["value"], top)
    main = slots[0] if slots and not slots[0].get("muted") else None
    return {"slots": slots, "total": total, "main": main}


def lab_tat(d1, d2, dep=None):
    """Lab TAT ผู้ป่วยนอก (สั่ง → รับสิ่งส่งตรวจ → รายงานผล) ตามชั่วโมงที่สั่ง

    เฉพาะ visit ที่ไม่ admit และสั่ง/รับ/รายงานผลภายในวันเดียวกับวันมารับบริการ
    ตัดทิ้งทั้งใบถ้าช่วงใดช่วงหนึ่งติดลบ/เกิน 6 ชม. — สองช่วงจึงรวมกันได้เท่ากับ TAT พอดี
    """
    params = {"d1": d1, "d2": d2}
    dep_cond = ""
    if dep:
        dep_cond = "and o.main_dep = %(dep)s"
        params["dep"] = dep
    rows = fetch(f"""
        select x.h, count(*) n, avg(x.a) o2r, avg(x.b) r2p
        from (select hour(l.order_time) h,
                     time_to_sec(timediff(l.receive_time, l.order_time))/60 a,
                     time_to_sec(timediff(l.report_time, l.receive_time))/60 b
              from lab_head l join ovst o on o.vn = l.vn
              where o.vstdate between %(d1)s and %(d2)s and o.an is null
                and l.order_date = o.vstdate and l.receive_date = o.vstdate
                and l.report_date = o.vstdate
                and hour(l.order_time) between {BN_FROM} and {BN_TO}
                {dep_cond}) x
        where x.a between 0 and {MAX_GAP} and x.b between 0 and {MAX_GAP}
        group by x.h""", params)
    by_h = {num(r["h"]): r for r in rows}
    hours, n_all, tot = [], 0, 0.0
    for h in _bn_hours():
        r = by_h.get(h, {})
        n = num(r.get("n"))
        o2r, r2p = num(r.get("o2r")), num(r.get("r2p"))
        o2p = round(o2r + r2p) if n else None
        n_all += n
        tot += (o2r + r2p) * n
        hours.append({"h": h, "label": f"{h:02d}:00", "n": n,
                      "o2r": round(o2r) if n else None, "r2p": round(r2p) if n else None,
                      "o2p": o2p, "level": _level(o2p, TARGET_LAB_TAT)})
    shown = [x for x in hours if x["n"] >= 20]
    worst = max(shown, key=lambda x: x["o2p"]) if shown else None
    return {"hours": hours, "avg": round(tot / n_all) if n_all else None, "n": n_all,
            "worst": worst, "target": TARGET_LAB_TAT}


def screening_lab(d1, d2, dep=None):
    """เวลาคัดกรอง (service4 → service11) แยก visit ที่มี Lab สั่งก่อนพบแพทย์ vs ไม่มี"""
    m = _mins("service4", "service11")
    params = {"d1": d1, "d2": d2}
    dep_cond = ""
    if dep:
        dep_cond = "and o.main_dep = %(dep)s"
        params["dep"] = dep
    rows = fetch(f"""
        select x.lab, count(*) n, avg(x.m) avg_m
        from (select exists(select 1 from lab_head l
                            where l.vn = s.vn and l.order_date = s.vstdate
                              and l.order_time < ifnull(s.service5, s.service11)) lab,
                     {m} m
              from {stmap.TABLE} s join ovst o on o.vn = s.vn
              where s.vstdate between %(d1)s and %(d2)s and o.an is null
                and s.service4 is not null and s.service11 is not null
                {dep_cond}) x
        where x.m between 0 and {MAX_GAP}
        group by x.lab""", params)
    by = {str(num(r["lab"])): r for r in rows}
    out = []
    for key, label, color in (("1", "มี Lab ก่อนพบแพทย์", "#dc3545"),
                              ("0", "ไม่มี Lab", "#1e8e5a")):
        r = by.get(key, {})
        out.append({"label": label, "n": num(r.get("n")),
                    "avg": _avg_or_none(r.get("avg_m")), "color": color})
    top = max((x["avg"] or 0 for x in out), default=0) or 1
    whole = sum(x["n"] for x in out)
    for x in out:
        x["bar"] = _pct(x["avg"] or 0, top)
        x["pct"] = _pct(x["n"], whole)
    lab, nolab = out
    ratio = round(lab["avg"] / nolab["avg"], 1) if lab["avg"] and nolab["avg"] else None
    return {"groups": out, "ratio": ratio}


def pharmacy_wait(d1, d2, dep=None):
    """เวลารอรับยา (service6 → service16) ตามชั่วโมงที่ใบสั่งยาถึงห้องยา"""
    params = {"d1": d1, "d2": d2}
    join_dep = ""
    dep_cond = ""
    if dep:
        join_dep = "join ovst _o on _o.vn = s.vn"
        dep_cond = "and _o.main_dep = %(dep)s"
        params["dep"] = dep
    rows = fetch(f"""
        select hour(s.service6) h, {_gap_n('service6', 'service16')} n,
          {_gap_avg('service6', 'service16')} w
        from {stmap.TABLE} s
        {join_dep}
        where s.vstdate between %(d1)s and %(d2)s and s.service6 is not null
        {dep_cond}
        group by h""", params)
    by_h = {num(r["h"]): r for r in rows}
    target = next(t for label, a, b, t in SLA if a == "service6")
    hours = []
    for h in _bn_hours():
        r = by_h.get(h, {})
        w = _avg_or_none(r.get("w"))
        hours.append({"h": h, "label": f"{h:02d}", "range": f"{h:02d}-{h + 1:02d}",
                      "n": num(r.get("n")), "wait": w, "level": _level(w, target)})
    shown = [x for x in hours if x["wait"] is not None and x["n"] >= 30]
    worst = max(shown, key=lambda x: x["wait"]) if shown else None
    best = min(shown, key=lambda x: x["wait"]) if shown else None
    return {"hours": hours, "target": target, "worst": worst, "best": best}


def bottleneck(d1, d2, hrs, dep=None):
    return {
        "dr": doctor_flow(d1, d2, hrs, dep=dep),
        "appt": appointment_times(d1, d2, dep=dep),
        "lab": lab_tat(d1, d2, dep=dep),
        "screen": screening_lab(d1, d2, dep=dep),
        "rx": pharmacy_wait(d1, d2, dep=dep),
    }


# ---------------------------------------- 16. ตารางรายแผนก (ค้นหา/เรียง/ดูรายเคส)

def dept_table(d1, d2):
    """ทุกแผนก (ovst.main_dep): จำนวน visit, เวลารอพบแพทย์, เวลารวม, % จบใน 2 ชม."""
    try:
        rows = fetch(f"""
            select o.main_dep dep, k.department name, count(*) n,
              sum(p.firstday = o.vstdate) new_pt,
              {_gap_avg('service11', 'service5')} wait_dr,
              avg(case when s.tt between 1 and {MAX_TOTAL} then s.tt end) total_avg,
              sum(s.tt between 1 and 120) done, sum(s.tt between 1 and {MAX_TOTAL}) tn
            from ovst o
              left join kskdepartment k on k.depcode = o.main_dep
              left join patient p on p.hn = o.hn
              left join (select s.*, time_to_sec(timediff({FINISH}, s.vsttime))/60 tt
                         from {stmap.TABLE} s where s.vstdate between %(d1)s and %(d2)s) s on s.vn = o.vn
            where o.vstdate between %(d1)s and %(d2)s
            group by o.main_dep, k.department order by n desc""", {"d1": d1, "d2": d2})
    except Exception:
        snap = _load_snapshot()
        if snap and snap.get("dept_table"):
            return snap["dept_table"]
        raise
    out = []
    for r in rows:
        tn = num(r.get("tn"))
        out.append({"dep": r["dep"] or "", "name": r["name"] or "ไม่ระบุแผนก", "n": num(r["n"]),
                    "new": num(r.get("new_pt")),
                    "wait_dr": _avg_or_none(r.get("wait_dr")),
                    "total": _avg_or_none(r.get("total_avg")),
                    "done_pct": _pct(num(r.get("done")), tn) if tn else None})
    return out


# ------------------------------------------------------ ดูรายเคส (drill down)

CASE_LIMIT = 1500


def cases(d1, d2, dep=None, step=None, over=False, with_hn=False):
    """รายการ visit พร้อมเวลาแต่ละขั้น — ใช้ตรวจสอบย้อนกลับว่าตัวเลขบนแดชบอร์ดมาจากเคสไหน

    dep  = กรองแผนก (ovst.main_dep) · step = ลำดับขั้นใน SLA (เรียงจากรอนานสุด)
    over = เฉพาะเคสที่ขั้นนั้นเกินเป้า · with_hn = แสดง HN (เฉพาะเครือข่ายภายใน ดู app._hn_allowed)
    """
    cols = []
    for i, (_, a, b, _) in enumerate(SLA):
        m = _mins(a, b)
        cols.append(f"case when {m} between 0 and {MAX_GAP} then round({m}) end g{i}")
    where = ["o.vstdate between %(d1)s and %(d2)s"]
    if dep:
        where.append("o.main_dep = %(dep)s")
    order = "o.vstdate, o.vsttime"
    if step is not None:
        where.append(f"{_mins(SLA[step][1], SLA[step][2])} between 0 and {MAX_GAP}")
        if over:
            where.append(f"{_mins(SLA[step][1], SLA[step][2])} > {SLA[step][3]}")
        order = f"g{step} desc"
    rows = fetch(f"""
        select o.hn, o.vstdate d, time_format(o.vsttime, '%%H:%%i') t, o.main_dep dep,
          k.department dname, pt.name right_name, o.ovstist in %(appt)s appt, o.an is not null adm,
          {', '.join(cols)},
          case when time_to_sec(timediff({FINISH}, s.vsttime))/60 between 1 and {MAX_TOTAL}
               then round(time_to_sec(timediff({FINISH}, s.vsttime))/60) end tt
        from ovst o
          left join {stmap.TABLE} s on s.vn = o.vn
          left join kskdepartment k on k.depcode = o.main_dep
          left join pttype pt on pt.pttype = o.pttype
        where {' and '.join(where)}
        order by {order} limit {CASE_LIMIT + 1}""", {"d1": d1, "d2": d2, "dep": dep, "appt": APPT_CODES})
    out = []
    for r in rows[:CASE_LIMIT]:
        item = {"d": str(r["d"]), "t": r["t"] or "", "dep": r["dep"] or "",
                "dname": r["dname"] or "ไม่ระบุแผนก", "right": r["right_name"] or "ไม่ระบุสิทธิ",
                "appt": num(r["appt"]) == 1, "adm": num(r["adm"]) == 1,
                "g": [None if r.get(f"g{i}") is None else int(num(r[f"g{i}"])) for i in range(len(SLA))],
                "tt": None if r.get("tt") is None else int(num(r["tt"]))}
        if with_hn:
            item["hn"] = r["hn"]
        out.append(item)
    return {"rows": out, "truncated": len(rows) > CASE_LIMIT, "limit": CASE_LIMIT,
            "steps": [{"label": l, "target": t} for l, _, _, t in SLA]}


# --------------------------------------- 17. แนวทางลดระยะเวลารอคอย (จากข้อมูลช่วงนี้)
#
# แต่ละข้อขึ้นเมื่อตัวชี้วัดของช่วงที่เลือกเกินเกณฑ์เท่านั้น พร้อมตัวเลขอ้างอิงจริง
# ระดับ: ratio = ค่าปัจจุบัน ÷ เป้าหมาย (≥ 1.5 = เร่งด่วน) — เรียงข้อที่เกินมากสุดขึ้นก่อน

def _rec(key, title, ratio, evidence, actions, track):
    return {"key": key, "title": title, "ratio": round(ratio, 2),
            "level": "high" if ratio >= 1.5 else "mid",
            "evidence": evidence, "actions": actions, "track": track}


def recommendations(d):
    out = []
    bn, kpi = d["bn"], d["kpi"]
    dr, ap, lab, scr, rx = bn["dr"], bn["appt"], bn["lab"], bn["screen"], bn["rx"]

    main = ap.get("main")
    if main and main["pct"] > 40 and ap["total"] >= 20:
        out.append(_rec(
            "slot", "กระจายเวลานัดหมายเป็นช่วง (Slot)", main["pct"] / 40,
            f"นัดเวลา {main['label']} {main['pct']}% ของนัดทั้งหมด · ผู้ป่วยมาถึงก่อน 09:00 น. {dr['early_pct']}%",
            ["นัดเป็นช่วงละ 30–60 นาที และจำกัดจำนวนต่อช่วงตามจำนวนแพทย์",
             "ผู้ป่วยโรคเรื้อรังที่อาการคงที่ นัดช่วงบ่าย หรือรับยาต่อที่ รพ.สต.",
             "พิมพ์เวลานัดในใบนัดชัดเจน และแจ้งว่าไม่ต้องมาก่อนเวลา"],
            "สัดส่วนนัดช่วงเวลาเดียว ≤ 40% (บล็อก 12)"))

    wd = kpi.get("wait_dr")
    if dr.get("first_avg") and dr["first_avg"] > "08:00" and dr["early_pct"] >= 30:
        ww = dr.get("worst_wait")
        ev = f"แพทย์เริ่มตรวจคนแรกเฉลี่ย {dr['first_avg']} น. · ผู้ป่วยมาถึง 06:00–08:59 น. {dr['early_pct']}%"
        if ww:
            ev += f" · มาถึงช่วง {ww['range']} น. รอพบแพทย์ {ww['wait']} นาที"
        out.append(_rec(
            "start", "เริ่มตรวจตรงเวลา และเตรียมผู้ป่วยให้พร้อมก่อนแพทย์เข้า",
            (wd or TARGET_WAIT_DR) / TARGET_WAIT_DR if wd else 1.2, ev,
            ["กำหนดเวลาเริ่มตรวจ 08:00 น. และติดตามเวลาแพทย์คนแรกรายวัน (บล็อก 11)",
             "เปิดคัดกรอง/เจาะเลือดตั้งแต่ 06:30 น. ให้ผู้ป่วยพร้อมพบแพทย์ทันทีที่เริ่มตรวจ",
             "จัดแพทย์เพิ่มในชั่วโมงที่ผู้ป่วยรอสะสมมากที่สุด"],
            f"แพทย์เริ่มตรวจคนแรก ≤ 08:00 น. · รอพบแพทย์ ≤ {TARGET_WAIT_DR} นาที"))

    lab_g, nolab_g = scr["groups"]
    if scr.get("ratio") and scr["ratio"] >= 2 and lab_g["n"] >= 20:
        out.append(_rec(
            "prelab", "ลดการรอผลแล็บก่อนพบแพทย์", min(scr["ratio"] / 2, 3),
            f"คัดกรองกลุ่มที่มี Lab ก่อนพบแพทย์ {lab_g['avg']} นาที เทียบกับไม่มี Lab {nolab_g['avg']} นาที "
            f"({scr['ratio']} เท่า) · {lab_g['n']:,} visit",
            ["เจาะเลือดล่วงหน้า 1–7 วันก่อนวันนัด หรือเจาะที่ รพ.สต. ให้ผลพร้อมในวันตรวจ",
             "ใช้ POCT (FBS, HbA1c ปลายนิ้ว) สำหรับผู้ป่วยเบาหวานที่ควบคุมได้",
             "แยกคิว: ผู้ป่วยที่ไม่ต้องรอผลแล็บพบแพทย์ก่อน ไม่ต้องรอตามลำดับมาถึง"],
            "เวลาคัดกรองกลุ่มมี Lab ≤ 2 เท่าของกลุ่มไม่มี Lab (บล็อก 14)"))

    lw = lab.get("worst")
    if lw and lw["o2p"] and lw["o2p"] > lab["target"]:
        out.append(_rec(
            "tat", "เร่งรอบส่งและรายงานผลแล็บช่วงพีค", lw["o2p"] / lab["target"],
            f"Lab TAT เฉลี่ย {lab['avg']} นาที · สั่งช่วง {lw['label']} น. ใช้ {lw['o2p']} นาที "
            f"(รอรับสิ่งส่งตรวจ {lw['o2r']} + ตรวจ/รายงาน {lw['r2p']}) เป้าหมาย ≤ {lab['target']} นาที",
            [f"ส่งสิ่งส่งตรวจเป็นรอบทุก 15 นาทีช่วง {lw['label']} น. ไม่รอสะสม",
             "เพิ่มเจ้าหน้าที่รับสิ่งส่งตรวจ/เครื่องตรวจช่วงพีค",
             "รายงานผลอัตโนมัติเข้า HOSxP ทันทีที่ตรวจเสร็จ"],
            f"Lab TAT ทุกชั่วโมง ≤ {lab['target']} นาที (บล็อก 13)"))

    rw, rb = rx.get("worst"), rx.get("best")
    if rw and rw["wait"] and rw["wait"] > rx["target"]:
        ev = f"รอรับยาช่วง {rw['range']} น. {rw['wait']} นาที"
        if rb:
            ev += f" เทียบ {rb['range']} น. {rb['wait']} นาที"
        out.append(_rec(
            "rx", "ลดการรอรับยาช่วงเช้า", rw["wait"] / rx["target"], ev + f" · เป้าหมาย ≤ {rx['target']} นาที",
            ["จัดยา refill ของผู้ป่วยนัดล่วงหน้าตั้งแต่วันก่อนนัด",
             f"เพิ่มเภสัชกร/ผู้ช่วยช่วง {rw['range']} น. หรือเปิดช่องจ่ายยาเพิ่ม",
             "ช่องทางด่วนสำหรับใบสั่งยาไม่เกิน 3 รายการ · ส่งยาทางไปรษณีย์/รับที่ รพ.สต."],
            f"รอรับยาทุกชั่วโมง ≤ {rx['target']} นาที (บล็อก 15)"))

    if wd and wd > TARGET_WAIT_DR:
        slow = [c for c in d["clinics"] if c["wait_dr"] is not None and c["wait_dr"] > TARGET_WAIT_DR
                and deps.group_of(c["dep"]) != "TELE"]
        slow.sort(key=lambda c: -c["wait_dr"])
        ev = f"รอพบแพทย์เฉลี่ย {wd} นาที (เป้าหมาย ≤ {TARGET_WAIT_DR})"
        if slow:
            ev += " · นานสุด: " + ", ".join(f"{c['name']} {c['wait_dr']} นาที" for c in slow[:3])
        out.append(_rec(
            "room", "จัดห้องตรวจ/แพทย์ให้สมดุลกับจำนวนผู้ป่วยรายชั่วโมง", wd / TARGET_WAIT_DR, ev,
            ["เทียบจำนวนผู้ป่วยมาถึงกับจำนวนที่แพทย์ตรวจได้รายชั่วโมง (บล็อก 11) แล้วจัดตารางแพทย์ใหม่",
             "เปิดห้องตรวจเพิ่ม/ย้ายแพทย์ไปแผนกที่รอนานในช่วงพีค",
             "จอเรียกคิวแสดงลำดับและเวลารอโดยประมาณ ลดการถามซ้ำที่จุดคัดกรอง"],
            f"รอพบแพทย์รายแผนก ≤ {TARGET_WAIT_DR} นาที (บล็อก 6, 16)"))

    ns = d["appt"].get("noshow_rate")
    if ns is not None and ns > TARGET_NOSHOW and d["appt"]["booked"] >= 20:
        out.append(_rec(
            "noshow", "ลดการผิดนัด", ns / TARGET_NOSHOW,
            f"ผิดนัด {ns}% ({d['appt']['noshow']:,} จาก {d['appt']['booked']:,} นัด) เป้าหมาย ≤ {TARGET_NOSHOW}%",
            ["แจ้งเตือนนัดทาง SMS/LINE หรือโทร 1–2 วันก่อนนัด",
             "ให้เลื่อนนัดเองได้ (โทร/LINE OA) แทนการไม่มาโดยไม่แจ้ง",
             "ทบทวนนัดที่ไม่จำเป็น เช่น นัดรับยาอย่างเดียว ให้รับที่ รพ.สต. แทน"],
            f"อัตราผิดนัด ≤ {TARGET_NOSHOW}% (บล็อก 7)"))

    mis = d.get("dept_mis") or {}
    mis_pct = _pct(mis.get("count", 0), kpi["total"]) if kpi["total"] else 0
    if mis.get("count", 0) >= MISROUTE_MIN and mis_pct >= MISROUTE_PCT:
        names = " · ".join(f"{x['name']} {x['count']:,}" for x in mis["rows"][:3])
        out.append(_rec(
            "misroute", "ตรวจการเลือกแผนกตอนส่งคิว", mis_pct / MISROUTE_PCT,
            f"เปิด visit ส่งคิวแรกไป {names} ราย ({mis_pct}% ของ visit) แต่ {mis['seen']:,} รายยังพบแพทย์"
            " — จำนวนและเวลารอรายแผนก (บล็อก 3, 6, 16) จึงคลาดเคลื่อน",
            ["ทบทวนกับห้องบัตรและจุดลงทะเบียน ER ว่าเลือกแผนกส่งคิวจากรายการอย่างไร",
             "ตั้งแผนกส่งต่อเริ่มต้นของแต่ละเครื่องลงทะเบียนให้ตรงกับจุดบริการ",
             "เปิด “ดูรายเคส” ของแผนกนั้น (บล็อก 16) เพื่อดูว่าเกิดจากจุดไหน ช่วงเวลาใด"],
            f"ส่งคิวไม่ตรง < {MISROUTE_PCT:g}% ของ visit (บล็อก 3)"))

    cov = _pct(d.get("time_n", 0), kpi["total"]) if kpi["total"] else 100
    if cov < 80:
        out.append(_rec(
            "data", "บันทึกเวลาแต่ละขั้นใน HOSxP ให้ครบ", 80 / max(cov, 1),
            f"มีเวลาครบทั้ง visit เพียง {cov}% — ตัวเลขเวลารอจะคลาดเคลื่อนถ้าไม่กดบันทึกเวลาทุกจุด",
            ["ทุกจุดบริการกดรับ/ส่งผู้ป่วยในโปรแกรมทันที ไม่บันทึกย้อนหลัง",
             "เปิดหน้า “ตรวจข้อมูล” (ลิงก์ท้ายหน้า) ดูว่าขั้นไหนไม่มีข้อมูล — ถ้าโรงพยาบาลกดเวลาลงคอลัมน์อื่น"
             " ให้ผู้ดูแลระบบตั้งค่า OPD_ST_MAP ตามที่หน้านั้นเสนอ",
             "ใช้หน้า “ดูรายเคส” ตรวจแผนกที่มีช่อง “–” มากและแจ้งหัวหน้างาน"],
            "visit ที่มีเวลาครบ ≥ 80%"))

    out.sort(key=lambda r: -r["ratio"])
    summary = {"total_time": kpi.get("total_time"), "done": kpi.get("done_120_pct"),
               "target_total": TARGET_TOTAL, "target_done": TARGET_DONE_PCT}
    return {"items": out, "summary": summary}


# ------------------------------------------------------------- ประกอบร่าง

#: แคชผลลัพธ์สั้น ๆ — หลายคนเปิดหน้าเดียวกันพร้อมกันจะไม่ยิง DB ซ้ำ
#: ตั้งอายุแคชด้วย env OPD_CACHE_TTL (วินาที, 0 = ปิด)
CACHE_TTL = int(os.getenv("OPD_CACHE_TTL", "120"))
_cache = TTLCache(maxsize=32)


def department_doctors(d1, d2, dep):
    """แพทย์ที่ออกตรวจในแผนกนี้ (Top 5): จำนวนตรวจ, เวลารอพบแพทย์เฉลี่ย, เวลาตรวจเฉลี่ยต่อเคส"""
    if not dep:
        return []
    rows = fetch(f"""
        select o.doctor, d.name, count(*) c,
          {_gap_avg('service11', 'service5')} wait_dr,
          {_gap_avg('service5', 'service12')} exam_m
        from ovst o
          left join doctor d on d.code = o.doctor
          left join {stmap.TABLE} s on s.vn = o.vn
        where o.vstdate between %(d1)s and %(d2)s
          and o.main_dep = %(dep)s
          and o.doctor is not null and o.doctor != ''
        group by o.doctor, d.name
        order by c desc limit 5""", {"d1": d1, "d2": d2, "dep": dep})
    out = []
    top = max((num(r["c"]) for r in rows), default=1)
    for r in rows:
        out.append({
            "code": r["doctor"] or "",
            "name": r["name"] or "ไม่ระบุชื่อแพทย์",
            "count": num(r["c"]),
            "wait_dr": _avg_or_none(r.get("wait_dr")),
            "exam_m": _avg_or_none(r.get("exam_m")),
            "bar": _pct(num(r["c"]), top)
        })
    return out


def department_info(dep):
    """ข้อมูลรายละเอียดของแผนก: รหัส, ชื่อ, กลุ่มงาน"""
    if not dep:
        return None
    r = fetch_one("""
        select depcode, department from kskdepartment where depcode = %(dep)s
    """, {"dep": dep})
    if not r:
        return {"dep": dep, "name": f"แผนก {dep}", "group": deps.group_of(dep)}
    return {
        "dep": r["depcode"],
        "name": r["department"] or f"แผนก {dep}",
        "group": deps.group_of(r["depcode"]),
        "group_name": deps.name_of(r["depcode"]) or ""
    }


def specialties(d1, d2):
    """ผู้ป่วยจำแนกตามสาขาวิชาชีพแพทย์ (ovst.spclty) ตามมาตรฐานกระทรวงสาธารณสุข"""
    rows = fetch(f"""
        select o.spclty code, ifnull(nullif(sp.name, ''), 'ไม่ระบุสาขา') name, count(*) c,
          sum(s.service5 is not null or s.service12 is not null) seen,
          {_gap_avg('service11', 'service5')} wait_dr
        from ovst o
          left join spclty sp on sp.spclty = o.spclty
          left join {stmap.TABLE} s on s.vn = o.vn
        where o.vstdate between %(d1)s and %(d2)s
        {_er_cond('o')}
        group by o.spclty, name order by c desc""", {"d1": d1, "d2": d2, "er": _er_deps()})
    allrows = [{"code": r["code"] or "-", "name": r["name"], "count": num(r["c"]),
                "seen": num(r["seen"]),
                "wait_dr": round(num(r["wait_dr"])) if r.get("wait_dr") is not None else None}
               for r in rows]
    top = max([d["count"] for d in allrows] + [1])
    for d in allrows:
        d["bar"] = _pct(d["count"], top)
    return allrows


def _load_snapshot(dep=None):
    snapshot_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_snapshot.json")
    if not os.path.exists(snapshot_path):
        return None
    try:
        import json
        with open(snapshot_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if dep:
            data = dict(data)
            data["dep"] = dep
            dep_meta = None
            try:
                dep_meta = department_info(dep)
            except Exception:
                pass
            data["department"] = dep_meta or {"dep": dep, "name": f"แผนก {dep}"}
        return data
    except Exception:
        return None


def build(d1=None, d2=None, dep=None):
    today = date.today().isoformat()
    d1 = d1 or today
    d2 = d2 or today
    if d1 > d2:
        d1, d2 = d2, d1

    dep_key = str(dep).strip() if dep else ""
    key = (db_tenant(), d1, d2, dep_key)
    if CACHE_TTL:
        hit = _cache.get(key, CACHE_TTL)
        if hit is not None:
            return hit
    try:
        return _build(key, d1, d2, dep=dep_key or None)
    except Exception:
        stale = _cache.get_stale(key)
        if stale and not isinstance(sys.exc_info()[1], bms_session.SessionError):
            return dict(stale[1], stale_since=stale[1]["generated_at"])
        snapshot = _load_snapshot(dep_key or None)
        if snapshot:
            return dict(snapshot, stale_since="ออฟไลน์ — ไม่สามารถเชื่อมต่อ HOSxP ได้ในขณะนี้")
        raise


def _build(key, d1, d2, dep=None):
    st = service_times(d1, d2, dep=dep)
    kpi = kpi_summary(d1, d2, st, dep=dep)
    days = kpi["period"]["days"]
    hrs = hourly(d1, d2, days, dep=dep)
    appt = appointments(d1, d2, kpi["total"], dep=dep)
    deps, mis = departments(d1, d2)

    dep_meta = department_info(dep) if dep else None
    dep_docs = department_doctors(d1, d2, dep) if dep else []

    data = {
        "generated_at": _thai_datetime(),
        "period_label": _thai_range(d1, d2),
        "d1": d1, "d2": d2, "days": days,
        "dep": dep,
        "department": dep_meta,
        "department_doctors": dep_docs,
        "kpi": kpi,
        "sla": st["sla"],
        "hourly": hrs,
        "depts": deps,
        "dept_mis": mis,
        "clinics": deps[:5],
        "flow": patient_flow(d1, d2, dep=dep),
        "appt": appt,
        "profile": patient_profile(d1, d2, kpi["total"], dep=dep),
        "alerts": alerts(kpi, st, appt, hrs),
        "bn": bottleneck(d1, d2, hrs, dep=dep),
        "dept_table": dept_table(d1, d2),
        "specialties": specialties(d1, d2),
        "specialties_top": specialties(d1, d2)[:5],
        "time_n": st["total_n"],
        "targets": {"total": TARGET_TOTAL, "wait_dr": TARGET_WAIT_DR,
                    "done": TARGET_DONE_PCT, "noshow": TARGET_NOSHOW},
    }
    data["recs"] = recommendations(data)
    if CACHE_TTL:
        _cache.set(key, data)
    return data


TH_MONTH = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
            "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]


def _thai_date(d):
    d = date.fromisoformat(d) if isinstance(d, str) else d
    return f"{d.day} {TH_MONTH[d.month - 1]} {d.year + 543}"


def _thai_range(d1, d2):
    a, b = date.fromisoformat(d1), date.fromisoformat(d2)
    if a == b:
        return f"วันที่ {_thai_date(a)}"
    if a.month == b.month and a.year == b.year:
        return f"วันที่ {a.day} - {b.day} {TH_MONTH[b.month - 1]} {b.year + 543}"
    return f"{_thai_date(a)} - {_thai_date(b)}"


def _thai_datetime():
    n = datetime.now()
    return f"{_thai_date(n.date())} เวลา {n:%H:%M} น."
