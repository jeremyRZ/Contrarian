/* ============================================================
   Contrarian 共享脚本
   - 通用工具：$ / cls / pill / checkConn
   - 下钻跳转：openAnalyze(code) -> analyze.html?code=
   - 导航激活态：由各页 <body data-page="xxx"> 控制
   ============================================================ */
const $ = (id) => document.getElementById(id);
const cls = (v) => (v >= 0) ? 'up' : 'down';
const pill = (s, clsname) => `<span class="pill ${clsname}">${s}</span>`;

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

// 错杀猎手列表点击股票 -> 跳转到单票深度页并带入代码
function openAnalyze(code) {
  window.location.href = 'analyze.html?code=' + encodeURIComponent(code);
}

// 导航激活态：根据 body 的 data-page 高亮对应链接
(function initNav() {
  const page = document.body.dataset.page;
  if (!page) return;
  document.querySelectorAll('.topnav .links a').forEach(a => {
    if (a.dataset.page === page) a.classList.add('active');
  });
})();
