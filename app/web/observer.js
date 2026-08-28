const observerState = {
  token: sessionStorage.getItem("great_world_observer_token"),
  snapshot: null,
};

const $ = (selector) => document.querySelector(selector);

function setText(element, value) {
  element.textContent = value ?? "";
}

async function observerApi(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-Observer-Token": observerState.token || "",
      ...(options.headers || {}),
    },
  });
  let body = {};
  try { body = await response.json(); } catch (_) { /* empty response */ }
  if (!response.ok) {
    const problem = new Error(body.detail || `请求失败（${response.status}）`);
    problem.status = response.status;
    throw problem;
  }
  return body;
}

function entityName(id) {
  const entities = observerState.snapshot?.truth?.entities || [];
  return entities.find((item) => item.id === id)?.name || id || "无位置";
}

function eventLabel(type) {
  return ({
    "world.created": "世界开始",
    "entity.created": "实体出现",
    "entity.moved": "位置变化",
    "activity.performed": "在场活动",
    "speech.uttered": "说话",
    "wish.submitted": "愿望出现",
    "child.goal_selected": "孩子选择目标",
    "capability.unlocked": "能力形成",
    "latent.fact_frozen": "隐藏事实冻结",
  })[type] || type;
}

function renderEpochs(snapshot) {
  const select = $("#epoch-select");
  const previous = select.value;
  select.replaceChildren();
  for (const epoch of snapshot.epochs) {
    const option = document.createElement("option");
    option.value = epoch.world_id;
    option.textContent = `Epoch ${epoch.epoch_index} · ${epoch.status === "active" ? "运行中" : "已封存"} · #${epoch.seq}`;
    select.append(option);
  }
  select.value = snapshot.world.id || previous;
}

function renderMap(snapshot) {
  const map = $("#truth-map");
  map.replaceChildren();
  const places = snapshot.truth.entities.filter((item) => item.kind === "place");
  for (const place of places) {
    const card = document.createElement("article");
    const title = document.createElement("h3");
    setText(title, place.name);
    const description = document.createElement("p");
    setText(description, place.attributes?.description || "");
    const occupants = document.createElement("div");
    occupants.className = "occupants";
    for (const entity of snapshot.truth.entities.filter((item) => item.location_id === place.id)) {
      const badge = document.createElement("span");
      badge.className = entity.is_agent ? "agent-badge" : "entity-badge";
      setText(badge, entity.name);
      occupants.append(badge);
    }
    card.append(title, description, occupants);
    map.append(card);
  }
}

function appendFact(list, term, value) {
  const dt = document.createElement("dt");
  const dd = document.createElement("dd");
  setText(dt, term);
  setText(dd, value);
  list.append(dt, dd);
}

function renderChild(snapshot) {
  const child = snapshot.truth.child;
  const list = $("#child-truth");
  list.replaceChildren();
  appendFact(list, "目标", child.goal || "尚未选择");
  appendFact(list, "能力", child.capabilities.join("、") || "尚无");
  appendFact(list, "记忆", (child.development.memory || []).join("、") || "空");
  appendFact(list, "知识", (child.development.knowledge || []).join("、") || "空");
  appendFact(list, "技能", (child.development.skills || []).join("、") || "空");
}

function renderEvents(snapshot) {
  const list = $("#event-ledger");
  list.replaceChildren();
  for (const event of [...snapshot.truth.events].reverse()) {
    const item = document.createElement("li");
    const line = document.createElement("div");
    line.className = "event-line";
    const title = document.createElement("strong");
    setText(title, `#${event.seq} · ${eventLabel(event.event_type)}`);
    const actor = document.createElement("span");
    setText(actor, event.actor_id ? entityName(event.actor_id) : "Kernel / 系统");
    line.append(title, actor);
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    setText(summary, "查看事实 payload");
    const pre = document.createElement("pre");
    setText(pre, JSON.stringify(event.payload, null, 2));
    details.append(summary, pre);
    item.append(line, details);
    list.append(item);
  }
}

function renderLatent(snapshot) {
  const container = $("#latent-facts");
  container.replaceChildren();
  if (!snapshot.truth.latent_facts.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    setText(empty, "尚未有隐藏事实因探索而冻结。");
    container.append(empty);
    return;
  }
  for (const fact of snapshot.truth.latent_facts) {
    const article = document.createElement("article");
    const title = document.createElement("strong");
    setText(title, fact.key);
    const value = document.createElement("pre");
    setText(value, JSON.stringify(fact.value, null, 2));
    article.append(title, value);
    container.append(article);
  }
}

function renderCognition(snapshot) {
  const select = $("#agent-select");
  const previous = select.value;
  select.replaceChildren();
  for (const [id, records] of Object.entries(snapshot.cognition)) {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = records.name;
    select.append(option);
  }
  if (previous && snapshot.cognition[previous]) select.value = previous;
  renderSelectedCognition();
}

