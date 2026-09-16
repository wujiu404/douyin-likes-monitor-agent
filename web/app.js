/* 抖音点赞监控 · 前端控制台
 *
 * 零构建：原生 JS，无依赖、无打包步骤。后端用 StaticFiles 直接托管这个目录。
 * 数据全部来自 /api/*，与后端的分层一一对应：
 *   总览 / 账号 / 快照 / 增量告警 / 评论确认 / 轮次台账 / 配置表
 */

const $ = (sel, root = document) => root.querySelector(sel);
const $all = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const TITLES = {
  overview: ['总览', '一轮扫描产生什么，这里一目了然'],
  accounts: ['监控账号', '扫描谁的作品 —— 停用后下一轮就不会被采集'],
  videos:   ['视频快照', '每次扫描追加一行，保留历史轨迹（不覆盖）'],
  deltas:   ['增量与告警', '本次点赞 − 上一次快照，超过阈值即告警'],
  reviews:  ['评论确认', '命中关键词的评论与拟回复 —— 必须人工确认才落终态'],
  rounds:   ['扫描台账', '每轮扫描的汇总，比翻日志直观'],
  config:   ['配置表', '控制面：改完下一轮调度立即生效，不用重启'],
};

/* ---------------------------------------------------------------- 工具 */

async function api(path, opts = {}) {
  const res = await fetch('/api' + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (_) { data = text; }
  if (!res.ok) throw new Error((data && data.detail) || `HTTP ${res.status}`);
  return data;
}

function toast(msg, kind = '') {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast show ' + kind;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.className = 'toast ' + kind; }, 2800);
}

const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const nfmt = (n) => Number(n ?? 0).toLocaleString('zh-CN');

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return esc(iso);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function stat(label, value, cls = '') {
  return `<div class="stat ${cls}"><div class="label">${esc(label)}</div><div class="num">${value}</div></div>`;
}

function table(headers, rows, emptyText = '暂无数据') {
  if (!rows.length) return `<div class="empty">${esc(emptyText)}</div>`;
  const th = headers.map((h) => `<th class="${h.num ? 'num' : ''}">${esc(h.label)}</th>`).join('');
  const tr = rows.map((r) => '<tr>' + headers.map((h) => {
    const v = h.render ? h.render(r) : esc(r[h.key]);
    return `<td class="${h.num ? 'num' : ''} ${h.wrap ? 'wrap' : ''}">${v}</td>`;
  }).join('') + '</tr>').join('');
  return `<div class="table-wrap"><table><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table></div>`;
}

/* ---------------------------------------------------------------- 视图 */

async function viewOverview() {
  const s = await api('/stats');
  const last = s.last_round;
  const cfg = s.config || {};

  const lastCard = last ? `
    <div class="card">
      <h2>最近一轮 <span class="hint">${esc(last.run_id)}</span></h2>
      <div class="stats">
        ${stat('账号', nfmt(last.account_count))}
        ${stat('视频', nfmt(last.video_count))}
        ${stat('告警', nfmt(last.alert_count), last.alert_count ? 'hl' : '')}
        ${stat('错误', nfmt(last.error_count), last.error_count ? 'hl' : 'ok')}
      </div>
      <p class="muted" style="margin:10px 0 0;font-size:12.5px">
        触发方式 <b>${esc(last.trigger_type)}</b>　
        数据来源 <b>${esc(last.source || '—')}</b>　
        开始 ${fmtTime(last.started_at)}　结束 ${fmtTime(last.finished_at)}
        ${last.note ? '<br>备注：' + esc(last.note) : ''}
      </p>
    </div>` : '<div class="card"><div class="empty">还没跑过扫描，点右上角「手动扫描」</div></div>';

  return `
    <div class="stats">
      ${stat('累计轮次', nfmt(s.rounds))}
      ${stat('监控账号', nfmt(s.accounts))}
      ${stat('快照记录', nfmt(s.snapshots))}
      ${stat('告警命中', nfmt(s.alerts), s.alerts ? 'hl' : '')}
      ${stat('待确认评论', nfmt(s.pending_comments), s.pending_comments ? 'hl' : 'ok')}
    </div>
    ${lastCard}
    <div class="card">
      <h2>当前控制面配置 <span class="hint">来自《配置表》，改完下一轮生效</span></h2>
      <div class="table-wrap"><table><tbody>
        <tr><td class="muted">扫描模式</td><td><span class="tag blue">${esc(cfg.scan_mode)}</span></td>
            <td class="muted">阈值</td><td><b>${esc(cfg.threshold)}</b></td></tr>
        <tr><td class="muted">扫描时点 / 间隔</td>
            <td>${esc(cfg.scan_mode === 'interval' ? cfg.scan_interval_minutes + ' 分钟' : cfg.scan_cron_hours)}</td>
            <td class="muted">回看天数</td><td>${esc(cfg.lookback_days)}</td></tr>
        <tr><td class="muted">评论关键词</td><td colspan="3">${esc(cfg.comment_keywords)}</td></tr>
      </tbody></table></div>
    </div>`;
}

async function viewAccounts() {
  const { items } = await api('/accounts?only_enabled=false');
  const rows = items.map((a) => ({
    ...a,
    status: a.enabled ? '<span class="tag green">启用</span>' : '<span class="tag gray">停用</span>',
    act: `<button class="btn sm no" data-del="${esc(a.sec_uid)}">删除</button>`,
  }));

  return `
    <div class="card">
      <h2>新增 / 更新账号</h2>
      <div class="form-row">
        <input id="accName" placeholder="账号名（如 测试账号 C）">
        <input id="accUid" placeholder="sec_uid" style="min-width:280px">
        <input id="accHome" placeholder="主页链接（可空）" style="min-width:220px">
        <input id="accNote" placeholder="备注（可空）">
        <button class="btn primary" id="accAdd">保存</button>
      </div>
      <p class="muted" style="font-size:12.5px;margin:0">
        同一个 sec_uid 重复保存会更新而不是新增。合规说明：只读公开数据，不自动点赞 / 评论 / 关注。
      </p>
    </div>
    <div class="card">
      <h2>账号列表 <span class="hint">共 ${items.length} 个</span></h2>
      ${table([
        { label: '账号名', key: 'name' },
        { label: 'sec_uid', key: 'sec_uid' },
        { label: '状态', render: (r) => r.status },
        { label: '备注', key: 'note' },
        { label: '操作', render: (r) => r.act },
      ], rows, '还没有账号')}
    </div>`;
}

async function viewVideos() {
  const { items } = await api('/videos?limit=120');
  const rows = items.map((v) => ({
    ...v,
    srcTag: `<span class="tag ${v.source === 'mock' ? 'gray' : 'blue'}">${esc(v.source)}</span>`,
  }));
  return `
    <div class="card">
      <h2>视频快照 <span class="hint">最近 120 条，按写入倒序</span></h2>
      ${table([
        { label: '扫描时间', render: (r) => esc(fmtTime(r.scanned_at)) },
        { label: '账号', key: 'account' },
        { label: 'video_id', key: 'video_id' },
        { label: '标题', key: 'title', wrap: true },
        { label: '点赞', render: (r) => nfmt(r.likes), num: true },
        { label: '评论', render: (r) => nfmt(r.comments), num: true },
        { label: '分享', render: (r) => nfmt(r.shares), num: true },
        { label: '来源', render: (r) => r.srcTag },
      ], rows, '还没有快照，先跑一轮扫描')}
    </div>`;
}

async function viewDeltas() {
  const onlyAlert = sessionStorage.getItem('deltas.only') === '1';
  const { items } = await api('/deltas?limit=120&only_alert=' + onlyAlert);
  const rows = items.map((d) => ({
    ...d,
    deltaTag: `<b style="color:${d.delta > 0 ? 'var(--danger)' : 'var(--muted)'}">${d.delta > 0 ? '+' : ''}${nfmt(d.delta)}</b>`,
    alertTag: d.is_alert ? '<span class="tag red">告警</span>' : '<span class="tag gray">—</span>',
    sentTag: d.alerted ? '<span class="tag green">已推送</span>' : '<span class="tag gray">未推送</span>',
  }));
  return `
    <div class="card">
      <h2>增量与告警 <span class="hint">阈值来自配置表，红色表示超阈值</span></h2>
      <div class="form-row">
        <button class="btn ${onlyAlert ? 'primary' : ''}" id="btnOnlyAlert">
          ${onlyAlert ? '只看告警（已开）' : '只看告警'}
        </button>
      </div>
      ${table([
        { label: 'video_id', key: 'video_id' },
        { label: '账号', key: 'account' },
        { label: '上一点赞', render: (r) => nfmt(r.prev_likes), num: true },
        { label: '当前点赞', render: (r) => nfmt(r.curr_likes), num: true },
        { label: '增量', render: (r) => r.deltaTag, num: true },
        { label: '判定', render: (r) => r.alertTag },
        { label: '推送', render: (r) => r.sentTag },
      ], rows, '还没有增量数据，至少跑两轮扫描')}
    </div>`;
}

async function viewReviews() {
  const { items } = await api('/reviews?limit=200');
  if (!items.length) {
    return `<div class="card"><div class="empty">还没有评论命中记录。命中关键词的评论要在有告警的视频上才会被采集。</div></div>`;
  }

  const groups = {};
  items.forEach((h) => { (groups[h.thread_id] ||= []).push(h); });

  const statusTag = (s) => ({
    pending: '<span class="tag amber">待确认</span>',
    approved: '<span class="tag green">已采纳</span>',
    ignored: '<span class="tag gray">已忽略</span>',
  }[s] || esc(s));

  const html = Object.entries(groups).map(([tid, list]) => {
    const pending = list.filter((x) => x.status === 'pending');
    const body = list.map((h) => `
      <div class="review-item">
        <div class="c">
          <div class="line1">${esc(h.content)}</div>
          ${h.draft ? `<div class="line2">拟回复：${esc(h.draft)}</div>` : ''}
          <div class="line3">
            命中 <b>${esc(h.keywords || '—')}</b>
            　曲目 ${esc(h.song_title || '未识别')}${h.song_artist ? ' / ' + esc(h.song_artist) : ''}
            　<span class="pill">${esc(h.account || '')}</span>
          </div>
        </div>
        <div style="display:flex;gap:6px;align-items:center">
          ${statusTag(h.status)}
          ${h.status === 'pending' ? `
            <button class="btn sm ok" data-rev="${esc(tid)}|${esc(h.comment_id)}|approved">采纳</button>
            <button class="btn sm no" data-rev="${esc(tid)}|${esc(h.comment_id)}|ignored">忽略</button>` : ''}
        </div>
      </div>`).join('');

    return `
      <div class="group">
        <div class="group-head">
          <div>
            <div class="t">${esc(list[0].video_id)}　<span class="muted" style="font-weight:400">${esc(list[0].account || '')}</span></div>
            <div class="muted" style="font-size:12px">thread_id: ${esc(tid)}　待确认 ${pending.length} / ${list.length}</div>
          </div>
          ${pending.length ? `<button class="btn sm" data-rev-all="${esc(tid)}">全部采纳</button>` : ''}
        </div>
        <div class="group-body">${body}</div>
      </div>`;
  }).join('');

  return `<div class="card">
      <h2>评论确认 <span class="hint">点「采纳 / 忽略」即提交；确认后子图恢复执行并回写状态</span></h2>
      <p class="muted" style="font-size:12.5px;margin-top:-4px">
        ⚠ 拟回复只落表，<b>不会</b>自动发到抖音。任何情况下都不调用评论发布接口。
      </p>
    </div>${html}`;
}

async function viewRounds() {
  const { items } = await api('/runs?limit=60');
  const rows = items.map((r) => ({
    ...r,
    trig: `<span class="tag ${r.trigger_type === 'cron' ? 'blue' : 'gray'}">${esc(r.trigger_type)}</span>`,
    alert: r.alert_count ? `<span class="tag red">${r.alert_count}</span>` : '<span class="muted">0</span>',
    err: r.error_count ? `<span class="tag red">${r.error_count}</span>` : '<span class="muted">0</span>',
  }));
  return `<div class="card">
      <h2>扫描台账 <span class="hint">最近 60 轮</span></h2>
      ${table([
        { label: 'run_id', key: 'run_id' },
        { label: '触发', render: (r) => r.trig },
        { label: '开始时间', render: (r) => esc(fmtTime(r.started_at)) },
        { label: '账号', render: (r) => nfmt(r.account_count), num: true },
        { label: '视频', render: (r) => nfmt(r.video_count), num: true },
        { label: '告警', render: (r) => r.alert },
        { label: '错误', render: (r) => r.err },
        { label: '来源', key: 'source' },
      ], rows, '还没有扫描记录')}
    </div>`;
}

async function viewConfig() {
  const { items, resolved, preset } = await api('/config');
  const rows = items.map((c) => `
    <tr>
      <td><code>${esc(c.key)}</code></td>
      <td><input data-cfg="${esc(c.key)}" value="${esc(c.value)}" style="min-width:130px"></td>
      <td><span class="tag gray">${esc(c.type)}</span></td>
      <td><span class="tag blue">${esc(c.scope)}</span></td>
      <td class="wrap muted">${esc(c.note || '')}</td>
      <td class="muted" style="font-size:12px">${esc(fmtTime(c.updated_at))}</td>
    </tr>`).join('');

  // 「混合档位」= 逐格改出来的半套配置。最坑的一种是：节奏和阈值都变了、
  // provider_chain 没变 —— 扫描永远走真实源，看上去像 mock 坏了。
  const badge = preset === 'demo' ? '<span class="tag green">演示档</span>'
    : preset === 'formal' ? '<span class="tag blue">正式档</span>'
    : '<span class="tag gray">⚠ 混合档位（不是完整的演示档或正式档）</span>';
  const mixedHint = preset === 'mixed' ? `
      <p class="muted" style="font-size:12.5px;margin:10px 0 0">
        ⚠ 当前配置是逐格改出来的，不属于任何一个完整档位。请注意
        <code>provider_chain</code>：只要它还是 <code>browser,mock</code>，
        扫描就会走真实数据源、不会用 mock 数据，演示时「增量 → 告警」这条动线不会触发。
        建议点下面的一键切档整体切一次。
      </p>` : '';

  return `
    <div class="card">
      <h2>配置表 <span class="hint">控制面 —— 改完点保存，下一轮调度立即生效</span></h2>
      <p class="muted" style="margin:0 0 10px;font-size:12.5px">当前档位：${badge}</p>
      <div class="form-row">
        <button class="btn primary" id="cfgSave">保存全部改动</button>
        <button class="btn" id="cfgDemo">一键切演示档</button>
        <button class="btn" id="cfgRestore">恢复正式档</button>
      </div>
      ${mixedHint}
      <div class="table-wrap"><table>
        <thead><tr><th>配置项</th><th>值</th><th>类型</th><th>生效范围</th><th>说明</th><th>更新时间</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
      <p class="muted" style="font-size:12.5px;margin:12px 0 0">
        当前生效值：<code>${esc(JSON.stringify(resolved))}</code>
      </p>
    </div>
    <div class="card">
      <h2>演示切档说明</h2>
      <p class="muted" style="margin:0;font-size:13px">
        <b>一键切演示档</b>会把整个档位一次切完（<code>scan_mode=interval</code>、
        <code>scan_interval_minutes=3</code>、<code>threshold=20</code>、
        <code>provider_chain=mock</code>）—— 节奏变快、告警变多，适合录屏。<br>
        其中 <code>provider_chain=mock</code> 是<b>必须的</b>：真实账号 3 分钟内的点赞增量
        接近 0，不换数据源的话阈值再低也告警不了，「增量 → 超阈值 → 推送」这条动线不会触发。<br>
        ⚠ 演示模式<b>只能短时开</b>：3 分钟一轮意味着每小时约 160 次外部调用，
        常开会把外部额度打爆。演示完记得点<b>恢复正式档</b>。
      </p>
    </div>`;
}

/* ---------------------------------------------------------------- 事件 */

async function submitDecisions(threadId, decisions) {
  await api('/reviews/' + encodeURIComponent(threadId), {
    method: 'POST',
    body: JSON.stringify({ decisions }),
  });
}

async function onReviewClick(tid, cid, status) {
  try {
    const r = await submitDecisions(tid, { [cid]: status });
    toast(`已${status === 'approved' ? '采纳' : '忽略'}${r.resumed ? '（子图已恢复）' : ''}`, 'ok');
    await render();
    await refreshShell();
  } catch (e) { toast(e.message, 'err'); }
}

async function onReviewAll(tid, items) {
  const decisions = {};
  items.filter((x) => x.status === 'pending').forEach((x) => { decisions[x.comment_id] = 'approved'; });
  try {
    const r = await submitDecisions(tid, decisions);
    toast(`已提交 ${Object.keys(decisions).length} 条${r.resumed ? '（子图已恢复）' : ''}`, 'ok');
    await render();
    await refreshShell();
  } catch (e) { toast(e.message, 'err'); }
}

function bindViewEvents() {
  const view = location.hash.replace('#/', '') || 'overview';

  if (view === 'deltas') {
    $('#btnOnlyAlert')?.addEventListener('click', () => {
      const cur = sessionStorage.getItem('deltas.only') === '1';
      sessionStorage.setItem('deltas.only', cur ? '0' : '1');
      render();
    });
  }

  if (view === 'accounts') {
    $('#accAdd')?.addEventListener('click', async () => {
      const name = $('#accName').value.trim();
      const sec_uid = $('#accUid').value.trim();
      if (!name || !sec_uid) return toast('账号名和 sec_uid 必填', 'err');
      try {
        await api('/accounts', {
          method: 'POST',
          body: JSON.stringify({ name, sec_uid, homepage: $('#accHome').value.trim(), note: $('#accNote').value.trim(), enabled: true }),
        });
        toast('已保存', 'ok'); render();
      } catch (e) { toast(e.message, 'err'); }
    });
    $all('[data-del]').forEach((b) => b.addEventListener('click', async () => {
      if (!confirm('确认删除这个监控账号？')) return;
      try { await api('/accounts/' + encodeURIComponent(b.dataset.del), { method: 'DELETE' }); toast('已删除', 'ok'); render(); }
      catch (e) { toast(e.message, 'err'); }
    }));
  }

  if (view === 'reviews') {
    $all('[data-rev]').forEach((b) => b.addEventListener('click', () => {
      const [tid, cid, st] = b.dataset.rev.split('|');
      onReviewClick(tid, cid, st);
    }));
    $all('[data-rev-all]').forEach((b) => b.addEventListener('click', async () => {
      const tid = b.dataset.revAll;
      const { items } = await api('/reviews?limit=200');
      onReviewAll(tid, items.filter((x) => x.thread_id === tid));
    }));
  }

  if (view === 'config') {
    const saveAll = async (pairs) => {
      for (const [k, v] of Object.entries(pairs)) {
        await api('/config', { method: 'PUT', body: JSON.stringify({ key: k, value: v }) });
      }
    };
    // 切档必须整体走服务端的档位定义（app/core/presets.py），
    // 不能在前端自己拼键值对 —— 以前就是那么干的，结果漏了 provider_chain，
    // 切完看着像演示档，扫描却仍走真实源，没有 mock 增量也没有告警。
    const applyPreset = async (name) => {
      const r = await api('/config/preset', { method: 'PUT', body: JSON.stringify({ name }) });
      // 侧栏「数据源」要立刻反映新链，别等 12 秒轮询（「上轮」那格仍会滞后一轮，这是对的）
      await refreshShell();
      const moved = Object.entries(r.changed || {}).map(([k, m]) => `${k}: ${m.from}→${m.to}`);
      // 服务端写完会读回来核对；不一致时如实回 verified=false ——
      // 别把「半套档位」说成切档成功（那正是用户反复点按钮的原因）
      return { moved, verified: r.verified !== false, scanMode: r.resolved?.scan_mode ?? '?' };
    };
    const presetToast = (title, { moved, verified, scanMode }) => toast(
      verified ? `${title}（改了 ${moved.length} 格）`
        : `档位写入了，但服务读回来核对不一致（scan_mode=${scanMode}）——可能同时被别处改过，请再点一次`,
      verified ? 'ok' : 'err',
    );
    $('#cfgSave')?.addEventListener('click', async () => {
      try {
        const pairs = {};
        $all('[data-cfg]').forEach((i) => { pairs[i.dataset.cfg] = i.value; });
        await saveAll(pairs);
        toast('配置已保存，下一轮调度生效', 'ok'); render();
      } catch (e) { toast(e.message, 'err'); }
    });
    $('#cfgDemo')?.addEventListener('click', async () => {
      try {
        presetToast('已切演示档：3 分钟一轮 / 阈值 20 / 数据源 mock', await applyPreset('demo'));
        render();
      } catch (e) { toast(e.message, 'err'); }
    });
    $('#cfgRestore')?.addEventListener('click', async () => {
      try {
        presetToast('已恢复正式档：12/18/22 点 / 阈值 300 / 真实源优先', await applyPreset('formal'));
        render();
      } catch (e) { toast(e.message, 'err'); }
    });
  }
}

/* ---------------------------------------------------------------- 扫描 */

async function doScan() {
  const btn = $('#btnScan');
  btn.disabled = true;
  btn.textContent = '扫描中…';
  try {
    const r = await api('/scan?trigger=manual', { method: 'POST' });
    const parts = [`${r.video_count} 条视频`, `${r.alert_count} 条告警`];
    // pending_comments 是**累计**待确认（含往轮遗留），所以措辞上要写清楚
    if (r.pending_comments) parts.push(`累计 ${r.pending_comments} 条待确认评论`);
    toast(`扫描完成：${parts.join('，')}（${r.duration_ms}ms）`, r.alert_count ? 'ok' : '');
    await refreshShell();
    await refreshScanState();
    await render();
  } catch (e) {
    // 409 = 可解释的状态（没有启用账号 / 回看窗口内没有新作品 / 上一轮还没跑完），
    // detail 里已经是人话。其中「跳过」是**正常状态**，不该显示成红色错误——
    // 账号这几天没发作品不是故障，用红色会让人去查 provider。
    const skipped = /没有新作品|没有启用中的监控账号/.test(e.message);
    toast(`${skipped ? '本轮跳过' : '扫描失败'}：${e.message}`, skipped ? '' : 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '手动扫描';
  }
}

/* ---------------------------------------------------------------- 壳 */

/* 侧栏底部 + 待确认角标。数据源拆成两格，因为它们回答的是两个不同问题：
 *   「数据源」= 当前配置的链，下一轮扫描按它走；
 *   「上轮」  = 最近一轮**实际**用到的源，切档不会重跑、所以它会滞后一轮。
 * 以前只有一格、且读的是 last_round.source —— 切回正式档后那格仍显示上一轮的
 * mock，看着像档位没切成功；而且没跑过扫描时它还硬编码兜底成 'mock'，
 * 等于替系统说假话（登录态过期那轮 source 为空，页面照样写「mock」）。 */
async function refreshShell() {
  try {
    const s = await api('/stats');
    const n = s.pending_comments || 0;
    $('#badgeReviews').textContent = n ? String(n) : '';

    const chain = String(s.config?.provider_chain || '')
      .split(',').map((x) => x.trim()).filter(Boolean);
    $('#chainInfo').textContent = chain.length ? chain.join(' → ') : '—';

    // 上轮用的源不是当前链首选 → 不是故障，是提醒「切完档要等下一轮才生效」
    const last = s.last_round?.source || '';
    const el = $('#lastSrcInfo');
    el.textContent = last || '—';
    el.style.color = last && last !== chain[0] ? 'var(--warn)' : '';
  } catch (_) { /* 后端没起来时不打扰用户 */ }
}

/* 扫描中 / 调度状态。调度描述与下次运行时间以后端为准（配置表改了会立刻反映），
 * 不要在前端按配置自己推算——那样一旦两边口径不一致，看板就在说谎。 */
async function refreshScanState() {
  try {
    const { scanning, scheduler } = await api('/scan/status');
    $('#scanState').textContent = scanning ? '是' : '否';
    $('#scanState').style.color = scanning ? 'var(--warn)' : '';

    if (scheduler && scheduler.enabled) {
      $('#schedInfo').textContent = scheduler.description || '—';
      $('#nextRun').textContent = scheduler.next_run_at ? fmtClock(scheduler.next_run_at) : '—';
    } else {
      $('#schedInfo').textContent = '未启用';
      $('#nextRun').textContent = '—';
    }
  } catch (_) {}
}

function fmtClock(iso) {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '—';
  const p = (n) => String(n).padStart(2, '0');
  const sameDay = d.toDateString() === new Date().toDateString();
  const hm = `${p(d.getHours())}:${p(d.getMinutes())}`;
  return sameDay ? hm : `${p(d.getMonth() + 1)}-${p(d.getDate())} ${hm}`;
}

const VIEWS = {
  overview: viewOverview,
  accounts: viewAccounts,
  videos: viewVideos,
  deltas: viewDeltas,
  reviews: viewReviews,
  rounds: viewRounds,
  config: viewConfig,
};

async function render() {
  const name = (location.hash.replace('#/', '') || 'overview').split('?')[0];
  const view = VIEWS[name] ? name : 'overview';

  $all('#nav a').forEach((a) => a.classList.toggle('active', a.dataset.view === view));
  const [title, sub] = TITLES[view];
  $('#pageTitle').textContent = title;
  $('#pageSub').textContent = sub;

  const box = $('#view');
  box.innerHTML = '<div class="loading">加载中…</div>';
  try {
    box.innerHTML = await VIEWS[view]();
    bindViewEvents();
  } catch (e) {
    box.innerHTML = `<div class="card"><div class="empty">加载失败：${esc(e.message)}</div></div>`;
  }
}

window.addEventListener('hashchange', render);
window.addEventListener('DOMContentLoaded', () => {
  if (!location.hash) location.hash = '#/overview';
  $('#btnScan').addEventListener('click', doScan);
  render();
  refreshShell();
  refreshScanState();
  setInterval(refreshShell, 12000);
  setInterval(refreshScanState, 4000);
});
