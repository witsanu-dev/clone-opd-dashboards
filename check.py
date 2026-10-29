"""ตรวจความพร้อมของข้อมูล HOSxP ก่อนใช้แดชบอร์ด (หน้า /check)

แต่ละโรงพยาบาลกดเวลาลงคอลัมน์ของ `service_time` ไม่เหมือนกัน หน้านี้บอกว่า
  - คอลัมน์ service1 … service20 มีข้อมูลกี่ % ของ visit และถูกกดจากจุดบริการไหน
  - ขั้นบริการที่แดชบอร์ดใช้ (stmap.STEPS) อ่านจากคอลัมน์ไหน ครอบคลุมเท่าไร
  - ช่องไหนบนแดชบอร์ดจะขึ้น "–" เพราะไม่มีคู่เวลาให้คำนวณ
  - ข้อเสนอค่า OPD_ST_MAP เมื่อขั้นไหนแทบไม่มีข้อมูล แต่มีคอลัมน์อื่นที่น่าจะใช้แทนได้
  - Data Audit: แผนกแต่ละรหัสถูกจัดเป็นกลุ่มอะไร และรหัสที่แอปสมมติไว้ (นัด, กลับบ้าน, ผลวินิจฉัยหลัก …)
    ตรงกับข้อมูลของโรงพยาบาลนี้หรือไม่ — รหัสของแต่ละ รพ. ไม่เหมือนกัน ถ้าไม่ตรง ตัวเลขบนแดชบอร์ดจะเพี้ยน
แสดงเฉพาะจำนวนนับและชื่อแผนก ไม่มีข้อมูลรายคน
"""

import os

import deps
import queries
import stmap
from db import fetch, fetch_one, num

#: % ของ visit ที่ถือว่า "ใช้ได้" / "มีบางส่วน" (ไม่ใช่ทุก visit ผ่านทุกขั้น เช่น ไม่มียา ไม่ต้องจ่ายเงิน)
OK_PCT, PART_PCT = 50, 10
#: คอลัมน์ที่จะเสนอเป็นตัวแทน ต้องมีข้อมูลอย่างน้อยเท่านี้
SUGGEST_PCT = 30

#: ขั้นมาตรฐาน → กลุ่มแผนก (deps.classify) ที่ควรเป็นผู้กดเวลา ใช้หาคอลัมน์ทดแทน
#: ขั้นที่อยู่ในกลุ่มเดียวกันเรียงตามลำดับเวลา: คอลัมน์ที่กดก่อน = ขั้นแรก
PAIRS = [
    (("service4", "service11"), {"SCR", "NCD", "ARI"}),
    (("service5", "service12"), {"R1", "R2", "R3", "R4", "R5", "DENT", "SPEC", "TELE"}),
    (("service6", "service16"), {"RX"}),
    (("service7",), {"CASH"}),
]


def _dep_name(code):
    name = deps.name_of(code)
    return name if name and name != code else f"(ไม่ระบุแผนก{' ' + code if code else ''})"


def _status(pct):
    return "ok" if pct >= OK_PCT else "part" if pct >= PART_PCT else "none"


def _columns(d1, d2):
    """สถิติของคอลัมน์จริงทุกตัว: ความครอบคลุม เวลาเฉลี่ยหลังลงทะเบียน และแผนกที่กด"""
    cols = []
    for c in stmap.ALL:
        m = f"time_to_sec(timediff(s.{c}, s.vsttime))/60"
        cols.append(f"sum(s.{c} is not null) n_{c}")
        cols.append(f"avg(case when {m} between 0 and {queries.MAX_GAP} then {m} end) m_{c}")
    r = fetch_one(f"""
        select count(*) visits, sum(s.vn is not null) st_rows, {', '.join(cols)}
        from ovst o left join service_time s on s.vn = o.vn
        where o.vstdate between %(d1)s and %(d2)s""", {"d1": d1, "d2": d2}) or {}
    visits = num(r.get("visits"))

    # แบ่งเป็นชุดละ 10 คอลัมน์ — BMS Session API นับตารางทุกครั้งที่อ้างถึง และรับไม่เกิน 20 ต่อคำสั่ง
    by = {}
    for i in range(0, len(stmap.ALL), 10):
        union = " union all ".join(
            f"select '{c}' c, s.{c}_dep dep, count(*) n from service_time s "
            f"where s.vstdate between %(d1)s and %(d2)s and s.{c} is not null group by s.{c}_dep"
            for c in stmap.ALL[i:i + 10])
        for row in fetch(union, {"d1": d1, "d2": d2}):
            by.setdefault(row["c"], []).append((num(row["n"]), row["dep"] or ""))

    used = {stmap.phys(c): c for c in stmap.STD if stmap.phys(c)}
    out = []
    for c in stmap.ALL:
        n = num(r.get(f"n_{c}"))
        pct = queries._pct(n, visits)
        top = sorted(by.get(c, []), reverse=True)[:3]
        m = r.get(f"m_{c}")
        out.append({
            "col": c, "n": n, "pct": pct, "status": _status(pct),
            "after": round(num(m)) if m is not None else None,
            "deps": [{"code": d, "name": _dep_name(d), "group": deps.group_of(d),
                      "pct": queries._pct(k, n, 0)} for k, d in top],
            "used_as": used.get(c),
        })
    return visits, num(r.get("st_rows")), out


