"use strict";

const appToken = document.querySelector('meta[name="app-token"]').content;
const form = document.getElementById("check-form");
const uidInput = document.getElementById("uid");
const secretInput = document.getElementById("sessdata");
const submitButton = document.getElementById("submit-button");
const toggleSecret = document.getElementById("toggle-secret");
const delayInput = document.getElementById("delay");
const delayValue = document.getElementById("delay-value");
const statusTitle = document.getElementById("status-title");
const statusBadge = document.getElementById("status-badge");
const statusMessage = document.getElementById("status-message");
const progressTrack = document.getElementById("progress-track");
const progressPage = document.getElementById("progress-page");
const progressCount = document.getElementById("progress-count");
const resultList = document.getElementById("result-list");
const resultNotice = document.getElementById("result-notice");
const historyList = document.getElementById("history-list");
const clearDataButton = document.getElementById("clear-data");
const toast = document.getElementById("toast");

let currentTaskId = null;
let checkState = "idle";
let currentUid = localStorage.getItem("lastFollowerUid") || "";
let currentDiff = null;
let activeTab = "removed";
let toastTimer = null;

if (currentUid) {
  uidInput.value = currentUid;
  clearDataButton.disabled = false;
  loadHistory(currentUid);
}

delayInput.addEventListener("input", () => {
  delayValue.value = `${delayInput.value} 秒`;
});

toggleSecret.addEventListener("click", () => {
  const showing = secretInput.type === "text";
  secretInput.type = showing ? "password" : "text";
  toggleSecret.textContent = showing ? "显示" : "隐藏";
});

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => {
      item.classList.toggle("active", item === button);
      item.setAttribute("aria-selected", item === button ? "true" : "false");
    });
    activeTab = button.dataset.tab;
    renderResultList();
  });
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (checkState === "running") {
    await cancelCurrentTask();
    return;
  }
  if (checkState !== "idle") return;

  const uid = uidInput.value.trim();
  const sessdata = secretInput.value.trim();
  if (!/^\d+$/.test(uid) || Number(uid) <= 0) {
    showToast("请输入正确的数字 UID", true);
    uidInput.focus();
    return;
  }
  if (!sessdata) {
    showToast("请输入 SESSDATA", true);
    secretInput.focus();
    return;
  }

  setCheckState("starting");
  setStatus("正在开始", "正在创建本地检查任务", "running");
  setProgress(null, 0, null, 0);
  try {
    const response = await apiFetch("/api/check", {
      method: "POST",
      body: JSON.stringify({ uid, sessdata, delay: Number(delayInput.value) }),
    });
    secretInput.value = "";
    secretInput.type = "password";
    toggleSecret.textContent = "显示";
    currentUid = uid;
    localStorage.setItem("lastFollowerUid", uid);
    clearDataButton.disabled = false;
    currentTaskId = response.task_id;
    setCheckState("running");
    await pollTask(response.task_id);
  } catch (error) {
    secretInput.value = "";
    setCheckState("idle");
    setStatus("检查失败", error.message, "error");
    showToast(error.message, true);
  }
});

async function cancelCurrentTask() {
  if (!currentTaskId || checkState !== "running") return;
  setCheckState("cancelling");
  setStatus("正在中止", "本次检查产生的数据将全部丢弃，请稍候。", "running");
  try {
    await apiFetch(`/api/tasks/${currentTaskId}/cancel`, { method: "POST" });
    showToast("已发送中止请求，本次结果不会保存");
  } catch (error) {
    setCheckState("running");
    showToast(error.message, true);
    window.setTimeout(() => pollTask(currentTaskId), 0);
  }
}

clearDataButton.addEventListener("click", async () => {
  if (!currentUid) return;
  const confirmed = window.confirm(
    `确定删除 UID ${currentUid} 的全部本地快照和报告吗？此操作无法撤销。`
  );
  if (!confirmed) return;
  try {
    await apiFetch(`/api/data/${encodeURIComponent(currentUid)}`, { method: "DELETE" });
    localStorage.removeItem("lastFollowerUid");
    uidInput.value = "";
    currentUid = "";
    currentDiff = null;
    clearDataButton.disabled = true;
    resetResults();
    historyList.innerHTML = '<div class="history-empty">本地记录已清除，当前未建立基线。首次检查会建立新的基线。</div>';
    setStatus("准备就绪", "可以输入 UID 开始建立新的粉丝快照。", "ready");
    showToast("当前 UID 的本地数据已清除");
  } catch (error) {
    showToast(error.message, true);
  }
});

