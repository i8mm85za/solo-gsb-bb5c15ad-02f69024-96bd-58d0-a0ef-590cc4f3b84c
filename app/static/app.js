"use strict";

// ---------- Session ----------
const SESSION_KEY = "calib_session_id";
function getSessionId() {
  let id = localStorage.getItem(SESSION_KEY);
  if (!id) {
    id = "sess-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
    localStorage.setItem(SESSION_KEY, id);
  }
  return id;
}
const SESSION_ID = getSessionId();
document.getElementById("session-id").textContent = SESSION_ID;

// ---------- State ----------
let analysis = null;
let stability = null; // 排名稳定性实验结果（纯前端临时态，不持久化）

const $ = (sel) => document.querySelector(sel);
const dashboard = $("#dashboard");
const emptyState = $("#empty-state");

// ---------- Helpers ----------
function fmt(v, digits = 2) {
  if (v === null || v === undefined) return "-";
  return Number(v).toFixed(digits);
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function deltaHtml(d) {
  if (d > 0) return `<span class="delta-up">↑${d}</span>`;
  if (d < 0) return `<span class="delta-down">↓${-d}</span>`;
  return `<span class="delta-flat">—</span>`;
}
let toastTimer = null;
function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (isError ? " error" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 4000 : 2200);
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body && body.detail) detail = body.detail;
    } catch (_) { /* keep default */ }
    throw new Error(detail);
  }
  return res.json();
}

// ---------- Rendering ----------
function render() {
  if (!analysis || analysis.counts.scores === 0) {
    dashboard.hidden = true;
    emptyState.hidden = false;
    return;
  }
  emptyState.hidden = true;
  dashboard.hidden = false;

  const ref = analysis.reference;
  $("#reference-info").innerHTML =
    `校准范围：<b>${esc(analysis.scope_label)}</b><br>` +
    `对每个校准分组计算中位数与 MAD：<code>Z = (原始分 − 中位数) / (MAD × ${ref.mad_k})</code>，` +
    `再映射回全局尺度：<code>校准分 = ${fmt(ref.global_median, 3)} + Z × ${fmt(ref.global_mad, 3)}</code>` +
    `（全局中位数 ${fmt(ref.global_median, 3)}，全局 MAD ${fmt(ref.global_mad, 3)}）。` +
    `分组 MAD 为 0（无离散度）时取 Z = 0。`;

  const c = analysis.counts;
  $("#stats-row").innerHTML = [
    ["选手", c.contestants], ["评委", c.judges], ["项目", c.items],
    ["评分条数", c.scores], ["保留原分裁决", c.decisions],
  ].map(([k, v]) => `<div class="stat"><b>${v}</b><span>${k}</span></div>`).join("");

  renderGroups();
  renderReversals();
  renderRanks();
  renderDetails();
  renderStability();
}

// ---------- Ranking stability experiment ----------
const stabilityTypeSel = $("#stability-type");
const stabilityNameSel = $("#stability-name");
const stabilityBtnRun = $("#btn-stability-run");
const stabilityBtnClear = $("#btn-stability-clear");

function setStabilityMsg(text, cls = "") {
  const el = $("#stability-msg");
  el.textContent = text;
  el.className = "msg " + cls;
}

// Discard the what-if result and return to the baseline table.
function clearStability(silent = false) {
  stability = null;
  setStabilityMsg("");
  renderStability();
  if (!silent) toast("已恢复基线排名");
}

function syncStabilityControls() {
  const hasData = !!(analysis && analysis.counts.scores > 0);
  const type = stabilityTypeSel.value;
  const names = hasData ? (type === "judge" ? analysis.judges : analysis.items) : [];
  const prev = stabilityNameSel.value;
  stabilityNameSel.innerHTML =
    `<option value="" disabled${names.length ? "" : " selected"}>请选择${type === "judge" ? "评委" : "项目"}…</option>` +
    names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
  if (names.includes(prev)) stabilityNameSel.value = prev;
  stabilityBtnRun.disabled = !hasData;
  stabilityNameSel.disabled = !hasData;
  stabilityTypeSel.disabled = !hasData;
  stabilityBtnClear.disabled = stability === null;
}

function signedNum(d, digits = 2) {
  if (d === null || d === undefined) return "-";
  return (d > 0 ? "+" : d < 0 ? "−" : "") + fmt(Math.abs(d), digits);
}

