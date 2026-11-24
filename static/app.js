"use strict";

const appToken = document.querySelector('meta[name="app-token"]').content;
const form = document.getElementById("check-form");
const submitButton = document.getElementById("submit-button");
const scanPanel = document.getElementById("scan-panel");
const scanCode = document.getElementById("scan-code");
const scanMessage = document.getElementById("scan-message");
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
const accountCard = document.getElementById("account-card");
const accountFace = document.getElementById("account-face");
const accountFallback = document.getElementById("account-fallback");
const accountName = document.getElementById("account-name");
const accountUid = document.getElementById("account-uid");
const accountSessionLabel = document.getElementById("account-session-label");
const switchAccountButton = document.getElementById("switch-account");
const toast = document.getElementById("toast");
const resultTools = document.getElementById("result-tools");
const pagination = document.getElementById("pagination");
const previousPage = document.getElementById("previous-page");
const nextPage = document.getElementById("next-page");
const pageSummary = document.getElementById("page-summary");
const lookupControls = document.getElementById("lookup-controls");
const lookupButton = document.getElementById("lookup-button");
const lookupMessage = document.getElementById("lookup-message");
const PAGE_SIZE = 100;

let currentTaskId = null;
let checkState = "idle";
let currentAccount = readStoredAccount();
let currentUid = currentAccount?.uid || localStorage.getItem("lastFollowerUid") || "";
let currentDiff = null;
let currentReport = null;
let currentPage = 1;
let lookupTask = null;
let lookupStarting = false;
let lookupText = "";
let activeTab = "removed";
let toastTimer = null;
let sessionAuthenticated = false;
let sessionSyncing = true;

if (currentUid) {
  showAccount(currentAccount || { uid: currentUid, name: "上次检查账号", face: "" }, false);
  clearDataButton.disabled = false;
  loadHistory(currentUid);
}

delayInput.addEventListener("input", () => {
  delayValue.value = `${delayInput.value} 秒`;
});

previousPage.addEventListener("click", () => {
  currentPage--;
  renderResultList();
  resultList.scrollIntoView({ block: "start" });
});
nextPage.addEventListener("click", () => {
  currentPage++;
  renderResultList();
  resultList.scrollIntoView({ block: "start" });
});

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => {
      item.classList.toggle("active", item === button);
      item.setAttribute("aria-selected", item === button ? "true" : "false");
    });
    activeTab = button.dataset.tab;
    currentPage = 1;
    lookupText = "";
    renderResultList();
  });
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (checkState === "running") {
    await cancelCurrentTask();
    return;
  }
  await startCheck();
});

async function startCheck() {
  if (checkState !== "idle" || lookupTask || lookupStarting || sessionSyncing) return;

  setCheckState("starting");
  setStatus("正在开始", "正在创建本地检查任务", "running");
  setProgress(null, 0, null, 0);
  try {
    const response = await apiFetch("/api/check", {
      method: "POST",
      body: JSON.stringify({ delay: Number(delayInput.value) }),
    });
    currentTaskId = response.task_id;
    setCheckState("running");
    await pollTask(response.task_id);
  } catch (error) {
    setCheckState("idle");
    setStatus("检查失败", error.message, "error");
    showToast(error.message, true);
  }
}

function applySession(session) {
  sessionAuthenticated = Boolean(session?.authenticated && session.account);
  if (sessionAuthenticated) {
    const switched = currentUid !== String(session.account.uid);
    if (switched) resetResults();
    showAccount(session.account);
    if (switched) loadHistory(currentUid);
  }
  accountSessionLabel.textContent = sessionAuthenticated ? "当前登录账号" : "上次检查账号";
  setCheckState(checkState);
}

async function refreshSession() {
  try {
    const session = await apiFetch("/api/session");
    applySession(session);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    sessionSyncing = false;
    setCheckState(checkState);
  }
}

switchAccountButton.addEventListener("click", async () => {
  if (checkState !== "idle" || lookupTask || lookupStarting || sessionSyncing) return;
  setCheckState("switching");
  try {
    await apiFetch("/api/session", { method: "DELETE" });
    sessionAuthenticated = false;
    localStorage.removeItem("lastFollowerUid");
    localStorage.removeItem("lastFollowerAccount");
    currentAccount = null;
    currentUid = "";
    accountCard.classList.add("hidden");
    accountSessionLabel.textContent = "上次检查账号";
    resetResults();
    historyList.innerHTML = '<div class="history-empty">等待新账号扫码，原账号的本地历史仍然保留。</div>';
    setCheckState("idle");
    await startCheck();
  } catch (error) {
    setCheckState("idle");
    showToast(error.message, true);
  }
});

