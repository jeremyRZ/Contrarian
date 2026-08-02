/* ============================================================
   Contrarian 共享脚本
   - 通用工具：$ / cls / pill / checkConn
   - 下钻跳转：openAnalyze(code) -> analyze.html?code=
   - 导航激活态：由各页 <body data-page="xxx"> 控制
   ============================================================ */
const $ = (id) => document.getElementById(id);
const cls = (v) => (v >= 0) ? 'up' : 'down';
const pill = (s, clsname) => `<span class="pill ${clsname}">${s}</span>`;

// 数字格式化：null/NaN/Inf → '—'，否则保留 d 位小数（消除浮点长尾如 0.011680000000000001）
function fmt(v, d = 2) {
  if (v === null || v === undefined || v === '' || (typeof v === 'number' && !isFinite(v))) return '—';
  const n = Number(v);
  if (isNaN(n)) return (v === '—' ? '—' : v);
  return n.toFixed(d);
}

// 全屏加载遮罩：扫描/读取耗时操作时给用户明确反馈
function showLoading(msg) {
  let el = document.getElementById('__loading');
  if (!el) {
    el = document.createElement('div');
    el.id = '__loading';
    el.innerHTML = '<div class="loading-box"><div class="spinner"></div><div class="loading-msg"></div></div>';
    document.body.appendChild(el);
  }
  el.querySelector('.loading-msg').textContent = msg || '加载中…';
  el.style.display = 'flex';
}
function hideLoading() {
  const el = document.getElementById('__loading');
  if (el) el.style.display = 'none';
}

async function checkConn() {
  const el = $('conn');
  if (!el) return;
  try {
    const r = await fetch('/health');
    const j = await r.json();
    if (j.connected) { el.className = 'status ok'; el.textContent = '富途已连接'; }
    else { el.className = 'status bad'; el.textContent = '富途未连接'; }
  } catch (e) { el.className = 'status bad'; el.textContent = '后端不可达'; }
}
checkConn();
setInterval(checkConn, 15000);

// 错杀猎手列表点击股票 -> 新标签页打开单票深度页并带入代码（保留原页面）
function openAnalyze(code) {
  window.open('analyze.html?code=' + encodeURIComponent(code), '_blank');
}

// 导航激活态：根据 body 的 data-page 高亮对应链接
(function initNav() {
  const page = document.body.dataset.page;
  if (!page) return;
  document.querySelectorAll('.topnav .links a').forEach(a => {
    if (a.dataset.page === page) a.classList.add('active');
  });
})();

// 首屏入场：.page-head 与 .card 轻微淡入上移，stagger 55ms（克制、不挡交互）
// 尊重 prefers-reduced-motion：用户要求减少动效时直接跳过
(function revealOnLoad() {
  function run() {
    const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduce) return;
    const items = document.querySelectorAll('.page-head, .card');
    if (!items.length) return;
    items.forEach((el, i) => {
      el.classList.add('reveal');
      el.style.animationDelay = Math.min(i * 55, 360) + 'ms';
    });
    // 双 rAF 确保起始样式已应用，再触发 rise 动画
    requestAnimationFrame(() => requestAnimationFrame(() => {
      items.forEach(el => el.classList.add('in'));
    }));
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', run);
  } else {
    run();
  }
})();