async function pollTask(taskId) {
  if (taskId !== currentTaskId) return;
  try {
    const task = await apiFetch(`/api/tasks/${taskId}`);
    const progress = task.progress || {};
    updateProgress(progress);

    if (["queued", "running", "cancelling"].includes(task.status)) {
      window.setTimeout(() => pollTask(taskId), 650);
      return;
    }
    currentTaskId = null;
    setCheckState("idle");
    if (task.status === "cancelled") {
      setStatus("检查已中止", "本次未保存快照或变化报告。", "cancelled");
      showToast("检查已中止，已有历史记录未受影响");
      return;
    }
    if (task.status === "error") {
      setStatus("检查失败", task.error || "未知错误", "error");
      showToast(task.error || "检查失败", true);
      return;
    }
    renderTaskResult(task.result);
    await loadHistory(task.uid);
  } catch (error) {
    currentTaskId = null;
    setCheckState("idle");
    setStatus("连接中断", error.message, "error");
    showToast(error.message, true);
  }
}

function updateProgress(progress) {
  const phaseTitles = {
    queued: "任务排队中",
    connecting: "验证登录状态",
    fetching: "读取粉丝列表",
    validating: "校验名单完整性",
    comparing: "比较两次快照",
    cancelling: "正在中止检查",
    cancelled: "检查已中止",
    done: "检查完成",
  };
  const collected = Number(progress.collected || 0);
  const total = progress.total == null ? null : Number(progress.total);
  const statusState = progress.phase === "done"
    ? "done"
    : progress.phase === "cancelled" ? "cancelled" : "running";
  setStatus(phaseTitles[progress.phase] || "正在检查", progress.message || "请稍候", statusState);
  setProgress(total ? Math.min(100, (collected / Math.min(total, 1000)) * 100) : null, collected, total, progress.page || 0);
}

function renderTaskResult(result) {
  currentDiff = result.diff || null;
  const diff = result.diff || {};
  updateStats(result.current_count || 0, diff);
  updateTabCounts(diff);

  if (!result.comparison_ready) {
    setStatus("名单不可比较", "快照已保存，但完整性和时间边界校验均未通过。", "error");
    showNotice((result.warnings || []).join("；") || "名单未通过比较条件校验", true);
  } else if (result.baseline) {
    const partial = Boolean(result.partial_comparable);
    setStatus(
      partial ? "时间边界基线已建立" : "基线已建立",
      partial
        ? "这是第一个通过时间边界校验的快照，下次检查将比较可确认范围。"
        : "这是第一个完整快照。下次检查时将自动比较变化。",
      "done",
    );
    showNotice(
      partial
        ? "粉丝超过1000；已保存可见窗口和关注时间边界，至少再检查一次才能判断变化。"
        : "已保存首个完整快照；至少需要两次检查才能判断变化。",
      false,
    );
  } else {
    const removed = Number(diff.suspected_unfollow_count || 0);
    const added = Number(diff.new_follow_count || 0);
    const bounded = diff.comparison_mode === "time_boundary";
    setStatus(
      bounded ? "时间边界检查完成" : "检查完成",
      `发现 ${removed} 位疑似取关、${added} 位新增关注。${bounded ? " 边界外变化未计入。" : ""}`,
      "done",
    );
    const warnings = [...(result.warnings || []), ...(diff.warnings || [])];
    if (warnings.length) showNotice(warnings.join("；"), diff.confidence === "low");
    else hideNotice();
  }
  setProgress(100, result.visible_count || result.current_count || 0, result.expected_total, result.pages || 0);
  renderResultList();
}

function updateStats(currentCount, diff) {
  document.getElementById("stat-current").textContent = formatNumber(currentCount);
  document.getElementById("stat-removed").textContent = formatNumber(diff.suspected_unfollow_count || 0);
  document.getElementById("stat-added").textContent = formatNumber(diff.new_follow_count || 0);
  document.getElementById("stat-renamed").textContent = formatNumber(diff.renamed_count || 0);
  const net = Number(diff.net_change || 0);
  document.getElementById("stat-net").textContent = `${net > 0 ? "+" : ""}${net} 净变化`;
}