async function cancelCurrentTask() {
  if (!currentTaskId || checkState !== "running") return;
  setCheckState("cancelling");
  showScanCode(null);
  setStatus("正在中止", "本次检查产生的数据将全部丢弃，请稍候。", "running");
  try {
    await apiFetch(`/api/tasks/${currentTaskId}/cancel`, { method: "POST" });
    showToast("已发送中止请求，本次结果不会保存");
  } catch (error) {
    setCheckState("running");
    showToast(error.message, true);
  }
}

lookupButton.addEventListener("click", async () => {
  if (lookupStarting || checkState !== "idle") return;
  if (lookupTask) {
    const pending = lookupTask;
    pending.cancelling = true;
    renderResultList();
    try {
      await apiFetch(`/api/tasks/${pending.id}/cancel`, { method: "POST" });
    } catch (error) {
      pending.cancelling = false;
      showToast(error.message, true);
      renderResultList();
    }
    return;
  }
  if (!currentReport || !resultRows().length) return;
  const { uid, file } = currentReport;
  const diff = currentDiff;
  const tab = activeTab;
  lookupStarting = true;
  setCheckState(checkState);
  try {
    const response = await apiFetch(`/api/reports/${encodeURIComponent(uid)}/${encodeURIComponent(file)}/lookup/${tab}`, { method: "POST" });
    lookupTask = { id: response.task_id, diff, tab, cancelling: false };
    lookupText = "正在启动补查…";
    await pollLookup(lookupTask);
  } catch (error) {
    lookupText = error.message;
    showToast(error.message, true);
  } finally {
    lookupStarting = false;
    setCheckState(checkState);
  }
});

async function pollLookup(pending) {
  if (lookupTask !== pending) return;
  try {
    const task = await apiFetch(`/api/tasks/${pending.id}`);
    if (task.session) applySession(task.session);
    if (["queued", "running", "cancelling"].includes(task.status)) {
      lookupText = pending.cancelling ? "正在中止补查…" : task.progress.message;
      renderResultList();
      window.setTimeout(() => pollLookup(pending), 650);
      return;
    }
    if (task.status === "done") {
      const updates = task.result.updates;
      for (const row of resultRows(pending.diff, pending.tab)) {
        if (Object.hasOwn(updates, row.uid)) Object.assign(row, updates[row.uid]);
      }
      lookupText = task.result.warning || `补查完成，更新 ${formatNumber(Object.keys(updates).length)} 人。`;
    } else {
      lookupText = task.status === "cancelled" ? "补查已中止，原有信息未改动。" : (task.error || "补查失败");
    }
  } catch (error) {
    lookupText = error.message;
  }
  lookupTask = null;
  setCheckState(checkState);
}

clearDataButton.addEventListener("click", async () => {
  if (!currentUid) return;
  const confirmed = window.confirm(
    `确定删除 ${currentAccount?.name || `UID ${currentUid}`} 的全部本地快照和报告吗？此操作无法撤销。`
  );
  if (!confirmed) return;
  try {
    await apiFetch(`/api/data/${encodeURIComponent(currentUid)}`, { method: "DELETE" });
    localStorage.removeItem("lastFollowerUid");
    localStorage.removeItem("lastFollowerAccount");
    currentAccount = null;
    currentUid = "";
    accountCard.classList.add("hidden");
    currentDiff = null;
    clearDataButton.disabled = true;
    resetResults();
    historyList.innerHTML = '<div class="history-empty">本地记录已清除，当前未建立基线。首次检查会建立新的基线。</div>';
    setStatus("准备就绪", "可以扫码登录并建立新的粉丝快照。", "ready");
    await refreshSession();
    showToast("当前账号的本地数据已清除");
  } catch (error) {
    showToast(error.message, true);
  }
});