function totalDeltaHtml(d) {
  if (d === null || d === undefined) return `<span class="delta-flat">-</span>`;
  if (d > 0) return `<span class="delta-up">${signedNum(d)}</span>`;
  if (d < 0) return `<span class="delta-down">${signedNum(d)}</span>`;
  return `<span class="delta-flat">0.00</span>`;
}

function renderStability() {
  const panel = $("#stability-result");
  syncStabilityControls();
  if (!stability) {
    panel.innerHTML = "";
    return;
  }
  const ex = stability.exclusion;
  const s = stability.summary;

  const head = `
    <tr>
      <th class="num">基线<br>最终名次</th><th>选手</th><th class="num">受影响<br>评分数</th>
      <th class="num">原始<br>基线总分</th><th class="num">实验<br>原始总分</th><th class="num">原始<br>总分变化</th>
      <th class="num">基线<br>原始名次</th><th class="num">实验<br>原始名次</th><th class="num">原始<br>名次变化</th>
      <th class="num">校准<br>基线总分</th><th class="num">实验<br>校准总分</th><th class="num">校准<br>总分变化</th>
      <th class="num">基线<br>校准名次</th><th class="num">实验<br>校准名次</th><th class="num">校准<br>名次变化</th>
      <th class="num">最终<br>基线总分</th><th class="num">实验<br>最终总分</th><th class="num">最终<br>总分变化</th>
      <th class="num">基线<br>最终名次</th><th class="num">实验<br>最终名次</th><th class="num">最终<br>名次变化</th>
    </tr>`;

  const rows = stability.contestants.map((r) => {
    if (!r.present) {
      return `<tr class="gone-row"><td class="num"><b>${r.baseline.eff_rank}</b></td>
        <td><span class="gone-name">${esc(r.contestant)}</span></td>
        <td class="num">${r.affected_scores}</td>
        <td class="num">${fmt(r.baseline.raw_total)}</td>
        <td colspan="17" class="num gone-note">无剩余评分，无法计算名次（校准分 / 最终分同样不可计算）</td></tr>`;
    }
    const b = r.baseline, sc = r.scenario;
    const cls = r.eff_rank_delta && r.eff_rank_delta !== 0 ? "rank-shift-row" : "";
    return `
    <tr class="${cls}">
      <td class="num"><b>${b.eff_rank}</b></td>
      <td>${esc(r.contestant)}</td>
      <td class="num"><b>${r.affected_scores}</b></td>
      <td class="num">${fmt(b.raw_total)}</td><td class="num">${fmt(sc.raw_total)}</td><td class="num">${totalDeltaHtml(r.raw_total_delta)}</td>
      <td class="num">${b.raw_rank}</td><td class="num">${sc.raw_rank}</td><td class="num">${deltaHtml(r.raw_rank_delta)}</td>
      <td class="num">${fmt(b.cal_total)}</td><td class="num">${fmt(sc.cal_total)}</td><td class="num">${totalDeltaHtml(r.cal_total_delta)}</td>
      <td class="num">${b.cal_rank}</td><td class="num">${sc.cal_rank}</td><td class="num">${deltaHtml(r.cal_rank_delta)}</td>
      <td class="num"><b>${fmt(b.eff_total)}</b></td><td class="num"><b>${fmt(sc.eff_total)}</b></td><td class="num">${totalDeltaHtml(r.eff_total_delta)}</td>
      <td class="num">${b.eff_rank}</td><td class="num"><b>${sc.eff_rank}</b></td><td class="num">${deltaHtml(r.eff_rank_delta)}</td>
    </tr>`;
  }).join("");

  panel.innerHTML = `
    <div class="stability-banner">
      实验：临时排除${ex.type_label} <b>${esc(ex.name)}</b>（校准范围：${esc(stability.scope_label)}）｜
      共移除 <b>${stability.removed_scores}</b> 条评分（${stability.baseline_counts.scores} → ${stability.scenario_counts.scores}）。
      最终名次变化：<b class="${s.eff_rank_changed ? "delta-down" : "delta-flat"}">${s.eff_rank_changed}</b> 位
      <span class="delta-up">↑${s.eff_moved_up}</span> / <span class="delta-down">↓${s.eff_moved_down}</span>；
      原始名次变化 ${s.raw_rank_changed} 位，校准名次变化 ${s.cal_rank_changed} 位；
      最终总分最大绝对变化 ${fmt(s.max_eff_total_delta_abs)}。
      <span class="stability-note">本结果为临时模拟，不写入会话裁决或数据库。</span>
    </div>
    <div class="table-wrap stability-table-wrap"><table>${head}${rows}</table></div>`;
}