def _suggest(steps, cols):
    """ขั้นที่ข้อมูลแทบไม่มี → คอลัมน์ที่ยังไม่ถูกใช้ ซึ่งกดจากจุดบริการกลุ่มที่ควรเป็น"""
    used = {s["phys"] for s in steps if s["phys"]}
    by_std = {s["col"]: s for s in steps}
    out = {}
    for std_cols, groups in PAIRS:
        if all(by_std[c]["pct"] >= PART_PCT for c in std_cols):
            continue
        cand = [c for c in cols
                if c["col"] not in used and c["pct"] >= SUGGEST_PCT and c["deps"]
                and c["deps"][0]["group"] in groups]
        cand.sort(key=lambda c: c["after"] if c["after"] is not None else 999)
        for std, c in zip(std_cols, cand):
            if by_std[std]["pct"] < PART_PCT:
                out[std] = c["col"]
                used.add(c["col"])
    return out


def _other_sources(d1, d2, visits):
    """ตารางเวลาอื่นของ HOSxP — แสดงเพื่อให้รู้ว่ามีข้อมูลหรือไม่ (แดชบอร์ดยังใช้ service_time)"""
    checks = [
        ("opd_dep_queue", "คิวรายหน่วยบริการ (เวลาเข้าคิว / รับคิว / เสร็จ)",
         f"""select count(distinct vn) v, sum(accept_datetime is not null) acc, count(*) n
             from opd_dep_queue where queue_datetime >= %(d1)s and queue_datetime < %(d2)s + interval 1 day"""),
        ("ovst_service_time", "ระยะเวลาให้บริการรายขั้น (OPD-SCREEN, OPD-DOCTOR …)",
         f"""select count(distinct vn) v, count(*) n from ovst_service_time
             where service_date between %(d1)s and %(d2)s"""),
        ("lab_head", "เวลาสั่ง / รับ / รายงานผล Lab",
         f"select count(distinct vn) v, count(*) n from lab_head where order_date between %(d1)s and %(d2)s"),
        ("er_regist", "เวลาเข้าห้องฉุกเฉิน",
         f"""select count(distinct vn) v, count(*) n from er_regist
             where enter_er_time >= %(d1)s and enter_er_time < %(d2)s + interval 1 day"""),
        ("oapp", "นัดหมาย",
         f"select count(*) v, count(*) n from oapp where nextdate between %(d1)s and %(d2)s"),
    ]
    out = []
    for table, label, sql in checks:
        try:
            r = fetch_one(sql, {"d1": d1, "d2": d2}) or {}
            v = num(r.get("v"))
            row = {"table": table, "label": label, "v": v, "n": num(r.get("n")),
                   "pct": queries._pct(v, visits) if table != "oapp" else None}
            if "acc" in r:
                row["acc"] = queries._pct(num(r.get("acc")), num(r.get("n")))
            out.append(row)
        except Exception as e:                       # บางรุ่นของ HOSxP ไม่มีตารางนี้
            out.append({"table": table, "label": label, "error": type(e).__name__})
    return out


# ------------------------------------------------------------ Data Audit

