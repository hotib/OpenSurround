"use strict";

const byId = (id) => document.getElementById(id);
let token = "";
let snapshot;
let draftRevision;
let draft = [];
let dirty = false;
let busy = false;
let polling = false;
let epoch = 0;
let lastInventory = "";
let notice = "";

async function request(path, method = "GET", body) {
  const headers = {"X-OpenSurround": "1"};
  if (token) headers.Authorization = "Bearer " + token;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method, headers, body: JSON.stringify(body), credentials: "omit", cache: "no-store",
  });
  const result = await response.json();
  if (!response.ok) throw Object.assign(new Error(result.error), {status: response.status});
  return result;
}

function setError(message = "") {
  byId("error").textContent = message;
  byId("error").hidden = !message;
}

function showFailure(error) {
  if (error.status === 401) {
    token = "";
    byId("login").hidden = false;
    byId("dashboard").hidden = true;
    byId("service").textContent = "Belépés szükséges";
    byId("token").focus();
  } else {
    byId("service").textContent = error.status ? "Elérhető" : "Kapcsolat megszakadt";
  }
  setError(error.status ? error.message : "Az OpenSurround jelenleg nem érhető el.");
}

function accept(state, reset = false) {
  const reload = reset || (!dirty && (!snapshot || state.revision !== snapshot.revision));
  snapshot = state;
  if (reload) {
    draftRevision = state.revision;
    draft = structuredClone(state.endpoints);
    dirty = false;
    byId("channel-list").replaceChildren(...state.channels.map((name) => {
      const badge = document.createElement("span");
      badge.textContent = name;
      return badge;
    }));
    renderRoutes();
  } else {
    refreshInventory();
  }
  byId("login").hidden = true;
  byId("dashboard").hidden = false;
  byId("service").textContent = "Elérhető";
  updateActions();
}

function updateActions() {
  const stale = dirty && draftRevision !== snapshot?.revision;
  byId("save").disabled = busy || !dirty || stale;
  byId("discard").disabled = busy || !snapshot;
  byId("restore").disabled = busy || dirty || !snapshot?.can_restore;
  const text = stale ? "Új mentés érhető el. Töltsd be a mentett verziót." :
    dirty ? "Mentetlen módosítások." :
    snapshot?.pending ? "A mentett útvonalak a következő bemeneti szünetben lépnek életbe." :
    notice || "A mentett útvonalak érvényben vannak.";
  if (byId("save-status").textContent !== text) byId("save-status").textContent = text;
}

function markDirty() {
  dirty = true;
  notice = "";
  updateActions();
}

function fillDeviceSelect(select, route) {
  const selected = route.client_id;
  const options = [];
  if (!selected || route.match_name) {
    options.push(new Option(route.match_name ? "Egyezés névrészlet alapján" : "Válassz eszközt", ""));
  }
  for (const device of snapshot.devices) {
    options.push(new Option(device.name + " · " + device.id +
      (device.connected ? "" : " (leválasztva)"), device.id));
  }
  if (selected && !snapshot.devices.some((device) => device.id === selected)) {
    options.push(new Option(selected + " (leválasztva)", selected));
  }
  select.replaceChildren(...options);
  select.value = selected;
  select.required = !route.match_name;
}

function connectionState(element, route) {
  const receivers = snapshot.devices.filter((device) => device.connected && (
    route.client_id ? device.id === route.client_id :
      route.match_name && device.name.toLowerCase().includes(route.match_name.toLowerCase())
  ));
  element.classList.toggle("online", receivers.length === 1);
  element.textContent = receivers.length > 1 ? "Több egyező eszköz" :
    receivers.length === 1 ? "Csatlakoztatva" : "Nincs csatlakoztatva";
}

function refreshInventory(force = false) {
  const signature = JSON.stringify(snapshot.devices);
  if (!force && signature === lastInventory) return;
  lastInventory = signature;
  const list = byId("devices");
  const focused = document.activeElement?.dataset.device;
  list.replaceChildren();
  for (const device of snapshot.devices) {
    const row = document.createElement("li");
    row.className = "device";
    const title = document.createElement("strong");
    title.textContent = device.name;
    const id = document.createElement("small");
    id.textContent = device.id;
    const status = document.createElement("span");
    connectionState(status, {client_id: device.id});
    status.classList.add("connection");
    row.append(title, id, status);
    const mapped = draft.some((route) => route.client_id === device.id ||
      (!route.client_id && route.match_name &&
       device.name.toLowerCase().includes(route.match_name.toLowerCase())));
    if (device.connected && !mapped) {
      const add = document.createElement("button");
      add.type = "button";
      add.textContent = "Útvonal hozzáadása";
      add.setAttribute("aria-label", "Útvonal hozzáadása ehhez: " + device.name);
      add.dataset.device = device.id;
      add.addEventListener("click", () => {
        draft.push({
          key: null, label: device.name, client_id: device.id, match_name: "",
          source_channels: [0], gain_db: 0, delay_ms: 0, muted: false,
        });
        markDirty();
        renderRoutes().at(-1)?.querySelector("input").focus();
      });
      row.append(add);
      if (focused === device.id) add.focus();
    }
    list.append(row);
  }
  if (focused !== undefined && document.activeElement === document.body) {
    [...list.querySelectorAll("button")].find((button) => button.dataset.device === focused)?.focus();
  }
  byId("empty-devices").hidden = snapshot.devices.length > 0;
  for (const [index, element] of [...byId("routes").children].entries()) {
    const select = element.querySelector('[data-field="client_id"]');
    if (select !== document.activeElement) fillDeviceSelect(select, draft[index]);
    connectionState(element.querySelector(".connection"), draft[index]);
  }
}

