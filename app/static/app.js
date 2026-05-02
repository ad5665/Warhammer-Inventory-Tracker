const DEFAULT_GAME = "wh40k_10e";

const state = {
  inventory: [],
  gameSystems: [],
  currentGame: localStorage.getItem("warhammer-stock-current-game") || DEFAULT_GAME,
  lastUnitQuery: "",
};

const fallbackSystems = [
  { id: "wh40k_10e", label: "Warhammer 40,000 10th Edition", short_label: "40k", catalogue_word: "units/models" },
  { id: "kill_team", label: "Warhammer 40,000: Kill Team", short_label: "Kill Team", catalogue_word: "teams/operatives" },
  { id: "age_of_sigmar_4e", label: "Warhammer Age of Sigmar 4th Edition", short_label: "AoS", catalogue_word: "warscrolls/units" },
];

const el = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function jsArg(value) {
  return JSON.stringify(String(value ?? ""));
}

function toast(message, type = "ok") {
  const box = el("toast");
  box.textContent = message;
  box.className = `toast ${type} show`;
  setTimeout(() => box.className = "toast", 4200);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (!(options.body instanceof FormData) && options.body !== undefined && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }

  const response = await fetch(path, { ...options, headers });

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      detail = body.detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }

  if (response.status === 204) {
    return null;
  }
  return response.json();
}

function activeSystem() {
  return state.gameSystems.find(system => system.id === state.currentGame)
    || fallbackSystems.find(system => system.id === state.currentGame)
    || fallbackSystems[0];
}

function withGame(path) {
  const separator = path.includes("?") ? "&" : "?";
  return `${path}${separator}game_system=${encodeURIComponent(state.currentGame)}`;
}

function formatImportDate(lastImport) {
  if (!lastImport || !lastImport.finished_at) return "Never";
  const date = new Date(lastImport.finished_at);
  if (Number.isNaN(date.getTime())) return lastImport.finished_at;
  return date.toLocaleString();
}

async function loadGameSystems() {
  try {
    state.gameSystems = await api("/api/game-systems");
  } catch (_) {
    state.gameSystems = fallbackSystems;
  }
  if (!state.gameSystems.some(system => system.id === state.currentGame)) {
    state.currentGame = DEFAULT_GAME;
  }
}

function renderTabs() {
  const tabs = el("game-tabs");
  tabs.innerHTML = state.gameSystems.map(system => `
    <button type="button" class="tab ${system.id === state.currentGame ? "active" : ""}" data-game="${esc(system.id)}">
      ${esc(system.short_label || system.label)}
    </button>
  `).join("");
}

function updateGameCopy() {
  const system = activeSystem();
  el("active-game-label").textContent = system.short_label || system.label;
  el("game-title").textContent = `${system.short_label || system.label} Stock Tracker`;
  const subtitles = {
    kill_team: "Track Kill Team squads and operatives, including built count, painted count, imported weapon loadouts, model numbers, storage location, and photos.",
    age_of_sigmar_4e: "Track Age of Sigmar armies and warscrolls, including built count, painted count, imported weapon loadouts, model numbers, storage location, and photos.",
    wh40k_10e: "Track Warhammer 40,000 units and models, including built count, painted count, imported weapon loadouts, model numbers, storage location, and photos.",
  };
  el("game-subtitle").textContent = subtitles[state.currentGame] || `Track ${system.label} models, including built count, painted count, imported weapon loadouts, model numbers, storage location, and photos.`;
  el("sync-copy").textContent = `Import the ${system.label} BSData catalogue. Later, click this again to pull updates.`;
  el("sync-btn").textContent = `Sync ${system.short_label || system.label} BSData`;
  const placeholders = {
    kill_team: "Legionary, Kommandos, Angels of Death, Plague Marine...",
    age_of_sigmar_4e: "Liberators, Clanrats, Blood Warriors, Treelord...",
    wh40k_10e: "Boyz, Intercessors, Carnifex, Chaos Lord...",
  };
  el("unit-query").placeholder = placeholders[state.currentGame] || "Search catalogue entries...";
  el("export-link").href = withGame("/api/export.csv");
}

async function loadStatus() {
  const status = await api(withGame("/api/status"));
  el("unit-count").textContent = status.active_unit_count ?? status.unit_count ?? 0;
  el("inventory-count").textContent = status.inventory_count ?? 0;
  el("image-count").textContent = status.image_count ?? 0;
  el("last-import").textContent = formatImportDate(status.last_import);
}

async function loadFactions() {
  const select = el("faction-filter");
  const current = select.value;
  const factions = await api(withGame("/api/factions"));
  select.innerHTML = '<option value="">All factions / armies / teams</option>';
  for (const item of factions) {
    const option = document.createElement("option");
    option.value = item.faction;
    option.textContent = `${item.faction} (${item.unit_count})`;
    select.appendChild(option);
  }
  select.value = Array.from(select.options).some(option => option.value === current) ? current : "";
}