#: แผนกที่ไม่เข้ากลุ่มใดแต่มี visit อย่างน้อยเท่านี้ (% ของ visit) → ให้ตรวจว่าควรอยู่กลุ่มไหน
DEP_WARN_PCT = 1.0
#: เกณฑ์ของรหัสอื่น ๆ (% ของ visit)
MISSING_WARN_PCT = 5.0       # firstday / สิทธิ ว่างเกินนี้ = ตัวเลขที่เกี่ยวข้องคลาดเคลื่อน
PDX_WARN_PCT = 80.0          # visit ที่มีวินิจฉัย แต่มีวินิจฉัยหลัก (diagtype 1) น้อยกว่านี้
HOME_WARN_PCT = 30.0         # visit ที่จบด้วยแผนก 999 (กลับบ้าน) น้อยกว่านี้

#: กลุ่มแผนก → ผลต่อแดชบอร์ด (แสดงในตาราง Data Audit)
GROUP_LABEL = {
    "ADMIN": "เวชระเบียน — ไม่ถือเป็นจุดที่ผู้ป่วยอยู่",
    "SCR": "ซักประวัติ / คัดกรอง", "NCD": "คลินิก NCD", "ARI": "คลินิก ARI", "LAB": "ห้องแล็บ",
    **{f"R{i}": f"ห้องตรวจ {i}" for i in range(1, 6)},
    "TELE": "Telemed — ผู้ป่วยไม่อยู่ใน รพ. (ไม่วาดในผังผู้ป่วย)",
    "ER": "ห้องฉุกเฉิน — แยกออกจากเวลารอ OPD",
    "DENT": "ทันตกรรม", "SPEC": "ปฐมภูมิ / คลินิกเฉพาะ", "MH": "สุขภาพจิต", "PT": "กายภาพบำบัด",
    "TTM": "แพทย์แผนไทย",
    "RX": "ห้องยา — ส่งคิวแรกมาที่นี่นับเป็น “ส่งคิวไม่ตรง”",
    "CASH": "การเงิน — ส่งคิวแรกมาที่นี่นับเป็น “ส่งคิวไม่ตรง”",
}


def _safe(fn, *args):
    """ตารางบางตัวไม่มีในบางรุ่น / BMS Session API ไม่อนุญาต → ข้ามข้อนั้น ไม่ให้ทั้งหน้าล้ม"""
    try:
        return fn(*args)
    except Exception as e:
        return {"error": type(e).__name__}


def _dep_audit(d1, d2, visits):
    """แผนกหลักของ visit (ovst.main_dep) แต่ละรหัสถูกจัดเป็นกลุ่มอะไร"""
    rows = fetch("""
        select o.main_dep dep, count(*) n from ovst o
        where o.vstdate between %(d1)s and %(d2)s group by o.main_dep""", {"d1": d1, "d2": d2})
    over = deps.overrides()
    out = []
    for r in sorted(rows, key=lambda r: -num(r["n"])):
        code, n = r["dep"] or "", num(r["n"])
        g = deps.group_of(code)
        pct = queries._pct(n, visits)
        home = code in queries.MISROUTE_CODES
        out.append({
            "code": code, "name": _dep_name(code), "n": n, "pct": pct, "group": g,
            "label": ("กลับบ้าน — ส่งคิวแรกมาที่นี่นับเป็น “ส่งคิวไม่ตรง”" if home and not g else
                      GROUP_LABEL.get(g, "ไม่เข้ากลุ่ม — นับเป็นคลินิก OPD ทั่วไป")),
            "override": code in over,
            "warn": g is None and not home and pct >= DEP_WARN_PCT,
        })
    return {"rows": out, "warn": [r for r in out if r["warn"]]}


