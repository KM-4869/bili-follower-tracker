"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");

function setup(fetch = async () => ({ ok: true, json: async () => ({}) }), session = { authenticated: false, account: null }, storedAccount = null) {
  const elements = new Map();
  const storage = new Map();
  if (storedAccount) storage.set("lastFollowerAccount", JSON.stringify(storedAccount));
  function getElement(id) {
    if (!elements.has(id)) {
      const classes = new Set();
      elements.set(id, {
        dataset: {}, attributes: {}, handlers: {}, textContent: "",
        classList: {
          add: (name) => classes.add(name),
          remove: (name) => classes.delete(name),
          contains: (name) => classes.has(name),
          toggle(name, force) {
            const add = force ?? !classes.has(name);
            if (add) classes.add(name);
            else classes.delete(name);
            return add;
          },
        },
        setAttribute(name, value) { this.attributes[name] = value; },
        getAttribute(name) { return this.attributes[name]; },
        removeAttribute(name) { delete this.attributes[name]; },
        querySelector: (selector) => getElement(`${id}:${selector}`),
        querySelectorAll: () => [],
        addEventListener(name, listener) { this.handlers[name] = listener; },
      });
    }
    return elements.get(id);
  }
  const context = vm.createContext({
    document: {
      querySelector: () => ({ content: "test-token" }),
      getElementById: getElement,
      querySelectorAll: () => [],
    },
    localStorage: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: (key) => storage.delete(key),
    },
    window: { setTimeout: () => 1, clearTimeout() {} },
    fetch: (url, options = {}) => url === "/api/session" && options.method !== "DELETE"
      ? Promise.resolve({ ok: true, json: async () => session }) : fetch(url, options),
  });
  vm.runInContext(script, context);
  return {
    run: (code) => vm.runInContext(code, context),
    element: getElement,
    ready: vm.runInContext("initialSessionRefresh", context),
    storage,
  };
}

function assertReset(ui) {
  const track = ui.element("progress-track");
  assert.equal(track.classList.contains("indeterminate"), false);
  assert.equal(track.dataset.progress, "0");
  assert.equal(track.attributes["aria-valuenow"], "0");
  assert.equal(ui.element("progress-count").textContent, "0 / —");
  assert.equal(ui.element("progress-page").textContent, "尚未请求");
}

test("cancellation clears the waiting animation even without a total", () => {
  const ui = setup();
  ui.run('updateProgress({phase: "waiting_scan"})');
  assert.equal(ui.element("progress-track").classList.contains("indeterminate"), true);
  ui.run('updateProgress({phase: "cancelled"})');
  assertReset(ui);
  assert.equal(ui.element("status-title").textContent, "检查已中止");
});

test("cancellation clears an existing numeric progress value", () => {
  const ui = setup();
  ui.run('updateProgress({phase: "fetching", collected: 50, total: 100, page: 2})');
  assert.equal(ui.element("progress-track").dataset.progress, "50");
  ui.run('updateProgress({phase: "cancelled", collected: 50, total: 100, page: 2})');
  assertReset(ui);
});

test("clicking cancel resets immediately and stale progress cannot restart it", async () => {
  let finishRequest;
  const ui = setup(() => new Promise((resolve) => { finishRequest = resolve; }));
  ui.run('currentTaskId = "task"; setCheckState("running"); updateProgress({phase: "waiting_scan"})');
  const cancel = ui.run("cancelCurrentTask()");
  assertReset(ui);
  ui.run('updateProgress({phase: "waiting_scan"})');
  assertReset(ui);
  assert.equal(ui.element("status-title").textContent, "正在中止检查");
  ui.run('updateProgress({phase: "cancelling", collected: 50, total: 100})');
  assertReset(ui);
  finishRequest({ ok: true, json: async () => ({}) });
  await cancel;
});