async function pollTask(taskId) {
  if (taskId !== currentTaskId) return;
  try {
    const task = await apiFetch(`/api/tasks/${taskId}`);
    if (taskId !== currentTaskId) return;
    if (task.session) applySession(task.session);
    if (task.account) {
      const switched = currentUid !== task.account.uid;
      if (switched) resetResults();
      showAccount(task.account);
      if (switched) loadHistory(task.account.uid);
    }
    const progress = task.progress || {};
    showScanCode(task.qr_image, progress.message);
    updateProgress(progress);

    if (["queued", "running", "cancelling"].includes(task.status)) {
      window.setTimeout(() => pollTask(taskId), 650);
      return;
    }
    currentTaskId = null;
    setCheckState("idle");
    if (task.status === "cancelled") {
      setProgress(0, 0, null, 0);
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
    showScanCode(null);
    setCheckState("idle");
    setStatus("连接中断", error.message, "error");
    showToast(error.message, true);
  }
}

function showScanCode(image, message) {
  if (!image) {
    scanCode.removeAttribute("src");
    scanPanel.classList.add("hidden");
    return;
  }
  if (scanCode.getAttribute("src") !== image) scanCode.src = image;
  scanMessage.textContent = message || "请用 B 站 App 扫码并确认";
  scanPanel.classList.remove("hidden");
}

function updateProgress(progress) {
  const phase = checkState === "cancelling" && progress.phase !== "cancelled"
    ? "cancelling" : progress.phase;
  const phaseTitles = {
    queued: "任务排队中",
    connecting: "申请登录二维码",
    waiting_scan: "等待扫码",
    scanned: "等待手机确认",
    identified: "账号识别成功",
    fetching: "读取粉丝列表",
    validating: "核对参考人数",
    enriching: "刷新最近粉丝信息",
    comparing: "比较两次快照",
    cancelling: "正在中止检查",
    cancelled: "检查已中止",
    done: "检查完成",
  };
  const collected = Number(progress.collected || 0);
  const total = progress.total == null ? null : Number(progress.total);
  const statusState = phase === "done"
    ? "done"
    : phase === "cancelled" ? "cancelled" : "running";
  setStatus(phaseTitles[phase] || "正在检查", phase !== progress.phase ? "正在中止，本次结果不会保存" : progress.message || "请稍候", statusState);
  if (phase === "cancelling" || phase === "cancelled") {
    setProgress(0, 0, null, 0);
    return;
  }
  setProgress(total ? Math.min(100, (collected / total) * 100) : null, collected, total, progress.page || 0);
}

function renderTaskResult(result) {
  currentDiff = result.diff || null;
  currentReport = result.report_file ? { uid: result.uid, file: result.report_file } : null;
  currentPage = 1;
  lookupText = "";
  const diff = result.diff || {};
  updateStats(result.expected_total, diff, result.visible_count);
  updateTabCounts(diff);

  if (result.baseline) {
    setStatus("基线已建立", "已保存这次读到的名单。下次检查时将自动比较变化。", "done");
    showNotice(["已保存首个快照；再检查一次就能判断变化。", ...(result.warnings || [])].join(" "), false);
  } else {
    const removed = Number(diff.suspected_unfollow_count || 0);
    const added = Number(diff.new_follow_count || 0);
    const legacy = diff.comparison_mode === "legacy_window";
    setStatus(
      "检查完成",
      legacy ? `旧名单中发现 ${removed} 位疑似取关，本次不统计新增。` : `发现 ${removed} 位疑似取关、${added} 位新增关注。`,
      "done",
    );
    const warnings = diff.warnings || [];
    if (warnings.length) showNotice(warnings.join("；"), diff.confidence === "low");
    else hideNotice();
  }
  setProgress(100, result.visible_count || result.current_count || 0, result.expected_total, result.pages || 0);
  renderResultList();
}

function listCountNote(total, listed) {
  return Number.isInteger(listed) && listed !== total ? `（名单人数：${formatNumber(listed)} 人）` : "";
}

function updateStats(platformTotal, diff, listedCount) {
  const countNote = document.getElementById("stat-count-note");
  countNote.textContent = listCountNote(platformTotal, listedCount);
  countNote.hidden = !countNote.textContent;
  document.getElementById("stat-current").textContent = platformTotal == null ? "—" : formatNumber(platformTotal);
  document.getElementById("stat-removed").textContent = formatNumber(diff.suspected_unfollow_count || 0);
  document.getElementById("stat-added").textContent = diff.comparison_mode === "legacy_window" ? "—" : formatNumber(diff.new_follow_count || 0);
  document.getElementById("stat-renamed").textContent = formatNumber(diff.renamed_count || 0);
  const net = Number(diff.net_change || 0);
  document.getElementById("stat-net").textContent = diff.net_change === null
    ? "净变化未统计" : `${net > 0 ? "+" : ""}${net} 净变化`;
}

function updateTabCounts(diff) {
  document.getElementById("tab-removed-count").textContent = diff.suspected_unfollow_count || 0;
  document.getElementById("tab-added-count").textContent = diff.comparison_mode === "legacy_window" ? "—" : (diff.new_follow_count || 0);
  document.getElementById("tab-renamed-count").textContent = diff.renamed_count || 0;
}

function resultRows(diff = currentDiff, tab = activeTab) {
  const key = { removed: "suspected_unfollows", added: "new_follows", renamed: "renamed" }[tab];
  return diff?.[key] || [];
}

function renderResultList() {
  if (!currentDiff) return;
  const rows = resultRows();
  const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  currentPage = Math.min(Math.max(1, currentPage), pages);
  const showLookup = Boolean(currentReport && rows.length);
  resultTools.classList.toggle("hidden", pages === 1 && !showLookup && !lookupTask);
  pagination.classList.toggle("hidden", pages === 1);
  pageSummary.textContent = `第 ${currentPage} / ${pages} 页 · 共 ${formatNumber(rows.length)} 人`;
  previousPage.disabled = currentPage === 1;
  nextPage.disabled = currentPage === pages;
  lookupControls.classList.toggle("hidden", !showLookup && !lookupTask);
  lookupButton.disabled = checkState !== "idle" || lookupStarting || Boolean(lookupTask?.cancelling);
  lookupButton.textContent = lookupTask ? (lookupTask.cancelling ? "正在中止" : "中止补查") : "补查状态";
  lookupMessage.textContent = lookupText || "按 UID 逐个更新关注时间和互关状态";

  if (!rows.length) {
    resultList.innerHTML = `
      <div class="empty-state">
        <div class="empty-orbit" aria-hidden="true"><i></i></div>
        <strong>这一类没有变化</strong>
        <p>${activeTab === "added" && currentDiff.comparison_mode === "legacy_window" ? "旧基线覆盖有限，本次不统计新增。" : "当前快照中没有发现对应记录。"}</p>
      </div>`;
    return;
  }

  resultList.innerHTML = rows.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE).map((item) => {
    const uid = escapeHtml(String(item.uid || ""));
    const displayName = activeTab === "renamed"
      ? `${escapeHtml(item.before || "未命名")} → ${escapeHtml(item.after || "未命名")}`
      : escapeHtml(item.name || "未命名用户");
    const avatarLetter = escapeHtml(((activeTab === "renamed" ? item.after : item.name) || "?").trim().slice(0, 1));
    const followedAt = item.followed_at ? formatDate(item.followed_at) : null;
    const followedLabel = followedAt ? `${item.follows_me === false ? "曾关注于" : "关注于"} ${followedAt.date} ${followedAt.time}` : "";
    const relation = item.relationship || (item.mutual === true ? "mutual" : item.mutual === false ? "follower" : "");
    const relationLabel = { mutual: "已互关", follower: "我的粉丝", following: "我的关注", stranger: "陌生人" }[relation];
    return `
      <div class="person-row">
        <div class="person-main">
          <span class="person-avatar">${avatarLetter}</span>
          <span class="person-text">
            <strong>${displayName}</strong>
            <small class="person-uid">UID ${uid}</small>
            ${followedLabel || relationLabel ? `<span class="person-meta">
              ${followedLabel ? `<span>${escapeHtml(followedLabel)}</span>` : ""}
              ${relationLabel ? `<span class="relation-badge ${relation === "mutual" ? "mutual" : "one-way"}">${relationLabel}</span>` : ""}
            </span>` : ""}
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
    if (uid !== currentUid) return;
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
            <span>当前 <b>${item.platform_total == null ? "—" : formatNumber(item.platform_total)}</b>${listCountNote(item.platform_total, item.current_visible_count)}</span>
            <span>疑似取关 <b>${formatNumber(item.suspected_unfollow_count)}</b></span>
            <span>新增 <b>${item.comparison_mode === "legacy_window" ? "—" : formatNumber(item.new_follow_count)}</b></span>
            <span>净变化 <b>${item.net_change === null ? "—" : `${net > 0 ? "+" : ""}${net}`}</b></span>
          </div>
          <button class="history-open" type="button" data-file="${escapeHtml(item.file)}">查看</button>
        </div>`;
    }).join("");
    historyList.querySelectorAll(".history-open").forEach((button) => {
      button.addEventListener("click", () => openHistory(uid, button.dataset.file));
    });
  } catch (error) {
    if (uid === currentUid) historyList.innerHTML = `<div class="history-empty">${escapeHtml(error.message)}</div>`;
  }
}