def _code_audit(d1, d2, visits):
    """รหัสที่แอปสมมติไว้ เทียบกับข้อมูลจริงของโรงพยาบาล → [{label, assume, found, status, fix}]"""
    p = {"d1": d1, "d2": d2, "appt": queries.APPT_CODES}
    out = []

    # 1. มาตามนัด (ovst.ovstist)
    def appt():
        rows = fetch("""
            select o.ovstist code, i.name, count(*) n from ovst o
              left join ovstist i on i.ovstist = o.ovstist
            where o.vstdate between %(d1)s and %(d2)s group by o.ovstist, i.name""", p)
        used = [r for r in rows if (r["code"] or "") in queries.APPT_CODES]
        cand = [r for r in rows if (r["code"] or "") not in queries.APPT_CODES and "นัด" in (r["name"] or "")]
        n = sum(num(r["n"]) for r in used)
        names = ", ".join(f"{r['code']} {r['name'] or ''}".strip() for r in used) or "ไม่มี visit ด้วยรหัสนี้"
        st = "bad" if not n else "warn" if cand else "ok"
        fix = None
        if cand:
            fix = "OPD_APPT_CODES=" + ",".join(queries.APPT_CODES + [r["code"] for r in cand])
        elif not n:
            fix = "ตรวจตาราง ovstist ว่ารหัสไหนคือ “นัดมา” แล้วตั้ง OPD_APPT_CODES"
        return {"label": "มาตามนัด (ovst.ovstist)", "assume": ", ".join(queries.APPT_CODES),
                "found": f"{names} · {queries._pct(n, visits)}% ของ visit"
                         + (" · มีรหัสอื่นที่ชื่อมีคำว่า “นัด”: " +
                            ", ".join(f"{r['code']} {r['name']}" for r in cand) if cand else ""),
                "status": st, "fix": fix,
                "impact": "บล็อก 2/9 (นัด vs Walk-in), บล็อก 7, หน้ารายเคส"}
    out.append(_safe(appt))

    # 2. ข้อมูลผู้ป่วย / สิทธิ / กลับบ้าน ในคำสั่งเดียว
    def basics():
        r = fetch_one("""
            select count(*) n, sum(o.cur_dep = '999') home,
              sum(p.hn is not null and p.firstday is null) nofirst,
              sum(o.pttype is null or o.pttype = '') nopt
            from ovst o left join patient p on p.hn = o.hn
            where o.vstdate between %(d1)s and %(d2)s""", p) or {}
        n = num(r.get("n"))
        home_pct, nofirst, nopt = (queries._pct(num(r.get(k)), n) for k in ("home", "nofirst", "nopt"))
        home_name = deps.name_of("999")
        return [
            {"label": "กลับบ้าน (แผนกปัจจุบัน 999)", "assume": "999",
             "found": f"999 = {home_name if home_name != '999' else 'ไม่มีใน kskdepartment'} · "
                      f"{home_pct}% ของ visit จบด้วย 999",
             "status": "bad" if home_name == "999" else "warn" if home_pct < HOME_WARN_PCT else "ok",
             "fix": None if home_pct >= HOME_WARN_PCT else "ผังผู้ป่วยใช้เวลาส่งกลับบ้านจากรหัสนี้ — ตรวจว่า รพ. ส่งกลับบ้านด้วยรหัสอะไร",
             "impact": "ผังผู้ป่วย (เวลาออก), บล็อก 3 ส่งคิวไม่ตรง"},
            {"label": "ผู้ป่วยใหม่ (patient.firstday)", "assume": "firstday = วันที่มา",
             "found": f"ไม่มี firstday {nofirst}% ของ visit",
             "status": "warn" if nofirst > MISSING_WARN_PCT else "ok", "fix": None,
             "impact": "จำนวนผู้ป่วยใหม่ (บล็อก 1, ตารางรายแผนก)"},
            {"label": "สิทธิการรักษา (ovst.pttype)", "assume": "ทุก visit มีสิทธิ",
             "found": f"ไม่มีสิทธิ {nopt}% ของ visit",
             "status": "warn" if nopt > MISSING_WARN_PCT else "ok", "fix": None,
             "impact": "บล็อก 8 สิทธิ"},
        ]
    b = _safe(basics)
    out.extend(b if isinstance(b, list) else [b])

    # 3. วินิจฉัยหลัก
    def pdx():
        r = fetch_one("""
            select count(distinct d.vn) dx, count(distinct case when d.diagtype = '1' then d.vn end) pdx
            from ovstdiag d join ovst o on o.vn = d.vn
            where o.vstdate between %(d1)s and %(d2)s""", p) or {}
        pct = queries._pct(num(r.get("pdx")), num(r.get("dx")))
        return {"label": "วินิจฉัยหลัก (ovstdiag.diagtype)", "assume": "1",
                "found": f"{pct}% ของ visit ที่มีวินิจฉัย มี diagtype 1",
                "status": "warn" if pct < PDX_WARN_PCT else "ok", "fix": None,
                "impact": "บล็อก 8 กลุ่มโรค Top 5"}
    out.append(_safe(pdx))

    # 4. แผนกฉุกเฉิน
    er = [c.strip() for c in queries.ER_DEP.split(",")] if queries.ER_DEP else deps.codes_in("ER")
    out.append({"label": "ห้องฉุกเฉิน (แยกออกจากเวลารอ OPD)",
                "assume": "OPD_ER_DEP" if queries.ER_DEP else "แผนกที่ชื่อมี ER / ฉุกเฉิน / อุบัติเหตุ",
                "found": ", ".join(f"{c} {deps.name_of(c)}" for c in er) or "ไม่พบแผนก",
                "status": "ok" if er else "bad",
                "fix": None if er else "ตั้ง OPD_DEP_MAP=<รหัส>=ER หรือ OPD_ER_DEP=<รหัส>",
                "impact": "ผู้ป่วย ER ถูกนับรวมเวลารอ OPD"})

    # 5. การส่งต่อผู้ป่วยภายนอก (Refer Out)
    def refer():
        r = fetch_one("""
            select count(distinct o.vn) total_v,
                   sum(case when o.ovstost = '54' then 1 else 0 end) ovstost_54,
                   sum(case when ro.vn is not null then 1 else 0 end) in_referout,
                   sum(case when o.ovstost = '54' and ro.vn is not null then 1 else 0 end) matched_refer
            from ovst o left join referout ro on ro.vn = o.vn
            where o.vstdate between %(d1)s and %(d2)s""", p) or {}
        ro_cnt = num(r.get("in_referout"))
        st_54 = num(r.get("ovstost_54"))
        matched = num(r.get("matched_refer"))
        if not ro_cnt and not st_54:
            return {"label": "การส่งต่อผู้ป่วย (Refer Out)", "assume": "ovstost = '54' ตรงกับ referout",
                    "found": "ไม่มีเคส Refer Out ในช่วงนี้", "status": "ok", "fix": None,
                    "impact": "สถิติการส่งต่อผู้ป่วยภายนอก"}
        diff = abs(ro_cnt - st_54)
        st = "ok" if diff == 0 else "warn"
        fix = "ลงบันทึกทั้งใบส่งต่อ referout และตั้ง ovstost=54 ให้ตรงกัน เพื่อความแม่นยำในการนับยอด" if diff > 0 else None
        return {"label": "การส่งต่อผู้ป่วย (Refer Out)", "assume": "ovstost = '54' ตรงกับ referout",
                "found": f"ตาราง referout มี {ro_cnt:,} ราย · ovst.ovstost=54 มี {st_54:,} ราย (ตรงกัน {matched:,} ราย)",
                "status": st, "fix": fix,
                "impact": "สถิติการส่งต่อผู้ป่วยภายนอก"}
    out.append(_safe(refer))

    # 6. คุณภาพข้อมูล Lab (TAT)
    def lab_tat():
        r = fetch_one("""
            select count(l.lab_order_number) orders,
                   sum(l.receive_date is not null) received,
                   sum(l.report_date is not null) reported
            from lab_head l
            where l.order_date between %(d1)s and %(d2)s""", p) or {}
        orders = num(r.get("orders"))
        if not orders:
            return {"label": "คุณภาพข้อมูล Lab (TAT)", "assume": "มีเวลาลงรับและรายงานผล",
                    "found": "ไม่มีการสั่งแล็บในช่วงนี้", "status": "ok", "fix": None,
                    "impact": "บล็อก 14 ระยะเวลารอผลแล็บ"}
        rec_pct = queries._pct(num(r.get("received")), orders)
        rep_pct = queries._pct(num(r.get("reported")), orders)
        st = "ok" if rec_pct >= 90 and rep_pct >= 85 else "warn" if rec_pct >= 70 else "bad"
        return {"label": "คุณภาพข้อมูล Lab (TAT)", "assume": "ลงรับ ≥90% · รายงานผล ≥85%",
                "found": f"สั่ง {orders:,} ใบ · ลงรับ {rec_pct}% · รายงานผลเสร็จ {rep_pct}%",
                "status": st, "fix": None if st == "ok" else "กำชับห้องแล็บลงเวลารับสิ่งส่งตรวจและรายงานผลในระบบ",
                "impact": "บล็อก 14 และเวลารอคอยของผู้ป่วยที่ตรวจแล็บ"}
    out.append(_safe(lab_tat))

    # 7. การบันทึกเวลาจ่ายยา (Pharmacy)
    def rx_workflow():
        r = fetch_one("""
            select sum(case when s.service6 is not null then 1 else 0 end) rx_ordered,
                   sum(case when s.service19 is not null then 1 else 0 end) rx_dispensed
            from ovst o left join service_time s on s.vn = o.vn
            where o.vstdate between %(d1)s and %(d2)s""", p) or {}
        rx_ord = num(r.get("rx_ordered"))
        rx_disp = num(r.get("rx_dispensed"))
        if not rx_ord:
            return {"label": "การบันทึกเวลาจ่ายยา (Pharmacy)", "assume": "มีเวลาจ่ายยา service19",
                    "found": "ไม่มีเคสสั่งยาในช่วงนี้", "status": "ok", "fix": None,
                    "impact": "บล็อก 15 เวลารอรับยา"}
        disp_pct = queries._pct(rx_disp, rx_ord)
        st = "ok" if disp_pct >= 85 else "warn" if disp_pct >= 60 else "bad"
        return {"label": "การบันทึกเวลาจ่ายยา (Pharmacy)", "assume": "กดจ่ายยา (service19) ≥85% ของเคสสั่งยา",
                "found": f"สั่งยา {rx_ord:,} ราย · บันทึกจ่ายยา {disp_pct}% ({rx_disp:,} ราย)",
                "status": st, "fix": None if st == "ok" else "ให้ห้องยากดบันทึกจ่ายยาใน HOSxP ทุกครั้งที่ส่งมอบยา",
                "impact": "บล็อก 15 และการจำลอง Flow 2.5D ในห้องยา"}
    out.append(_safe(rx_workflow))

    # 8. สาขาวิชาชีพแพทย์ (Doctor Specialty)
    def spclty():
        r = fetch_one("""
            select count(distinct o.vn) total_v,
                   sum(case when o.spclty is not null and o.spclty != '' and sp.name is not null then 1 else 0 end) has_sp
            from ovst o left join spclty sp on sp.spclty = o.spclty
            where o.vstdate between %(d1)s and %(d2)s""", p) or {}
        tot = num(r.get("total_v"))
        has_sp = num(r.get("has_sp"))
        sp_pct = queries._pct(has_sp, tot)
        st = "ok" if sp_pct >= 90 else "warn" if sp_pct >= 70 else "bad"
        return {"label": "สาขาวิชาชีพแพทย์ (ovst.spclty)", "assume": "ระบุสาขาวิชาชีพแพทย์ ≥90%",
                "found": f"ตรงตามมาตรฐานกระทรวง {sp_pct}% ({has_sp:,}/{tot:,} ราย)",
                "status": st, "fix": None if st == "ok" else "ตรวจสอบตาราง spclty และการเลือกแผนกตรวจใน HOSxP",
                "impact": "บล็อกสถิติผู้ป่วยแยกตามสาขาวิชาชีพแพทย์"}
    out.append(_safe(spclty))

    return out