function renderGroups() {
  const byItem = analysis.scope === "judge_item";
  const head = byItem
    ? `<tr><th>评委</th><th>项目</th><th class="num">条数</th><th class="num">中位数</th><th class="num">MAD</th><th class="num">MAD×1.4826</th></tr>`
    : `<tr><th>评委</th><th class="num">条数</th><th class="num">中位数</th><th class="num">MAD</th><th class="num">MAD×1.4826</th></tr>`;
  const rows = analysis.groups.map((g) => {
    const flat = g.mad === 0 ? ' <span class="zline">(无离散度)</span>' : "";
    return byItem
      ? `<tr><td>${esc(g.judge)}</td><td>${esc(g.item)}</td><td class="num">${g.n}</td><td class="num">${fmt(g.median)}</td><td class="num">${fmt(g.raw_mad, 3)}${flat}</td><td class="num">${fmt(g.mad, 3)}</td></tr>`
      : `<tr><td>${esc(g.judge)}</td><td class="num">${g.n}</td><td class="num">${fmt(g.median)}</td><td class="num">${fmt(g.raw_mad, 3)}${flat}</td><td class="num">${fmt(g.mad, 3)}</td></tr>`;
  }).join("");
  $("#group-table-wrap").innerHTML = `<table>${head}${rows}</table>`;
}

function renderReversals() {
  const rvs = analysis.reversals;
  const badge = $("#reversal-count");
  badge.textContent = rvs.length;
  badge.className = "badge" + (rvs.length === 0 ? " zero" : "");
  const panel = $("#reversal-panel");
  if (rvs.length === 0) {
    panel.innerHTML = `<p class="no-reversal">✓ 标准化前后未检测到选手对名次反转。</p>`;
    return;
  }
  panel.innerHTML = rvs.map((r) => `
    <div class="reversal-card">
      <b>${esc(r.winner_before)}</b>（原始第 ${r.raw_rank_before} 名）
      <span class="arrow">→ 反超 →</span>
      <b>${esc(r.winner_after)}</b>（原始第 ${r.raw_rank_loser} 名）
      <br>
      <small>校准后：<b>${esc(r.winner_after)}</b> 第 ${r.cal_rank_winner} 名，${esc(r.winner_before)} 第 ${r.cal_rank_loser} 名；
      原始分差 ${fmt(r.raw_gap)}，校准后反向分差 ${fmt(r.cal_gap)}</small>
    </div>`).join("");
}

function renderRanks() {
  const head = `<tr>
    <th class="num">最终名次</th><th>选手</th>
    <th class="num">原始总分</th><th class="num">原始名次</th>
    <th class="num">校准总分</th><th class="num">校准名次</th>
    <th class="num">校准后变化</th>
    <th class="num">最终总分</th><th class="num">保留原分条数</th>
  </tr>`;
  // Show final-rank ordering; blocks already arrive sorted by eff_rank.
  const rows = analysis.contestants.map((b) => `
    <tr>
      <td class="num"><b>${b.eff_rank}</b></td>
      <td>${esc(b.contestant)}</td>
      <td class="num">${fmt(b.raw_total)}</td>
      <td class="num">${b.raw_rank}</td>
      <td class="num">${fmt(b.cal_total)}</td>
      <td class="num">${b.cal_rank}</td>
      <td class="num">${deltaHtml(b.rank_delta)}</td>
      <td class="num"><b>${fmt(b.eff_total)}</b></td>
      <td class="num">${b.raw_overrides}</td>
    </tr>`).join("");
  $("#rank-table-wrap").innerHTML = `<table>${head}${rows}</table>`;
}