function renderRoutes() {
  const container = byId("routes");
  container.replaceChildren(...draft.map((route, index) => {
    const element = byId("route-template").content.firstElementChild.cloneNode(true);
    const input = (name) => element.querySelector('[data-field="' + name + '"]');
    const legend = element.querySelector("legend span");
    const connection = element.querySelector(".connection");
    const legacy = element.querySelector(".legacy");
    const updateLabel = () => { legend.textContent = route.label || "Új útvonal"; };
    const updateLegacy = () => {
      legacy.hidden = Boolean(route.client_id);
      input("match_name").required = !route.client_id;
    };
    updateLabel();
    updateLegacy();
    for (const name of ["label", "match_name", "gain_db", "delay_ms"]) {
      const field = input(name);
      field.value = route[name];
      field.addEventListener("input", () => {
        route[name] = field.type === "number" ? field.valueAsNumber : field.value;
        if (name === "label") updateLabel();
        if (name === "match_name") {
          input("client_id").required = !route.match_name;
          refreshInventory(true);
        }
        markDirty();
      });
    }
    input("client_id").addEventListener("blur", () => fillDeviceSelect(input("client_id"), route));
    input("client_id").addEventListener("change", ({target}) => {
      route.client_id = target.value;
      updateLegacy();
      connectionState(connection, route);
      refreshInventory(true);
      markDirty();
    });
    const first = input("first");
    const second = input("second");
    second.add(new Option("Nincs (monó)", ""));
    for (const [number, name] of snapshot.channels.entries()) {
      first.add(new Option(name, number));
      second.add(new Option(name, number));
    }
    first.value = route.source_channels[0];
    second.value = route.source_channels[1] ?? "";
    const updateSources = () => {
      route.source_channels = second.value === "" ? [Number(first.value)] : [Number(first.value), Number(second.value)];
      markDirty();
    };
    first.addEventListener("change", updateSources);
    second.addEventListener("change", updateSources);
    input("muted").checked = route.muted;
    input("muted").addEventListener("change", ({target}) => {
      route.muted = target.checked;
      markDirty();
    });
    element.querySelector(".remove").addEventListener("click", () => {
      draft.splice(index, 1);
      markDirty();
      const remaining = renderRoutes();
      (remaining[Math.min(index, remaining.length - 1)]?.querySelector("input") ||
        byId("devices").querySelector("button") || byId("discard")).focus();
    });
    return element;
  }));
  byId("empty-routes").hidden = draft.length > 0;
  refreshInventory(true);
  return [...container.children];
}

async function act(action, success) {
  if (busy) return;
  busy = true;
  epoch += 1;
  updateActions();
  byId("routes").inert = true;
  byId("devices").inert = true;
  try {
    setError();
    accept(await action(), true);
    notice = success;
  } catch (error) {
    showFailure(error);
  } finally {
    busy = false;
    byId("routes").inert = false;
    byId("devices").inert = false;
    updateActions();
  }
}

byId("settings").addEventListener("submit", (event) => {
  event.preventDefault();
  if (dirty) act(() => request("/api/routes", "PUT", {
    revision: draftRevision, endpoints: draft,
  }), "Az útvonalak mentve.");
});
byId("discard").addEventListener("click", () => {
  if (!dirty || window.confirm("Eldobod a mentetlen módosításokat?")) {
    act(() => request("/api/state"), "A mentett útvonalak betöltve.");
  }
});
byId("restore").addEventListener("click", () => {
  if (window.confirm("Visszaállítod az előző mentés útvonalait?")) {
    act(() => request("/api/restore", "POST", {revision: snapshot.revision}), "Az előző mentés visszaállítva.");
  }
});
byId("login").addEventListener("submit", (event) => {
  event.preventDefault();
  token = byId("token").value.trim();
  byId("token").value = "";
  act(() => request("/api/state"), "");
});

async function poll() {
  if (polling || busy || !byId("login").hidden) return;
  polling = true;
  const started = epoch;
  try {
    const state = await request("/api/state");
    if (started === epoch) accept(state);
  } catch (error) {
    if (started === epoch) showFailure(error);
  } finally {
    polling = false;
  }
}

poll();
setInterval(poll, 3500);