async function openHistory(uid, filename) {
  if (checkState !== "idle" || lookupTask || lookupStarting) return;
  try {
    const report = await apiFetch(`/api/reports/${encodeURIComponent(uid)}/${encodeURIComponent(filename)}`);
    if (uid !== currentUid || checkState !== "idle" || lookupTask || lookupStarting) return;
    currentDiff = report;
    currentReport = { uid, file: filename };
    currentPage = 1;
    if (!lookupTask) lookupText = "";
    updateStats(report.platform_total, report, report.current_visible_count);
    updateTabCounts(report);
    renderResultList();
    const bounded = report.comparison_mode === "time_boundary";
    const legacy = report.comparison_mode === "legacy_window";
    if (report.warnings?.length) showNotice(report.warnings.join("；"), report.confidence === "low");
    else hideNotice();
    setStatus(
      bounded ? "正在查看时间边界报告" : "正在查看历史报告",
      `${formatDate(report.current_captured_at).date} 的检查结果${bounded ? "（仅含边界内可确认变化）" : legacy ? "（旧基线覆盖有限）" : ""}`,
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
  headers["X-App-Token"] = appToken;
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
  if (state === "cancelling") setProgress(0, 0, null, 0);
  const busy = state !== "idle";
  const cancellable = state === "running";
  const lookupBusy = lookupStarting || Boolean(lookupTask);
  submitButton.disabled = !["idle", "running"].includes(state) || lookupBusy || sessionSyncing;
  submitButton.classList.toggle("cancel", state === "running" || state === "cancelling");
  submitButton.querySelector("span").textContent = {
    idle: sessionAuthenticated ? "开始检查粉丝变化" : "扫码并检查粉丝变化",
    starting: "正在启动检查",
    running: "中止本次检查",
    cancelling: "正在中止检查",
    switching: "正在切换账号",
  }[state];
  submitButton.querySelector("b").textContent = cancellable ? "×" : "→";
  submitButton.setAttribute("aria-label", cancellable ? "中止本次检查" : sessionAuthenticated ? "开始检查粉丝变化" : "扫码并检查粉丝变化");
  delayInput.disabled = busy || lookupBusy;
  clearDataButton.disabled = busy || lookupBusy || !currentUid;
  switchAccountButton.disabled = busy || lookupBusy || sessionSyncing;
  switchAccountButton.classList.toggle("hidden", !sessionAuthenticated);
  if (!cancellable) showScanCode(null);
  renderResultList();
}

function readStoredAccount() {
  try {
    const value = JSON.parse(localStorage.getItem("lastFollowerAccount") || "null");
    return value && /^\d+$/.test(String(value.uid || "")) ? value : null;
  } catch (_) {
    return null;
  }
}

function showAccount(account, persist = true) {
  const uid = String(account.uid || "");
  if (!/^\d+$/.test(uid)) return;
  const name = String(account.name || "未命名用户");
  const face = String(account.face || "");
  currentAccount = { uid, name, face };
  currentUid = uid;
  accountName.textContent = name;
  accountUid.textContent = `UID ${uid}`;
  accountFallback.textContent = name.trim().slice(0, 1) || "?";
  accountFace.hidden = true;
  accountFace.removeAttribute("src");
  if (face) {
    accountFace.onload = () => {
      accountFace.hidden = false;
      accountFallback.hidden = true;
    };
    accountFace.onerror = () => {
      accountFace.hidden = true;
      accountFallback.hidden = false;
    };
    accountFallback.hidden = false;
    accountFace.src = face;
  } else {
    accountFallback.hidden = false;
  }
  accountCard.classList.remove("hidden");
  clearDataButton.disabled = checkState !== "idle";
  if (persist) {
    localStorage.setItem("lastFollowerUid", uid);
    localStorage.setItem("lastFollowerAccount", JSON.stringify(currentAccount));
  }
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
  currentReport = null;
  currentPage = 1;
  lookupText = "";
  resultTools.classList.add("hidden");
  updateStats(null, emptyDiff());
  updateTabCounts(emptyDiff());
  document.getElementById("stat-removed").textContent = "—";
  document.getElementById("stat-added").textContent = "—";
  document.getElementById("stat-renamed").textContent = "—";
  document.getElementById("stat-net").textContent = "等待检查";
  hideNotice();
  resultList.innerHTML = '<div class="empty-state"><div class="empty-orbit" aria-hidden="true"><i></i></div><strong>结果会显示在这里</strong><p>检查完成后会和上一次可用快照对比。没有旧快照时，先建立基线。</p></div>';
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

setCheckState("idle");
const initialSessionRefresh = refreshSession();
