const state = {
  token: localStorage.getItem("great_world_consent_token"),
  worldId: localStorage.getItem("great_world_id"),
  accessCode: null,
  view: null,
};

const $ = (selector) => document.querySelector(selector);

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (state.token) headers["X-Consent-Token"] = state.token;
  if (state.accessCode) headers["X-World-Access-Code"] = state.accessCode;
  const response = await fetch(path, { ...options, headers });
  let body = {};
  try { body = await response.json(); } catch (_) { /* no response body */ }
  if (!response.ok) throw new Error(body.detail || body.message || `请求失败（${response.status}）`);
  return body;
}

function text(element, value) {
  element.textContent = value ?? "";
}

async function loadConsentNotice() {
  const notice = await api("/api/reality/consent-notice");
  const copy = $("#consent-copy");
  copy.replaceChildren();
  const intro = document.createElement("p");
  text(intro, notice.summary);
  copy.append(intro);
  const list = document.createElement("ul");
  for (const item of notice.points) {
    const li = document.createElement("li");
    text(li, item);
    list.append(li);
  }
  copy.append(list);
  $("#consent-form").dataset.noticeVersion = notice.version;
  $("#access-code-fields").hidden = !notice.access_code_required;
}

async function enterWorld() {
  const joined = await api("/api/worlds/default/join", { method: "POST", body: "{}" });
  state.worldId = joined.world_id;
  localStorage.setItem("great_world_id", state.worldId);
  $("#world-app").hidden = false;
  $("#fork-button").disabled = false;
  await refreshView();
}

async function refreshView() {
  if (!state.worldId) return;
  const view = await api(`/api/worlds/${encodeURIComponent(state.worldId)}/view`);
  state.view = view;
  render(view);
}

function render(view) {
  text($("#world-name"), view.world.name);
  text($("#world-tick"), view.world.tick);
  text($("#self-name"), view.self.name);
  text($("#self-location"), `此刻在：${view.self.location_name || "你无法确定的地方"}`);
  text($("#child-description"), view.child.description);
  text($("#child-goal"), view.child.goal ? `ta 选择了：${view.child.goal}` : "ta 还没有选择目标。");

  const chips = $("#child-capabilities");
  chips.replaceChildren();
  for (const capability of view.child.capabilities) {
    const chip = document.createElement("span");
    chip.className = "chip";
    text(chip, capability);
    chips.append(chip);
  }

  renderLocations(view.locations, view.self.location_id);
  renderTimeline(view.experiences);
  renderWishes(view.wishes);
  populateActionTargets(view.locations, view.visible_entities);
}

function renderLocations(locations, currentId) {
  const grid = $("#location-grid");
  grid.replaceChildren();
  for (const location of locations) {
    const card = document.createElement("article");
    card.className = `location-card${location.id === currentId ? " current" : ""}`;
    const title = document.createElement("h3");
    text(title, location.name);
    const description = document.createElement("p");
    text(description, location.description || "这里的细节还没有进入你的经验。 ");
    card.append(title, description);
    if (location.id === currentId) {
      const marker = document.createElement("span");
      marker.className = "marker";
      text(marker, "你在这里");
      card.append(marker);
    }
    const people = document.createElement("div");
    people.className = "resident-list";
    for (const resident of location.occupants || []) {
      const badge = document.createElement("span");
      badge.className = "resident";
      text(badge, resident.name);
      people.append(badge);
    }
    card.append(people);
    grid.append(card);
  }
}

function renderTimeline(experiences) {
  const list = $("#timeline");
  list.replaceChildren();
  if (!experiences.length) {
    const empty = document.createElement("li");
    empty.className = "empty-state";
    text(empty, "你刚刚来到这里。先做一件事。");
    list.append(empty);
    return;
  }
  const template = $("#timeline-item-template");
  for (const experience of [...experiences].reverse()) {
    const item = template.content.cloneNode(true);
    text(item.querySelector(".timeline-tick"), `T.${experience.tick}`);
    text(item.querySelector(".timeline-title"), experience.summary);
    text(item.querySelector(".timeline-detail"), experience.detail || "");
    list.append(item);
  }
}

function renderWishes(wishes) {
  const pool = $("#wish-pool");
  pool.replaceChildren();
  if (!wishes.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    text(empty, "水面平静，还没有愿望。");
    pool.append(empty);
    return;
  }
  for (const wish of wishes) {
    const card = document.createElement("div");
    card.className = "wish";
    text(card, `“${wish.text}”`);
    pool.append(card);
  }
}