test("cancelled task resets the bar even when progress has no phase", async () => {
  const ui = setup(async () => ({
    ok: true,
    json: async () => ({ status: "cancelled", progress: {} }),
  }));
  ui.run('currentTaskId = "task"; setCheckState("running"); updateProgress({phase: "waiting_scan"})');
  await ui.run('pollTask("task")');
  assertReset(ui);
  assert.equal(ui.element("status-title").textContent, "检查已中止");
});

test("a new check can animate normally after cancellation", () => {
  const ui = setup();
  ui.run('setCheckState("cancelling"); updateProgress({phase: "cancelled"}); setCheckState("idle")');
  assertReset(ui);
  ui.run('setCheckState("running"); updateProgress({phase: "waiting_scan"})');
  assert.equal(ui.element("progress-track").classList.contains("indeterminate"), true);
  ui.run('updateProgress({phase: "fetching", collected: 10, total: 20, page: 1})');
  assert.equal(ui.element("progress-track").classList.contains("indeterminate"), false);
  assert.equal(ui.element("progress-track").dataset.progress, "50");
});

test("refreshing the page recovers an active server-side session", async () => {
  const account = { uid: "123", name: "当前账号", face: "" };
  const ui = setup(undefined, { authenticated: true, account });
  await ui.ready;
  assert.equal(ui.element("submit-button").querySelector("span").textContent, "开始检查粉丝变化");
  assert.equal(ui.element("switch-account").classList.contains("hidden"), false);
  assert.equal(ui.element("account-session-label").textContent, "当前登录账号");
  assert.equal(ui.storage.size, 2);
  assert.deepEqual(JSON.parse(ui.storage.get("lastFollowerAccount")), account);
});

test("a remembered profile is not treated as a surviving login after restart", async () => {
  const ui = setup(undefined, undefined, { uid: "123", name: "上次账号", face: "" });
  await ui.ready;
  assert.equal(ui.element("submit-button").querySelector("span").textContent, "扫码并检查粉丝变化");
  assert.equal(ui.element("switch-account").classList.contains("hidden"), true);
  assert.equal(ui.element("account-session-label").textContent, "上次检查账号");
});

test("switching accounts clears the old session then starts a fresh check", async () => {
  const requests = [];
  const ui = setup(async (url, options) => {
    requests.push({ url, options });
    const response = url === "/api/check" ? { task_id: "new-task" }
      : url === "/api/tasks/new-task" ? { status: "running", progress: {phase: "waiting_scan"} }
      : { authenticated: false, account: null };
    return { ok: true, json: async () => response };
  }, { authenticated: true, account: { uid: "123", name: "旧账号", face: "" } });
  await ui.ready;
  requests.length = 0;
  ui.run('delayInput.value = "1"');
  await ui.element("switch-account").handlers.click();
  assert.equal(requests[0].url, "/api/session");
  assert.equal(requests[0].options.method, "DELETE");
  assert.equal(requests[1].url, "/api/check");
  assert.deepEqual(JSON.parse(requests[1].options.body), { delay: 1 });
  assert.equal(ui.storage.size, 0);
  assert.equal(ui.run("sessionAuthenticated"), false);
  assert.equal(ui.run("currentUid"), "");
  assert.equal(ui.element("account-card").classList.contains("hidden"), true);
  assert.equal(ui.element("status-title").textContent, "等待扫码");
});

test("switch account is blocked while checking or looking up relationships", async () => {
  let deletions = 0;
  const ui = setup(async (url) => {
    if (url === "/api/session") deletions++;
    return { ok: true, json: async () => ({}) };
  }, { authenticated: true, account: { uid: "123", name: "账号", face: "" } });
  await ui.ready;
  ui.run('setCheckState("running")');
  assert.equal(ui.element("switch-account").disabled, true);
  await ui.element("switch-account").handlers.click();
  ui.run('setCheckState("idle"); lookupStarting = true; setCheckState("idle")');
  assert.equal(ui.element("switch-account").disabled, true);
  await ui.element("switch-account").handlers.click();
  assert.equal(deletions, 0);
});