function renderDetails() {
  const panel = $("#detail-panel");
  panel.innerHTML = analysis.contestants.map((b, idx) => {
    const items = b.items.map((ib) => {
      const scores = ib.scores.map((s) => {
        const flat = s.group_mad === 0;
        const useCal = !s.use_raw;
        return `
        <div class="score-row" data-id="${s.id}">
          <div class="who">${esc(s.judge)}<small>${esc(ib.item)}</small></div>
          <div class="score-val">
            <span class="raw">原分 ${fmt(s.raw)}</span><br>
            <span class="cal">校准 ${fmt(s.calibrated)}</span>
            <small>（Z=${fmt(s.z, 2)}，中位数 ${fmt(s.group_median)}）</small>
            ${flat ? '<br><span class="zline">该分组 MAD=0，校准分恒等于全局中位数</span>' : ""}
          </div>
          <div class="score-val muted">
            最终分<br><span class="eff">${fmt(s.effective)}</span>
          </div>
          <div class="choice">
            <label><input type="radio" name="choice-${s.id}" value="cal" ${useCal ? "checked" : ""}> 采用校准分</label>
            <label><input type="radio" name="choice-${s.id}" value="raw" ${s.use_raw ? "checked" : ""}> 保留原分</label>
            <input type="text" class="reason-input" data-id="${s.id}" maxlength="2000"
                   placeholder="理由（可选，将写入审计导出）" value="${esc(s.reason)}">
          </div>
        </div>`;
      }).join("");
      return `<div style="margin:6px 0 12px;"><b>${esc(ib.item)}</b>
        <small style="color:var(--muted)">（原始均分 ${fmt(ib.raw_avg)} → 校准均分 ${fmt(ib.cal_avg)} → 最终均分 ${fmt(ib.eff_avg)}）</small>
        ${scores}</div>`;
    }).join("");
    return `
      <div class="contestant-block ${idx > 1 ? "collapsed" : ""}" data-contestant="${esc(b.contestant)}">
        <div class="contestant-head">
          <h4>${esc(b.contestant)}</h4>
          <span class="meta">最终名次 ${b.eff_rank} ｜ 最终总分 ${fmt(b.eff_total)} ｜ 原始名次 ${b.raw_rank} → 校准名次 ${b.cal_rank} ｜ ${b.item_count} 个项目（点击折叠）</span>
        </div>
        <div class="contestant-body">${items}</div>
      </div>`;
  }).join("");

  panel.querySelectorAll(".contestant-head").forEach((h) => {
    h.addEventListener("click", () => h.parentElement.classList.toggle("collapsed"));
  });
  panel.querySelectorAll('input[type="radio"]').forEach((radio) => {
    radio.addEventListener("change", onChoiceChange);
  });
  panel.querySelectorAll(".reason-input").forEach((input) => {
    input.addEventListener("input", onReasonInput);
  });
}

// ---------- Decision updates ----------
let inflight = false;
let queued = null;

function currentState(scoreId) {
  for (const b of analysis.contestants) {
    for (const ib of b.items) {
      for (const s of ib.scores) {
        if (s.id === scoreId) return s;
      }
    }
  }
  return null;
}

async function pushDecision(scoreId, useRaw, reason) {
  if (inflight) {
    queued = { scoreId, useRaw, reason };
    return;
  }
  inflight = true;
  try {
    analysis = await api(
      `/api/sessions/${SESSION_ID}/scores/${scoreId}/decision`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ use_raw: useRaw, reason }),
      }
    );
    stability = null; // 裁决变化会改变最终总分，实验结果作废
    render();
    toast("排名已实时重算");
  } catch (err) {
    toast("保存失败：" + err.message, true);
    await loadAnalysis(); // resync from server
  } finally {
    inflight = false;
    if (queued) {
      const q = queued;
      queued = null;
      pushDecision(q.scoreId, q.useRaw, q.reason);
    }
  }
}

function onChoiceChange(ev) {
  const row = ev.target.closest(".score-row");
  const scoreId = Number(row.dataset.id);
  const useRaw = ev.target.value === "raw";
  const reasonInput = row.querySelector(".reason-input");
  pushDecision(scoreId, useRaw, reasonInput.value);
}

const reasonTimers = new Map();
function onReasonInput(ev) {
  const scoreId = Number(ev.target.dataset.id);
  clearTimeout(reasonTimers.get(scoreId));
  reasonTimers.set(scoreId, setTimeout(() => {
    const s = currentState(scoreId);
    if (s) pushDecision(scoreId, s.use_raw, ev.target.value);
  }, 500));
}