function populateActionTargets(locations, entities) {
  const destination = $("#move-destination");
  const previousDestination = destination.value;
  destination.replaceChildren();
  for (const location of locations) {
    const option = new Option(location.name, location.id);
    destination.add(option);
  }
  if ([...destination.options].some((o) => o.value === previousDestination)) destination.value = previousDestination;

  const target = $("#explore-target");
  const previousTarget = target.value;
  target.replaceChildren();
  for (const entity of entities) target.add(new Option(entity.name, entity.id));
  if ([...target.options].some((o) => o.value === previousTarget)) target.value = previousTarget;
}

function actionPayload(kind) {
  if (kind === "move") return { destination_id: $("#move-destination").value };
  if (kind === "speak") return { text: $("#speech-text").value.trim() };
  if (kind === "wish") return { text: $("#wish-text").value.trim() };
  if (kind === "explore") return { target_id: $("#explore-target").value, aspect: $("#explore-aspect").value };
  return {};
}

async function performAction(event) {
  event.preventDefault();
  const kind = $("#action-kind").value;
  const status = $("#action-status");
  status.classList.remove("error");
  text(status, "世界正在核对这项行动…");
  try {
    await api(`/api/worlds/${encodeURIComponent(state.worldId)}/actions`, {
      method: "POST",
      body: JSON.stringify({ type: kind, payload: actionPayload(kind) }),
    });
    $("#speech-text").value = "";
    $("#wish-text").value = "";
    text(status, "行动已经成为历史的一部分。");
    await refreshView();
  } catch (error) {
    status.classList.add("error");
    text(status, error.message);
  }
}

function updateActionFields() {
  const kind = $("#action-kind").value;
  for (const name of ["move", "speak", "wish", "explore"]) {
    $(`#${name}-fields`).hidden = kind !== name;
  }
}

async function advanceWorld() {
  const status = $("#action-status");
  status.classList.remove("error");
  text(status, "等待世界中的行动者回应…");
  try {
    const result = await api(`/api/worlds/${encodeURIComponent(state.worldId)}/advance`, { method: "POST", body: "{}" });
    text(status, result.message || "世界回应了一次。");
    await refreshView();
  } catch (error) {
    status.classList.add("error");
    text(status, error.message);
  }
}

async function forkWorld() {
  if (!state.view) return;
  const confirmed = window.confirm(`从时刻 ${state.view.world.tick} 创建一个可独立发展的分支？原世界不会改变。`);
  if (!confirmed) return;
  try {
    const result = await api(`/api/worlds/${encodeURIComponent(state.worldId)}/forks`, {
      method: "POST",
      body: JSON.stringify({ at_seq: state.view.world.seq }),
    });
    state.worldId = result.world_id;
    localStorage.setItem("great_world_id", state.worldId);
    await refreshView();
  } catch (error) {
    text($("#action-status"), error.message);
  }
}

async function submitConsent(event) {
  event.preventDefault();
  const error = $("#consent-error");
  text(error, "");
  try {
    state.accessCode = $("#access-code").value.trim() || null;
    const result = await api("/api/reality/consents", {
      method: "POST",
      body: JSON.stringify({
        display_name: $("#display-name").value.trim(),
        accepted: $("#consent-check").checked,
        notice_version: $("#consent-form").dataset.noticeVersion,
      }),
    });
    state.token = result.consent_token;
    state.accessCode = null;
    $("#access-code").value = "";
    localStorage.setItem("great_world_consent_token", state.token);
    $("#consent-dialog").close();
    await enterWorld();
  } catch (problem) {
    text(error, problem.message);
  }
}

async function init() {
  await loadConsentNotice();
  $("#consent-form").addEventListener("submit", submitConsent);
  $("#action-form").addEventListener("submit", performAction);
  $("#action-kind").addEventListener("change", updateActionFields);
  $("#advance-button").addEventListener("click", advanceWorld);
  $("#refresh-button").addEventListener("click", refreshView);
  $("#fork-button").addEventListener("click", forkWorld);
  updateActionFields();

  if (!state.token) {
    $("#consent-dialog").showModal();
    return;
  }
  try {
    await enterWorld();
  } catch (_) {
    state.token = null;
    state.worldId = null;
    localStorage.removeItem("great_world_consent_token");
    localStorage.removeItem("great_world_id");
    $("#consent-dialog").showModal();
  }
}

init().catch((error) => {
  document.body.textContent = `世界入口暂时无法打开：${error.message}`;
});