def audit(d1, d2, visits):
    """Data Audit ทั้งหมด + สรุปจำนวนเรื่องที่ควรแก้ (ใช้ทั้งหน้า /check และ setup_check.py)"""
    dep = _safe(_dep_audit, d1, d2, visits)
    codes = _code_audit(d1, d2, visits)
    issues = []
    for w in (dep.get("warn") or []):
        issues.append(f"แผนก {w['code']} {w['name']} ({w['pct']}% ของ visit) ไม่เข้ากลุ่มใด")
    for c in codes:
        if c.get("status") in ("warn", "bad"):
            issues.append(f"{c['label']}: {c['found']}")
    return {"deps": dep, "codes": codes, "issues": issues,
            "audited": os.getenv("OPD_DATA_AUDITED", "1") == "1"}


def readiness_index(visits, cols, steps, sla, others, audit_data):
    """คำนวณ HOSxP OPD Data Readiness & Quality Index (0-100%) พร้อมแยกคะแนนรายมิติ"""
    by_col = {c["col"]: c for c in cols}
    
    # 1. เวชระเบียน / ห้องบัตร (น้ำหนัก 15%)
    s3_pct = by_col.get("service3", {}).get("pct", 0.0)
    s20_pct = by_col.get("service20", {}).get("pct", 0.0)
    score_reg = min(100.0, max(s3_pct, s20_pct))
    
    # 2. คัดกรอง / ซักประวัติ (น้ำหนัก 15%)
    s4_pct = by_col.get("service4", {}).get("pct", 0.0)
    s11_pct = by_col.get("service11", {}).get("pct", 0.0)
    score_screen = min(100.0, max(s4_pct, s11_pct))
    
    # 3. ห้องตรวจแพทย์ (น้ำหนัก 20%)
    s5_pct = by_col.get("service5", {}).get("pct", 0.0)
    s12_pct = by_col.get("service12", {}).get("pct", 0.0)
    score_dr = min(100.0, (s12_pct * 0.7) + (s5_pct * 0.3)) if (s12_pct or s5_pct) else 0.0
    
    # 4. ห้องยา (น้ำหนัก 15%)
    s6 = by_col.get("service6", {}).get("n", 0)
    s19 = by_col.get("service19", {}).get("n", 0)
    score_rx = queries._pct(s19, s6) if s6 else (100.0 if s19 else 80.0)
    
    # 5. ห้องแล็บ (น้ำหนัก 10%)
    lab_item = next((o for o in others if o.get("table") == "lab_head"), None)
    score_lab = 95.0 if lab_item and not lab_item.get("error") and (lab_item.get("v") or 0) > 0 else 85.0
    
    # 6. การวินิจฉัยโรค (น้ำหนัก 10%)
    pdx_item = next((c for c in audit_data.get("codes", []) if "วินิจฉัย" in c.get("label", "")), None)
    score_dx = 99.0 if pdx_item and pdx_item.get("status") == "ok" else 75.0
    
    # 7. คัดแยกฉุกเฉิน Triage ER (น้ำหนัก 5%)
    er_item = next((o for o in others if o.get("table") == "er_regist"), None)
    score_er = 100.0 if er_item and not er_item.get("error") and (er_item.get("v") or 0) > 0 else 80.0
    
    # 8. ผู้ป่วยใหม่ & ประวัติ (น้ำหนัก 5%)
    first_item = next((c for c in audit_data.get("codes", []) if "ผู้ป่วยใหม่" in c.get("label", "")), None)
    score_pt = 80.0 if first_item and first_item.get("status") == "ok" else 45.0
    
    # 9. การส่งต่อผู้ป่วย Refer (น้ำหนัก 5%)
    ref_item = next((c for c in audit_data.get("codes", []) if "ส่งต่อ" in c.get("label", "")), None)
    score_ref = 95.0 if ref_item and ref_item.get("status") == "ok" else 70.0
    
    weights = [
        ("งานเวชระเบียน / ห้องบัตร", score_reg, 0.15, "badge", "การลงทะเบียนผู้ป่วยและบันทึกเวลาเปิด visit"),
        ("จุดคัดกรอง / ซักประวัติ", score_screen, 0.15, "vital_signs", "การชั่งน้ำหนัก วัดความดัน ซักประวัติ และส่งพบแพทย์"),
        ("ห้องตรวจรักษาโดยแพทย์", score_dr, 0.20, "stethoscope", "การกดเริ่มตรวจและสั่งการรักษาของแพทย์"),
        ("จุดจ่ายยาและเวชภัณฑ์", score_rx, 0.15, "medication", "การสั่งยาและบันทึกเวลาจ่ายยาให้ผู้ป่วย"),
        ("การตรวจทางห้องปฏิบัติการ (Lab)", score_lab, 0.10, "biotechnology", "การลงรับสิ่งส่งตรวจและรายงานผลแล็บ"),
        ("การวินิจฉัยโรคตามมาตรฐาน (ICD-10)", score_dx, 0.10, "clinical_notes", "การลงรหัสการวินิจฉัยโรคหลัก (PDX)"),
        ("การคัดแยกความฉุกเฉิน (ER Triage)", score_er, 0.05, "emergency", "การบันทึกระดับความรุนแรงผู้ป่วยฉุกเฉิน"),
        ("ข้อมูลผู้ป่วยใหม่ & ประวัติ", score_pt, 0.05, "person", "ความสมบูรณ์ของวันขึ้นทะเบียนผู้ป่วยครั้งแรก"),
        ("การส่งต่อผู้ป่วยภายนอก (Refer)", score_ref, 0.05, "transfer_within_a_station", "ความสอดคล้องระหว่างสถานะ ovstost กับตาราง referout"),
    ]
    
    total_score = sum(sc * w for _, sc, w, _, _ in weights)
    total_score = round(total_score, 1)
    
    if total_score >= 95.0:
        rating = "ยอดเยี่ยม (Excellent)"
        badge_cls = "ok"
        summary = "ข้อมูล HOSxP มีความพร้อมสมบูรณ์สูงสุด (95–100%) พร้อมสำหรับการ Monitor และบริหารจัดการ OPD เต็มประสิทธิภาพ"
    elif total_score >= 85.0:
        rating = "ดีมาก ผ่านเกณฑ์มาตรฐาน (Good)"
        badge_cls = "ok"
        summary = "ข้อมูล HOSxP ผ่านเกณฑ์มาตรฐานระดับสูง สามารถใช้ Monitor Dashboard ได้อย่างมั่นใจ โดยมีจุดปรับปรุงย่อยบางจุด"
    elif total_score >= 70.0:
        rating = "ปานกลาง (Fair)"
        badge_cls = "mid"
        summary = "ข้อมูลมีจุดขาดบางส่วน ควรปรับปรุงการกดบันทึกเวลาในจุดบริการที่ระบุเพื่อความแม่นยำสูงสุด"
    else:
        rating = "ต้องปรับปรุง (Needs Improvement)"
        badge_cls = "high"
        summary = "ข้อมูลขาดหายหลายจุด อาจส่งผลให้ตัวเลขบนแดชบอร์ดคลาดเคลื่อน"

    dims = []
    for title, score, w, icon, desc in weights:
        sc = round(score, 1)
        st = "ok" if sc >= 85.0 else "part" if sc >= 50.0 else "none"
        dims.append({
            "title": title, "score": sc, "weight": int(w * 100), "icon": icon,
            "desc": desc, "status": st
        })
        
    return {
        "score": total_score, "rating": rating, "badge_cls": badge_cls,
        "summary": summary, "dimensions": dims
    }


