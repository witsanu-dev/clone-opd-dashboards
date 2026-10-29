# คู่มือการอัปเดตและดูแลระบบ OPD Dashboard

เอกสารสรุปขั้นตอนการดึงโค้ดเวอร์ชันล่าสุด (Git Pull) และการบริหารจัดการ Service สำหรับผู้ดูแลระบบ

---

## 1. ขั้นตอนการอัปเดตระบบ (Update Procedure)

เมื่อมีการปรับปรุงโค้ดและ push ขึ้น GitHub แล้ว ให้เข้าไปที่เซิร์ฟเวอร์และรันคำสั่งดังนี้:

```bash
# 1. เข้าสู่โฟลเดอร์โปรเจกต์
cd /home/wsnapp/clone-opd-dashboards

# 2. เปิดใช้งาน Python Virtual Environment
source .venv/bin/activate

# 3. ดึงโค้ดเวอร์ชันล่าสุดจาก GitHub
git pull origin main

# 4. อัปเดต Dependencies (กรณีมีการเพิ่มหรือปรับแพ็กเกจ)
pip install -r requirements.txt

# 5. รีสตาร์ต Service ใน PM2 เพื่อให้โค้ดใหม่มีผลทันที
pm2 restart opd-dashboards
```

---

## 2. การสั่งเริ่มทำงานครั้งแรก (Initial Start)

กรณีเริ่มรัน service เป็นครั้งแรกด้วยชื่อ `opd-dashboards`:

```bash
cd /home/wsnapp/clone-opd-dashboards
source .venv/bin/activate

# รันด้วย Gunicorn (แนะนำสำหรับ Production)
pm2 start "gunicorn -w 4 -b 0.0.0.0:1335 app:app" --name "opd-dashboards"

# หรือรันผ่าน python โดยตรง
# pm2 start app.py --name "opd-dashboards" --interpreter .venv/bin/python

# บันทึกสถานะให้ทำงานอัตโนมัติตอนเปิดเครื่อง
pm2 save
```

---

## 3. การตรวจสอบสถานะและการทำงาน (Monitoring)

```bash
# ตรวจสอบสถานะการทำงานของ Service (ดู uptime, cpu, memory)
pm2 status

# ดู Log สดแบบเรียลไทม์
pm2 logs opd-dashboards

# ดู Log ล่าสุด 50 บรรทัด
pm2 logs opd-dashboards --lines 50 --nostream

# ตรวจสอบพอร์ต 1335 ว่ากำลัง Listen อยู่หรือไม่
netstat -tulnp | grep 1335
# หรือ
ss -tulnp | grep 1335
```

---

## 4. คำสั่งจัดการ Service (PM2 Commands)

| คำสั่ง | คำอธิบาย |
| :--- | :--- |
| `pm2 restart opd-dashboards` | รีสตาร์ตระบบ |
| `pm2 stop opd-dashboards` | หยุดการทำงานชั่วคราว |
| `pm2 start opd-dashboards` | เริ่มการทำงาน |
| `pm2 reload opd-dashboards` | รีโหลดแบบ Zero-downtime (เมื่อใช้ gunicorn) |
| `pm2 save` | บันทึกสถานะปัจจุบัน เพื่อให้สตาร์ทอัตโนมัติเมื่อเปิด/รีบูตเครื่อง |

---

## 5. ข้อมูลระบบ (System Info)

- **URL เข้าใช้งาน:** `http://10.250.101.18:1335`
- **Path โปรเจกต์:** `/home/wsnapp/clone-opd-dashboards`
- **Service Name (PM2):** `opd-dashboards`
- **Config Database:** `.env` (สิทธิ์ `chmod 600`)
- **Repository:** `https://github.com/witsanu-dev/clone-opd-dashboards.git`