// ---------- Data actions ----------
async function loadAnalysis() {
  try {
    analysis = await api(`/api/sessions/${SESSION_ID}/analysis`);
    stability = null;
    render();
  } catch (_) {
    analysis = null;
    stability = null;
    render();
  }
}

function setMsg(text, cls = "") {
  const el = $("#import-msg");
  el.textContent = text;
  el.className = "msg " + cls;
}

$("#file-input").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  const scope = document.querySelector('input[name="scope"]:checked').value;
  setMsg("上传中…");
  const fd = new FormData();
  fd.append("file", file);
  try {
    analysis = await api(`/api/sessions/${SESSION_ID}/upload?scope=${encodeURIComponent(scope)}`, {
      method: "POST",
      body: fd,
    });
    stability = null;
    render();
    setMsg(`已导入 ${analysis.counts.scores} 条评分`, "ok");
    toast("上传成功，已完成稳健标准化");
  } catch (err) {
    setMsg(err.message, "error");
    toast("上传失败：" + err.message, true);
  } finally {
    ev.target.value = "";
  }
});

$("#btn-sample").addEventListener("click", async () => {
  const scope = document.querySelector('input[name="scope"]:checked').value;
  setMsg("载入示例中…");
  try {
    analysis = await api(`/api/sessions/${SESSION_ID}/sample?scope=${encodeURIComponent(scope)}`, {
      method: "POST",
    });
    stability = null;
    render();
    setMsg(`示例数据已载入（${analysis.counts.scores} 条评分）`, "ok");
    toast("示例数据已载入");
  } catch (err) {
    setMsg(err.message, "error");
    toast("载入失败：" + err.message, true);
  }
});

document.querySelectorAll('input[name="scope"]').forEach((radio) => {
  radio.addEventListener("change", async (ev) => {
    if (!analysis || analysis.counts.scores === 0) return;
    try {
      analysis = await api(`/api/sessions/${SESSION_ID}/scope`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope: ev.target.value }),
      });
      stability = null; // 实验按校准范围重算，旧结果与新基线无关
      render();
      toast("校准范围已切换");
    } catch (err) {
      toast("切换失败：" + err.message, true);
      loadAnalysis();
    }
  });
});

$("#btn-reset-decisions").addEventListener("click", async () => {
  if (!confirm("确定清除全部「保留原分」裁决与理由，统一恢复为采用校准分？")) return;
  try {
    analysis = await api(`/api/sessions/${SESSION_ID}/decisions/reset`, { method: "POST" });
    stability = null;
    render();
    toast("已全部恢复为采用校准分");
  } catch (err) {
    toast("操作失败：" + err.message, true);
  }
});

$("#btn-export").addEventListener("click", () => {
  window.location.href = `/api/sessions/${SESSION_ID}/export`;
});

$("#btn-clear").addEventListener("click", async () => {
  if (!confirm("确定清空当前会话？\n将删除本会话上传的全部评分、裁决与理由（数据仅保存在容器卷中，不可恢复）。")) return;
  try {
    await api(`/api/sessions/${SESSION_ID}`, { method: "DELETE" });
    analysis = null;
    stability = null;
    render();
    setMsg("会话已清空。");
    toast("会话已清空");
  } catch (err) {
    toast("清空失败：" + err.message, true);
  }
});

// ---------- Ranking stability experiment actions ----------
stabilityTypeSel.addEventListener("change", syncStabilityControls);

stabilityBtnRun.addEventListener("click", async () => {
  const type = stabilityTypeSel.value;
  const name = stabilityNameSel.value;
  if (!name) {
    setStabilityMsg(`请先选择要排除的${type === "judge" ? "评委" : "项目"}。`, "error");
    return;
  }
  setStabilityMsg("计算中…");
  try {
    stability = await api(`/api/sessions/${SESSION_ID}/stability`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ exclude_type: type, exclude_name: name }),
    });
    setStabilityMsg("实验完成（临时结果，未保存）", "ok");
    renderStability();
  } catch (err) {
    stability = null;
    setStabilityMsg(err.message, "error");
    renderStability();
    toast("实验失败：" + err.message, true);
  }
});

stabilityBtnClear.addEventListener("click", () => clearStability());

// ---------- Init ----------
loadAnalysis();