function unitStatsPills(unit) {
  const stats = unit.stats || {};
  const wanted = ["M", "Move", "APL", "GA", "DF", "T", "Toughness", "SV", "Save", "W", "Wounds", "Health", "LD", "Leadership", "OC", "Control"];
  const pills = [];
  const used = new Set();
  if (unit.entry_type) {
    pills.push(`<span class="pill">${esc(unit.entry_type)}</span>`);
  }
  for (const key of wanted) {
    if (stats[key] && !used.has(key.toLowerCase())) {
      used.add(key.toLowerCase());
      pills.push(`<span class="pill">${esc(key)} ${esc(stats[key])}</span>`);
    }
  }
  if (unit.points !== null && unit.points !== undefined) {
    pills.unshift(`<span class="pill">${esc(unit.points)} pts</span>`);
  }
  if (unit.wargear_option_count) {
    pills.push(`<span class="pill">${esc(unit.wargear_option_count)} weapons</span>`);
  }
  if (unit.keywords) {
    for (const keyword of unit.keywords.split(",").slice(0, 4)) {
      const cleaned = keyword.trim();
      if (cleaned) pills.push(`<span class="pill">${esc(cleaned)}</span>`);
    }
  }
  return pills.length ? `<div class="stat-pills">${pills.join("")}</div>` : "";
}

async function searchUnits() {
  const query = el("unit-query").value.trim();
  const faction = el("faction-filter").value;
  const params = new URLSearchParams({ limit: "100" });
  if (query) params.set("query", query);
  if (faction) params.set("faction", faction);

  const container = el("unit-results");
  container.innerHTML = '<div class="empty">Searching...</div>';

  try {
    const units = await api(withGame(`/api/units?${params.toString()}`));
    state.lastUnitQuery = query;
    if (!units.length) {
      container.innerHTML = '<div class="empty">No catalogue entries found. Try syncing BSData or using a broader search.</div>';
      return;
    }
    container.classList.remove("empty");
    container.innerHTML = units.map(unit => `
      <div class="result-card">
        <div>
          <div class="result-title">${esc(unit.name)}</div>
          <div class="result-meta">${esc(unit.faction)} · ${esc(unit.catalogue_file)}</div>
          ${unitStatsPills(unit)}
        </div>
        <button class="secondary" onclick="addUnit(${unit.id})">Add</button>
      </div>
    `).join("");
  } catch (error) {
    container.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
  }
}

async function addUnit(unitId) {
  try {
    await api("/api/inventory", {
      method: "POST",
      body: JSON.stringify({
        game_system: state.currentGame,
        unit_id: unitId,
        quantity: 1,
        models_owned: 1,
        built_count: 0,
        painted_count: 0,
      }),
    });
    toast("Added to inventory.");
    await Promise.all([loadInventory(), loadStatus()]);
  } catch (error) {
    toast(error.message, "error");
  }
}

function numberInput(item, field, width = 76) {
  return `<input data-field="${field}" type="number" min="0" value="${Number(item[field] || 0)}" style="width:${width}px">`;
}

function textInput(item, field, placeholder = "") {
  return `<input data-field="${field}" value="${esc(item[field] || "")}" placeholder="${esc(placeholder)}">`;
}

function textareaInput(item, field, rows = 2, placeholder = "") {
  return `<textarea data-field="${field}" rows="${rows}" placeholder="${esc(placeholder)}">${esc(item[field] || "")}</textarea>`;
}

function wargearStatSummary(option) {
  const stats = option.stats || {};
  const preferred = ["Range", "R", "A", "Atk", "BS", "WS", "WS/BS", "Hit", "S", "Wnd", "AP", "Rnd", "D", "Dmg", "SR", "Ability", "!"];
  const parts = [];
  const used = new Set();
  for (const key of preferred) {
    if (stats[key] !== undefined && stats[key] !== null && String(stats[key]).trim() !== "") {
      parts.push(`${key} ${stats[key]}`);
      used.add(key.toLowerCase());
    }
  }
  if (!parts.length) {
    for (const [key, value] of Object.entries(stats).slice(0, 4)) {
      if (!used.has(key.toLowerCase()) && String(value).trim() !== "") {
        parts.push(`${key} ${value}`);
      }
    }
  }
  return parts.slice(0, 6).join(" · ");
}

