const state = { positions: [], market: 'ALL', errors: [], strategy: null };
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number = (value, digits = 2) => value == null || value === '' || !Number.isFinite(Number(value)) ? '—' : Number(value).toLocaleString('zh-CN', {minimumFractionDigits: digits, maximumFractionDigits: digits});

async function api(url, timeout = 18000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(url, {signal: controller.signal});
    const body = await response.json();
    if (!body.ok) throw new Error(body.error || '数据源没有返回结果');
    return body.data;
  } finally { clearTimeout(timer); }
}

function marketName(market) { return {HK:'港股', CN:'A股', US:'美股'}[market] || market; }
function friendlyError(market, message) {
  const text = String(message || '连接失败');
  if (/多个.*账户|multiple/i.test(text)) return `检测到多个${marketName(market)}账户，请在配置中指定账户`;
  if (/environment param|trd_env/i.test(text)) return `${marketName(market)}账户环境不匹配，请检查实盘/模拟配置`;
  if (/OpenD|port|socket|连接|connect/i.test(text)) return '富途 OpenD 当前不可用';
  if (/timeout|abort/i.test(text)) return '读取超时，请稍后刷新';
  return `${marketName(market)}账户暂时无法读取`;
}
function positionMarket(row) {
  const code = String(row.code || '');
  return code.startsWith('US.') ? 'US' : code.startsWith('SH.') || code.startsWith('SZ.') ? 'CN' : 'HK';
}
function pnlValue(row) { return Number(row.pl_val ?? row.unrealized_pnl ?? 0); }

function renderAccounts(results) {
  $('account_band').innerHTML = ['HK','CN','US'].map(market => {
    const result = results[market];
    const count = result?.items?.length || 0;
    const unavailable = result?.error;
    return `<button data-market="${market}" class="account-cell ${unavailable ? 'unavailable' : ''}">
      <span>${marketName(market)}</span><strong>${unavailable ? '暂不可用' : count + ' 项持仓'}</strong><small>${unavailable ? esc(friendlyError(market, result.error)) : market === 'US' ? '老虎证券 · 只读' : '富途 · 只读'}</small>
    </button>`;
  }).join('');
  document.querySelectorAll('.account-cell').forEach(button => button.onclick = () => setMarket(button.dataset.market));
}

function renderPositions() {
  const rows = state.market === 'ALL' ? state.positions : state.positions.filter(row => positionMarket(row) === state.market);
  $('position_rows').innerHTML = rows.length ? rows
    .sort((a,b) => Math.abs(pnlValue(b)) - Math.abs(pnlValue(a)))
    .map(row => {
      const market = positionMarket(row), pnl = pnlValue(row), ratio = Number(row.pl_ratio ?? 0);
      const action = pnl < 0 ? '检查失效条件' : '继续持有观察';
      return `<tr>
        <td><b>${esc(row.stock_name || row.name || row.code)}</b><small>${esc(row.code)}</small></td>
        <td><span class="market-tag ${market.toLowerCase()}">${marketName(market)}</span></td>
        <td class="num">${number(row.qty, 0)}</td><td class="num">${number(row.cost_price)}</td><td class="num">${number(row.nominal_price)}</td>
        <td class="num">${number(row.market_val)}</td><td class="num ${pnl > 0 ? 'profit' : pnl < 0 ? 'loss' : ''}">${number(pnl)}<small>${ratio ? number(ratio * (Math.abs(ratio) < 2 ? 100 : 1), 1) + '%' : ''}</small></td>
        <td><a class="row-action" href="analyze.html?code=${encodeURIComponent(row.code)}">${action}</a></td>
      </tr>`;
    }).join('') : `<tr><td colspan="8" class="empty">${state.errors.length ? '当前筛选没有可用持仓；请查看上方账户状态。' : '当前账户没有持仓。'}</td></tr>`;
}

function setMarket(market) {
  state.market = market;
  document.querySelectorAll('.market-filter button').forEach(button => button.classList.toggle('active', button.dataset.market === market));
  renderPositions();
  document.querySelector('#portfolio').scrollIntoView({behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
}

function renderActions(data) {
  const queue = data?.action_queue || [];
  state.strategy = data;
  $('action_rows').innerHTML = queue.length ? queue.slice(0, 6).map(item => `<a href="${item.code ? 'analyze.html?code=' + encodeURIComponent(item.code) : 'strategy-center.html'}">
    <span class="priority ${esc(String(item.level || item.priority || 'watch').toLowerCase())}">${esc(item.level_name || item.level || '关注')}</span>
    <span><b>${esc(item.name || item.title || item.code || '组合任务')}</b><small>${esc(item.reason || item.detail || item.action || '')}</small></span>
    <strong>${esc(item.action || '查看')}</strong>
  </a>`).join('') : '<div class="calm-state"><b>今天没有必须执行的交易动作</b><span>保持现有仓位，等待满足门控条件的信号。</span></div>';
  const must = queue.filter(x => /must|urgent|sell|必须|止损/i.test(`${x.level} ${x.action}`)).length;
  const opp = queue.filter(x => /opportunity|buy|review|机会|买入/i.test(`${x.level} ${x.action}`)).length;
  $('must_count').textContent = must;
  $('opp_count').textContent = opp;
  $('watch_count').textContent = Math.max(0, queue.length - must - opp);
  $('today_verdict').textContent = must ? `先处理 ${must} 项风险` : opp ? `评估 ${opp} 个机会` : '今天不必强行交易';
  $('today_reason').textContent = must ? '风险动作永远排在新机会之前。' : opp ? '只有证据和仓位边界同时通过才考虑执行。' : '没有正式信号时，现金也是仓位。';
}

async function load() {
  $('refresh').disabled = true;
  state.errors = [];
  const marketRequests = Object.fromEntries(await Promise.all(['HK','CN','US'].map(async market => {
    try { return [market, await api('/api/positions?market=' + market)]; }
    catch (error) { state.errors.push(`${marketName(market)}：${error.message}`); return [market, {items:[], error:error.message}]; }
  })));
  state.positions = Object.values(marketRequests).flatMap(x => x.items || []);
  renderAccounts(marketRequests); renderPositions();
  try { renderActions(await api('/strategy-center/status', 25000)); }
  catch (error) { state.errors.push('今日策略：' + error.message); renderActions(null); }
  $('error_count').textContent = state.errors.length;
  $('source_state').classList.toggle('warning', state.errors.length > 0);
  $('source_state').querySelector('span').textContent = state.errors.length ? `${state.errors.length} 个数据源需检查` : '数据源已同步';
  $('as_of').textContent = `组合更新时间 ${new Date().toLocaleString('zh-CN', {hour12:false})} · 共 ${state.positions.length} 项持仓`;
  $('refresh').disabled = false;
}

document.querySelectorAll('.market-filter button').forEach(button => button.onclick = () => setMarket(button.dataset.market));
$('refresh').onclick = load;
load();