function updateTabCounts(diff) {
  document.getElementById("tab-removed-count").textContent = diff.suspected_unfollow_count || 0;
  document.getElementById("tab-added-count").textContent = diff.new_follow_count || 0;
  document.getElementById("tab-renamed-count").textContent = diff.renamed_count || 0;
}

function renderResultList() {
  if (!currentDiff) return;
  let rows = [];
  if (activeTab === "removed") rows = currentDiff.suspected_unfollows || [];
  if (activeTab === "added") rows = currentDiff.new_follows || [];
  if (activeTab === "renamed") rows = currentDiff.renamed || [];

  if (!rows.length) {
    resultList.innerHTML = `
      <div class="empty-state">
        <div class="empty-orbit" aria-hidden="true"><i></i></div>
        <strong>这一类没有变化</strong>
        <p>当前快照中没有发现对应记录。</p>
      </div>`;
    return;
  }

  resultList.innerHTML = rows.map((item) => {
    const uid = escapeHtml(String(item.uid || ""));
    const displayName = activeTab === "renamed"
      ? `${escapeHtml(item.before || "未命名")} → ${escapeHtml(item.after || "未命名")}`
      : escapeHtml(item.name || "未命名用户");
    const avatarLetter = escapeHtml(((activeTab === "renamed" ? item.after : item.name) || "?").trim().slice(0, 1));
    const followedAt = item.followed_at ? formatDate(item.followed_at) : null;
    const followedLabel = followedAt
      ? `关注于 ${followedAt.date} ${followedAt.time}`
      : "关注时间未记录";
    const mutualLabel = item.mutual === true
      ? "已互关"
      : item.mutual === false ? "未互关" : "互关状态未知";
    const mutualClass = item.mutual === true
      ? "mutual"
      : item.mutual === false ? "one-way" : "unknown";
    return `
      <div class="person-row">
        <div class="person-main">
          <span class="person-avatar">${avatarLetter}</span>
          <span class="person-text">
            <strong>${displayName}</strong>
            <small class="person-uid">UID ${uid}</small>
            <span class="person-meta">
              <span>${escapeHtml(followedLabel)}</span>
              <span class="relation-badge ${mutualClass}">${mutualLabel}</span>
            </span>
          </span>
        </div>
        <a class="profile-link" href="https://space.bilibili.com/${encodeURIComponent(item.uid || "")}" target="_blank" rel="noopener noreferrer">打开空间</a>
      </div>`;
  }).join("");
}

async function loadHistory(uid) {
  if (!uid) return;
  try {
    const payload = await apiFetch(`/api/history/${encodeURIComponent(uid)}`);
    const reports = payload.reports || [];
    if (!reports.length) {
      historyList.innerHTML = payload.baseline_established
        ? '<div class="history-empty">已建立基线，下一次可比较检查后会生成变化记录。</div>'
        : '<div class="history-empty">未建立基线。首次检查会建立基线，第二次可比较检查后才会生成变化记录。</div>';
      return;
    }
    historyList.innerHTML = reports.map((item) => {
      const date = formatDate(item.captured_at);
      const net = Number(item.net_change || 0);
      return `
        <div class="history-item">
          <div class="history-time"><strong>${escapeHtml(date.date)}</strong><small>${escapeHtml(date.time)}</small></div>
          <div class="history-summary">
            <span>当前 <b>${formatNumber(item.current_count)}</b></span>
            <span>疑似取关 <b>${formatNumber(item.suspected_unfollow_count)}</b></span>
            <span>新增 <b>${formatNumber(item.new_follow_count)}</b></span>
            <span>净变化 <b>${net > 0 ? "+" : ""}${net}</b></span>
          </div>
          <button class="history-open" type="button" data-file="${escapeHtml(item.file)}">查看</button>
        </div>`;
    }).join("");
    historyList.querySelectorAll(".history-open").forEach((button) => {
      button.addEventListener("click", () => openHistory(uid, button.dataset.file));
    });
  } catch (error) {
    historyList.innerHTML = `<div class="history-empty">${escapeHtml(error.message)}</div>`;
  }
}

