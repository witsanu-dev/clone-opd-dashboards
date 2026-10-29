/* ผังผู้ป่วย OPD แบบ 2.5D (isometric) — วาดด้วย canvas ล้วน ไม่ใช้ไลบรารีภายนอก
 *
 * ข้อมูล: /api/flow.json?d=YYYY-MM-DD → visits[{i, s:[[นาทีที่เข้า, ลำดับจุดบริการ],...], e:นาทีที่ออก}]
 * ทุกเฟรมคำนวณว่าเวลาจำลอง T แต่ละ visit อยู่จุดไหน แล้วให้หุ่นเดินไปเก้าอี้ว่างของจุดนั้น
 *
 * ผังไล่จาก docs/floorplan.jpg — แก้ตำแหน่ง/ขนาดห้องได้ที่ LAYOUT ด้านล่าง (หน่วย ≈ เมตร,
 * x = ไปทางขวาล่างของจอ, y = ไปทางซ้ายล่างของจอ)
 */
(function () {
  'use strict';

  var CFG = window.FLOW;

  // ------------------------------------------------------------------ ผังอาคาร
  // ไล่จากแผนผังอาคารและบริเวณ (docs/floorplan.jpg) — ใส่พิกัดเป็น "พิกเซลบนภาพผัง"
  // [ซ้าย, บน, ขวา, ล่าง] แล้วแปลงเป็นเมตรด้วย px() (8 px ≈ 1 ม.) แก้ตำแหน่งได้โดยเทียบกับภาพ
  // ทิศบนของภาพผังอยู่ด้านบนของจอ (ภาพหมุน 45° ตามมุมมอง isometric)
  var PX = 8, PX0 = 235, PY0 = 70;
  function px(x0, y0, x1, y1) { return [(x0 - PX0) / PX, (y0 - PY0) / PX, (x1 - x0) / PX, (y1 - y0) / PX]; }
  function pt(x, y) { return [(x - PX0) / PX, (y - PY0) / PX]; }

  // จุดบริการที่มีข้อมูล — kind = สีพื้น, counter = เคาน์เตอร์ชิดผนังด้านหลัง, cap = ที่นั่งจริง (ไม่ใส่ = นับเก้าอี้ในผัง),
  // room = ห้องตรวจ (โต๊ะแพทย์ + เตียง), open = โซนในโถงเดียวกัน (ไม่มีผนังกั้น), door = ทางเข้า
  var LAYOUT = {
    RX:   { r: px(385, 85, 468, 190),  kind: 'rx',   open: true },         // รอรับยา หน้าช่องจ่ายยา (ขยายเต็มแนวห้องยา)
    REG:  { r: px(650, 82, 705, 190),  kind: 'hall', counter: true },      // ห้องบัตร
    NCD:  { r: px(553, 82, 650, 190),  kind: 'hall', counter: true },
    // โถงหน้าห้องตรวจ (กว้าง 2 แถว): แถวในสุดรอผลแล็บ แถวติดห้องตรวจรอพบแพทย์
    LABW: { r: px(445, 190, 625, 245), kind: 'lab',  open: true },
    DRW:  { r: px(445, 245, 625, 300), kind: 'hall', open: true },
    CASH: { r: px(468, 135, 553, 190), kind: 'rx',   open: true, counter: true },  // การเงิน ฝั่งขวาของจุดรอรับยา
    SCR:  { r: px(625, 190, 705, 300), kind: 'hall', open: true, counter: true },  // ซักประวัติ ใกล้ทางเข้า
    MH:   { r: px(345, 300, 405, 370), kind: 'room2', counter: true },     // สุขภาพจิต ต่อห้องตรวจ 5 หน้าห้องยา
    R5:   { r: px(405, 300, 448, 370), kind: 'room', room: true, short: 'ห้อง 5' },
    R4:   { r: px(448, 300, 493, 370), kind: 'room', room: true, short: 'ห้อง 4' },
    R3:   { r: px(493, 300, 538, 370), kind: 'room', room: true, short: 'ห้อง 3' },
    R2:   { r: px(538, 300, 582, 370), kind: 'room', room: true, short: 'ห้อง 2' },
    R1:   { r: px(582, 300, 625, 370), kind: 'room', room: true, short: 'ห้อง 1' },   // เดิมเป็นห้อง Telemed
    LAB:  { r: px(245, 315, 330, 405), kind: 'lab',  counter: true },
    PT:   { r: px(150, 100, 225, 200), kind: 'room2', counter: true },     // กายภาพ อาคารแยกหลังห้องยา
    TTM:  { r: px(150, 5, 225, 85),    kind: 'ttm',   counter: true },     // แพทย์แผนไทย อีกฝั่งของอาคารกายภาพ
    ARI:  { r: px(632, 305, 690, 375), kind: 'ari',  door: 'ari' },
    ER:   { r: px(562, 385, 625, 468), kind: 'er',   door: 'er' },          // ห้องฉุกเฉิน (ย่อพื้นที่ลง)
    REGER: { r: px(625, 385, 668, 468), kind: 'hall', door: 'er', counter: true },  // ห้องบัตรหน้า ER
    DENT: { r: px(542, 510, 668, 562), kind: 'room2', door: 'admin' },
    TELE: { r: px(542, 605, 590, 680), kind: 'room', counter: true, door: 'admin' },  // Telemed ซ้ายสุดของปฐมภูมิ
    SPEC: { r: px(590, 605, 668, 680), kind: 'room2', door: 'admin' }
  };
  // ห้อง/อาคารที่ไม่มีข้อมูลผู้ป่วย — วาดไว้ให้ผังครบ
  var DECOR = [
    { name: 'X-ray', r: px(468, 82, 553, 135), kind: 'decor' },          // ติดคลินิก NCD
    // ห้องยาอยู่ฝั่งอาคารกายภาพ ช่องจ่ายยาหันเข้าโถง OPD (ทิศตะวันออก) ผู้ป่วยรอรับยาในโถงหน้าช่อง
    { name: 'ห้องยา', r: px(332, 85, 385, 240), kind: 'rx', counter: 'east' }
  ];
  // พื้นอาคาร (ฐาน + ขอบ)
  var BUILDINGS = [
    px(328, 80, 722, 302),      // อาคาร OPD (โถงขยายลงมาให้หน้าห้องตรวจกว้าง)
    px(343, 298, 627, 372),     // แถวห้องตรวจ + สุขภาพจิต
    px(630, 303, 692, 377),     // ARI
    px(560, 383, 670, 470),     // ER + ห้องบัตรหน้า ER
    px(540, 508, 670, 682),     // อาคารปฐมภูมิ (ทันตกรรม, Telemed, ปฐมภูมิ)
    px(243, 313, 332, 407),     // LAB
    px(148, 98, 227, 202),      // กายภาพบำบัด
    px(148, 3, 227, 87)         // แพทย์แผนไทย
  ];
  // ทางเดินมีหลังคา (พื้นอย่างเดียว)
  var WALKS = [px(330, 240, 345, 316), px(227, 140, 332, 165), px(178, 87, 198, 100), px(705, 195, 735, 228),
               px(692, 325, 735, 345), px(668, 410, 735, 440), px(668, 520, 735, 550), px(668, 585, 735, 610)];
  // ถนนเส้นหลัก (ฝั่งตะวันออก) — ผู้ป่วยเดินมาจากลานจอดรถทางใต้ตามถนนแล้วเลี้ยวเข้าประตู
  var ROAD = px(735, 40, 770, 760);
  var ROAD_X = (752 - PX0) / PX, SPAWN_Y = (760 - PY0) / PX;
  var DOORS = {
    main:  { p: pt(712, 212) },
    ari:   { p: pt(700, 335) },
    er:    { p: pt(680, 425) },
    admin: { p: pt(680, 560) }
  };
  var FLOOR = {
    hall: '#fbe6e1', lab: '#ece6fb', room: '#e3edfb', rx: '#e0f4ea',
    er: '#fde0de', ari: '#fff1d6', room2: '#e3f3f7', ttm: '#f3ecdc', decor: '#eef1f5'
  };

  var TREES = null;          // null = ต้นไม้ชุดเดิมของผังตัวอย่าง

  // ผังของโรงพยาบาลเอง: แอปอ่าน flow_layout.json ส่งมาใน FLOW.layout แล้วแทนผังด้านบนทั้งชุด
  // (รูปแบบไฟล์ดู flow_layout.example.json — พิกัดเป็นพิกเซลบนภาพผัง แปลงเป็นเมตรด้วย scale/origin)
  if (CFG.layout) applyLayout(CFG.layout);
  function applyLayout(j) {
    var s = j.scale || 8, o = j.origin || [0, 0];
    var R = function (r) { return [(r[0] - o[0]) / s, (r[1] - o[1]) / s, (r[2] - r[0]) / s, (r[3] - r[1]) / s]; };
    var T = function (p) { return [(p[0] - o[0]) / s, (p[1] - o[1]) / s]; };
    var copy = function (src) { var x = {}; for (var k in src) x[k] = src[k]; x.r = R(src.r); return x; };
    LAYOUT = {};
    Object.keys(j.areas || {}).forEach(function (k) { LAYOUT[k] = copy(j.areas[k]); });
    DECOR = (j.decor || []).map(copy);
    BUILDINGS = (j.buildings || []).map(R);
    WALKS = (j.walks || []).map(R);
    if (j.road) {
      ROAD = R(j.road);
      ROAD_X = ROAD[0] + ROAD[2] / 2; SPAWN_Y = ROAD[1] + ROAD[3];
    }
    DOORS = {};
    Object.keys(j.doors || {}).forEach(function (k) { DOORS[k] = { p: T(j.doors[k]) }; });
    if (!DOORS.main) DOORS.main = { p: [ROAD_X, SPAWN_Y - 2] };
    TREES = (j.trees || []).map(T);
    if (!BUILDINGS.length) BUILDINGS = Object.keys(LAYOUT).map(function (k) { return LAYOUT[k].r; });
  }

  var COL = { ok: '#1fa463', mid: '#f59e0b', high: '#e5484d' };
  // ความหนาแน่น = คนในโซน ÷ ที่นั่ง · < 45% ว่าง · 45–79% ปานกลาง · ≥ 80% แน่น
  var DENS = { low: { c: '#1fa463', t: 'ว่าง' }, mid: { c: '#f59e0b', t: 'ปานกลาง' }, full: { c: '#e5484d', t: 'แน่น' } };
  function densLevel(r) { return r >= 0.8 ? 'full' : r >= 0.45 ? 'mid' : 'low'; }
  function median(xs) {
    if (!xs.length) return 0;
    var a = xs.slice().sort(function (p, q) { return p - q; }), m = a.length >> 1;
    return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2;
  }
  // สีเสื้อ = ความเร่งด่วน (u จาก API) ตามสีคัดแยก ER ของ สธ. — ระดับ 2 ใช้ส้มแทนชมพู
  // ไม่ให้ซ้ำกับกางเกงชมพูของผู้หญิง · 6 = ช่องทางพิเศษ OPD (ผู้สูงอายุ / พระภิกษุ / ผู้พิการ)
  var SHIRT = { 1: '#dc2626', 2: '#f97316', 3: '#facc15', 4: '#22c55e', 5: '#ffffff', 6: '#8b5cf6' };
  var PANTS = { 1: '#3b82f6', 2: '#f472b6', 0: '#64748b' };   // ชาย ฟ้า · หญิง ชมพู · ไม่ระบุ เทา
  var MOOD = { mid: 'sentiment_dissatisfied', high: 'sentiment_very_dissatisfied' };                         // รอเกินเป้า / เกิน 1.5 เท่า
  var URG_NAME = { 1: 'วิกฤต', 2: 'ฉุกเฉิน', 3: 'เร่งด่วน', 4: 'กึ่งเร่งด่วน', 5: 'ทั่วไป', 6: 'สูงอายุ / พระภิกษุ / ผู้พิการ' };
  var hover = null;         // หุ่นที่เมาส์ชี้ / แตะอยู่
  function level(mins, target) {
    if (mins <= target) return 'ok';
    return mins >= target * 1.5 ? 'high' : 'mid';
  }

  // ------------------------------------------------------------------ สถานะ
  var canvas = document.getElementById('map');
  var ctx = canvas.getContext('2d');
  var tlc = document.getElementById('timeline');
  var tctx = tlc.getContext('2d');
  var $ = function (id) { return document.getElementById(id); };

  var data = null;          // คำตอบล่าสุดจาก API
  var areas = [];           // จุดบริการ + เรขาคณิต (ลำดับตาม API)
  var people = new Map();   // รหัส visit → สถานะหุ่น
  var occ = null;           // จำนวนคนใน รพ. รายนาที (สำหรับกราฟไทม์ไลน์)

  var state = {
    date: CFG.date,
    live: CFG.date === CFG.today,
    playing: true,
    speed: 60,              // นาทีจำลองต่อ 1 นาทีจริง (×60 = 1 นาทีต่อวินาที)
    T: 6 * 60,              // เวลาจำลอง (นาทีนับจากเที่ยงคืน)
    lastT: null
  };

  var cam = { k: 12, ox: 0, oy: 0 };
  var W = 0, H = 0, DPR = 1;
  var C30 = Math.cos(Math.PI / 6), S30 = 0.5, ZK = 0.95;

  function P(x, y, z) {
    return [cam.ox + (x - y) * C30 * cam.k, cam.oy + ((x + y) * S30 - (z || 0) * ZK) * cam.k];
  }

  // ------------------------------------------------------------------ เรขาคณิต
  // จุดบริการที่ไม่มีในผัง (ผังของโรงพยาบาลเองไม่ได้ใส่ไว้) — เรียงเป็นแถวใต้อาคารทั้งหมด
  var spare = 0, spareAt = null;
  function spareSpot(id) {
    if (!spareAt) {
      spareAt = [1e9, 0];
      BUILDINGS.forEach(function (b) { spareAt = [Math.min(spareAt[0], b[0]), Math.max(spareAt[1], b[1] + b[3])]; });
    }
    var L = { r: [spareAt[0] + (spare++) * 7, spareAt[1] + 4, 6, 6], kind: 'hall', spare: true };
    LAYOUT[id] = L;
    BUILDINGS.push(L.r);
    return L;
  }

  function buildAreas() {
    spare = 0;
    areas = data.areas.map(function (a) {
      var L = LAYOUT[a.id] || spareSpot(a.id);
      var r = L.r, x = r[0], y = r[1], w = r[2], d = r[3];
      var seats = [];
      if (L.room) {
        // ห้องตรวจ: โต๊ะแพทย์ + เก้าอี้ผู้ป่วย 1 + เก้าอี้รอในห้อง
        // ประตูอยู่ฝั่งโถง (ด้านบนของผัง): แพทย์นั่งในสุด เก้าอี้ผู้ป่วยหน้าโต๊ะ เก้าอี้รอชิดประตู
        seats = [[x + w / 2, y + d - 3.2], [x + w - 1.0, y + 1.3], [x + w - 1.0, y + 2.6],
                 [x + 1.0, y + 1.3], [x + 1.0, y + 2.6]];
      } else {
        var top = L.counter ? 2.7 : 1.4, sx = 1.45, sy = 2.0;
        for (var yy = y + top; yy <= y + d - 1.0; yy += sy) {
          for (var xx = x + 1.2; xx <= x + w - 0.9; xx += sx) {
            if (xx > x + w / 2 - 0.7 && xx < x + w / 2 + 0.5) continue;   // ทางเดินกลาง
            seats.push([xx, yy]);
          }
        }
      }
      // ที่ยืนเมื่อเก้าอี้เต็ม: แทรกระหว่างแถว แล้วขยายออกไปเรื่อย ๆ
      var stand = [];
      for (var k = 0; k < 4; k++) {
        var off = [[0.6, 0.85], [0, 0.85], [0.6, 0.0], [0.3, 0.42]][k];
        seats.forEach(function (s) {
          var px = s[0] + off[0], py = s[1] + off[1];
          if (px < x + w - 0.4 && py < y + d - 0.4) stand.push([px, py]);
        });
      }
      return {
        id: a.id, name: a.name, target: a.target, nowait: !!a.nowait, L: L,
        x: x, y: y, w: w, d: d, cx: x + w / 2, cy: y + d / 2,
        chairs: seats, spots: seats.concat(stand), used: [],
        door: (DOORS[L.door] || DOORS.main).p, n: 0, avg: 0, max: 0
      };
    });
  }

  // ------------------------------------------------------------------ จำลอง
  function whereAt(v, T) {
    if (T < v.s[0][0] || T >= v.e) return null;
    var seg = v.s[0];
    for (var i = 1; i < v.s.length; i++) {
      if (v.s[i][0] <= T) seg = v.s[i]; else break;
    }
    return seg;
  }

  function freeSpot(a) {
    for (var i = 0; i < a.used.length; i++) if (!a.used[i]) return i;
    return a.used.length;
  }

  function spotPos(a, i) {
    if (i < a.spots.length) return a.spots[i];
    // เกินที่ยืนทั้งหมด — วางสุ่มแบบคงที่ในห้อง
    var h = Math.sin(i * 12.9898) * 43758.5453, f = h - Math.floor(h);
    var g = Math.sin(i * 78.233) * 12345.678, e = g - Math.floor(g);
    return [a.x + 0.6 + f * (a.w - 1.2), a.y + 0.6 + e * (a.d - 1.2)];
  }

  function step(dt) {
    var T = state.T;
    // กระโดดเวลา (ลากไทม์ไลน์/เปลี่ยนวัน) → จัดที่นั่งใหม่ทั้งหมดแล้ววางหุ่นทันที ไม่ต้องเดิน
    var jump = state.lastT === null || T < state.lastT - 0.01 || T - state.lastT > 20;
    state.lastT = T;
    if (jump) {
      areas.forEach(function (a) { a.used = []; });
      people.forEach(function (p) {
        p.area = -1; p.spot = -1; p.vis = false; p.path = null; p.leaving = false;
      });
    }

    var arrivals = [];
    var visits = data.visits;
    var arrived = 0, inside = 0, left = 0;
    for (var i = 0; i < visits.length; i++) {
      var v = visits[i];
      var p = people.get(v.i);
      if (!p) {
        p = { area: -1, spot: -1, x: 0, y: 0, vis: false, a: 0, enter: 0, sit: false };
        people.set(v.i, p);
      }
      p.seen = true; p.u = v.u; p.g = v.g;
      if (T >= v.s[0][0]) arrived++;
      if (T >= v.e) left++;
      var seg = whereAt(v, T);
      var to = seg ? seg[1] : -1;
      var prevEnter = p.enter;
      if (seg) { inside++; p.enter = seg[0]; p.total = v.s[0][0]; }
      if (to !== p.area) {
        // ได้รับบริการทันเวลา: ออกจากจุดเดิมภายในเป้าหมายของจุดนั้น
        if (!jump && p.area >= 0 && p.vis) {
          var fa = areas[p.area], leftAt = seg ? seg[0] : v.e;
          // นับเฉพาะจุดที่ "รอ" (ไม่นับห้องบัตรที่ผ่านใน 2 นาทีทุกคน และเวลาอยู่ในห้องตรวจ)
          var waitArea = !fa.nowait && !fa.L.room && fa.id !== 'REG' && fa.id !== 'REGER';
          if (waitArea && leftAt - prevEnter <= fa.target) p.heart = performance.now();
        }
        if (p.area >= 0 && p.spot >= 0) areas[p.area].used[p.spot] = false;
        var from = p.area;
        p.area = to; p.spot = -1;
        if (to >= 0) {
          arrivals.push(p);
          if (from < 0 && !p.vis) {
            // เพิ่งมาถึง รพ.: เดินจากลานจอดรถ (ทางใต้) ตามถนน แล้วเลี้ยวเข้าประตูของจุดนั้น
            var dr = areas[to].door;
            p.x = ROAD_X; p.y = SPAWN_Y;
            p.vis = true; p.a = jump ? 1 : 0; p.path = jump ? null : [[ROAD_X, dr[1]], [dr[0], dr[1]]];
          }
        } else if (p.vis) {
          // ออกจาก รพ.: เดินออกทางประตู แล้วหายไป
          var src = from >= 0 ? areas[from] : areas[0];
          if (jump) { p.vis = false; }
          else {
            var sd = src.door;
            p.path = [[sd[0], sd[1]], [ROAD_X, sd[1]], [ROAD_X, SPAWN_Y]]; p.leaving = true;
          }
        }
      }
    }
    // ลบหุ่นที่ไม่มีในข้อมูลแล้ว (เปลี่ยนวัน)
    people.forEach(function (p, key) { if (!p.seen) people.delete(key); p.seen = false; });

    arrivals.sort(function (a, b) { return a.enter - b.enter; });
    arrivals.forEach(function (p) {
      var a = areas[p.area];
      p.spot = freeSpot(a);
      a.used[p.spot] = true;
      if (p.leaving) { p.path = null; p.leaving = false; }
      var pos = spotPos(a, p.spot);
      p.tx = pos[0]; p.ty = pos[1];
      p.sit = p.spot < a.chairs.length;
      if (jump) { p.x = p.tx; p.y = p.ty; p.path = null; p.a = 1; p.vis = true; }
    });

    // เดิน
    var spd = 6;   // เมตร/วินาที (เร็วกว่าคนจริงเพื่อให้ทันเวลาจำลอง)
    people.forEach(function (p) {
      if (!p.vis) return;
      var tx = p.tx, ty = p.ty;
      if (p.path && p.path.length) { tx = p.path[0][0]; ty = p.path[0][1]; }
      else if (p.leaving) { p.vis = false; return; }
      var dx = tx - p.x, dy = ty - p.y, dist = Math.sqrt(dx * dx + dy * dy);
      var mv = Math.max(spd, dist * 2.2) * dt;
      if (dist <= mv) {
        p.x = tx; p.y = ty;
        if (p.path && p.path.length) p.path.shift();
        p.walking = !!(p.path && p.path.length);
      } else {
        p.x += dx / dist * mv; p.y += dy / dist * mv; p.walking = true;
      }
      p.a = Math.min(1, p.a + dt * 3);
    });

    // สถิติรายจุด — เวลารอใช้ "ค่ากลาง" (มัธยฐาน) ไม่ให้คนที่ค้างนานผิดปกติดึงตัวเลขสูงเกินจริง
    // ห้องฉุกเฉินไม่คิดเวลารอ/ความหนาแน่น (ไม่ได้รอคิวแบบ OPD)
    areas.forEach(function (a) { a.n = 0; a.waits = []; a.max = 0; });
    var worst = null, inSum = 0, capSum = 0;
    people.forEach(function (p) {
      if (p.area < 0) return;
      var a = areas[p.area], w = T - p.enter;
      p.wait = w;
      p.lv = a.nowait ? 'ok' : level(w, a.target);
      a.n++;
      if (a.nowait) return;
      a.waits.push(w);
      if (w > a.max) a.max = w;
      if (!worst || w > worst.w) worst = { w: w, a: a };
    });
    areas.forEach(function (a) {
      a.served = a.L.room || a.id === 'TELE' ? servedAt(a, T) : 0;   // แพทย์ตรวจแล้วกี่รายจนถึงเวลานี้
      a.avg = median(a.waits);
      a.lv = a.waits.length ? level(a.avg, a.target) : 'ok';
      a.cap = Math.max(1, a.L.cap || a.chairs.length);   // ที่นั่งจริงใส่ cap ใน LAYOUT ได้
      a.dens = a.n / a.cap;
      a.dl = densLevel(a.dens);
      if (!a.nowait) { inSum += a.n; capSum += a.cap; }
    });
    var dens = capSum ? inSum / capSum : 0;
    state.stats = { arrived: arrived, inside: inside, left: left, worst: worst, dens: dens };
  }

  // ------------------------------------------------------------------ วาด
  function poly(pts, fill, stroke, lw) {
    ctx.beginPath();
    for (var i = 0; i < pts.length; i++) {
      var p = P(pts[i][0], pts[i][1], pts[i][2]);
      if (i) ctx.lineTo(p[0], p[1]); else ctx.moveTo(p[0], p[1]);
    }
    ctx.closePath();
    if (fill) { ctx.fillStyle = fill; ctx.fill(); }
    if (stroke) { ctx.strokeStyle = stroke; ctx.lineWidth = lw || 1; ctx.stroke(); }
  }

  function rect(x, y, w, d, z, fill, stroke) {
    poly([[x, y, z], [x + w, y, z], [x + w, y + d, z], [x, y + d, z]], fill, stroke);
  }

  function box(x, y, z, w, d, h, top, left, right) {
    // left = หน้าทิศใต้ (y+d), right = หน้าทิศตะวันออก (x+w) — สองหน้าที่หันเข้าหาจอ
    poly([[x, y + d, z], [x + w, y + d, z], [x + w, y + d, z + h], [x, y + d, z + h]], left);
    poly([[x + w, y, z], [x + w, y + d, z], [x + w, y + d, z + h], [x + w, y, z + h]], right);
    poly([[x, y, z + h], [x + w, y, z + h], [x + w, y + d, z + h], [x, y + d, z + h]], top);
  }

  function shade(hex, f) {
    var n = parseInt(hex.slice(1), 16);
    var r = Math.round(((n >> 16) & 255) * f), g = Math.round(((n >> 8) & 255) * f), b = Math.round((n & 255) * f);
    return 'rgb(' + Math.min(255, r) + ',' + Math.min(255, g) + ',' + Math.min(255, b) + ')';
  }

  // วัตถุที่ต้องเรียงความลึก: ผนัง เก้าอี้ เคาน์เตอร์ ต้นไม้ หุ่น
  var statics = [];
  function buildStatics() {
    statics = [];
    var WH = 1.25;
    function wallX(x0, x1, y) {   // ผนังแนวแกน x (ด้านหลังทิศเหนือของห้อง)
      for (var x = x0; x < x1 - 0.001; x += 1) {
        (function (xa, xb) {
          statics.push({ k: (xa + xb) / 2 + y - 0.05, draw: function () {
            poly([[xa, y, 0], [xb, y, 0], [xb, y, WH], [xa, y, WH]], 'rgba(255,255,255,.88)', null);
            poly([[xa, y - 0.15, WH], [xb, y - 0.15, WH], [xb, y, WH], [xa, y, WH]], '#c9d3e1');
          } });
        })(x, Math.min(x + 1, x1));
      }
    }
    function wallY(y0, y1, x) {   // ผนังแนวแกน y (ด้านหลังทิศตะวันตกของห้อง)
      for (var y = y0; y < y1 - 0.001; y += 1) {
        (function (ya, yb) {
          statics.push({ k: x + (ya + yb) / 2 - 0.05, draw: function () {
            poly([[x, ya, 0], [x, yb, 0], [x, yb, WH], [x, ya, WH]], 'rgba(238,242,248,.9)', null);
            poly([[x - 0.15, ya, WH], [x, ya, WH], [x, yb, WH], [x - 0.15, yb, WH]], '#c9d3e1');
          } });
        })(y, Math.min(y + 1, y1));
      }
    }
    // เคาน์เตอร์บริการ: ชิดผนังด้านหลัง (ทิศเหนือ) หรือ 'east' = ช่องบริการหันออกทิศตะวันออก
    function counter(x, y, w, d, side) {
      if (side === 'east') {
        var ch = Math.min(d - 2, 9), cy = y + (d - ch) / 2, cx = x + w - 1.0;
        statics.push({ k: cx + 0.35 + cy + ch / 2, draw: function () {
          box(cx, cy, 0, 0.7, ch, 0.95, '#ffffff', '#d6deea', '#c4cfdf');
          for (var m = 0; m < 3; m++) {
            box(cx + 0.1, cy + 0.8 + m * (ch - 1.6) / 2 - 0.25, 0.95, 0.08, 0.5, 0.4, '#334155', '#1e293b', '#0f172a');
          }
        } });
        return;
      }
      var cw = Math.min(w - 2, 7), cx2 = x + (w - cw) / 2;
      statics.push({ k: cx2 + cw / 2 + y + 1.3, draw: function () {
        box(cx2, y + 0.9, 0, cw, 0.7, 0.95, '#ffffff', '#d6deea', '#c4cfdf');
        for (var m = 0; m < 3; m++) {   // จอคอมพิวเตอร์บนเคาน์เตอร์
          box(cx2 + 0.8 + m * (cw - 1.6) / 2 - 0.25, y + 1.0, 0.95, 0.5, 0.08, 0.4, '#334155', '#1e293b', '#0f172a');
        }
      } });
    }
    DECOR.forEach(function (o) {
      if (o.counter) counter(o.r[0], o.r[1], o.r[2], o.r[3], o.counter);
      if (o.open) return;
      wallX(o.r[0], o.r[0] + o.r[2], o.r[1]);
      wallY(o.r[1], o.r[1] + o.r[3], o.r[0]);
    });
    areas.forEach(function (a) {
      if (!a.L.open) {          // โซนในโถงเดียวกันไม่มีผนังกั้น
        wallX(a.x, a.x + a.w, a.y);
        wallY(a.y, a.y + a.d, a.x);
      }
      if (a.L.counter) counter(a.x, a.y, a.w, a.d, a.L.counter);
      if (a.L.room) {
        // โต๊ะแพทย์อยู่ด้านในสุดของห้อง (ประตูอยู่ฝั่งโถง)
        var dw = Math.min(2.8, a.w - 1.6), dx = a.x + (a.w - dw) / 2, dy = a.y + a.d - 2.4;
        statics.push({ k: dx + dw / 2 + dy + 0.6, draw: function () {
          box(dx, dy, 0, dw, 1.1, 0.85, '#f8fafc', '#dbe3ee', '#cbd5e1');
          box(dx + dw / 2 - 0.35, dy + 0.9, 0.85, 0.7, 0.1, 0.45, '#334155', '#1e293b', '#0f172a');
        } });
        // เตียงตรวจชิดผนังด้านตะวันตก
        statics.push({ k: a.x + 0.9 + a.y + 4.6, draw: function () {
          box(a.x + 0.35, a.y + 3.6, 0, 1.0, 2.0, 0.7, '#e0f2fe', '#bae6fd', '#93c5fd');
        } });
      }
      a.chairs.forEach(function (c) {
        statics.push({ k: c[0] + c[1] - 0.2, draw: function () {
          var x = c[0] - 0.28, y = c[1] - 0.25;
          box(x, y, 0, 0.56, 0.5, 0.42, '#6b9ae8', '#3f6fc6', '#335fae');
          box(x, y - 0.12, 0.42, 0.56, 0.12, 0.42, '#7aa6ef', '#3f6fc6', '#335fae');
        } });
      });
    });
    // ต้นไม้รอบอาคาร (ตำแหน่งคงที่)
    var trees = TREES ? TREES.slice() : [];
    if (!TREES) {
      for (var j = 5; j <= 85; j += 7) trees.push([ROAD_X + 5, j]);           // ริมถนนฝั่งตะวันออก
      for (var i = 15; i <= 58; i += 7) trees.push([i, -2.5]);                // หลังอาคาร OPD
      [[3, 24], [10, 22], [-6, 34], [-14, 4], [-14, 22], [1, 4], [2, 46], [8, 50], [14, 47], [20, 44], [28, 46], [33, 52],
       [30, 62], [24, 70], [33, 74], [16, 60]].forEach(function (t) { trees.push(t); });
    }
    trees.forEach(function (t) {
      statics.push({ k: t[0] + t[1], draw: function () {
        var b = P(t[0], t[1], 0);
        ctx.fillStyle = 'rgba(0,0,0,.12)';
        ctx.beginPath(); ctx.ellipse(b[0], b[1], 0.9 * cam.k, 0.45 * cam.k, 0, 0, 7); ctx.fill();
        box(t[0] - 0.12, t[1] - 0.12, 0, 0.24, 0.24, 1.0, '#8b5e3c', '#7a5133', '#6b462c');
        var c = P(t[0], t[1], 2.0);
        var gr = ctx.createRadialGradient(c[0] - 0.3 * cam.k, c[1] - 0.4 * cam.k, 0.1 * cam.k, c[0], c[1], 1.1 * cam.k);
        gr.addColorStop(0, '#5fd38a'); gr.addColorStop(1, '#22984f');
        ctx.fillStyle = gr;
        ctx.beginPath(); ctx.arc(c[0], c[1], 1.05 * cam.k, 0, 7); ctx.fill();
      } });
    });
  }

  function drawPerson(p) {
    // หุ่นใหญ่กว่าสเกลจริง 25% ให้เห็นชัดเมื่อซูมออก — ความสูงที่นั่งยังอิงสเกลจริงของเก้าอี้
    var k = cam.k * 1.25, base = P(p.x, p.y, 0);
    var shirt = SHIRT[p.u] || SHIRT[5], pants = PANTS[p.g] || PANTS[0];
    var sit = p.sit && !p.walking;
    var z0 = sit ? 0.42 : 0;
    var Y = function (z) { return base[1] - (z0 * cam.k + (z - z0) * k) * ZK; };
    var x = base[0];
    ctx.globalAlpha = p.a;
    ctx.fillStyle = 'rgba(0,0,0,.16)';
    ctx.beginPath(); ctx.ellipse(x, base[1], 0.28 * k, 0.14 * k, 0, 0, 7); ctx.fill();
    if (k < 5) {          // ซูมออกมาก: วาดเป็นจุดสีเสื้อ ขอบสีกางเกง
      ctx.fillStyle = shirt; ctx.strokeStyle = pants; ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.arc(x, Y(0.8), Math.max(1.8, 0.34 * k), 0, 7); ctx.fill(); ctx.stroke();
      ctx.globalAlpha = 1; return;
    }
    var bodyB = sit ? 0.6 : 0.66, bodyT = sit ? 1.1 : 1.34;
    ctx.lineCap = 'round';
    if (sit) {            // นั่ง: ตัก + ขาห้อยหน้าเก้าอี้
      ctx.fillStyle = pants;
      roundRect(x - 0.21 * k, Y(0.66), 0.42 * k, Y(0.42) - Y(0.66) + 0.12 * k, 0.08 * k); ctx.fill();
      ctx.strokeStyle = pants; ctx.lineWidth = 0.15 * k;
      ctx.beginPath();
      ctx.moveTo(x - 0.08 * k, Y(0.42) + 0.1 * k); ctx.lineTo(x - 0.08 * k, base[1] - 0.02 * k);
      ctx.moveTo(x + 0.08 * k, Y(0.42) + 0.1 * k); ctx.lineTo(x + 0.08 * k, base[1] - 0.02 * k);
      ctx.stroke();
    } else {              // ยืน/เดิน: ขาสีกางเกง
      ctx.strokeStyle = pants; ctx.lineWidth = 0.17 * k;
      var sw = p.walking ? Math.sin(performance.now() / 90 + p.x) * 0.07 * k : 0;
      ctx.beginPath(); ctx.moveTo(x - 0.08 * k, Y(0.68)); ctx.lineTo(x - 0.08 * k + sw, Y(0.02));
      ctx.moveTo(x + 0.08 * k, Y(0.68)); ctx.lineTo(x + 0.08 * k - sw, Y(0.02)); ctx.stroke();
    }
    var bw = 0.44 * k;
    ctx.fillStyle = shirt;
    roundRect(x - bw / 2, Y(bodyT), bw, Y(bodyB) - Y(bodyT), 0.16 * k);
    ctx.fill();
    if (p.u === 5) { ctx.strokeStyle = 'rgba(15,42,92,.35)'; ctx.lineWidth = 1; ctx.stroke(); }  // เสื้อขาวบนพื้นสีอ่อน
    var hy = Y(bodyT + 0.24), hr = 0.2 * k;
    if (p.g === 2) {      // ผู้หญิง: ผมยาวคลุมไหล่ด้านหลังศีรษะ
      ctx.fillStyle = '#3b2f2a';
      roundRect(x - hr * 1.12, hy - hr * 0.6, hr * 2.24, hr * 2.3, hr); ctx.fill();
    }
    ctx.fillStyle = '#f3c9a1';
    ctx.beginPath(); ctx.arc(x, hy, hr, 0, 7); ctx.fill();
    ctx.fillStyle = '#3b2f2a';     // ผมด้านบน (ผู้ชายสั้น ผู้หญิงมีหน้าม้า)
    ctx.beginPath(); ctx.arc(x, hy - 0.05 * k, hr, Math.PI * (p.g === 2 ? 0.95 : 1.05), Math.PI * (p.g === 2 ? 2.05 : 1.95)); ctx.fill();
    ctx.globalAlpha = 1;
    // Material Symbol favorite (หัวใจ) ได้รับบริการทันเวลา — ลอยขึ้นแล้วจางหาย 2.5 วินาที
    var ht = p.heart ? performance.now() - p.heart : 1e9;
    if (ht < 2500 && cam.k >= 4) {
      var f = ht / 2500;
      ctx.globalAlpha = Math.min(1, (1 - f) * 1.6);
      ctx.fillStyle = '#ef4444';
      ctx.font = Math.round(0.6 * k * (1 + 0.25 * Math.sin(Math.min(1, f * 4) * Math.PI))) +
        'px "Material Symbols Outlined", sans-serif';
      ctx.textAlign = 'center'; ctx.textBaseline = 'bottom';
      ctx.fillText('favorite', x, hy - hr - 0.05 * k - f * 0.9 * k);
      ctx.textAlign = 'left'; ctx.globalAlpha = 1;
      return;
    }
    // หัวร้อน: รอนานเกินเป้า — ลอยขึ้นลงเบา ๆ ให้สะดุดตา
    var mood = areas[p.area] && !areas[p.area].nowait ? MOOD[p.lv] : null;
    if (mood && cam.k >= 6 && !p.leaving) {
      var bob = Math.sin(performance.now() / 260 + p.x * 3) * 0.06 * k;
      ctx.fillStyle = p.lv === 'high' ? '#dc2626' : '#f59e0b';
      ctx.font = Math.round((p.lv === 'high' ? 0.62 : 0.5) * k) + 'px "Material Symbols Outlined", sans-serif';
      ctx.textAlign = 'center'; ctx.textBaseline = 'bottom';
      ctx.fillText(mood, x, hy - hr - 0.05 * k + bob);
      ctx.textAlign = 'left';
    }
  }

  function drawDoctor(a) {
    var x = a.x + a.w / 2, y = a.y + a.d - 0.8, k = cam.k;
    var b0 = P(x, y, 0.55), b1 = P(x, y, 1.3), h = P(x, y, 1.54);
    ctx.fillStyle = '#ffffff'; ctx.strokeStyle = '#94a3b8'; ctx.lineWidth = 1;
    roundRect(b1[0] - 0.22 * k, b1[1], 0.44 * k, b0[1] - b1[1], 0.16 * k); ctx.fill(); ctx.stroke();
    ctx.fillStyle = '#f3c9a1'; ctx.beginPath(); ctx.arc(h[0], h[1], 0.2 * k, 0, 7); ctx.fill();
    ctx.fillStyle = '#1d2433';
    ctx.beginPath(); ctx.arc(h[0], h[1] - 0.05 * k, 0.2 * k, Math.PI * 1.05, Math.PI * 1.95); ctx.fill();
  }

  function roundRect(x, y, w, h, r) {
    r = Math.min(r, w / 2, Math.abs(h) / 2);
    ctx.beginPath();
    ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
  }

  function draw() {
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    ctx.clearRect(0, 0, W, H);
    var bg = ctx.createLinearGradient(0, 0, 0, H);
    bg.addColorStop(0, '#eef7ef'); bg.addColorStop(1, '#dcefe0');
    ctx.fillStyle = bg; ctx.fillRect(0, 0, W, H);

    // ถนนเส้นหลัก (ลานจอดรถอยู่ทางใต้ นอกกรอบภาพ)
    rect(ROAD[0], ROAD[1], ROAD[2], ROAD[3], 0, '#d5dbe3');
    for (var m = ROAD[1] + 1; m < ROAD[1] + ROAD[3] - 2; m += 4) rect(ROAD_X - 0.12, m, 0.25, 2, 0, '#ffffff');
    // ทางเดินมีหลังคา
    WALKS.forEach(function (w) { rect(w[0], w[1], w[2], w[3], 0, '#e7e2d6'); });
    // อาคาร
    BUILDINGS.forEach(function (b) {
      box(b[0], b[1], -0.35, b[2], b[3], 0.35, '#f5f7fa', '#cfd7e3', '#bfc9d8');
    });
    // ห้องที่ไม่มีข้อมูล
    DECOR.forEach(function (o) { rect(o.r[0], o.r[1], o.r[2], o.r[3], 0, FLOOR[o.kind] || FLOOR.decor, 'rgba(15,42,92,.10)'); });
    // พื้นแต่ละจุด
    areas.forEach(function (a) {
      var f = FLOOR[a.L.kind] || '#eef2f8';
      rect(a.x, a.y, a.w, a.d, 0, f, 'rgba(15,42,92,.10)');
      // แถบสีสถานะที่ขอบหน้า (เห็นจากระยะไกล)
      if (a.n) rect(a.x, a.y + a.d - 0.35, a.w, 0.35, 0.01, COL[a.lv]);
    });

    // ทุกอย่างที่มีความสูง — เรียงจากหลังไปหน้า
    var items = statics.slice();
    people.forEach(function (p) {
      if (p.vis) items.push({ k: p.x + p.y, p: p });
    });
    areas.forEach(function (a) {
      if (a.L.room && a.n) items.push({ k: a.x + a.w / 2 + a.y + a.d - 0.8, doc: a });
    });
    items.sort(function (a, b) { return a.k - b.k; });
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      if (it.p) drawPerson(it.p); else if (it.doc) drawDoctor(it.doc); else it.draw();
    }

    // ป้ายชื่อจุด (ทับทุกอย่าง)
    var fs = Math.round(Math.max(12, Math.min(18, cam.k * 1.15)));
    if (cam.k >= 9) DECOR.forEach(function (o) {
      var p = P(o.r[0] + o.r[2] * 0.5, o.r[1] + o.r[3] * 0.5, 0.2);
      ctx.font = '600 ' + (fs - 1) + 'px Kanit, sans-serif';
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillStyle = 'rgba(71,85,105,.75)'; ctx.fillText(o.name, p[0], p[1]);
      ctx.textAlign = 'left';
    });
    drawLabels(fs);
    if (hover) drawHover(hover);
  }

  // ป้ายลอยเหนือผนังด้านหลังของแต่ละจุด (ไม่บังหุ่นที่นั่งอยู่ด้านหน้า)
  // ถ้าป้ายชนกัน เลื่อนป้ายที่อยู่ด้านหลังขึ้นไป แล้วลากเส้นชี้กลับลงมาที่จุด
  function drawLabels(fs) {
    var boxes = [];
    areas.forEach(function (a) {
      if (!a.n && !a.served && cam.k < 9) return;     // ซูมออกมาก: ป้ายเฉพาะจุดที่มีคน/มีการตรวจแล้ว
      var b = measureLabel(a, fs);
      b.a = a;
      if (a.L.room) {
        // ห้องตรวจ: ป้ายอยู่ทางซ้ายของห้อง (ขอบด้านใต้ = ซ้ายล่างบนจอ) ไม่ทับคนในโถงหน้าห้องตรวจ
        var q = P(a.x + a.w * 0.5, a.y + a.d, 0.6);
        b.side = true; b.ax = q[0]; b.ay = q[1];
        b.x = q[0] - b.w - 14; b.y = q[1] - b.h / 2;
      } else {
        var p = P(a.x + a.w * 0.5, a.y + 0.2, 3.0);
        b.ax = p[0]; b.ay = p[1];
        b.x = p[0] - b.w / 2; b.y = p[1] - b.h - 6;
      }
      boxes.push(b);
    });
    // วางจากหน้าไปหลัง (ป้ายด้านหน้าจออยู่กับที่ ป้ายด้านหลังหลบขึ้น)
    boxes.sort(function (p, q) { return q.ay - p.ay; });
    var placed = [];
    boxes.forEach(function (b) {
      for (var guard = 0; guard < 12; guard++) {
        var hit = null;
        for (var i = 0; i < placed.length; i++) {
          var o = placed[i];
          if (b.x < o.x + o.w + 4 && b.x + b.w + 4 > o.x && b.y < o.y + o.h + 3 && b.y + b.h + 3 > o.y) { hit = o; break; }
        }
        if (!hit) break;
        b.y = b.side ? hit.y + hit.h + 4 : hit.y - b.h - 4;   // ป้ายห้องตรวจหลบลง ป้ายอื่นหลบขึ้น
      }
      placed.push(b);
    });
    placed.forEach(function (b) {
      ctx.strokeStyle = 'rgba(15,42,92,.35)'; ctx.lineWidth = 1.2;
      ctx.beginPath();
      if (b.side) { ctx.moveTo(b.x + b.w, b.y + b.h / 2); ctx.lineTo(b.ax, b.ay); }
      else { ctx.moveTo(b.ax, b.y + b.h); ctx.lineTo(b.ax, b.ay); }
      ctx.stroke();
      ctx.fillStyle = 'rgba(15,42,92,.35)';
      ctx.beginPath(); ctx.arc(b.ax, b.ay, 2.2, 0, 7); ctx.fill();
      renderLabel(b, fs);
    });
  }

  function labelName(a) {
    // ห้องตรวจแคบ ป้ายชื่อเต็มทับกัน — ใช้ชื่อสั้นจนกว่าจะซูมเข้าใกล้
    return a.L.short && cam.k < 24 ? a.L.short
      : a.name.length > 18 && cam.k < 14 ? a.name.split(' / ')[0] : a.name;
  }

  function measureLabel(a, fs) {
    var name = labelName(a) + (a.served ? ' · ตรวจแล้ว ' + a.served : ''), cnt = a.n ? a.n + ' คน' : '';
    ctx.font = '600 ' + fs + 'px Kanit, sans-serif';
    var nw = ctx.measureText(name).width;
    ctx.font = '600 ' + (fs - 2) + 'px Kanit, sans-serif';
    var cw = cnt ? ctx.measureText(cnt).width + 14 : 0;
    var pad = 9, h = fs + 12;
    return { name: name, cnt: cnt, nw: nw, cw: cw, pad: pad, h: h, w: nw + pad * 2 + (cnt ? cw + 6 : 0) };
  }

  function renderLabel(b, fs) {
    var x0 = b.x, y0 = b.y, h = b.h, w = b.w;
    ctx.fillStyle = 'rgba(0,0,0,.14)'; roundRect(x0 + 1, y0 + 2, w, h, h / 2); ctx.fill();
    ctx.fillStyle = '#ffffff'; roundRect(x0, y0, w, h, h / 2); ctx.fill();
    ctx.textBaseline = 'middle';
    ctx.font = '600 ' + fs + 'px Kanit, sans-serif';
    ctx.fillStyle = '#0f2a5c'; ctx.fillText(b.name, x0 + b.pad, y0 + h / 2 + 1);
    if (b.cnt) {
      var bx = x0 + b.pad + b.nw + 6;
      ctx.fillStyle = COL[b.a.lv]; roundRect(bx, y0 + 4, b.cw, h - 8, (h - 8) / 2); ctx.fill();
      ctx.font = '600 ' + (fs - 2) + 'px Kanit, sans-serif';
      ctx.fillStyle = '#fff'; ctx.fillText(b.cnt, bx + 7, y0 + h / 2 + 1);
    }
  }

  // ------------------------------------------------------------------ ชี้ดูเวลารอรายคน
  // แสดงเฉพาะเวลา/จุดบริการ/สีเสื้อ — ไม่มีข้อมูลระบุตัวตน (API ไม่ได้ส่งมาตั้งแต่ต้น)
  function figureCenter(p) {
    var b = P(p.x, p.y, 0);
    return [b[0], b[1] - 0.85 * cam.k * 1.25 * ZK];
  }

  function pick(cx, cy) {
    var best = null, bd = Math.max(10, 0.5 * cam.k * 1.25);
    people.forEach(function (p) {
      if (!p.vis || p.area < 0 || p.leaving) return;
      var c = figureCenter(p), d = Math.hypot(c[0] - cx, c[1] - cy);
      if (d < bd || (best && Math.abs(d - bd) < 2 && p.x + p.y > best.x + best.y)) { bd = d; best = p; }
    });
    return best;
  }

  function drawHover(p) {
    if (!p.vis || p.area < 0) return;
    var b = P(p.x, p.y, 0), k = cam.k * 1.25;
    ctx.strokeStyle = '#0f2a5c'; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.ellipse(b[0], b[1], 0.45 * k, 0.22 * k, 0, 0, 7); ctx.stroke();
  }

  var tip = document.getElementById('tip');
  function showTip(p, cx, cy) {
    hover = p;
    if (!p) { tip.hidden = true; return; }
    tip.hidden = false;
    tip.dataset.x = cx; tip.dataset.y = cy;
    updateTip();
  }
  function updateTip() {
    var p = hover;
    if (!p) return;
    if (!p.vis || p.area < 0) { tip.hidden = true; hover = null; return; }
    var a = areas[p.area], w = state.T - p.enter, tot = state.T - p.total;
    var lv = level(w, a.target);
    tip.innerHTML = '<b>' + a.name + '</b>' +
      (a.nowait ? '<div>อยู่ห้องฉุกเฉิน · ไม่คิดเวลารอคิว</div>'
        : '<div class="w ' + lv + '">' + (MOOD[lv] ? MOOD[lv] + ' ' : '') + 'รอที่จุดนี้ ' + mins(w) + '</div>' +
          '<div>เป้าหมาย ≤ ' + a.target + ' นาที</div>') +
      '<div>อยู่ใน รพ. ' + mins(tot) + '</div>' +
      '<div class="u"><i style="background:' + (SHIRT[p.u] || SHIRT[5]) + '"></i>' + (URG_NAME[p.u] || 'ทั่วไป') +
      ' · ' + (p.g === 1 ? 'ชาย' : p.g === 2 ? 'หญิง' : 'ไม่ระบุเพศ') + '</div>';
    var cx = +tip.dataset.x, cy = +tip.dataset.y, tw = tip.offsetWidth, th = tip.offsetHeight;
    var x = cx + 16, y = cy + 16;
    if (x + tw > W - 8) x = cx - tw - 12;
    if (y + th > H - 8) y = cy - th - 12;
    tip.style.left = Math.max(8, x) + 'px'; tip.style.top = Math.max(8, y) + 'px';
  }

  // ------------------------------------------------------------------ กล้อง
  function bounds(only) {
    var xs = [], ys = [];
    (only || BUILDINGS).forEach(function (b) {
      [[b[0], b[1]], [b[0] + b[2], b[1]], [b[0], b[1] + b[3]], [b[0] + b[2], b[1] + b[3]]].forEach(function (c) {
        xs.push((c[0] - c[1]) * C30); ys.push((c[0] + c[1]) * S30);
      });
    });
    ys.push(-3 * ZK);
    return [Math.min.apply(0, xs), Math.min.apply(0, ys), Math.max.apply(0, xs), Math.max.apply(0, ys)];
  }

  function fit() {
    // จอมือถือแนวตั้ง: พอดีเฉพาะอาคาร OPD หลัก (อาคารอื่นลากดูได้) ไม่งั้นหุ่นเล็กเกินไป
    var mob = W < 900, b = bounds(mob && H > W ? [BUILDINGS[0]] : null);
    var padL = mob ? 10 : 40, padR = mob ? 10 : 330, padT = mob ? 120 : 210, padB = mob ? 130 : 60;
    var aw = W - padL - padR, ah = H - padT - padB;
    cam.k = Math.max(3, Math.min(aw / (b[2] - b[0]), ah / (b[3] - b[1])));
    cam.ox = padL + (aw - (b[2] - b[0]) * cam.k) / 2 - b[0] * cam.k;
    cam.oy = padT + (ah - (b[3] - b[1]) * cam.k) / 2 - b[1] * cam.k;
  }

  function zoomAt(f, px, py) {
    var nk = Math.max(3, Math.min(60, cam.k * f));
    f = nk / cam.k;
    cam.ox = px - (px - cam.ox) * f;
    cam.oy = py - (py - cam.oy) * f;
    cam.k = nk;
  }

  function focusArea(a) {
    var target = Math.max(cam.k, W < 900 ? 15 : 22);
    var from = { k: cam.k, ox: cam.ox, oy: cam.oy };
    var cx = W < 900 ? W / 2 : (W - 320) / 2 + 20, cy = H / 2 + 40;
    var sx = (a.cx - a.cy) * C30, sy = (a.cx + a.cy) * S30;
    var to = { k: target, ox: cx - sx * target, oy: cy - sy * target };
    var t0 = performance.now();
    (function anim() {
      var t = Math.min(1, (performance.now() - t0) / 450), e = t * (2 - t);
      cam.k = from.k + (to.k - from.k) * e;
      cam.ox = from.ox + (to.ox - from.ox) * e;
      cam.oy = from.oy + (to.oy - from.oy) * e;
      if (t < 1) requestAnimationFrame(anim);
    })();
  }

  function resize() {
    DPR = window.devicePixelRatio || 1;
    W = window.innerWidth; H = window.innerHeight;
    // แถบบนสูงไม่เท่ากันตามความกว้างจอ (ตัวเลขขึ้นบรรทัดใหม่บนมือถือ) — วางแผงไว้ใต้แถบเสมอ
    var top = document.querySelector('.bar').offsetHeight + 8;
    $('panel').style.top = top + 'px';
    if (W >= 900) document.querySelector('.ctrl').style.top = top + 'px';
    else document.querySelector('.ctrl').style.top = '';
    canvas.width = W * DPR; canvas.height = H * DPR;
    var r = tlc.getBoundingClientRect();
    tlc.width = r.width * DPR; tlc.height = r.height * DPR;
    fit();
  }

  // ลากแผนที่ / หมุนล้อซูม / บีบนิ้วซูม
  var ptrs = new Map(), pinch = null;
  var downAt = null;
  canvas.addEventListener('pointerdown', function (e) {
    downAt = [e.clientX, e.clientY];
    canvas.setPointerCapture(e.pointerId);
    ptrs.set(e.pointerId, [e.clientX, e.clientY]);
    canvas.classList.add('drag');
  });
  canvas.addEventListener('pointermove', function (e) {
    if (!ptrs.has(e.pointerId)) {
      if (e.pointerType === 'mouse') showTip(pick(e.clientX, e.clientY), e.clientX, e.clientY);
      return;
    }
    var prev = ptrs.get(e.pointerId);
    ptrs.set(e.pointerId, [e.clientX, e.clientY]);
    if (ptrs.size === 2) {
      var pts = Array.from(ptrs.values());
      var dd = Math.hypot(pts[0][0] - pts[1][0], pts[0][1] - pts[1][1]);
      var mx = (pts[0][0] + pts[1][0]) / 2, my = (pts[0][1] + pts[1][1]) / 2;
      if (pinch) zoomAt(dd / pinch, mx, my);
      pinch = dd;
      return;
    }
    cam.ox += e.clientX - prev[0]; cam.oy += e.clientY - prev[1];
  });
  function up(e) {
    // แตะ/คลิกโดยไม่ลาก = เลือกหุ่น (จอสัมผัสไม่มี hover)
    if (downAt && e.type === 'pointerup' && ptrs.size === 1 &&
        Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) < 6) {
      showTip(pick(e.clientX, e.clientY), e.clientX, e.clientY);
    }
    downAt = null;
    ptrs.delete(e.pointerId); pinch = null;
    if (!ptrs.size) canvas.classList.remove('drag');
  }
  canvas.addEventListener('pointerup', up);
  canvas.addEventListener('pointerleave', function (e) { if (e.pointerType === 'mouse' && !ptrs.size) showTip(null); });
  canvas.addEventListener('pointercancel', up);
  canvas.addEventListener('wheel', function (e) {
    e.preventDefault();
    zoomAt(e.deltaY < 0 ? 1.12 : 1 / 1.12, e.clientX, e.clientY);
  }, { passive: false });
  $('zIn').onclick = function () { zoomAt(1.25, W / 2, H / 2); };
  $('zOut').onclick = function () { zoomAt(0.8, W / 2, H / 2); };
  $('zFit').onclick = fit;
  $('zFull').onclick = function () {
    if (document.fullscreenElement) document.exitFullscreen();
    else if (document.documentElement.requestFullscreen) document.documentElement.requestFullscreen();
  };

  // ------------------------------------------------------------------ ไทม์ไลน์
  var DAY = 24 * 60, TL0 = 0, TL1 = DAY;

  // เวลาที่เข้าแต่ละจุด (เรียงแล้ว) — ใช้นับ "ตรวจแล้ว" ของห้องตรวจ ณ เวลาจำลอง
  function buildServed() {
    areas.forEach(function (a) { a.entries = []; });
    data.visits.forEach(function (v) {
      v.s.forEach(function (seg) { var a = areas[seg[1]]; if (a) a.entries.push(seg[0]); });
    });
    areas.forEach(function (a) { a.entries.sort(function (p, q) { return p - q; }); });
  }
  function servedAt(a, T) {
    var lo = 0, hi = a.entries.length;
    while (lo < hi) { var m = (lo + hi) >> 1; if (a.entries[m] <= T) lo = m + 1; else hi = m; }
    return lo;
  }

  function buildOcc() {
    buildServed();
    occ = new Float32Array(DAY + 1);
    data.visits.forEach(function (v) {
      var a = Math.max(0, Math.floor(v.s[0][0])), b = Math.min(DAY, Math.ceil(v.e));
      for (var t = a; t < b; t++) occ[t]++;
    });
    // ตั้งช่วงไทม์ไลน์ให้ครอบช่วงที่มีคนจริง (ปัดเป็นชั่วโมง) ไม่ให้กราฟแบนติดขอบ
    var first = -1, last = -1;
    for (var t = 0; t <= DAY; t++) if (occ[t] >= 2) { if (first < 0) first = t; last = t; }
    TL0 = first < 0 ? 6 * 60 : Math.max(0, Math.min(6 * 60, Math.floor(first / 60) * 60));
    TL1 = last < 0 ? 20 * 60 : Math.min(24 * 60, Math.max(17 * 60, Math.ceil(last / 60) * 60));
  }

  function drawTimeline() {
    var w = tlc.width / DPR, h = tlc.height / DPR, c = tctx;
    c.setTransform(DPR, 0, 0, DPR, 0, 0);
    c.clearRect(0, 0, w, h);
    var axis = 14, ch = h - axis;
    c.fillStyle = '#f1f5fb'; c.fillRect(0, 0, w, ch);
    if (occ) {
      var mx = 1;
      for (var t = TL0; t <= TL1; t++) mx = Math.max(mx, occ[t]);
      var X = function (t) { return (t - TL0) / (TL1 - TL0) * w; };
      c.beginPath(); c.moveTo(0, ch);
      for (var t2 = TL0; t2 <= TL1; t2 += 2) c.lineTo(X(t2), ch - occ[t2] / mx * (ch - 6));
      c.lineTo(w, ch); c.closePath();
      var g = c.createLinearGradient(0, 0, 0, ch);
      g.addColorStop(0, 'rgba(21,101,216,.55)'); g.addColorStop(1, 'rgba(21,101,216,.08)');
      c.fillStyle = g; c.fill();
      // ส่วนที่ผ่านไปแล้ว
      var xn = X(Math.min(state.T, TL1));
      c.fillStyle = 'rgba(15,42,92,.08)'; c.fillRect(0, 0, xn, ch);
      // ชั่วโมง
      c.fillStyle = '#6b7280'; c.font = '10px Sarabun, sans-serif'; c.textAlign = 'center'; c.textBaseline = 'top';
      var stepH = (TL1 - TL0) / 60 > 12 ? 2 : 1;
      for (var hh = Math.ceil(TL0 / 60); hh * 60 <= TL1; hh += stepH) {
        var x = X(hh * 60);
        c.fillRect(x, ch, 1, 3);
        c.fillText(String(hh).padStart(2, '0'), Math.min(w - 6, Math.max(6, x)), ch + 3);
      }
      // ตำแหน่งเวลาปัจจุบัน
      c.fillStyle = state.live ? '#059669' : '#f59e0b';
      c.fillRect(xn - 1, 0, 2, ch);
      c.beginPath(); c.arc(xn, 4, 4, 0, 7); c.fill();
      c.fillStyle = '#0f2a5c'; c.font = '600 10px Kanit, sans-serif'; c.textAlign = 'left'; c.textBaseline = 'top';
      c.fillText('สูงสุด ' + mx + ' คน', 4, 2);
    }
  }

  function scrub(e) {
    var r = tlc.getBoundingClientRect();
    var f = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    setLive(false);
    state.T = Math.min(TL0 + f * (TL1 - TL0), maxT());
  }
  var scrubbing = false;
  tlc.addEventListener('pointerdown', function (e) { scrubbing = true; tlc.setPointerCapture(e.pointerId); scrub(e); });
  tlc.addEventListener('pointermove', function (e) { if (scrubbing) scrub(e); });
  tlc.addEventListener('pointerup', function () { scrubbing = false; });

  // ------------------------------------------------------------------ ตัวควบคุม
  function nowMin() {
    var d = new Date();
    return d.getHours() * 60 + d.getMinutes() + d.getSeconds() / 60;
  }
  function maxT() { return state.date === CFG.today ? nowMin() : 24 * 60 - 0.01; }
  function hhmm(t) {
    t = Math.max(0, Math.floor(t));
    return String(Math.floor(t / 60) % 24).padStart(2, '0') + ':' + String(t % 60).padStart(2, '0');
  }
  function mins(m) {
    m = Math.round(m);
    return m >= 60 ? Math.floor(m / 60) + ' ชม. ' + (m % 60) + ' นาที' : m + ' นาที';
  }

  function setLive(on) {
    state.live = on && state.date === CFG.today;
    $('btnLive').classList.toggle('on', state.live);
    $('btnLive').style.display = state.date === CFG.today ? '' : 'none';
    $('mode').textContent = state.live ? 'LIVE' : 'REPLAY';
    $('modeDot').classList.toggle('replay', !state.live);
    if (state.live) state.playing = true;
    syncButtons();
  }
  function syncButtons() {
    $('btnPlay').innerHTML = state.playing && !state.live ? '<span class="material-symbols-outlined" style="font-size:16px;vertical-align:-2px">pause</span>' : '<span class="material-symbols-outlined" style="font-size:16px;vertical-align:-2px">play_arrow</span>';
    $('btnPlay').classList.toggle('on', state.playing && !state.live);
    document.querySelectorAll('#speeds button').forEach(function (b) {
      b.classList.toggle('on', !state.live && +b.dataset.s === state.speed);
    });
  }
  $('btnLive').onclick = function () { setLive(true); state.T = nowMin(); };
  $('btnPlay').onclick = function () {
    if (state.live) { setLive(false); state.playing = true; }
    else state.playing = !state.playing;
    if (state.playing && state.T >= maxT() - 0.05) state.T = TL0;
    syncButtons();
  };
  document.querySelectorAll('#speeds button').forEach(function (b) {
    b.onclick = function () {
      state.speed = +b.dataset.s;
      if (state.live) { setLive(false); }
      state.playing = true;
      syncButtons();
    };
  });
  $('date').onchange = function () {
    var v = this.value;
    if (!v || v > CFG.today) { this.value = state.date; return; }
    state.date = v;
    try { history.replaceState(null, '', '?d=' + v + (CFG.tv ? '&tv=1' : '')); } catch (e) {}
    load(true);
  };
  $('fold').onclick = function () {
    var p = $('panel'); p.classList.toggle('folded');
    this.textContent = p.classList.contains('folded') ? '▸' : '▾';
  };

  // ------------------------------------------------------------------ แผงจุดบริการ
  var rowEls = [];
  function buildPanel() {
    var box = $('rows'); box.innerHTML = '';
    rowEls = areas.map(function (a) {
      var el = document.createElement('div');
      el.className = 'arow';
      el.innerHTML = '<span class="n"></span><span class="v"></span><span class="trk"><i></i></span>';
      el.querySelector('.n').textContent = a.name;
      el.title = 'เป้าหมาย ≤ ' + a.target + ' นาที — คลิกเพื่อซูมไปที่จุดนี้';
      el.onclick = function () {
        focusArea(a);
        if (W < 900) { $('panel').classList.add('folded'); $('fold').textContent = '▸'; }
      };
      box.appendChild(el);
      return el;
    });
  }
  function updatePanel() {
    areas.forEach(function (a, i) {
      var el = rowEls[i];
      el.classList.toggle('zero', !a.n && !a.served);
      var sv = a.served ? ' · <span class="sv">ตรวจแล้ว ' + a.served + '</span>' : '';
      el.querySelector('.v').innerHTML = !a.n ? (a.served ? '<span class="sv">ตรวจแล้ว ' + a.served + '</span>' : '–')
        : a.nowait ? '<b>' + a.n + '</b> คน'
        : '<b>' + a.n + '</b>/' + a.cap + ' · <span style="color:' + COL[a.lv] + '">' + Math.round(a.avg) + ' น.</span>' + sv;
      // แถบ = ความหนาแน่น (คน ÷ ที่นั่ง) สีตามเกณฑ์ ว่าง/ปานกลาง/แน่น
      var bar = el.querySelector('i');
      bar.style.width = Math.min(100, a.dens * 100) + '%';
      bar.style.background = a.nowait ? '#94a3b8' : DENS[a.dl].c;
      el.title = a.nowait ? 'ห้องฉุกเฉิน — ไม่คิดเวลารอ/ความหนาแน่น'
        : 'ความหนาแน่น ' + Math.round(a.dens * 100) + '% (' + DENS[a.dl].t + ') · รอค่ากลาง ' +
          Math.round(a.avg) + ' นาที (เป้าหมาย ≤ ' + a.target + ') — คลิกเพื่อซูมไปที่จุดนี้';
    });
    var s = state.stats || {};
    var remoteN = 0;
    (data.remote || []).forEach(function (t) { if (t <= state.T) remoteN++; });
    var notes = [];
    if (data.remote && data.remote.length) notes.push('Telemed/PCU นอก รพ. ' + remoteN + ' ราย');
    if (data.admitted) notes.push('Admit ' + data.admitted + ' ราย (ทั้งวัน)');
    $('remote').textContent = notes.length ? notes.join(' · ') + ' — ไม่แสดงในผัง' : '';
    $('stArrived').textContent = s.arrived || 0;
    $('stIn').textContent = s.inside || 0;
    $('stLeft').textContent = s.left || 0;
    var dl = densLevel(s.dens || 0);
    $('stDens').textContent = Math.round((s.dens || 0) * 100) + '% ' + DENS[dl].t;
    $('stDens').style.color = dl === 'full' ? '#fca5a5' : dl === 'mid' ? '#fcd34d' : '#86efac';
    $('stWorst').textContent = s.worst ? mins(s.worst.w) + ' · ' + s.worst.a.name.split(' / ')[0] : '–';
    $('stWorst').style.color = s.worst ? (s.worst.w >= s.worst.a.target * 1.5 ? '#fca5a5' : '') : '';
  }

  // ------------------------------------------------------------------ โหลดข้อมูล
  var loading = false, lastFetch = 0;
  function load(reset) {
    if (loading) return;
    loading = true;
    $('note').textContent = 'กำลังโหลดข้อมูล…';
    fetch(CFG.api + '?d=' + encodeURIComponent(state.date), { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (j) {
        var first = !data;
        data = j; lastFetch = Date.now();
        if (first || reset) {
          buildAreas(); buildStatics(); buildPanel();
          people.clear();
          state.lastT = null;
          buildOcc();
          var tq = /^(\d{1,2}):(\d{2})$/.exec(first ? CFG.t || '' : '');
          if (tq) { setLive(false); state.T = Math.min(+tq[1] * 60 + +tq[2], maxT()); state.playing = true; }
          else if (state.date === CFG.today) { setLive(true); state.T = nowMin(); }
          else { setLive(false); state.T = TL0 + 30; state.playing = true; }
          resize();
        } else {
          buildOcc();
        }
        $('dateLabel').textContent = j.date_label;
        $('note').textContent = 'ผู้ป่วยในผัง ' + j.visits.length + ' ราย · ข้อมูล ณ ' + j.generated +
          (j.is_today ? ' (รีเฟรชทุก 1 นาที)' : '') + ' · ลากกราฟเพื่อเลื่อนเวลา';
      })
      .catch(function (e) { $('note').textContent = 'โหลดข้อมูลไม่สำเร็จ (' + e.message + ') — ลองใหม่อีกครั้ง'; })
      .then(function () { loading = false; });
  }

  // ------------------------------------------------------------------ วนลูป
  var prev = performance.now(), panelAt = 0;
  function frame(now) {
    var dt = Math.min(0.1, (now - prev) / 1000);
    prev = now;
    if (data) {
      if (state.live) {
        state.T = nowMin();
        if (Date.now() - lastFetch > 60000) load(false);
      } else if (state.playing) {
        state.T += dt * state.speed / 60;
        var mt = maxT();
        if (state.T >= mt) {
          state.T = mt;
          if (state.date === CFG.today) setLive(true); else { state.playing = false; syncButtons(); }
        }
      }
      step(dt);
      draw();
      if (now - panelAt > 250) { updatePanel(); drawTimeline(); updateTip(); panelAt = now; }
      $('clock').textContent = hhmm(state.T);
    }
    requestAnimationFrame(frame);
  }

  if (CFG.tv) document.body.classList.add('tv');
  if (window.innerWidth < 900) { $('panel').classList.add('folded'); $('fold').textContent = '▸'; }
  window.addEventListener('resize', resize);
  resize();
  load(true);
  requestAnimationFrame(frame);
})();