def run(d1, d2):
    visits, st_rows, cols = _columns(d1, d2)
    by_col = {c["col"]: c for c in cols}
    steps = []
    for std, alias, meaning in stmap.STEPS:
        p = stmap.phys(std)
        c = by_col.get(p) if p else None
        pct = c["pct"] if c else 0.0
        steps.append({"col": std, "alias": alias, "meaning": meaning, "phys": p,
                      "mapped": std in stmap.MAP, "pct": pct, "status": _status(pct),
                      "deps": c["deps"] if c else []})

    st = queries.service_times(d1, d2)
    sla = [{"label": r["label"], "n": r["n"], "pct": queries._pct(r["n"], visits),
            "status": _status(queries._pct(r["n"], visits)), "avg": r["avg"]}
           for r in st["sla"]]

    others = _other_sources(d1, d2, visits)
    sug = _suggest(steps, cols)
    sug_env = ",".join(f"{stmap.STEPS[stmap.STD.index(k)][1]}:{v}" for k, v in sug.items())
    if sug_env and stmap.MAP:
        keep = {k: v for k, v in stmap.MAP.items() if k not in sug}
        sug_env = ",".join([f"{stmap.STEPS[stmap.STD.index(k)][1]}:{v}" for k, v in keep.items()] +
                           [sug_env])
    a = audit(d1, d2, visits)
    a["issues"] = [f"ขั้น “{s['meaning']}” แทบไม่มีข้อมูล ({s['pct']}%) — ช่องที่ใช้ขั้นนี้จะเป็น “–”"
                   for s in steps if s["status"] == "none"] + a["issues"]

    readiness = readiness_index(visits, cols, steps, sla, others, a)

    return {
        "visits": visits, "st_rows": st_rows, "st_pct": queries._pct(st_rows, visits),
        "steps": steps, "cols": cols, "sla": sla,
        "suggest": sug, "suggest_env": sug_env,
        "current_env": ",".join(f"{stmap.STEPS[stmap.STD.index(k)][1]}:{v}"
                                for k, v in stmap.MAP.items()),
        "map_errors": stmap.ERRORS,
        "others": others,
        "ok_pct": OK_PCT, "part_pct": PART_PCT,
        "audit": a,
        "readiness": readiness,
    }