function renderSelectedCognition() {
  const id = $("#agent-select").value;
  const records = observerState.snapshot?.cognition?.[id];
  const container = $("#cognition-summary");
  container.replaceChildren();
  if (!records) return;
  const groups = [
    ["可观察体验", records.perceptions, "details"],
    ["信念", records.beliefs, "object_value"],
    ["记忆", records.memories, "content"],
  ];
  for (const [label, items, field] of groups) {
    const section = document.createElement("section");
    const heading = document.createElement("h3");
    setText(heading, `${label} · ${items.length}`);
    const pre = document.createElement("pre");
    const latest = items.slice(-4).map((item) => item[field]);
    setText(pre, latest.length ? JSON.stringify(latest, null, 2) : "暂无");
    section.append(heading, pre);
    container.append(section);
  }
}

function renderSnapshot(snapshot) {
  observerState.snapshot = snapshot;
  setText($("#connection-state"), `已连接 · ${snapshot.world.id}`);
  setText($("#world-status"), snapshot.world.status === "active" ? "运行中 · 只读观察" : "已封存 · 只读");
  setText($("#metric-epoch"), snapshot.world.epoch_index ?? "—");
  setText($("#metric-events"), snapshot.truth.event_count);
  setText($("#metric-entities"), snapshot.truth.entities.length);
  setText($("#metric-agents"), snapshot.truth.entities.filter((item) => item.is_agent).length);
  setText($("#metric-latent"), snapshot.truth.latent_facts.length);
  renderEpochs(snapshot);
  renderMap(snapshot);
  renderChild(snapshot);
  renderEvents(snapshot);
  renderLatent(snapshot);
  renderCognition(snapshot);
  $("#open-reset").disabled = snapshot.world.status !== "active" || !snapshot.world.is_default;
}

async function loadSnapshot(worldId = null) {
  const path = worldId
    ? `/api/observer/worlds/${encodeURIComponent(worldId)}`
    : "/api/observer/worlds/current";
  renderSnapshot(await observerApi(path));
}

async function connectObserver() {
  const error = $("#auth-error");
  setText(error, "");
  observerState.token = $("#observer-token").value.trim();
  try {
    await loadSnapshot();
    sessionStorage.setItem("great_world_observer_token", observerState.token);
    $("#observer-auth").hidden = true;
    $("#observer-main").hidden = false;
  } catch (problem) {
    setText(error, problem.message);
  }
}

async function askObserver(event) {
  event.preventDefault();
  const answer = $("#observer-answer");
  setText(answer, "正在读取快照……");
  try {
    const result = await observerApi("/api/observer/query", {
      method: "POST",
      body: JSON.stringify({
        question: $("#observer-question").value.trim(),
        world_id: observerState.snapshot.world.id,
      }),
    });
    setText(answer, `${result.answer}\n\n证据：${result.evidence.join("、") || "当前快照"}`);
  } catch (problem) {
    setText(answer, problem.message);
  }
}

function openResetDialog() {
  const worldId = observerState.snapshot.world.id;
  const phrase = `RESET ${worldId}`;
  setText($("#reset-phrase"), phrase);
  $("#reset-confirmation").value = "";
  setText($("#reset-error"), "");
  $("#reset-dialog").showModal();
}

async function resetWorld(event) {
  event.preventDefault();
  const error = $("#reset-error");
  const worldId = observerState.snapshot.world.id;
  try {
    const result = await observerApi("/api/observer/reset", {
      method: "POST",
      body: JSON.stringify({
        world_id: worldId,
        confirmation: $("#reset-confirmation").value,
      }),
    });
    $("#reset-dialog").close();
    await loadSnapshot(result.world_id);
    setText($("#observer-answer"), `旧世界 ${result.archived_world_id} 已封存。Epoch ${result.epoch_index} 已使用新 seed 开始。`);
  } catch (problem) {
    setText(error, problem.message);
  }
}

function bindEvents() {
  $("#observer-connect").addEventListener("click", connectObserver);
  $("#observer-token").addEventListener("keydown", (event) => {
    if (event.key === "Enter") connectObserver();
  });
  $("#observer-refresh").addEventListener("click", () => loadSnapshot(observerState.snapshot.world.id));
  $("#epoch-select").addEventListener("change", (event) => loadSnapshot(event.target.value));
  $("#agent-select").addEventListener("change", renderSelectedCognition);
  $("#observer-query-form").addEventListener("submit", askObserver);
  $("#open-reset").addEventListener("click", openResetDialog);
  $("#cancel-reset").addEventListener("click", () => $("#reset-dialog").close());
  $("#reset-form").addEventListener("submit", resetWorld);
}

async function initObserver() {
  bindEvents();
  if (!observerState.token) return;
  $("#observer-token").value = observerState.token;
  await connectObserver();
}

initObserver().catch((problem) => setText($("#auth-error"), problem.message));