function renderWargear(item) {
  const options = item.wargear_options || item.current_wargear_options || [];
  const selections = item.wargear_selections || {};

  if (!options.length) {
    return `
      <div class="wargear-cell wargear-fallback">
        ${textareaInput(item, "wargear", 3, "Weapons / specialist gear")}
        <div class="row-sub">No BSData weapon list found for this row; use notes here.</div>
      </div>
    `;
  }

  return `
    <div class="wargear-cell">
      <div class="wargear-picker" data-wargear-item="${item.id}">
        ${options.map(option => {
          const value = Number(selections[option.key] || 0);
          const stats = wargearStatSummary(option);
          return `
            <div class="wargear-row">
              <div class="wargear-name">
                <span>${esc(option.name)}</span>
                <small>${esc(option.kind || "Weapon")}${stats ? ` · ${esc(stats)}` : ""}</small>
              </div>
              <div class="qty-stepper" aria-label="${esc(option.name)} quantity">
                <input data-wargear-key="${esc(option.key)}" type="number" min="0" max="999" step="1" value="${value}">
              </div>
            </div>
          `;
        }).join("")}
      </div>
      <div class="row-sub">Set the number built with each weapon, then Save.</div>
    </div>
  `;
}

function collectWargearSelections(row) {
  const selections = {};
  row.querySelectorAll("[data-wargear-key]").forEach(input => {
    const key = input.dataset.wargearKey;
    const value = Math.max(0, Number(input.value || 0));
    if (key && value > 0) {
      selections[key] = value;
    }
  });
  return selections;
}

function renderPhotos(item) {
  const images = item.images || [];
  const gallery = images.length ? `
    <div class="thumb-grid">
      ${images.map(image => `
        <figure class="thumb">
          <a href="${esc(image.url)}" target="_blank" rel="noopener">
            <img src="${esc(image.url)}" alt="${esc(image.image_role || "model photo")}">
          </a>
          <figcaption>${esc(image.image_role || "photo")}</figcaption>
          <button type="button" class="thumb-delete" onclick="deleteImage(${image.id})" title="Delete photo">×</button>
        </figure>
      `).join("")}
    </div>
  ` : '<div class="row-sub">No photos yet</div>';

  return `
    <div class="photo-cell">
      ${gallery}
      <div class="photo-upload">
        <select data-image-role="${item.id}" title="Photo type">
          <option value="built">Built</option>
          <option value="painted">Painted</option>
          <option value="wip">WIP</option>
          <option value="reference">Reference</option>
          <option value="other">Other</option>
        </select>
        <label class="file-button">
          Upload
          <input type="file" accept="image/*" onchange="uploadImage(${item.id}, this)">
        </label>
      </div>
    </div>
  `;
}

function renderInventory() {
  const body = el("inventory-body");
  if (!state.inventory.length) {
    body.innerHTML = '<tr><td colspan="13" class="empty-cell">No inventory yet. Search a catalogue entry or add a custom item.</td></tr>';
    return;
  }

  body.innerHTML = state.inventory.map(item => {
    const inactive = item.unit_id && item.unit_active === 0 ? '<div class="row-sub">Catalogue entry not active after latest import</div>' : '';
    const points = item.current_points !== null && item.current_points !== undefined ? `<div class="row-sub">${esc(item.current_points)} pts currently</div>` : "";
    return `
      <tr data-id="${item.id}">
        <td>
          <div class="row-title">${esc(item.unit_name)}</div>
          <div class="row-sub">${item.unit_id ? "BSData linked" : "Custom item"}</div>
          ${points}${inactive}
        </td>
        <td>${textInput(item, "faction", "Faction / army / team")}</td>
        <td>${numberInput(item, "quantity")}</td>
        <td>${numberInput(item, "models_owned")}</td>
        <td>${numberInput(item, "built_count")}</td>
        <td>${numberInput(item, "painted_count")}</td>
        <td><span class="backlog">${item.unbuilt_count} build</span><br><span class="backlog">${item.unpainted_count} paint</span></td>
        <td>${textInput(item, "model_number", "Base #")}</td>
        <td>${renderWargear(item)}</td>
        <td>${textInput(item, "storage_location", "Shelf / case")}</td>
        <td>${renderPhotos(item)}</td>
        <td>${textareaInput(item, "notes", 3, "Notes")}</td>
        <td>
          <div class="actions">
            <button class="secondary" onclick="saveItem(${item.id})">Save</button>
            <button class="danger" onclick="deleteItem(${item.id})">Delete</button>
          </div>
        </td>
      </tr>
    `;
  }).join("");
}

async function loadInventory() {
  state.inventory = await api(withGame("/api/inventory"));
  renderInventory();
}

function collectItemPayload(itemId) {
  const original = state.inventory.find(item => item.id === itemId);
  const row = document.querySelector(`tr[data-id="${itemId}"]`);
  if (!original || !row) throw new Error("Could not find item row.");

  const field = (name) => row.querySelector(`[data-field="${name}"]`)?.value || "";
  const numberField = (name) => Number(field(name) || 0);

  return {
    game_system: original.game_system || state.currentGame,
    unit_id: original.unit_id,
    unit_name: original.unit_name,
    catalogue_file: original.catalogue_file,
    faction: field("faction"),
    quantity: numberField("quantity"),
    models_owned: numberField("models_owned"),
    built_count: numberField("built_count"),
    painted_count: numberField("painted_count"),
    wargear: field("wargear"),
    wargear_selections: collectWargearSelections(row),
    model_number: field("model_number"),
    storage_location: field("storage_location"),
    notes: field("notes"),
    acquired_on: original.acquired_on,
  };
}