async function openHistory(uid, filename) {
  try {
    const report = await apiFetch(`/api/reports/${encodeURIComponent(uid)}/${encodeURIComponent(filename)}`);
    currentDiff = report;
    updateStats(report.current_count || 0, report);
    updateTabCounts(report);
    renderResultList();
    const bounded = report.comparison_mode === "time_boundary";
    setStatus(
      bounded ? "正在查看时间边界报告" : "正在查看历史报告",
      `${formatDate(report.current_captured_at).date} 的检查结果${bounded ? "（仅含边界内可确认变化）" : ""}`,
      "done",
    );
    document.querySelector(".results-card").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    showToast(error.message, true);
  }
}

async function apiFetch(url, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";
  if (options.method && options.method !== "GET") headers["X-App-Token"] = appToken;
  const response = await fetch(url, { ...options, headers, cache: "no-store" });
  let payload = {};
  try { payload = await response.json(); } catch (_) { payload = {}; }
  if (!response.ok) throw new Error(payload.error || `请求失败（HTTP ${response.status}）`);
  return payload;
}

function setStatus(title, message, state) {
  statusTitle.textContent = title;
  statusMessage.textContent = message;
  statusBadge.className = `status-badge ${state}`;
  statusBadge.textContent = { ready: "等待开始", running: "进行中", done: "已完成", error: "需注意", cancelled: "已中止" }[state] || state;
}

function setProgress(percent, collected, total, page) {
  progressTrack.classList.toggle("indeterminate", percent == null);
  const rounded = percent == null ? 0 : Math.max(0, Math.min(100, Math.round(percent / 5) * 5));
  progressTrack.dataset.progress = String(rounded);
  progressTrack.setAttribute("aria-valuenow", String(rounded));
  progressPage.textContent = page ? `已读取第 ${page} 页` : "尚未请求";
  progressCount.textContent = `${formatNumber(collected || 0)} / ${total == null ? "—" : formatNumber(total)}`;
}

function setCheckState(state) {
  checkState = state;
  const busy = state !== "idle";
  const cancellable = state === "running";
  submitButton.disabled = state === "starting" || state === "cancelling";
  submitButton.classList.toggle("cancel", state === "running" || state === "cancelling");
  submitButton.querySelector("span").textContent = {
    idle: "开始检查粉丝变化",
    starting: "正在启动检查",
    running: "中止本次检查",
    cancelling: "正在中止检查",
  }[state];
  submitButton.querySelector("b").textContent = cancellable ? "×" : "→";
  submitButton.setAttribute("aria-label", cancellable ? "中止本次检查" : "开始检查粉丝变化");
  uidInput.disabled = busy;
  secretInput.disabled = busy;
  toggleSecret.disabled = busy;
  delayInput.disabled = busy;
  clearDataButton.disabled = busy || !currentUid;
}

function showNotice(message, isError) {
  resultNotice.textContent = message;
  resultNotice.classList.remove("hidden");
  resultNotice.classList.toggle("error", Boolean(isError));
}

function hideNotice() {
  resultNotice.classList.add("hidden");
  resultNotice.classList.remove("error");
}

function resetResults() {
  currentDiff = null;
  updateStats(0, emptyDiff());
  updateTabCounts(emptyDiff());
  document.getElementById("stat-current").textContent = "—";
  document.getElementById("stat-removed").textContent = "—";
  document.getElementById("stat-added").textContent = "—";
  document.getElementById("stat-renamed").textContent = "—";
  document.getElementById("stat-net").textContent = "等待检查";
  hideNotice();
  resultList.innerHTML = '<div class="empty-state"><div class="empty-orbit" aria-hidden="true"><i></i></div><strong>结果会显示在这里</strong><p>建立两次完整快照或通过校验的时间边界快照后，就能看到可确认范围内的变化。</p></div>';
  setProgress(0, 0, null, 0);
}

function emptyDiff() {
  return { suspected_unfollow_count: 0, new_follow_count: 0, renamed_count: 0, net_change: 0 };
}

function showToast(message, isError = false) {
  window.clearTimeout(toastTimer);
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.add("show");
  toastTimer = window.setTimeout(() => toast.classList.remove("show"), 3500);
}

function formatNumber(value) {
  return Number(value || 0).toLocaleString("zh-CN");
}

function formatDate(value) {
  if (!value) return { date: "未知日期", time: "" };
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return { date: value, time: "" };
  return {
    date: new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" }).format(date),
    time: new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(date),
  };
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
