"use strict";
const $ = (id) => document.getElementById(id);
let sidebarKey = "";
let access = sessionStorage.getItem("agent-access") || "",
  projects = [],
  project = null,
  sessionId = null,
  conversationData = null,
  sessionMap = new Map(),
  detailCache = new Map(),
  selectedTaskId = null,
  detailPanel = "project",
  renderKey = "",
  drawerKey = "",
  loading = false,
  sending = false,
  reconnecting = false;
const activeStatuses = new Set([
  "queued",
  "running",
  "cancelling",
  "awaiting_plan",
  "awaiting_diff",
  "awaiting_database",
  "waiting_environment",
]);
const statusLabels = {
  idle: "新对话",
  queued: "等待执行",
  running: "执行中",
  cancelling: "正在停止并清理资源",
  completed: "已完成",
  failed: "执行中断",
  cancelled: "已停止",
  awaiting_plan: "等待实施范围确认",
  awaiting_diff: "等待你确认改动",
  awaiting_database: "等待删库确认",
  waiting_environment: "等待验证环境",
  passed: "验证通过",
  unavailable: "验证未运行",
  not_run: "未运行验证",
};
const nodeNames = {
  APPROVE_DATABASE: "等待你确认数据库删除",
  DELETE_DATABASE: "正在删除数据库并清理连接",
  RESPOND: "正在理解当前问题",
  RETRIEVE_CONTEXT: "正在读取代码与项目规范",
  PLAN: "正在制定实施计划",
  ANSWER: "正在整理回答",
  APPROVE_PLAN: "实施计划等待你确认",
  GENERATE_PATCH: "正在生成文件改动",
  SANDBOX_VERIFY: "正在运行隔离测试",
  CODE_REVIEW: "正在进行 AI 代码检查",
  PREPARE_DATABASE: "正在准备项目数据库",
  APPROVE_DIFF: "最终改动等待你确认",
  POWERSHELL_COMMANDS: "正在执行已批准的命令",
  WAIT_ENVIRONMENT: "等待验证环境",
};
const panels = [
  ["project", "项目与文件"],
  ["tools", "工具连接"],
  ["database", "业务数据库"],
  ["usage", "Token 用量"],
  ["plan", "实施计划"],
  ["diff", "文件改动"],
  ["tests", "验证报告"],
  ["sources", "引用依据"],
  ["events", "执行记录"],
  ["models", "模型交互"],
];
nodeNames.CANCELLING = "正在停止请求并清理测试资源";
function node(tag, text, cls) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (cls) el.className = cls;
  return el;
}
function error(message) {
  $("error").textContent = message;
  $("error").classList.toggle("hidden", !message);
}
function localError(id, message) {
  $(id).textContent = message;
  $(id).classList.toggle("hidden", !message);
}
function button(label, action, kind = "secondary") {
  const el = node("button", label, kind);
  el.type = "button";
  el.onclick = async () => {
    el.disabled = true;
    try {
      error("");
      await action();
    } catch (e) {
      error(e.message);
    } finally {
      el.disabled = false;
    }
  };
  return el;
}
function card(title) {
  const el = node("section", undefined, "card");
  el.append(node("h2", title, "section-title"));
  return el;
}
function pre(text) {
  return node(
    "pre",
    typeof text === "string" ? text : JSON.stringify(text, null, 2),
    "code",
  );
}
function badge(status) {
  return node(
    "span",
    statusLabels[status] || status,
    "badge " +
      (["completed", "passed"].includes(status)
        ? "good"
        : ["failed", "cancelled"].includes(status)
          ? "bad"
          : [
                "awaiting_plan",
                "awaiting_diff",
                "awaiting_database",
                "waiting_environment",
              ].includes(status)
            ? "wait"
            : ""),
  );
}
async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      ...options,
      headers: {
        Authorization: "Bearer " + access,
        "Content-Type": "application/json",
        ...(options.headers || {}),
      },
    });
  } catch {
    throw Error("暂时无法连接工作台，请稍后刷新。");
  }
  let value = await response.text();
  try {
    value = JSON.parse(value);
  } catch {}
  if (!response.ok)
    throw Error(
      value.error?.message || value.detail || "操作未完成，请刷新后重试。",
    );
  return value;
}
const date = (value) =>
  new Date(value).toLocaleString("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
function richText(text) {
  const el = node("div", undefined, "message-body");
  const parts = String(text || "").split(/```[^\n]*\n([\s\S]*?)```/g);
  parts.forEach((part, i) => el.append(i % 2 ? pre(part) : node("div", part)));
  return el;
}
function taskFor(id) {
  return conversationData?.tasks.find((t) => t.id === id);
}
function currentTask() {
  return taskFor(selectedTaskId) || conversationData?.tasks.at(-1);
}
async function taskDetail(id) {
  const t = taskFor(id);
  const saved = detailCache.get(id);
  if (saved && saved.task.updated_at === t?.updated_at) return saved;
  const data = await request("/api/tasks/" + encodeURIComponent(id));
  detailCache.set(id, data);
  return data;
}

async function login() {
  await request("/api/me");
  const env = await request("/api/environment");
  $("model-note").textContent = env.model_configured ? env.model : "模型未配置";
  $("project-runtime").querySelector('[value="java-mysql"]').disabled =
    !env.mysql_test_network;
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");
  sessionStorage.setItem("agent-access", access);
  await loadProjects();
  const remembered =
    sessionStorage.getItem("agent-active-project") ||
    localStorage.getItem("agent-project");
  const initial = projects.find((p) => p.id === remembered) || projects[0];
  if (initial) await selectProject(initial);
  else renderWelcome();
}
async function loadProjects() {
  projects = (await request("/api/projects")).filter((p) => p.enabled);
  await Promise.all(
    projects.map(async (item) =>
      sessionMap.set(
        item.id,
        await request(
          "/api/sessions?project_id=" + encodeURIComponent(item.id),
        ),
      ),
    ),
  );
  renderSidebar();
}
function renderSidebar() {
  const nextKey = JSON.stringify([
    project?.id,
    sessionId,
    projects.map((p) => [
      p.id,
      p.name,
      p.root,
      (sessionMap.get(p.id) || []).map((s) => [s.id, s.title, s.status]),
    ]),
  ]);
  if (nextKey === sidebarKey) return;
  sidebarKey = nextKey;
  const target = $("projects"),
    previous = target.scrollTop;
  target.replaceChildren();
  for (const item of projects) {
    const group = node("div", undefined, "project-group"),
      heading = node("div", undefined, "project-heading");
    const main = button(
      "",
      () => selectProject(item),
      "project-button" + (project?.id === item.id ? " active" : ""),
    );
    main.title = item.root;
    main.append(node("span", "▱", "folder-icon"), node("span", item.name));
    main.setAttribute("aria-label", "项目：" + item.name);
    const plus = button("＋", () => newChat(item), "icon-button project-new");
    plus.setAttribute("aria-label", "在" + item.name + "中新建对话");
    heading.append(main, recordMenuButton("project", item), plus);
    group.append(heading);
    const sessions = sessionMap.get(item.id) || [];
    if (project?.id === item.id) {
      const list = node("div", undefined, "session-list");
      for (const session of sessions) {
        const row = node("div", undefined, "session-row"),
          control = button(
            "",
            () => selectSession(session.id, item),
            "session-item" + (session.id === sessionId ? " active" : ""),
          );
        control.title = session.title;
        const dot = node(
          "span",
          "",
          "session-dot" +
            (["awaiting_plan", "awaiting_diff", "awaiting_database"].includes(
              session.status,
            )
              ? " wait"
              : session.status === "failed"
                ? " bad"
                : ""),
        );
        control.append(dot, node("span", session.title, "session-title"));
        control.setAttribute("aria-label", "对话：" + session.title);
        row.append(control, recordMenuButton("session", session));
        list.append(row);
      }
      if (!sessions.length)
        list.append(
          node("p", "还没有对话，开始一个新需求吧", "no-conversations"),
        );
      group.append(list);
    }
    target.append(group);
  }
  target.scrollTop = previous;
}
function rememberDraft() {
  if (sessionId)
    sessionStorage.setItem("agent-draft:" + sessionId, $("prompt").value);
}
async function selectProject(item) {
  error("");
  rememberDraft();
  project = item;
  sessionStorage.setItem("agent-active-project", item.id);
  localStorage.setItem("agent-project", item.id);
  $("breadcrumb").textContent = item.name;
  $("sidebar").classList.remove("open");
  sessionMap.set(
    item.id,
    await request("/api/sessions?project_id=" + encodeURIComponent(item.id)),
  );
  const sessions = sessionMap.get(item.id) || [];
  const remembered =
    sessionStorage.getItem("agent-active-session:" + item.id) ||
    localStorage.getItem("agent-session:" + item.id);
  const next = sessions.find((s) => s.id === remembered) || sessions[0];
  if (next) await selectSession(next.id, item);
  else {
    sessionId = null;
    conversationData = null;
    selectedTaskId = null;
    renderKey = "";
    $("chat-title").textContent = "新对话";
    $("prompt").value = "";
    renderSidebar();
    renderWelcome();
    updateComposer();
    if (!$("inspector").classList.contains("hidden"))
      await renderInspector(true);
  }
}
async function selectSession(id, item = project) {
  rememberDraft();
  project = item;
  sessionId = id;
  selectedTaskId = null;
  renderKey = "";
  drawerKey = "";
  conversationData = null;
  sessionStorage.setItem("agent-active-session:" + item.id, id);
  localStorage.setItem("agent-session:" + item.id, id);
  $("breadcrumb").textContent = item.name;
  $("prompt").value = sessionStorage.getItem("agent-draft:" + id) || "";
  $("mode").value =
    (sessionStorage.getItem("agent-mode:" + id) === "approval_write"
      ? "auto"
      : sessionStorage.getItem("agent-mode:" + id)) ||
    (item.allow_write ? "auto" : "readonly");
  $("engine").value = sessionStorage.getItem("agent-engine:" + id) || "model";
  $("sidebar").classList.remove("open");
  await loadConversation(true);
  renderSidebar();
}
async function newChat(item = project) {
  error("");
  if (!item) return openAddProject();
  rememberDraft();
  project = item;
  const row = await request("/api/sessions", {
    method: "POST",
    body: JSON.stringify({ project_id: item.id }),
  });
  sessionMap.set(item.id, [row, ...(sessionMap.get(item.id) || [])]);
  await selectSession(row.id, item);
  $("prompt").focus();
}
function renderWelcome() {
  const target = $("messages");
  target.replaceChildren();
  const el = node("div", undefined, "welcome");
  el.append(
    node("div", "✳", "welcome-symbol"),
    node(
      "h2",
      project ? "今天想在这个项目里做些什么？" : "你的下一个想法，从这里开始",
    ),
    node(
      "p",
      project
        ? "可以理解代码、完善功能，或把一个想法变成可运行的项目。"
        : "点击左侧工作项目旁的＋，添加一个已有项目或新建文件夹。",
    ),
  );
  const options = node("div", undefined, "suggestions");
  for (const [label, prompt] of [
    ["了解项目", "解释当前项目的结构、运行方式和主要功能，引用真实代码。"],
    ["完善功能", "请先分析当前项目，给出一个简单功能的实施计划。"],
    ["补充测试", "为当前项目的核心功能补充测试，先给出精确的修改计划。"],
  ])
    options.append(
      button(
        label,
        () => {
          $("prompt").value = prompt;
          $("prompt").focus();
        },
        "suggestion",
      ),
    );
  el.append(options);
  target.append(el);
}
async function loadConversation(force = false) {
  if (!sessionId) return;
  const id = sessionId;
  const data = await request("/api/sessions/" + encodeURIComponent(id));
  if (sessionId !== id) return;
  if (conversationData?.messages.length > 100 && data.messages.length) {
    const ids = new Set(data.messages.map((m) => m.id));
    data.messages = [
      ...conversationData.messages.filter(
        (m) => !ids.has(m.id) && m.created_at < data.messages[0].created_at,
      ),
      ...data.messages,
    ];
    data.has_more = conversationData.has_more;
  }
  const signature = JSON.stringify([
    data.messages.map((m) => [m.id, m.updated_at]),
    data.tasks.map((t) => [
      t.id,
      t.updated_at,
      t.approval?.expired,
      t.approval?.expires_epoch,
    ]),
    data.session.title,
  ]);
  conversationData = data;
  if (!sessionStorage.getItem("agent-mode:" + id) && data.tasks.length) {
    const restored = data.tasks.at(-1).requested_mode || data.tasks.at(-1).mode;
    $("mode").value = restored === "approval_write" ? "auto" : restored;
  }
  if (!sessionStorage.getItem("agent-engine:" + id) && data.tasks.length)
    $("engine").value = data.tasks.at(-1).engine;
  $("chat-title").textContent = data.session.title;
  const legacy = data.messages.some((m) => m.legacy);
  $("history-note").textContent = legacy
    ? "历史部分依据保存的任务输入、计划与结果恢复；未补写当时没有保存的逐轮模型消息。"
    : "";
  $("history-note").classList.toggle("hidden", !legacy);
  if (signature !== renderKey || force) {
    renderKey = signature;
    renderMessages(force);
    updateComposer();
    await renderInspector();
  }
  const rows = sessionMap.get(project.id) || [];
  const index = rows.findIndex((s) => s.id === id);
  if (index >= 0) rows[index] = data.session;
  else rows.unshift(data.session);
  sessionMap.set(
    project.id,
    rows.sort((a, b) => b.last_activity.localeCompare(a.last_activity)),
  );
  renderSidebar();
}
function renderMessages(force = false) {
  renderTurnMessages(force);
}
function approvalCard(t) {
  return humanApprovalCard(t);
}
function updateComposer() {
  const active = conversationData?.tasks.find(turnIsActive);
  $("send").disabled = sending || !!active || !project;
  $("mode").disabled = !!active;
  $("engine").disabled = !!active;
  $("prompt").disabled = false;
  const strip = $("active-round");
  strip.replaceChildren();
  strip.classList.toggle("hidden", !active);
  if (active) {
    const text = node("span");
    text.append(
      node("i", "", "working-dot"),
      node(
        "span",
        active.status === "completed"
          ? "正在保存项目文件"
          : nodeNames[active.current_node] || statusLabels[active.status],
      ),
    );
    strip.append(text);
    if (active.status.startsWith("awaiting"))
      strip.append(
        button(
          "查看审批",
          () => {
            $("conversation").scrollTop = $("conversation").scrollHeight;
          },
          "quiet",
        ),
      );
    if (active.status !== "completed")
      strip.append(button("停止本轮", () => stopTask(active.id), "quiet"));
    $("composer-note").textContent = active.status.startsWith("awaiting")
      ? "先处理上方审批；可以提前输入下一条消息，当前执行结束后发送。"
      : "当前对话正在处理这一轮。输入会保留，结束后可继续发送。";
  } else
    $("composer-note").textContent =
      "Enter 发送 · Shift + Enter 换行 · 修改与命令需审批";
  if (project && !project.allow_write && !active) $("mode").value = "readonly";
}
async function sendMessage(event) {
  event.preventDefault();
  if (sending) return;
  const content = $("prompt").value.trim();
  if (!content) return;
  if (!project) return openAddProject();
  const chosenMode = $("mode").value,
    chosenEngine = $("engine").value;
  if (!sessionId) await newChat(project);
  $("mode").value = chosenMode;
  $("engine").value = chosenEngine;
  sessionStorage.setItem("agent-mode:" + sessionId, chosenMode);
  sessionStorage.setItem("agent-engine:" + sessionId, chosenEngine);
  sending = true;
  updateComposer();
  try {
    const task = await request("/api/sessions/" + sessionId + "/messages", {
      method: "POST",
      body: JSON.stringify({
        content,
        mode: $("mode").value,
        engine: $("engine").value,
        client_request_id: crypto.randomUUID(),
      }),
    });
    $("prompt").value = "";
    rememberDraft();
    selectedTaskId = task.id;
    renderKey = "";
    await loadConversation(true);
  } catch (e) {
    error(e.message);
  } finally {
    sending = false;
    updateComposer();
  }
}
async function loadOlder() {
  const id = sessionId;
  const first = conversationData.messages[0];
  const data = await request(
    "/api/sessions/" + id + "?before=" + encodeURIComponent(first.id),
  );
  if (id !== sessionId) return;
  const scroller = $("conversation"),
    height = scroller.scrollHeight,
    position = scroller.scrollTop;
  conversationData.messages = [...data.messages, ...conversationData.messages];
  conversationData.has_more = data.has_more;
  renderMessages();
  scroller.scrollTop = position + scroller.scrollHeight - height;
}
async function decide(t, decision) {
  try {
    await request("/api/approvals/" + t.approval_id, {
      method: "POST",
      body: JSON.stringify({
        decision,
        decision_revision: t.approval?.decision_revision || 0,
        apply_to_project:
          decision === "approved" && t.status === "awaiting_diff",
        database_name:
          t.status === "awaiting_database"
            ? t.database_operation.target.database
            : null,
      }),
    });
  } finally {
    await loadConversation();
  }
}
async function renewApproval(t) {
  await request("/api/approvals/" + t.approval_id + "/renew", {
    method: "POST",
  });
  renderKey = "";
  await loadConversation();
}
async function resumeTask(id) {
  await request("/api/tasks/" + id + "/resume", { method: "POST" });
  await loadConversation();
}
async function stopTask(id) {
  await request("/api/tasks/" + id + "/cancel", { method: "POST" });
  await loadConversation();
}
async function applyTask(id) {
  await request("/api/tasks/" + id + "/apply", { method: "POST" });
  renderKey = "";
  detailCache.delete(id);
  await loadConversation();
}
async function exportTask(id) {
  const result = await request("/api/tasks/" + id + "/export");
  const url = URL.createObjectURL(
    new Blob([result.diff], { type: "text/plain;charset=utf-8" }),
  );
  const a = node("a");
  a.href = url;
  a.download = id + ".diff";
  a.click();
  URL.revokeObjectURL(url);
}

async function openInspector(panel = detailPanel, id = null) {
  detailPanel = panel;
  if (id) selectedTaskId = id;
  $("inspector").classList.remove("hidden");
  drawerKey = "";
  await renderInspector(true);
}
function renderDetailMenu() {
  const menu = $("detail-menu");
  menu.replaceChildren();
  for (const [key, label] of panels) {
    const b = button(
      label,
      () => openInspector(key),
      "detail-tab" + (key === detailPanel ? " active" : ""),
    );
    b.setAttribute("aria-pressed", String(key === detailPanel));
    menu.append(b);
  }
}
async function renderInspector(force = false) {
  if ($("inspector").classList.contains("hidden")) return;
  if (
    !force &&
    detailPanel === "usage" &&
    document.activeElement?.closest(".budget-editor")
  )
    return;
  const t = currentTask();
  const key = [
    project?.id,
    sessionId,
    detailPanel,
    t?.id,
    t?.updated_at,
    conversationData?.session.usage.input_tokens,
  ].join("|");
  if (key === drawerKey && !force) return;
  drawerKey = key;
  renderDetailMenu();
  const content = $("detail-content");
  const scroll = force ? 0 : content.scrollTop;
  content.replaceChildren();
  if (!project) {
    content.append(node("p", "先选择一个项目。", "muted"));
    return;
  }
  if (
    !["project", "tools", "database"].includes(detailPanel) &&
    conversationData?.tasks.length
  ) {
    const pick = node("select", undefined, "detail-round-picker");
    pick.setAttribute("aria-label", "查看哪一轮");
    for (const [index, task] of conversationData.tasks.entries()) {
      const option = node(
        "option",
        "第 " + (index + 1) + " 轮 · " + task.user_input.slice(0, 32),
      );
      option.value = task.id;
      option.selected = task.id === t?.id;
      pick.append(option);
    }
    pick.onchange = () => openInspector(detailPanel, pick.value);
    content.append(pick);
  }
  if (detailPanel === "project") {
    const scope = card("项目与文件位置");
    const root = node("div");
    root.id = "project-scope";
    scope.append(root);
    content.append(scope);
    renderProjectScope(project);
    scope.append(
      button("查看业务数据库", () => openInspector("database"), "inline-link"),
    );
    return;
  }
  if (detailPanel === "database") {
    await renderBusinessDatabase(content);
    return;
  }
  if (detailPanel === "tools") {
    const data = await request("/api/projects/" + project.id + "/tools");
    if (detailPanel !== "tools" || key !== drawerKey) return;
    content.append(renderToolInventory(data));
    content.append(
      button("检查工具连接", async () => {
        const result = await request(
          "/api/projects/" + project.id + "/tools/check",
          { method: "POST" },
        );
        content.replaceChildren(renderToolInventory(result));
        content.append(node("p", "连接检查已保存到审计记录。", "muted field"));
      }),
    );
    return;
  }
  if (detailPanel === "usage") {
    await renderProjectTokenBudget(content);
    if (!t) return;
  }
  if (!t) {
    content.append(node("p", "开始一轮对话后，这里会显示对应记录。", "muted"));
    return;
  }
  const panel = detailPanel;
  const detail = await taskDetail(t.id);
  if (panel !== detailPanel || t.id !== currentTask()?.id || key !== drawerKey)
    return;
  if (panel === "usage") {
    const box = card("当前对话累计用量");
    const u = conversationData.session.usage;
    const facts = node("div", undefined, "facts");
    for (const [label, value] of [
      ["输入 token", u.input_tokens],
      ["缓存命中", u.cache_read_tokens],
      ["输出 token", u.output_tokens],
      ["累计 token", u.input_tokens + u.output_tokens],
    ]) {
      const f = node("div", label, "fact");
      f.append(node("strong", tokenValue(value)));
      facts.append(f);
    }
    box.append(
      facts,
      node("p", "缓存命中属于输入 token，不再重复相加。", "muted"),
    );
    content.append(box, renderTokenDetails(detail));
  } else if (panel === "plan") {
    const p = detail.plans.at(-1);
    if (!p) content.append(node("p", "这一轮还没有实施计划。", "muted"));
    else {
      const box = card(p.goal);
      const list = node("ol", undefined, "list");
      for (const step of p.steps) list.append(node("li", step));
      box.append(
        list,
        node("p", "文件范围：" + p.files.join("、"), "field"),
        node(
          "p",
          "数据库影响：" +
            (p.database_change === null
              ? "待确认"
              : p.database_change
                ? "有变更"
                : "无变更"),
          "field",
        ),
        node("h3", "测试方案"),
        node("p", p.test_plan),
        node("h3", "回滚方案"),
        node("p", p.rollback_plan),
      );
      if (p.risks.length) {
        box.append(node("h3", "风险"), node("p", p.risks.join("\n")));
      }
      for (const cmd of p.powershell_commands || [])
        box.append(node("h3", "已计划的 PowerShell 命令"), pre(cmd.script));
      content.append(box);
    }
  } else if (panel === "diff") {
    const patch = detail.patchs.at(-1);
    if (!patch) content.append(node("p", "这一轮还没有文件改动。", "muted"));
    else {
      const diff = await request("/api/artifacts/" + patch.diff_artifact_id);
      const box = card("文件改动");
      box.append(pre(diff));
      if (t.status === "completed" && t.summary?.test_status === "passed") {
        const actions = node("div", undefined, "phase-actions");
        actions.append(button("下载补丁", () => exportTask(t.id)));
        if (t.summary.applied_to_project)
          actions.append(node("p", "修改已写回所选项目。"));
        else if (["pending", "writing"].includes(t.delivery?.status))
          actions.append(node("p", "正在保存到项目文件夹…"));
        else
          actions.append(
            button(
              t.delivery?.status === "failed" ? "重试保存" : "写入项目",
              () => applyTask(t.id),
              "primary",
            ),
          );
        box.append(actions);
      }
      content.append(box);
    }
  } else if (panel === "tests") {
    const run = detail.task.verification;
    if (!run) content.append(node("p", "这一轮尚未运行验证。", "muted"));
    else {
      const box = card("验证报告");
      box.append(
        badge(run.status),
        node(
          "p",
          run.reason ||
            "退出码：" +
              run.exit_code +
              "；测试数量：" +
              (run.test_count ?? "未报告"),
          "field",
        ),
      );
      if (run.verification_kind === "project_reset")
        box.append(
          node(
            "p",
            "文件清单验证：检查 " +
              run.checked_file_count +
              " 个批准文件，删除 " +
              run.deleted_file_count +
              " 个。未运行应用构建或数据库集成测试。",
            "field",
          ),
        );
      renderDatabaseVerification(run, box);
      for (const [label, name] of [
        ["标准输出", "stdout_id"],
        ["错误与测试输出", "stderr_id"],
      ])
        if (run[name]) {
          const text = await request("/api/artifacts/" + run[name]);
          const more = node("details");
          more.append(node("summary", label), pre(text));
          box.append(more);
        }
      content.append(box);
    }
  } else if (panel === "sources") {
    if (detail.task.review) {
      const box = card("AI 代码检查");
      box.append(node("p", detail.task.review.summary));
      for (const issue of detail.task.review.issues || []) {
        const el = node("div", undefined, "evidence");
        el.append(
          node("strong", issue.title),
          node(
            "p",
            issue.path +
              ":" +
              issue.line +
              (issue.side === "old" ? "（删除前）" : "（修改后）") +
              " · " +
              issue.severity,
          ),
          node("p", issue.explanation),
        );
        box.append(el);
      }
      content.append(box);
    }
    const box = card("引用依据");
    box.append(
      node(
        "p",
        "引用由工具读取实际内容后生成；需求文件可能由用户提供，代码也可能来自 Agent 本次生成的补丁。",
      ),
    );
    for (const source of detail.evidences) {
      const more = node("details");
      more.append(
        node(
          "summary",
          source.path +
            " · " +
            source.start_line +
            "–" +
            source.end_line +
            " 行" +
            (source.source_type === "patch" ? " · 补丁副本" : ""),
        ),
        pre(source.snippet),
        node("p", source.source_id, "small"),
      );
      box.append(more);
    }
    content.append(box);
  } else if (panel === "events") {
    const box = card("执行记录");
    const user = node("details");
    user.append(
      node("summary", "查看本轮原始提示词"),
      pre(detail.task.user_input),
    );
    box.append(user);
    const stages = new Set([
      "AIReviewCompleted",
      "ProjectDatabasePrepared",
      "NodeStarted",
      "TaskCreated",
      "ApprovalDecided",
      "AfterTest",
      "TaskCompleted",
      "TaskFailed",
      "PatchApplied",
      "PowerShellExecuted",
      "TaskCancelled",
      "TaskStopped",
    ]);
    const labels = {
      AIReviewCompleted: "AI 代码检查结束",
      ProjectDatabasePrepared: "项目数据库已准备",
      TaskCreated: "收到本轮需求",
      ApprovalDecided: "审批已处理",
      AfterTest: "测试已结束",
      TaskCompleted: "本轮已结束",
      TaskFailed: "执行中断",
      PatchApplied: "修改已写入项目",
      PowerShellExecuted: "命令已执行",
      TaskCancelled: "停止请求已记录",
      TaskStopped: "执行已停止",
    };
    const show = (events, target) => {
      const timeline = node("div", undefined, "timeline");
      for (const event of events) {
        const item = node("div", undefined, "event");
        item.append(
          node(
            "strong",
            event.kind === "NodeStarted"
              ? nodeNames[event.detail.node] || event.detail.node
              : labels[event.kind] || event.kind,
          ),
          node("p", date(event.created_at), "event-time"),
        );
        const more = node("details");
        more.append(node("summary", "查看详情"), pre(event.detail));
        item.append(more);
        timeline.append(item);
      }
      target.append(timeline);
    };
    show(
      detail.events.filter((e) => stages.has(e.kind)),
      box,
    );
    const all = node("details");
    all.append(
      node("summary", "展开完整审计记录（" + detail.events.length + " 条）"),
    );
    show(detail.events, all);
    box.append(all);
    content.append(box);
    if (detail.command_runs.length) {
      const commands = card("PowerShell 记录");
      for (const run of detail.command_runs) {
        commands.append(badge(run.status), pre(run.arguments.script));
        for (const field of ["stdout_id", "stderr_id"])
          if (run[field])
            commands.append(pre(await request("/api/artifacts/" + run[field])));
      }
      content.append(commands);
    }
  } else if (panel === "models") {
    const box = card("模型交互");
    box.append(
      node(
        "p",
        "这里保存新对话中的模型可见回复与工具请求名称。每次模型调用与一次用户对话轮次分别计数。",
      ),
    );
    const records = conversationData.messages.filter(
      (m) => m.kind === "model" && m.task_id === t.id,
    );
    if (!records.length)
      box.append(
        node(
          "p",
          t.engine === "demo"
            ? "这一轮使用确定性演示，没有调用模型。"
            : "这条历史任务没有保存逐次模型回复，不能从用量记录还原。",
          "field",
        ),
      );
    for (const [i, m] of records.entries()) {
      const more = node("details");
      more.append(
        node(
          "summary",
          "第 " +
            (i + 1) +
            " 次 · " +
            ({
              context_agent: "分析",
              planner_agent: "规划",
              coding_agent: "编码",
              conversation_agent: "对话",
              review_agent: "AI 检查",
              summary_agent: "最终答复",
            }[m.metadata.agent_role] || "模型"),
        ),
      );
      if (m.content) more.append(pre(m.content));
      if (m.metadata.tools.length)
        more.append(node("p", "请求工具：" + m.metadata.tools.join("、")));
      more.append(
        node(
          "p",
          "输入 " +
            m.metadata.input_tokens +
            " · 输出 " +
            m.metadata.output_tokens,
        ),
      );
      if (m.metadata.output_limit_tokens)
        more.append(
          node(
            "p",
            "本次输出上限 " +
              m.metadata.output_limit_tokens +
              " token · 请求超时 " +
              m.metadata.request_timeout_seconds +
              " 秒",
            "muted",
          ),
        );
      if (m.metadata.output_truncated)
        more.append(
          node(
            "p",
            "本次输出被截断，未采用为完整结果；后续处理见执行记录。",
            "muted",
          ),
        );
      box.append(more);
    }
    content.append(box);
  }
  content.scrollTop = scroll;
}

function openAddProject() {
  $("project-form").reset();
  $("project-token-custom-label").classList.add("hidden");
  $("project-token-custom").disabled = true;
  projectSelection = null;
  localError("project-error", "");
  $("project-dialog").showModal();
}
$("login-form").onsubmit = async (event) => {
  event.preventDefault();
  access = $("token").value.trim();
  try {
    await login();
  } catch (e) {
    localError("login-error", e.message);
  }
};
$("logout").onclick = () => {
  sessionStorage.removeItem("agent-access");
  location.reload();
};
$("new-chat").onclick = () => newChat().catch((e) => error(e.message));
$("add-project").onclick = openAddProject;
$("close-project-dialog").onclick = () => $("project-dialog").close();
$("open-details").onclick = () =>
  openInspector().catch((e) => error(e.message));
$("close-details").onclick = () => $("inspector").classList.add("hidden");
$("toggle-sidebar").onclick = () => $("sidebar").classList.toggle("open");
$("chat-form").onsubmit = sendMessage;
$("prompt").oninput = () => {
  rememberDraft();
  $("prompt").style.height = "auto";
  $("prompt").style.height = Math.min($("prompt").scrollHeight, 170) + "px";
};
$("prompt").onkeydown = (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!$("send").disabled) $("chat-form").requestSubmit();
  }
};
$("demo").onclick = async () => {
  try {
    const item = await request("/api/demo", { method: "POST" });
    await loadProjects();
    await selectProject(item);
  } catch (e) {
    error(e.message);
  }
};
$("rename-chat").onclick = () => {
  if (!sessionId) return;
  $("session-title").value = conversationData.session.title;
  $("rename-dialog").showModal();
};
$("close-rename").onclick = () => $("rename-dialog").close();
$("rename-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await request("/api/sessions/" + sessionId, {
      method: "PATCH",
      body: JSON.stringify({ title: $("session-title").value }),
    });
    $("rename-dialog").close();
    await loadConversation();
  } catch (err) {
    error(err.message);
  }
};
$("project-form").onsubmit = async (event) => {
  event.preventDefault();
  const submit = $("project-form").querySelector("button[type=submit]");
  submit.disabled = true;
  try {
    localError("project-error", "");
    if (!projectSelection) {
      try {
        projectSelection = await request("/api/project-selections", {
          method: "POST",
          body: JSON.stringify({ path: $("project-path").value }),
        });
      } catch (e) {
        if (!projects.some((p) => $("project-path").value.startsWith(p.root)))
          throw e;
      }
    }
    const input = {
      name: $("project-name").value,
      token_budget: newProjectTokenBudget(),
      root: $("project-path").value,
      selection_id: projectSelection?.selection_id || null,
      allow_write: $("allow-write").checked,
      mcp_servers: $("enable-project-mysql").checked
        ? ["project_files", "mysql_readonly"]
        : ["project_files"],
    };
    if ($("project-runtime").value === "java-mysql")
      Object.assign(input, {
        sandbox_image: "ai-agent/spring-mysql-it:0.1",
        test_command: [
          "mvn",
          "-o",
          "-B",
          "-Dmaven.repo.local=/opt/m2",
          "verify",
        ],
        database_test_profile: "mysql",
      });
    const item = await request("/api/projects", {
      method: "POST",
      body: JSON.stringify(input),
    });
    $("project-dialog").close();
    await loadProjects();
    await selectProject(item);
    await newChat(item);
  } catch (e) {
    localError("project-error", e.message);
  } finally {
    submit.disabled = false;
  }
};
initLibrary();
initTokenBudgets();
initMySQLConfig();
initProjectFolderPicker();
$("folder-create").onclick = async () => {
  try {
    localError("folder-error", "");
    if (!folderSelection) throw Error("先打开要存放项目的磁盘或文件夹。");
    projectSelection = await request("/api/project-folders/create", {
      method: "POST",
      body: JSON.stringify({
        parent: folderSelection,
        name: $("folder-name").value,
      }),
    });
    $("project-path").value = projectSelection.root;
    if (!$("project-name").value)
      $("project-name").value = $("folder-name").value;
    $("project-folder-dialog").close();
  } catch (e) {
    localError("folder-error", e.message);
  }
};
document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    newChat().catch((e) => error(e.message));
  }
  if (event.key === "Escape") {
    $("inspector").classList.add("hidden");
    $("sidebar").classList.remove("open");
  }
});
const sideDetails = node("details", undefined, "side-details");
sideDetails.append(node("summary", "项目详情"));
for (const [key, label] of panels.slice(0, 4))
  sideDetails.append(button(label, () => openInspector(key), "quiet"));
$("projects").after(sideDetails);
setInterval(async () => {
  if (!access || !sessionId || loading) return;
  loading = true;
  try {
    await loadConversation();
    if (reconnecting) {
      error("");
      reconnecting = false;
    }
  } catch (e) {
    reconnecting = true;
    error(e.message);
  } finally {
    loading = false;
  }
}, 1500);
if (access)
  login().catch(() => {
    access = "";
    sessionStorage.removeItem("agent-access");
  });

$("mode").onchange = () => {
  if (sessionId)
    sessionStorage.setItem("agent-mode:" + sessionId, $("mode").value);
};
$("engine").onchange = () => {
  if (sessionId)
    sessionStorage.setItem("agent-engine:" + sessionId, $("engine").value);
};