async function saveItem(itemId) {
  try {
    await api(`/api/inventory/${itemId}`, {
      method: "PUT",
      body: JSON.stringify(collectItemPayload(itemId)),
    });
    toast("Inventory item saved.");
    await loadInventory();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function deleteItem(itemId) {
  const original = state.inventory.find(item => item.id === itemId);
  if (!confirm(`Delete ${original?.unit_name || "this item"}? Photos attached to it will also be removed.`)) return;
  try {
    await api(`/api/inventory/${itemId}`, { method: "DELETE" });
    toast("Inventory item deleted.");
    await Promise.all([loadInventory(), loadStatus()]);
  } catch (error) {
    toast(error.message, "error");
  }
}

async function uploadImage(itemId, input) {
  const file = input.files && input.files[0];
  if (!file) return;
  const role = document.querySelector(`[data-image-role="${itemId}"]`)?.value || "other";
  const formData = new FormData();
  formData.append("image", file);
  formData.append("image_role", role);

  try {
    await api(`/api/inventory/${itemId}/images`, {
      method: "POST",
      body: formData,
    });
    toast("Photo uploaded.");
    await Promise.all([loadInventory(), loadStatus()]);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    input.value = "";
  }
}

async function deleteImage(imageId) {
  if (!confirm("Delete this photo?")) return;
  try {
    await api(`/api/images/${imageId}`, { method: "DELETE" });
    toast("Photo deleted.");
    await Promise.all([loadInventory(), loadStatus()]);
  } catch (error) {
    toast(error.message, "error");
  }
}

async function syncBsdata() {
  const btn = el("sync-btn");
  const system = activeSystem();
  btn.disabled = true;
  btn.textContent = "Syncing...";
  try {
    const result = await api(`/api/sync/${state.currentGame}`, { method: "POST", body: "{}" });
    const errorSuffix = result.errors?.length ? ` (${result.errors.length} catalogue errors)` : "";
    toast(`Imported ${result.units_imported} ${system.catalogue_word || "entries"} from ${result.files_scanned} files${errorSuffix}.`);
    await Promise.all([loadStatus(), loadFactions(), searchUnits()]);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    btn.disabled = false;
    updateGameCopy();
  }
}

function customFormPayload(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  data.game_system = state.currentGame;
  for (const key of ["quantity", "models_owned", "built_count", "painted_count"]) {
    data[key] = Number(data[key] || 0);
  }
  return data;
}

async function refreshGameData() {
  renderTabs();
  updateGameCopy();
  el("unit-results").innerHTML = '<div class="empty">Search after syncing this game system.</div>';
  await Promise.all([loadStatus(), loadFactions(), loadInventory()]);
  await searchUnits();
}

async function setGameSystem(gameSystem) {
  if (gameSystem === state.currentGame) return;
  state.currentGame = gameSystem;
  localStorage.setItem("warhammer-stock-current-game", gameSystem);
  try {
    await refreshGameData();
  } catch (error) {
    toast(error.message, "error");
  }
}

function wireEvents() {
  el("sync-btn").addEventListener("click", syncBsdata);
  el("faction-filter").addEventListener("change", searchUnits);
  el("game-tabs").addEventListener("click", (event) => {
    const button = event.target.closest("[data-game]");
    if (!button) return;
    setGameSystem(button.dataset.game);
  });

  let searchTimer = null;
  el("unit-query").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(searchUnits, 250);
  });

  el("custom-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    try {
      await api("/api/inventory", {
        method: "POST",
        body: JSON.stringify(customFormPayload(form)),
      });
      form.reset();
      form.querySelector('[name="quantity"]').value = 1;
      form.querySelector('[name="models_owned"]').value = 1;
      form.querySelector('[name="built_count"]').value = 0;
      form.querySelector('[name="painted_count"]').value = 0;
      toast("Custom item added.");
      await Promise.all([loadInventory(), loadStatus()]);
    } catch (error) {
      toast(error.message, "error");
    }
  });
}

window.addUnit = addUnit;
window.saveItem = saveItem;
window.deleteItem = deleteItem;
window.uploadImage = uploadImage;
window.deleteImage = deleteImage;

// Load the app.
document.addEventListener("DOMContentLoaded", async () => {
  wireEvents();
  try {
    await loadGameSystems();
    await refreshGameData();
  } catch (error) {
    toast(error.message, "error");
  }
});
