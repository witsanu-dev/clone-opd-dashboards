/**
 * live_dashboard.js — ระบบอัปเดตข้อมูล OPD Dashboard อัตโนมัติแบบ Real-time (ไม่ต้อง Reload หน้า)
 *
 * ทำงานเฉพาะเมื่อดูข้อมูล "วันนี้" (isToday = 1)
 * - ดึงข้อมูล Snapshot จาก /api/live ทุก 45 วินาที
 * - อัปเดตเฉพาะตัวเลขและสถานะคิวสดแบบ Micro-patching ไม่กระตุก ไม่ขัดจังหวะผู้ใช้
 * - หยุดทำงานอัตโนมัติเมื่อผู้ใช้สลับไปแท็บอื่น (Page Visibility API) เพื่อไม่เปลือง DB Connection
 * - ผู้ใช้สามารถกดสลับเปิด/ปิด Real-time ได้บนแถบ Toolbar
 */
(function () {
  'use strict';

  var isToday = document.body.dataset.isToday === '1';
  if (!isToday) return; // ทำงานเฉพาะวันที่ดูข้อมูลวันนี้

  var INTERVAL = 45; // วินาที
  var timer = INTERVAL;
  var isPaused = false;
  var intervalId = null;

  var bar = document.querySelector('.toolbar');
  if (!bar) return;

  var stamp = bar.querySelector('.stamp');
  if (stamp) stamp.id = 'live-stamp';

  // สร้างปุ่ม Live Pill Indicator บน Toolbar
  var livePill = document.createElement('span');
  livePill.className = 'live-pill';
  livePill.id = 'live-toggle';
  livePill.title = 'คลิกเพื่อหยุด/เริ่มการอัปเดตอัตโนมัติแบบ Real-time';
  livePill.innerHTML = '<span class="live-dot"></span> Real-time <span id="live-timer">' + INTERVAL + '</span>s';

  if (stamp) {
    bar.insertBefore(livePill, stamp);
  } else {
    bar.appendChild(livePill);
  }

  // อ่านสถานะที่ผู้ใช้เคยเลือกไว้ใน Session
  try {
    if (sessionStorage.getItem('opd_live_paused') === '1') {
      isPaused = true;
      livePill.classList.add('paused');
      livePill.innerHTML = '<span class="live-dot"></span> หยุด Real-time (คลิกเพื่อเริ่ม)';
    }
  } catch (e) {}

  // เมื่อผู้ใช้คลิกปุ่ม Live Pill
  livePill.addEventListener('click', function () {
    isPaused = !isPaused;
    if (isPaused) {
      livePill.classList.add('paused');
      livePill.innerHTML = '<span class="live-dot"></span> หยุด Real-time (คลิกเพื่อเริ่ม)';
      try { sessionStorage.setItem('opd_live_paused', '1'); } catch (e) {}
    } else {
      livePill.classList.remove('paused');
      timer = INTERVAL;
      livePill.innerHTML = '<span class="live-dot"></span> Real-time <span id="live-timer">' + timer + '</span>s';
      try { sessionStorage.removeItem('opd_live_paused'); } catch (e) {}
      fetchLive(); // ดึงทันทีเมื่อกดเปิด
    }
  });

  // หยุดทำงานเมื่อแท็บไม่อยู่หน้าจอ (Page Visibility API)
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) {
      // หน้าต่างถูกย่อหรือสลับแท็บ → พักไว้ ไม่ยิง DB
    } else if (!isPaused) {
      // สลับกลับมา → ดึงทันทีถ้าเกินเวลา
      fetchLive();
    }
  });

  // ตัวนับถอยหลังทุก 1 วินาที
  intervalId = setInterval(function () {
    if (isPaused || document.hidden) return;
    timer--;
    var timerEl = document.getElementById('live-timer');
    if (timerEl) timerEl.textContent = timer;

    if (timer <= 0) {
      timer = INTERVAL;
      if (timerEl) timerEl.textContent = timer;
      fetchLive();
    }
  }, 1000);

  function flash(el) {
    if (!el) return;
    el.classList.remove('updated-flash');
    void el.offsetWidth; // บังคับ reflow เพื่อรันแอนิเมชันใหม่
    el.classList.add('updated-flash');
  }

  function setText(id, text) {
    var el = document.getElementById(id);
    if (!el) return;
    if (el.textContent !== String(text)) {
      el.textContent = text;
      flash(el);
    }
  }

  // ดึงข้อมูล Snapshot จาก /api/live (รองรับ ?dep=... ถ้าอยู่ในหน้าแดชบอร์ดรายแผนก)
  function fetchLive() {
    var dep = document.body.dataset.dep || '';
    var url = '/api/live' + (dep ? ('?dep=' + encodeURIComponent(dep)) : '');
    fetch(url, { cache: 'no-store' })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        if (!data || data.status !== 'ok') return;

        // 1. อัปเดตตราประทับเวลา
        if (data.generated_at) {
          var stampEl = document.getElementById('live-stamp');
          if (stampEl) stampEl.textContent = 'ข้อมูล ณ ' + data.generated_at;

          var heroMeta = document.querySelector('.hero .meta');
          if (heroMeta) {
            heroMeta.innerHTML = '<b>วันนี้</b><br>ข้อมูลจาก HOSxP · ณ ' + data.generated_at;
          }
        }

        // 2. อัปเดต KPI ภาพรวม
        if (typeof data.total_today === 'number') {
          setText('kpi-total', data.total_today.toLocaleString());
        }
        if (typeof data.new_today === 'number') {
          setText('kpi-new', data.new_today.toLocaleString());
        }
        if (typeof data.old_today === 'number') {
          setText('kpi-old', data.old_today.toLocaleString());
        }
        if (data.avg_total !== null && data.avg_total !== undefined) {
          setText('kpi-total-time', data.avg_total + ' นาที');
        }
        if (data.avg_wait_dr !== null && data.avg_wait_dr !== undefined) {
          setText('kpi-wait-dr', data.avg_wait_dr + ' นาที');
        }
        if (data.done_120_pct !== null && data.done_120_pct !== undefined) {
          setText('kpi-done-pct', data.done_120_pct + '%');
        }

        // 3. อัปเดตสถานะคิวสด (Live Queue Backlog)
        setText('live-active', (data.active || 0).toLocaleString());
        setText('live-wait-dr', (data.wait_dr || 0).toLocaleString());
        setText('live-in-dr', (data.in_dr || 0).toLocaleString());
        setText('live-wait-rx', (data.wait_rx || 0).toLocaleString());
        setText('live-finished', (data.finished || 0).toLocaleString());
      })
      .catch(function (err) {
        console.warn('[OPD Live Dashboard] update skipped:', err.message);
      });
  }

  // เรียกครั้งแรกทันทีหลังโหลดหน้าเสร็จ 5 วินาที เพื่อซิงค์คิวปัจจุบัน
  setTimeout(fetchLive, 5000);
})();
