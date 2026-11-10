let currentResult = null;
let erModel = null;
let erLayout = { version: 3, mode: "full", nodes: [], zoom: 1, translate: [0, 0] };
let erMode = "full";
let selectedElement = null;
let csrfToken = "";
let saveTimer = null;
let dragState = null;
let panState = null;
let resizeState = null;
let currentGeometry = null;
let schemaLoadedByAction = false;
let aiRequestBusy = false;

const SVG_NS = "http://www.w3.org/2000/svg";

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&#39;",
    "\"": "&quot;"
  }[c]));
}

function setStatus(message) {
  const el = $("status");
  if (el) el.textContent = message;
}

let currentWorkspace = null;
let workspaces = [];
let historyLoadSequence = 0;

function toggleSessionMenu(force = null) {
  const menu = $("sessionMenu");
  const trigger = $("sessionTrigger");
  if (!menu || !trigger) return;
  const currentlyOpen = !menu.hidden;
  const shouldOpen = force === null ? !currentlyOpen : Boolean(force);
  menu.hidden = !shouldOpen;
  trigger.setAttribute("aria-expanded", String(shouldOpen));
}

function renderWorkspaceList() {
  const target = $("sessionList");
  if (!target) return;
  if (!workspaces.length) {
    target.innerHTML = `<div class="session-empty">No workspaces yet.</div>`;
    return;
  }
  target.innerHTML = workspaces.map((workspace) => {
    const active = currentWorkspace?.id === workspace.id;
    const date = workspace.updated_at ? new Date(workspace.updated_at).toLocaleString() : "";
    const state = workspace.data_state === "cleared" ? "Empty schema" : "Sample data";
    return `
      <article class="session-item ${active ? "active" : ""}">
        <button class="session-select" onclick="selectWorkspace('${esc(workspace.id)}')">
          <span class="session-item-mark">${active ? "●" : "○"}</span>
          <span class="session-item-copy">
            <strong>${esc(workspace.name)}</strong>
            <small>${esc(state)} · ${esc(date)}</small>
          </span>
        </button>
        <div class="session-item-actions">
          <button class="icon-button" title="Rename workspace" onclick="renameWorkspace('${esc(workspace.id)}')">✎</button>
          <button class="icon-button danger-icon" title="Delete workspace" onclick="deleteWorkspace('${esc(workspace.id)}')">×</button>
        </div>
      </article>`;
  }).join("");
}

function applyWorkspace(workspace) {
  currentWorkspace = workspace || null;
  const name = currentWorkspace?.name || "My Workspace";
  if ($("sessionName")) $("sessionName").textContent = name;
  if ($("workspaceBadge")) $("workspaceBadge").textContent = `Session · ${name}`;
  renderWorkspaceList();
}

async function loadWorkspaces() {
  const response = await fetch("/api/sessions");
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Unable to load workspaces");
  workspaces = Array.isArray(data.sessions) ? data.sessions : [];
  applyWorkspace(data.current);
}

async function selectWorkspace(id) {
  if (!id || currentWorkspace?.id === id) {
    toggleSessionMenu(false);
    return;
  }
  setStatus("Switching workspace…");
  try {
    await ensureCsrf();
    const response = await fetch("/api/sessions/select", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify({ id })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to switch workspace");
    await switchToWorkspace(data.session);
  } catch (error) {
    console.error(error);
    setStatus(error.message || "Workspace switch failed");
  }
}

async function switchToWorkspace(workspace) {
  applyWorkspace(workspace);
  toggleSessionMenu(false);
  currentResult = null;
  erModel = null;
  selectedElement = null;
  schemaLoadedByAction = false;
  erLayout = { version: 3, mode: "full", nodes: [], zoom: 1, translate: [0, 0] };
  erMode = "full";
  if ($("query")) $("query").value = "";
  setERMode("full");
  renderInitialPanels();
  clearERCanvas();
  setStatus(`Opening ${workspace.name}…`);
  await loadHistory();
  setStatus(`${workspace.name} ready · enter a query to begin`);
}

async function createWorkspace() {
  const proposed = window.prompt("Name this SQL session", `Session ${workspaces.length + 1}`);
  if (proposed === null) return;
  const name = proposed.trim();
  if (!name) {
    setStatus("Workspace name cannot be empty");
    return;
  }
  setStatus("Creating workspace…");
  try {
    await ensureCsrf();
    const response = await fetch("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify({ name })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to create workspace");
    workspaces = Array.isArray(data.sessions) ? data.sessions : workspaces;
    await switchToWorkspace(data.session);
  } catch (error) {
    console.error(error);
    setStatus(error.message || "Workspace creation failed");
  }
}

async function renameWorkspace(id) {
  const workspace = workspaces.find((item) => item.id === id);
  if (!workspace) return;
  const proposed = window.prompt("Rename workspace", workspace.name);
  if (proposed === null) return;
  const name = proposed.trim();
  if (!name) {
    setStatus("Workspace name cannot be empty");
    return;
  }
  try {
    await ensureCsrf();
    const response = await fetch(`/api/sessions/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify({ name })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to rename workspace");
    workspaces = workspaces.map((item) => item.id === id ? data.session : item);
    if (currentWorkspace?.id === id) applyWorkspace(data.session);
    renderWorkspaceList();
    setStatus("Workspace renamed");
  } catch (error) {
    console.error(error);
    setStatus(error.message || "Rename failed");
  }
}

async function deleteWorkspace(id) {
  const workspace = workspaces.find((item) => item.id === id);
  if (!workspace) return;
  const confirmed = window.confirm(
    `Delete “${workspace.name}”?\n\n` +
    "This removes its database sandbox, saved query history, ER layouts, and session data.\n\nThis cannot be undone."
  );
  if (!confirmed) return;

  setStatus("Deleting workspace…");
  try {
    await ensureCsrf();
    const response = await fetch(`/api/sessions/${encodeURIComponent(id)}`, {
      method: "DELETE",
      headers: { "X-CSRF-Token": csrfToken }
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to delete workspace");
    workspaces = Array.isArray(data.sessions) ? data.sessions : [];
    if (data.deleted_id === currentWorkspace?.id) {
      await switchToWorkspace(data.session);
    } else {
      applyWorkspace(data.session || currentWorkspace);
      toggleSessionMenu(true);
      setStatus("Workspace deleted");
    }
  } catch (error) {
    console.error(error);
    setStatus(error.message || "Delete failed");
  }
}

document.addEventListener("click", (event) => {
  const switcher = $("sessionSwitcher");
  if (!switcher || switcher.contains(event.target)) return;
  toggleSessionMenu(false);
});

function activatePanel(name) {
  document.querySelectorAll(".learning-tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.panel === name);
  });
  document.querySelectorAll(".learning-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `panel-${name}`);
  });
  if (name === "history") loadHistory();
}

function setERMode(mode) {
  erMode = mode === "focus" ? "focus" : "full";
  $("modeFull")?.classList.toggle("active", erMode === "full");
  $("modeFocus")?.classList.toggle("active", erMode === "focus");
  drawER();
}

async function ensureCsrf() {
  if (csrfToken) return csrfToken;
  const response = await fetch("/api/csrf");
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Session expired");
  csrfToken = data.token;
  return csrfToken;
}

async function runAnalysis() {
  const queryNode = $("query");
  if (!queryNode) return;
  const query = queryNode.value.trim();
  if (!query) {
    setStatus("Enter a query first");
    return;
  }
  const button = $("analyzeButton");
  const submittedSessionId = currentWorkspace?.id || null;
  if (button) button.disabled = true;
  setStatus("Analyzing schema + query…");
  try {
    await ensureCsrf();
    const response = await fetch("/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify({
        query,
        session_id: submittedSessionId,
        er_layout: erLayout
      })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Analysis failed");
    // The request is saved against the session selected when Analyze was
    // clicked. Do not paint its result into a different session if the user
    // switched workspaces while the request was running.
    if (submittedSessionId && currentWorkspace?.id !== submittedSessionId) {
      setStatus("Query saved to its original session. Switch back to view it in History.");
      return;
    }
    currentResult = data;
    hydrateResult(data);
    activatePanel("analysis");
    setStatus(data.parsed?.validation_errors?.length ? "Review query issues" : "Analysis complete");
  } catch (error) {
    console.error(error);
    setStatus(error.message || "Analysis failed");
  } finally {
    if (button) button.disabled = false;
  }
}

function hydrateResult(result) {
  currentResult = result;
  erModel = result.er_model || erModel;
  erLayout = mergeLayoutWithModel(result.er_layout || result.er_state || {}, erModel);
  selectedElement = null;
  if ($("query")) $("query").value = result.query || $("query").value;
  renderAnalysis(result.parsed || {});
  renderCost(result.performance || {});
  renderSecurity(result.security || []);
  renderOutput(result.output || {});
  renderAI(result.ai || {});
  drawER();
  if (result.er_recovery_message) showERToast(result.er_recovery_message);
  else hideERToast();
}

function normalizeLayout(layout) {
  const sourceVersion = Number(layout?.version);
  const nodes = sourceVersion >= 3 && Array.isArray(layout?.nodes) ? layout.nodes : [];
  const seen = new Set();
  const clean = [];
  for (const node of nodes) {
    const id = String(node?.id || "").toLowerCase();
    const x = Number(node?.x);
    const y = Number(node?.y);
    if (!id || seen.has(id) || !Number.isFinite(x) || !Number.isFinite(y)) continue;
    seen.add(id);
    clean.push({ id, x, y });
  }
  const zoomValue = Number(layout?.zoom);
  const zoom = Number.isFinite(zoomValue) ? Math.min(3, Math.max(.3, zoomValue)) : 1;
  const translate = Array.isArray(layout?.translate) && layout.translate.length === 2
    ? [Number(layout.translate[0]) || 0, Number(layout.translate[1]) || 0]
    : [0, 0];
  return { version: 3, mode: layout?.mode === "focus" ? "focus" : "full", nodes: clean, zoom, translate };
}

function defaultEntityPositions(entities) {
  const list = Array.isArray(entities) ? entities : [];
  if (!list.length) return [];

  // Relationship-aware layered layout. This is deliberately deterministic:
  // parent tables stay to the left, dependent tables move right, and siblings
  // are spaced vertically. It avoids the random/criss-cross effect of a force
  // simulation while keeping the diagram easy to learn.
  const ids = new Set(list.map((e) => e.id));
  const relations = (erModel?.relationships || []).filter((r) => ids.has(r.from) && ids.has(r.to));
  const incoming = new Map(list.map((e) => [e.id, []]));
  const outgoing = new Map(list.map((e) => [e.id, []]));

  relations.forEach((r) => {
    if (!incoming.has(r.to) || !outgoing.has(r.from)) return;
    incoming.get(r.to).push(r.from);
    outgoing.get(r.from).push(r.to);
  });

  const roots = list.filter((e) => (incoming.get(e.id) || []).length === 0);
  const queue = roots.length ? [...roots.map((e) => e.id)] : [list[0].id];
  const depth = new Map(queue.map((id) => [id, 0]));

  while (queue.length) {
    const id = queue.shift();
    const nextDepth = (depth.get(id) || 0) + 1;
    for (const child of outgoing.get(id) || []) {
      if (!depth.has(child) || nextDepth < depth.get(child)) {
        depth.set(child, nextDepth);
        queue.push(child);
      }
    }
  }

  list.forEach((e) => { if (!depth.has(e.id)) depth.set(e.id, 0); });

  const columns = new Map();
  list.forEach((entity) => {
    const d = depth.get(entity.id) || 0;
    if (!columns.has(d)) columns.set(d, []);
    columns.get(d).push(entity);
  });

  const orderedColumns = [...columns.keys()].sort((a, b) => a - b);
  const result = [];
  const gapX = 470;
  const gapY = 330;

  orderedColumns.forEach((d, columnIndex) => {
    const column = columns.get(d) || [];
    column.sort((a, b) => {
      const aParents = (incoming.get(a.id) || []).join(',');
      const bParents = (incoming.get(b.id) || []).join(',');
      return aParents.localeCompare(bParents) || a.name.localeCompare(b.name);
    });
    const totalHeight = Math.max(0, (column.length - 1) * gapY);
    column.forEach((entity, index) => {
      result.push({
        id: entity.id,
        x: 240 + columnIndex * gapX,
        y: 500 - totalHeight / 2 + index * gapY
      });
    });
  });

  // Any disconnected tables are placed in a clean bottom row.
  const placed = new Set(result.map((n) => n.id));
  const disconnected = list.filter((e) => !placed.has(e.id));
  disconnected.forEach((entity, index) => {
    result.push({ id: entity.id, x: 240 + index * gapX, y: 900 });
  });
  return result;
}

function mergeLayoutWithModel(layout, model) {
  const normalized = normalizeLayout(layout);
  if (!model?.entities?.length) return normalized;
  const defaults = new Map(defaultEntityPositions(model.entities).map((node) => [node.id, node]));
  const saved = new Map(normalized.nodes.map((node) => [node.id, node]));
  return {
    ...normalized,
    nodes: model.entities.map((entity) => {
      const old = saved.get(entity.id);
      const fallback = defaults.get(entity.id) || { x: 800, y: 500 };
      return { id: entity.id, x: old?.x ?? fallback.x, y: old?.y ?? fallback.y };
    })
  };
}

function visibleEntities() {
  if (!erModel?.entities) return [];
  const search = $("erSearch")?.value.trim().toLowerCase() || "";
  const keyOnly = Boolean($("keyFilter")?.checked);
  return erModel.entities.filter((entity) => {
    const attrs = entity.attributes || [];
    const searchable = [entity.name, ...attrs.map((a) => `${entity.name}.${a.name}`)].join(" ").toLowerCase();
    if (search && !searchable.includes(search)) return false;
    if (erMode === "focus" && !entity.query_relevant) return false;
    if (keyOnly && !(entity.primary_key?.length || attrs.some((a) => a.foreign_key))) return false;
    return true;
  });
}

function relationshipsVisible() {
  return Boolean($("relationshipFilter")?.checked);
}

function attributesVisible() {
  return Boolean($("attributeFilter")?.checked);
}

function renderInitialPanels() {
  renderAnalysis({});
  renderCost({ available: false, message: "Run a SELECT/WITH query to generate the execution plan.", plan: [], insights: [] });
  renderSecurity([]);
  renderOutput({ available: false, message: "Run a query to see read-only sandbox output." });
  renderAI({ provider: "Schema Tutor", summary: [], teaching: [], suggestions: [], er_explanation: {} });
}

function renderAnalysis(parsed) {
  const target = $("panel-analysis");
  if (!target) return;
  const focusTables = erModel?.query_focus?.tables || parsed.tables || [];
  const joins = parsed.joins || [];
  target.innerHTML = `
    <div class="panel-heading"><div><div class="panel-kicker">QUERY ANALYSIS</div><h2>Understand the SQL</h2></div><span class="badge">${focusTables.length} table(s)</span></div>
    <div class="metric-grid">
      <div><span>Base table</span><strong>${esc(parsed.from || "—")}</strong></div>
      <div><span>Expressions</span><strong>${parsed.select?.length || 0}</strong></div>
      <div><span>JOINs</span><strong>${joins.length}</strong></div>
    </div>
    ${parsed.validation_errors?.length ? `<div class="content-card error"><h3>Query issues</h3>${parsed.validation_errors.map((x) => `<p>${esc(x)}</p>`).join("")}</div>` : ""}
    <div class="content-card">
      <div class="card-heading-row"><div><h3>Query focus</h3><span class="muted">The schema remains authoritative.</span></div><button class="small secondary" onclick="setERMode('focus')">Show focus</button></div>
      <div class="chip-row">${focusTables.length ? focusTables.map((t) => `<button class="entity-chip ${isFocused(t) ? "hot" : ""}" onclick="focusEntity('${esc(t)}')">${esc(t)}</button>`).join("") : `<span class="muted">Run a query to populate focus.</span>`}</div>
    </div>
    ${parsed.select?.length ? `<div class="content-card"><h3>SELECT</h3>${parsed.select.map((x) => `<code class="block-code">${esc(x)}</code>`).join("")}</div>` : ""}
    ${joins.map((j) => `<div class="content-card"><h3>${esc(j.type)} JOIN · ${esc(j.table)}</h3><code class="block-code">${esc(j.on)}</code></div>`).join("")}
    ${parsed.where ? `<div class="content-card"><h3>WHERE</h3><code class="block-code">${esc(parsed.where)}</code></div>` : ""}
    <div class="content-card"><div class="card-heading-row"><div><h3>Database model</h3><span class="muted">${erModel?.entities?.length || 0} entities · ${erModel?.relationships?.length || 0} relationships</span></div><button class="small secondary" onclick="showEROverview()">Explain ER</button></div></div>
  `;
}

function renderCost(performance) {
  const target = $("panel-cost");
  if (!target) return;
  const plan = performance.plan || [];
  target.innerHTML = `
    <div class="panel-heading"><div><div class="panel-kicker">EXECUTION COST</div><h2>Actual SQLite plan</h2></div><span class="badge ${performance.available ? "good" : ""}">${performance.available ? "Available" : "Waiting"}</span></div>
    <div class="metric-grid"><div><span>Plan steps</span><strong>${plan.length}</strong></div><div><span>Mode</span><strong>Read-only</strong></div><div><span>Engine</span><strong>SQLite</strong></div></div>
    <div class="content-card"><p>${esc(performance.message || "Run a query to generate an execution plan.")}</p></div>
    ${(performance.insights || []).map((x) => `<div class="content-card"><p>${esc(x)}</p></div>`).join("")}
    ${plan.length ? `<div class="content-card"><h3>Plan details</h3><div class="table-scroll"><table><thead><tr><th>ID</th><th>Parent</th><th>Detail</th></tr></thead><tbody>${plan.map((x) => `<tr><td>${esc(x.id)}</td><td>${esc(x.parent)}</td><td>${esc(x.detail)}</td></tr>`).join("")}</tbody></table></div></div>` : ""}
  `;
}

function renderSecurity(items) {
  const target = $("panel-security");
  if (!target) return;
  target.innerHTML = `
    <div class="panel-heading"><div><div class="panel-kicker">QUERY SAFETY</div><h2>Security review</h2></div><span class="badge ${items.length ? "warn-badge" : "good"}">${items.length ? `${items.length} finding(s)` : "No findings"}</span></div>
    ${items.length ? items.map((x) => `<div class="content-card warn"><h3>${esc(x.pattern)}</h3><p>${esc(x.message)}</p></div>`).join("") : `<div class="content-card success"><h3>No pattern-based findings</h3><p>The scanner checks a small set of risky SQL text patterns.</p></div>`}
  `;
}

function renderOutput(output) {
  const target = $("panel-output");
  if (!target) return;
  if (!output.available) {
    target.innerHTML = `<div class="panel-heading"><div><div class="panel-kicker">RESULT</div><h2>Query output</h2></div><span class="badge">Waiting</span></div><div class="content-card"><p>${esc(output.message || "Run a query to see output.")}</p></div>`;
    return;
  }
  const columns = output.columns || [];
  const rows = output.rows || [];
  target.innerHTML = `
    <div class="panel-heading"><div><div class="panel-kicker">RESULT</div><h2>Query output</h2></div><span class="badge good">${rows.length} row(s)</span></div>
    <div class="content-card"><p>${esc(output.message || "")}</p>${rows.length ? `<div class="table-scroll"><table><thead><tr>${columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${columns.map((c) => `<td>${esc(row[c])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : `<p class="muted">The query returned no rows.</p>`}</div>
  `;
}

function renderAI(ai) {
  const target = $("panel-ai");
  if (!target) return;
  const explanation = ai.er_explanation || {};
  const context = ai.context || {};
  const entities = explanation.entities || [];
  const relationships = explanation.relationships || [];
  const notation = explanation.notation || [];
  const overview = explanation.overview || ai.summary || [];
  const suggestions = ai.suggestions || [];

  target.innerHTML = `
    <div class="panel-heading"><div><div class="panel-kicker">AI TUTOR</div><h2>${esc(ai.provider || "Schema Tutor")}</h2></div><span class="badge">${context.entity_count || erModel?.entities?.length || 0} entities</span></div>
    <div id="aiContextCard" class="context-card"><strong>Click anything in the ER diagram.</strong><span>The tutor will explain the selected entity, attribute, relationship, key, datatype, or notation.</span></div>
    <div class="tutor-section"><h3>Overview</h3>${overview.length ? overview.map((x) => `<div class="ai-item">${esc(x)}</div>`).join("") : `<div class="ai-item muted">Run a query or ask a question.</div>`}</div>
    <div class="tutor-section"><h3>Entities & attributes</h3><div class="tutor-list">${entities.map((x) => `<button class="tutor-row ${x.query_relevant ? "relevant" : ""}" onclick="focusEntity('${esc(x.name)}')"><strong>${esc(x.name)}</strong><span>${esc(x.text)}</span></button>`).join("")}</div></div>
    <div class="tutor-section"><h3>Relationships</h3><div class="tutor-list">${relationships.map((x) => `<button class="tutor-row ${x.query_relevant ? "relevant" : ""}" onclick="focusRelationship('${esc(x.name)}')"><strong>${esc(x.name)}</strong><span>${esc(x.text)}</span></button>`).join("")}</div></div>
    <div class="tutor-section"><h3>ER notation</h3>${notation.map((x) => `<div class="ai-item">${esc(x)}</div>`).join("")}</div>
    <div class="tutor-section"><h3>SQL teaching</h3>${(ai.teaching || []).map((x) => `<div class="ai-item">${esc(x)}</div>`).join("")}</div>
    <div class="ai-chat">
      <div class="ai-chat-title"><div><div class="panel-kicker">ASK THE TUTOR</div><strong>Ask about this database</strong></div><button class="small secondary" onclick="askAI('Explain the complete ER diagram and how the relationships work.')">Explain ER</button></div>
      <textarea id="aiQuestion" class="ai-question" placeholder="Why is orders.user_id a foreign key? Why is users → orders 1:N? Explain this query…"></textarea>
      <div class="ai-chat-actions"><button id="aiAskButton" onclick="askAI()">Ask AI</button><button class="secondary small" onclick="askAI('Explain the currently selected ER element.')">Explain selection</button><button class="secondary small" onclick="askAI('Explain the current SQL query using the ER diagram.')">Explain query</button></div>
      <div id="aiAnswer"></div>
      <div id="aiSuggestions" class="ai-suggestions">${suggestions.map((x) => `<button class="ai-chip" onclick="askAI('${esc(x)}')">${esc(x)}</button>`).join("")}</div>
    </div>
  `;
}

function updateTutorContext(title, text) {
  const card = $("aiContextCard");
  if (!card) return;
  card.innerHTML = `<strong>${esc(title)}</strong><span>${esc(text)}</span>`;
}

function showEROverview() {
  selectedElement = null;
  clearSelectionStyles();
  const count = erModel?.entities?.length || 0;
  const relationships = erModel?.relationships?.length || 0;
  updateTutorContext("Complete ER model", `The model contains ${count} entities and ${relationships} relationships. Rectangles are entities, ovals are attributes, diamonds are relationships, and underlined attributes are primary keys.`);
  activatePanel("ai");
}

function findEntity(name) {
  const wanted = String(name || "").toLowerCase();
  return erModel?.entities?.find((entity) => String(entity.name).toLowerCase() === wanted || String(entity.id).toLowerCase() === wanted) || null;
}

function focusEntity(name, openTutor = true) {
  const entity = findEntity(name);
  if (!entity) return;
  selectedElement = { type: "entity", id: entity.id };
  clearSelectionStyles();
  markSelection();
  const relationships = (erModel.relationships || []).filter((r) => r.from === entity.id || r.to === entity.id);
  const attrs = (entity.attributes || []).map((a) => `${a.name} (${a.type}${a.key ? `, ${a.key}` : ""}${a.foreign_key ? ", FK" : ""})`).join("; ");
  updateTutorContext(`${entity.name} — entity`, `Primary key: ${(entity.primary_key || []).join(", ") || "none declared"}. Attributes: ${attrs || "none"}. Relationships: ${relationships.length}.`);
  centerOnEntity(entity.id);
  if (openTutor) activatePanel("ai");
}

function focusAttribute(entityId, attributeId, openTutor = true) {
  const entity = erModel?.entities?.find((e) => e.id === entityId);
  const attribute = entity?.attributes?.find((a) => a.id === attributeId);
  if (!entity || !attribute) return;
  selectedElement = { type: "attribute", id: attribute.id };
  clearSelectionStyles();
  markSelection();
  let text = `${entity.name}.${attribute.name} is ${attribute.type}.`;
  if (attribute.key) text += ` Key role: ${attribute.key}.`;
  if (attribute.foreign_key) text += " It is a foreign key.";
  if (attribute.references) text += ` It references ${attribute.references.table}.${attribute.references.column}.`;
  text += attribute.nullable ? " It is nullable." : " It is NOT NULL.";
  updateTutorContext(`${entity.name}.${attribute.name} — attribute`, text);
  centerOnEntity(entity.id);
  if (openTutor) activatePanel("ai");
}

function focusRelationship(name, openTutor = true) {
  const relationship = erModel?.relationships?.find((r) => r.name === String(name));
  if (!relationship) return;
  selectedElement = { type: "relationship", id: relationship.id };
  clearSelectionStyles();
  markSelection();
  const source = findEntityById(relationship.from);
  const target = findEntityById(relationship.to);
  let text = `${relationship.name} is ${relationship.cardinality}. ${relationship.from_attributes?.join(", ") || "parent key"} connects to ${relationship.to_attributes?.join(", ") || "child key"}.`;
  text += ` Participation: ${relationship.participation?.from || "unknown"} on the parent side and ${relationship.participation?.to || "unknown"} on the child side.`;
  if (relationship.identifying) text += " It is an identifying relationship.";
  if (relationship.inferred_from_query) text += " This particular connection was inferred from the SQL JOIN.";
  updateTutorContext(`${relationship.name} — relationship`, `${text} Entities: ${source?.name || relationship.from} ↔ ${target?.name || relationship.to}.`);
  centerOnRelationship(relationship.id);
  if (openTutor) activatePanel("ai");
}

function findEntityById(id) {
  return erModel?.entities?.find((entity) => entity.id === id) || null;
}

function isFocused(tableName) {
  return Boolean(erModel?.query_focus?.tables?.includes(String(tableName).toLowerCase()));
}

function applyERFilters() {
  drawER();
}

function entitySize(entity) {
  return { width: Math.max(190, Math.min(260, 150 + String(entity.name).length * 7)), height: 68 };
}

function svgEl(tag, attrs = {}) {
  const el = document.createElementNS(SVG_NS, tag);
  Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, value));
  return el;
}

function svgText(parent, text, attrs = {}) {
  const node = svgEl("text", attrs);
  node.textContent = String(text ?? "");
  parent.appendChild(node);
  return node;
}

function edgePoint(center, target, width, height) {
  const dx = target.x - center.x;
  const dy = target.y - center.y;
  const absX = Math.abs(dx);
  const absY = Math.abs(dy);
  let scale = 1;
  if (absX > 0) scale = Math.min(scale, (width / 2) / absX);
  if (absY > 0) scale = Math.min(scale, (height / 2) / absY);
  return { x: center.x + dx * scale, y: center.y + dy * scale };
}

function attributePoint(center, target) {
  const dx = target.x - center.x;
  const dy = target.y - center.y;
  const dist = Math.max(1, Math.hypot(dx, dy));
  const edge = 55;
  return { x: center.x + dx / dist * edge, y: center.y + dy / dist * edge };
}

function attributeAngle(index, total) {
  if (total <= 6) {
    const angles = [-Math.PI / 2, -Math.PI / 6, Math.PI / 6, 5 * Math.PI / 6, 7 * Math.PI / 6, 5 * Math.PI / 3];
    return angles[index] ?? (-Math.PI / 2 + index * ((Math.PI * 2) / total));
  }
  return -Math.PI / 2 + index * ((Math.PI * 2) / total);
}

function attributeOrbit(index) {
  return 225 + (index % 2) * 18;
}

function getPositions(entities) {
  const defaults = new Map(defaultEntityPositions(entities).map((n) => [n.id, n]));
  const positions = new Map();
  entities.forEach((entity) => {
    const saved = erLayout.nodes.find((node) => node.id === entity.id);
    const fallback = defaults.get(entity.id) || { x: 800, y: 500 };
    positions.set(entity.id, { x: saved?.x ?? fallback.x, y: saved?.y ?? fallback.y });
  });
  return positions;
}

function clearERCanvas(message = "Enter a SQL query and press Analyze Query to build the ER diagram.") {
  const svg = $("erGraph");
  if (svg) {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    svg.style.display = "none";
  }
  const empty = $("erEmpty");
  if (empty) {
    empty.style.display = "grid";
    empty.innerHTML = `<div class="empty-symbol">◇</div><h3>Nothing to visualize yet</h3><p>${esc(message)}</p>`;
  }
  const subtitle = $("erSubtitle");
  if (subtitle) subtitle.textContent = "Waiting for SQL input…";
  const map = $("schemaMap");
  if (map) map.style.display = "none";
}

function drawER() {
  const svg = $("erGraph");
  if (!svg) return;
  const query = $("query")?.value.trim() || "";
  const hasUserQuery = Boolean(query || currentResult?.query);
  if (!hasUserQuery && !schemaLoadedByAction) {
    clearERCanvas();
    return;
  }
  svg.style.display = "block";
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  const entities = visibleEntities();
  const allCount = erModel?.entities?.length || 0;
  const empty = $("erEmpty");
  if (empty) empty.style.display = entities.length ? "none" : "grid";

  const subtitle = $("erSubtitle");
  if (subtitle) subtitle.textContent = `${erMode === "focus" ? "Query Focus" : "Full Schema"} · Chen notation · ${entities.length} visible / ${allCount} total`;

  if (!erModel || !entities.length) {
    updateSchemaMap([], new Map());
    const map = $("schemaMap");
    if (map) map.style.display = erModel ? "block" : "none";
    return;
  }
  const map = $("schemaMap");
  if (map) map.style.display = "block";

  erLayout = mergeLayoutWithModel(erLayout, erModel);
  const positions = getPositions(entities);
  const visibleIds = new Set(entities.map((entity) => entity.id));
  const root = svgEl("g", { class: "er-root" });
  const relationLines = svgEl("g", { class: "relation-lines" });
  const relationShapes = svgEl("g", { class: "relation-shapes" });
  const attributeLines = svgEl("g", { class: "attribute-lines" });
  const attributes = svgEl("g", { class: "attributes" });
  const entitiesLayer = svgEl("g", { class: "entities-layer" });

  root.append(relationLines, relationShapes, attributeLines, attributes, entitiesLayer);
  svg.appendChild(root);

  const relationParts = [];
  const attributeParts = [];
  const entityParts = [];

  const relations = relationshipsVisible()
    ? (erModel.relationships || []).filter((r) => visibleIds.has(r.from) && visibleIds.has(r.to))
    : [];

  relations.forEach((relation) => {
    const source = findEntityById(relation.from);
    const target = findEntityById(relation.to);
    if (!source || !target) return;

    const group = svgEl("g", { class: `relationship-group ${relation.query_relevant ? "hot" : ""}` });
    group.dataset.relId = relation.id;

    const lineA = svgEl("line", { class: "er-association" });
    const lineB = svgEl("line", { class: "er-association" });
    const diamond = svgEl("polygon", { class: "er-relationship" });
    const label = svgText(group, relation.name, { class: "relationship-label", "text-anchor": "middle" });
    const card = svgText(group, relation.cardinality || "", { class: "cardinality-label", "text-anchor": "middle" });
    const part = svgText(group, `${relation.participation?.from || ""} / ${relation.participation?.to || ""}`, { class: "participation-label", "text-anchor": "middle" });

    group.append(lineA, lineB, diamond);

    if (relation.identifying) {
      group.appendChild(svgEl("polygon", { class: "er-relationship-inner" }));
    }

    group.addEventListener("click", (event) => {
      event.stopPropagation();
      if (dragState?.moved) return;
      focusRelationship(relation.name);
    });

    relationLines.append(lineA, lineB);
    relationShapes.append(group);
    relationParts.push({ relation, group, lineA, lineB, diamond, inner: group.querySelector(".er-relationship-inner"), label, card, part });
  });

  entities.forEach((entity) => {
    const position = positions.get(entity.id);
    if (!position) return;

    const size = entitySize(entity);
    const group = svgEl("g", { class: `er-entity ${entity.query_relevant ? "hot" : ""}` });
    group.dataset.entityId = entity.id;

    const rect = svgEl("rect", { x: -size.width / 2, y: -size.height / 2, width: size.width, height: size.height, rx: 2, class: `entity-box ${entity.weak ? "weak" : ""}` });
    group.appendChild(rect);
    if (entity.weak) group.appendChild(svgEl("rect", { x: -size.width / 2 + 7, y: -size.height / 2 + 7, width: size.width - 14, height: size.height - 14, rx: 1, class: "entity-box-inner" }));
    svgText(group, entity.name, { x: 0, y: 6, class: "entity-title", "text-anchor": "middle" });

    const hit = svgEl("rect", { x: -size.width / 2, y: -size.height / 2, width: size.width, height: size.height, fill: "transparent", cursor: "grab" });
    hit.addEventListener("pointerdown", (event) => {
      event.stopPropagation();
      beginEntityDrag(event, entity);
    });
    hit.addEventListener("click", (event) => {
      event.stopPropagation();
      if (dragState?.moved) return;
      focusEntity(entity.name);
    });
    group.appendChild(hit);

    entitiesLayer.appendChild(group);
    entityParts.push({ entity, group, position, size });

    if (!attributesVisible()) return;

    const attrs = entity.attributes || [];
    attrs.forEach((attribute, index) => {
      const keyOnly = Boolean($("keyFilter")?.checked);
      if (keyOnly && !attribute.key && !attribute.foreign_key) return;
      if (erMode === "focus" && !attribute.query_relevant && !attribute.key && !attribute.foreign_key) return;

      const angle = attributeAngle(index, attrs.length);
      const orbit = attributeOrbit(index);
      const x = position.x + Math.cos(angle) * orbit;
      const y = position.y + Math.sin(angle) * orbit * .7;
      const rx = Math.min(120, Math.max(72, 48 + String(attribute.name).length * 2.8));
      const ry = 26;
      const groupAttr = svgEl("g", { class: `attribute-group ${attribute.query_relevant ? "hot" : ""}` });
      groupAttr.dataset.attributeId = attribute.id;
      groupAttr.dataset.entityId = entity.id;
      const connector = svgEl("line", { class: "attribute-line" });
      const ellipse = svgEl("ellipse", { cx: x, cy: y, rx, ry, class: `attribute-oval ${attribute.key ? "key" : ""}` });
      groupAttr.appendChild(connector);
      groupAttr.appendChild(ellipse);
      if (attribute.multivalued) groupAttr.appendChild(svgEl("ellipse", { cx: x, cy: y, rx: rx + 5, ry: ry + 5, class: "attribute-oval-inner" }));
      svgText(groupAttr, `${attribute.name} : ${attribute.type}`, { x, y: y + 3, class: "attribute-label", "text-anchor": "middle" });
      if (attribute.key === "PK" || attribute.key === "PK/FK") groupAttr.appendChild(svgEl("line", { x1: x - Math.min(32, rx * .6), x2: x + Math.min(32, rx * .6), y1: y + 10, y2: y + 10, class: "pk-underline" }));
      groupAttr.addEventListener("click", (event) => {
        event.stopPropagation();
        focusAttribute(entity.id, attribute.id);
      });
      attributeLines.appendChild(connector);
      attributes.appendChild(groupAttr);
      attributeParts.push({ entity, attribute, group: groupAttr, connector, x, y, rx, ry });
    });
  });

  currentGeometry = { positions, relationParts, attributeParts, entityParts };
  updateGeometry(currentGeometry);
  applyTransform(root);
  updateSchemaMap(entities, positions, relations);
  markSelection();
}

function updateGeometry(geometry) {
  if (!geometry) return;
  const { positions, relationParts, attributeParts, entityParts } = geometry;

  relationParts.forEach((part) => {
    const source = findEntityById(part.relation.from);
    const target = findEntityById(part.relation.to);
    const sourcePos = positions.get(part.relation.from);
    const targetPos = positions.get(part.relation.to);
    if (!source || !target || !sourcePos || !targetPos) return;

    const sourceSize = entitySize(source);
    const targetSize = entitySize(target);
    const dx = targetPos.x - sourcePos.x;
    const dy = targetPos.y - sourcePos.y;
    const len = Math.max(1, Math.hypot(dx, dy));
    const normalX = -dy / len;
    const normalY = dx / len;
    const mid = { x: (sourcePos.x + targetPos.x) / 2 + normalX * 18, y: (sourcePos.y + targetPos.y) / 2 + normalY * 18 };
    const sourceEdge = edgePoint(sourcePos, mid, sourceSize.width, sourceSize.height);
    const targetEdge = edgePoint(targetPos, mid, targetSize.width, targetSize.height);

    const width = Math.max(60, Math.min(130, 32 + String(part.relation.name).length * 3.2));
    const height = 24;
    const diamondPoints = `${mid.x},${mid.y - height} ${mid.x + width},${mid.y} ${mid.x},${mid.y + height} ${mid.x - width},${mid.y}`;

    part.lineA.setAttribute("x1", sourceEdge.x); part.lineA.setAttribute("y1", sourceEdge.y); part.lineA.setAttribute("x2", mid.x); part.lineA.setAttribute("y2", mid.y);
    part.lineB.setAttribute("x1", mid.x); part.lineB.setAttribute("y1", mid.y); part.lineB.setAttribute("x2", targetEdge.x); part.lineB.setAttribute("y2", targetEdge.y);
    part.diamond.setAttribute("points", diamondPoints);
    if (part.inner) part.inner.setAttribute("points", `${mid.x},${mid.y - height + 5} ${mid.x + width - 6},${mid.y} ${mid.x},${mid.y + height - 5} ${mid.x - width + 6},${mid.y}`);
    part.label.setAttribute("x", mid.x); part.label.setAttribute("y", mid.y + 3);
    part.card.setAttribute("x", mid.x); part.card.setAttribute("y", mid.y - height - 8);
    part.part.setAttribute("x", mid.x); part.part.setAttribute("y", mid.y + height + 13);
  });

  attributeParts.forEach((part) => {
    const center = positions.get(part.entity.id);
    if (!center) return;
    const attrs = part.entity.attributes || [];
    const index = attrs.findIndex((a) => a.id === part.attribute.id);
    const angle = attributeAngle(index, attrs.length);
    const orbit = attributeOrbit(index);
    const x = center.x + Math.cos(angle) * orbit;
    const y = center.y + Math.sin(angle) * orbit * .7;
    const start = attributePoint(center, { x, y });

    part.connector.setAttribute("x1", start.x); part.connector.setAttribute("y1", start.y); part.connector.setAttribute("x2", x); part.connector.setAttribute("y2", y);
    part.group.querySelector(".attribute-oval")?.setAttribute("cx", x); part.group.querySelector(".attribute-oval")?.setAttribute("cy", y);
    part.group.querySelector(".attribute-oval-inner")?.setAttribute("cx", x); part.group.querySelector(".attribute-oval-inner")?.setAttribute("cy", y);
    const label = part.group.querySelector(".attribute-label");
    if (label) { label.setAttribute("x", x); label.setAttribute("y", y + 3); }
    const underline = part.group.querySelector(".pk-underline");
    if (underline) { underline.setAttribute("x1", x - Math.min(32, part.rx * .6)); underline.setAttribute("x2", x + Math.min(32, part.rx * .6)); underline.setAttribute("y1", y + 10); underline.setAttribute("y2", y + 10); }
  });

  entityParts.forEach((part) => {
    const position = positions.get(part.entity.id);
    if (position) part.group.setAttribute("transform", `translate(${position.x},${position.y})`);
  });
}

function applyTransform(root) {
  root.setAttribute("transform", `translate(${erLayout.translate[0]},${erLayout.translate[1]}) scale(${erLayout.zoom})`);
}

function beginEntityDrag(event, entity) {
  if (event.button !== 0) return;
  const pos = currentGeometry?.positions?.get(entity.id) || { x: 800, y: 500 };
  dragState = { entityId: entity.id, startX: event.clientX, startY: event.clientY, baseX: pos.x, baseY: pos.y, moved: false };
  window.addEventListener("pointermove", handleEntityDrag);
  window.addEventListener("pointerup", endEntityDrag, { once: true });
}

function handleEntityDrag(event) {
  if (!dragState || !currentGeometry) return;
  const dx = event.clientX - dragState.startX;
  const dy = event.clientY - dragState.startY;
  if (Math.abs(dx) > 3 || Math.abs(dy) > 3) dragState.moved = true;
  const scale = Math.max(.3, erLayout.zoom);
  const x = dragState.baseX + dx / scale;
  const y = dragState.baseY + dy / scale;
  const live = currentGeometry.positions.get(dragState.entityId);
  if (live) { live.x = x; live.y = y; }
  persistPosition(dragState.entityId, x, y);
  updateGeometry(currentGeometry);
  updateSchemaMap([...currentGeometry.positions.keys()].map((id) => findEntityById(id)).filter(Boolean), currentGeometry.positions, currentGeometry.relationParts.map((p) => p.relation));
}

function endEntityDrag() {
  window.removeEventListener("pointermove", handleEntityDrag);
  if (dragState?.moved) scheduleLayoutSave();
  dragState = null;
}

function beginPan(event) {
  if (event.button !== 0 || dragState) return;
  panState = { startX: event.clientX, startY: event.clientY, startTx: erLayout.translate[0], startTy: erLayout.translate[1] };
  window.addEventListener("pointermove", handlePan);
  window.addEventListener("pointerup", endPan, { once: true });
}

function handlePan(event) {
  if (!panState) return;
  erLayout.translate = [panState.startTx + event.clientX - panState.startX, panState.startTy + event.clientY - panState.startY];
  const root = $("erGraph")?.querySelector(".er-root");
  if (root) applyTransform(root);
  if (currentGeometry) updateSchemaMap([...currentGeometry.positions.keys()].map((id) => findEntityById(id)).filter(Boolean), currentGeometry.positions, currentGeometry.relationParts.map((p) => p.relation));
}

function endPan() {
  window.removeEventListener("pointermove", handlePan);
  panState = null;
  scheduleLayoutSave();
}

function persistPosition(id, x, y) {
  const node = erLayout.nodes.find((item) => item.id === id);
  if (node) { node.x = x; node.y = y; }
  else erLayout.nodes.push({ id, x, y });
}

function initCanvasPointerHandlers() {
  const svg = $("erGraph");
  if (!svg) return;
  svg.addEventListener("wheel", (event) => {
    event.preventDefault();
    const direction = event.deltaY > 0 ? .9 : 1.1;
    erLayout.zoom = Math.min(2.6, Math.max(.35, erLayout.zoom * direction));
    const root = svg.querySelector(".er-root");
    if (root) applyTransform(root);
    scheduleLayoutSave();
  }, { passive: false });
  svg.addEventListener("pointerdown", (event) => {
    if (event.target === svg || event.target?.classList?.contains("er-root")) beginPan(event);
  });
}

function clearSelectionStyles() {
  document.querySelectorAll(".er-entity, .attribute-group, .relationship-group").forEach((el) => el.classList.remove("selected"));
}

function markSelection() {
  if (!selectedElement) return;
  if (selectedElement.type === "entity") {
    document.querySelectorAll(".er-entity").forEach((el) => { if (el.dataset.entityId === selectedElement.id) el.classList.add("selected"); });
  } else if (selectedElement.type === "attribute") {
    document.querySelectorAll(".attribute-group").forEach((el) => { if (el.dataset.attributeId === selectedElement.id) el.classList.add("selected"); });
  } else if (selectedElement.type === "relationship") {
    document.querySelectorAll(".relationship-group").forEach((el) => { if (el.dataset.relId === selectedElement.id) el.classList.add("selected"); });
  }
}

function centerOnEntity(entityId) {
  const pos = currentGeometry?.positions?.get(entityId);
  const svg = $("erGraph");
  if (!pos || !svg) return;
  erLayout.translate = [svg.clientWidth / 2 - pos.x * erLayout.zoom, svg.clientHeight / 2 - pos.y * erLayout.zoom];
  const root = svg.querySelector(".er-root");
  if (root) applyTransform(root);
  scheduleLayoutSave();
}

function centerOnRelationship(relationId) {
  const relation = erModel?.relationships?.find((r) => r.id === relationId);
  if (!relation) return;
  const a = currentGeometry?.positions?.get(relation.from);
  const b = currentGeometry?.positions?.get(relation.to);
  const svg = $("erGraph");
  if (!a || !b || !svg) return;
  const x = (a.x + b.x) / 2;
  const y = (a.y + b.y) / 2;
  erLayout.translate = [svg.clientWidth / 2 - x * erLayout.zoom, svg.clientHeight / 2 - y * erLayout.zoom];
  const root = svg.querySelector(".er-root");
  if (root) applyTransform(root);
}

function fitER() {
  if (!erModel?.entities?.length || !currentGeometry) return;
  const nodes = [...currentGeometry.positions.values()];
  const minX = Math.min(...nodes.map((p) => p.x));
  const maxX = Math.max(...nodes.map((p) => p.x));
  const minY = Math.min(...nodes.map((p) => p.y));
  const maxY = Math.max(...nodes.map((p) => p.y));
  const svg = $("erGraph");
  const width = svg?.clientWidth || 1000;
  const height = svg?.clientHeight || 700;
  const contentWidth = Math.max(700, maxX - minX + 550);
  const contentHeight = Math.max(500, maxY - minY + 460);
  erLayout.zoom = Math.min(1.25, Math.max(.45, Math.min(width / contentWidth, height / contentHeight)));
  const centerX = (minX + maxX) / 2;
  const centerY = (minY + maxY) / 2;
  erLayout.translate = [width / 2 - centerX * erLayout.zoom, height / 2 - centerY * erLayout.zoom];
  const root = svg.querySelector(".er-root");
  if (root) applyTransform(root);
  scheduleLayoutSave();
}

function resetERLayout() {
  if (!erModel?.entities?.length) return;
  erLayout = { version: 3, mode: erMode, nodes: defaultEntityPositions(erModel.entities), zoom: 1, translate: [0, 0] };
  drawER();
  fitER();
  scheduleLayoutSave();
}

function updateSchemaMap(entities, positions, relationships = []) {
  const svg = $("schemaMapSvg");
  const count = $("schemaMapCount");
  if (!svg) return;
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  // The minimap always represents the COMPLETE schema, even when the main
  // canvas is in Query Focus. This makes it a real navigation map.
  const allEntities = erModel?.entities || entities || [];
  const allRelations = erModel?.relationships || relationships || [];
  if (count) count.textContent = `${allEntities.length}`;
  if (!allEntities.length) return;

  const sourcePositions = positions && positions.size ? positions : new Map(
    defaultEntityPositions(allEntities).map((node) => [node.id, { x: node.x, y: node.y }])
  );
  const raw = allEntities.map((entity) => sourcePositions.get(entity.id) || { x: 0, y: 0 });
  const minX = Math.min(...raw.map((p) => p.x));
  const maxX = Math.max(...raw.map((p) => p.x));
  const minY = Math.min(...raw.map((p) => p.y));
  const maxY = Math.max(...raw.map((p) => p.y));
  const pad = 18;
  const width = 240 - pad * 2;
  const height = 150 - pad * 2;
  const sx = Math.max(1, maxX - minX);
  const sy = Math.max(1, maxY - minY);
  const scale = Math.min(width / sx, height / sy);
  const offsetX = pad + (width - sx * scale) / 2;
  const offsetY = pad + (height - sy * scale) / 2;
  const pts = new Map();

  allEntities.forEach((entity) => {
    const p = sourcePositions.get(entity.id) || { x: minX, y: minY };
    pts.set(entity.id, {
      x: offsetX + (p.x - minX) * scale,
      y: offsetY + (p.y - minY) * scale
    });
  });

  allRelations.forEach((relation) => {
    const a = pts.get(relation.from);
    const b = pts.get(relation.to);
    if (!a || !b) return;
    svg.appendChild(svgEl("line", {
      x1: a.x, y1: a.y, x2: b.x, y2: b.y,
      class: `schema-map-line ${relation.query_relevant ? "hot" : ""}`
    }));
  });

  allEntities.forEach((entity) => {
    const p = pts.get(entity.id);
    if (!p) return;
    const group = svgEl("g", { class: "schema-map-item" });
    const node = svgEl("rect", {
      x: p.x - 18, y: p.y - 6, width: 36, height: 12, rx: 3,
      class: `schema-map-node ${entity.query_relevant ? "hot" : ""}`
    });
    const label = svgText(group, entity.name.length > 7 ? `${entity.name.slice(0, 6)}…` : entity.name, {
      x: p.x, y: p.y + 2.5, class: "schema-map-label", "text-anchor": "middle"
    });
    group.appendChild(node);
    group.addEventListener("click", (event) => {
      event.stopPropagation();
      setERMode("full");
      focusEntity(entity.name, false);
      centerOnEntity(entity.id);
    });
    group.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        setERMode("full");
        focusEntity(entity.name, false);
        centerOnEntity(entity.id);
      }
    });
    group.setAttribute("tabindex", "0");
    svg.appendChild(group);
  });
}

function scheduleLayoutSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveCurrentLayout, 700);
}

async function saveCurrentLayout() {
  if (!currentResult?.id) return;
  try {
    await ensureCsrf();
    await fetch(`/history/${encodeURIComponent(currentResult.id)}/state`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify({
        session_id: currentResult.session_id || currentWorkspace?.id || null,
        er_layout: erLayout
      })
    });
  } catch (error) {
    console.warn("Layout save failed", error);
  }
}

async function loadCanonicalModel() {
  setStatus("Refreshing schema…");
  try {
    const response = await fetch("/api/er-model");
    const model = await response.json();
    if (!response.ok) throw new Error(model.error || "Unable to load ER model");
    erModel = model;
    erLayout = mergeLayoutWithModel(erLayout, erModel);
    schemaLoadedByAction = true;
    renderInitialPanels();
    drawER();
    setStatus("Schema ready");
  } catch (error) {
    console.error(error);
    setStatus(error.message || "Schema refresh failed");
  }
}

async function loadHistory() {
  const target = $("panel-history");
  if (!target) return;
  const requestSequence = ++historyLoadSequence;
  const requestedSessionId = currentWorkspace?.id || "";
  target.innerHTML = `<div class="panel-heading"><div><div class="panel-kicker">SAVED WORK</div><h2>History</h2></div><button class="small secondary" onclick="loadHistory()">Refresh</button></div><div class="content-card"><p>Loading history…</p></div>`;
  try {
    const params = new URLSearchParams({ limit: "100" });
    if (requestedSessionId) params.set("session_id", requestedSessionId);
    const response = await fetch(`/history?${params.toString()}`);
    const items = await response.json();
    // Ignore responses that finish after the user has switched to another session.
    if (requestSequence !== historyLoadSequence || requestedSessionId !== (currentWorkspace?.id || "")) return;
    if (!response.ok) throw new Error(items.error || "Unable to load history");
    target.innerHTML = `<div class="panel-heading"><div><div class="panel-kicker">SAVED WORK</div><h2>History</h2></div><span class="badge">${items.length}</span></div>${items.length ? items.map(renderHistoryItem).join("") : `<div class="content-card"><p>No saved queries in this session yet.</p></div>`}`;
  } catch (error) {
    if (requestSequence !== historyLoadSequence) return;
    target.innerHTML = `<div class="content-card error"><p>${esc(error.message)}</p></div>`;
  }
}

function renderHistoryItem(item) {
  const query = String(item.query || "").replace(/\s+/g, " ").trim();
  const entities = item.er_model?.entities?.length || 0;
  const relationships = item.er_model?.relationships?.length || 0;
  const recovery = item.er_recovery_message ? `<div class="history-warning">${esc(item.er_recovery_message)}</div>` : "";
  return `<article class="history-card"><div class="history-top"><span class="badge">${item.parsed?.validation_errors?.length ? "Needs review" : "Analyzed"}</span><span class="history-date">${esc(new Date(item.created_at || "").toLocaleString())}</span></div><div class="history-query" title="${esc(query)}">${esc(query || "(empty query)")}</div><div class="history-meta"><span>${entities} entities</span><span>${relationships} relationships</span></div>${recovery}<button class="secondary history-restore" onclick="restoreHistory('${esc(item.id)}', '${esc(item.session_id || currentWorkspace?.id || "")}')">Restore this state</button></article>`;
}

async function restoreHistory(id, sessionId = currentWorkspace?.id) {
  setStatus("Restoring saved state…");
  try {
    const params = new URLSearchParams();
    if (sessionId) params.set("session_id", sessionId);
    const suffix = params.toString() ? `?${params.toString()}` : "";
    const response = await fetch(`/history/${encodeURIComponent(id)}${suffix}`);
    const item = await response.json();
    if (!response.ok) throw new Error(item.error || "Could not restore history");
    hydrateResult(item);
    activatePanel("history");
    setStatus(item.er_recovery_message || "Historical state restored");
  } catch (error) {
    setStatus(error.message || "Restore failed");
  }
}

function selectedContext() {
  return selectedElement || null;
}

async function askAI(textOverride = null) {
  if (aiRequestBusy) return;
  const questionNode = $("aiQuestion");
  const question = String(textOverride ?? questionNode?.value ?? "").trim();
  if (!question) {
    setStatus("Write a question for the AI Tutor");
    activatePanel("ai");
    return;
  }
  if (!erModel) {
    setStatus("The ER model is still loading");
    return;
  }

  aiRequestBusy = true;
  const button = $("aiAskButton");
  if (button) button.disabled = true;
  const answerNode = $("aiAnswer");
  if (answerNode) answerNode.innerHTML = `<div class="ai-item">Thinking with the ER model…</div>`;
  activatePanel("ai");

  try {
    await ensureCsrf();
    const response = await fetch("/api/ai/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify({
        question,
        selected: selectedContext(),
        query: currentResult?.query || $("query")?.value || "",
        parsed: currentResult?.parsed || {},
        er_model: erModel
      })
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "AI request failed");
    if (answerNode) answerNode.innerHTML = `<div class="ai-item ai-answer"><strong>${esc(result.provider || "AI Tutor")}</strong><br>${esc(result.answer || "No answer returned.")}</div>`;
    applyAIHighlight(result.highlight || {});
    const suggestions = $("aiSuggestions");
    if (suggestions) suggestions.innerHTML = (result.suggestions || []).map((s) => `<button class="ai-chip" onclick="askAI('${esc(s)}')">${esc(s)}</button>`).join("");
    if (questionNode && !textOverride) questionNode.value = "";
    setStatus("AI Tutor answered");
  } catch (error) {
    if (answerNode) answerNode.innerHTML = `<div class="ai-item warn-item">${esc(error.message || "AI request failed")}</div>`;
    setStatus(error.message || "AI request failed");
  } finally {
    aiRequestBusy = false;
    if (button) button.disabled = false;
  }
}

function applyAIHighlight(highlight) {
  clearSelectionStyles();
  const entityIds = new Set(highlight.entities || []);
  const relationIds = new Set(highlight.relationships || []);
  const attributeIds = new Set(highlight.attributes || []);
  document.querySelectorAll(".er-entity").forEach((el) => { if (entityIds.has(el.dataset.entityId)) el.classList.add("selected"); });
  document.querySelectorAll(".relationship-group").forEach((el) => { if (relationIds.has(el.dataset.relId)) el.classList.add("selected"); });
  document.querySelectorAll(".attribute-group").forEach((el) => { if (attributeIds.has(el.dataset.attributeId)) el.classList.add("selected"); });
}

function showERToast(message) {
  const toast = $("erToast");
  if (!toast) return;
  toast.hidden = false;
  toast.textContent = message;
}

function hideERToast() {
  const toast = $("erToast");
  if (toast) toast.hidden = true;
}

function initResize() {
  const divider = $("workspaceDivider");
  const side = $("learningSide");
  const workspace = $("workspace");
  if (!divider || !side || !workspace) return;
  divider.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    resizeState = { startX: event.clientX, startWidth: side.getBoundingClientRect().width };
    document.body.classList.add("resizing");
    window.addEventListener("pointermove", resizeMove);
    window.addEventListener("pointerup", resizeEnd, { once: true });
  });
  function resizeMove(event) {
    if (!resizeState) return;
    const total = workspace.getBoundingClientRect().width;
    const width = Math.min(total * .62, Math.max(330, resizeState.startWidth - (event.clientX - resizeState.startX)));
    workspace.style.setProperty("--learning-width", `${width}px`);
  }
  function resizeEnd() {
    resizeState = null;
    document.body.classList.remove("resizing");
    window.removeEventListener("pointermove", resizeMove);
  }
}

function activateLearningSection(section) {
  const actions = {
    schema: () => { setERMode("full"); fitER(); },
    query: () => { $("query")?.focus(); },
    explain: () => { activatePanel("analysis"); },
    practice: () => { activatePanel("ai"); $("aiQuestion")?.focus(); }
  };
  actions[section]?.();
}

async function clearDatabase() {
  const confirmed = window.confirm(
    "Delete every row from your personal SQL sandbox?\n\n" +
    "The table structure, columns, primary keys and relationships will remain.\n\n" +
    "This cannot be undone."
  );
  if (!confirmed) return;

  setStatus("Deleting sandbox data…");
  try {
    await ensureCsrf();
    const response = await fetch("/api/database/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken }
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Unable to clear database");

    currentResult = null;
    erModel = null;
    selectedElement = null;
    schemaLoadedByAction = false;
    erLayout = { version: 3, mode: erMode, nodes: [], zoom: 1, translate: [0, 0] };
    renderInitialPanels();
    clearERCanvas("This session is empty. Enter a SQL query to begin.");
    await loadHistory();
    currentWorkspace = result.workspace || currentWorkspace;
    if (currentWorkspace) {
      workspaces = workspaces.map((item) => item.id === currentWorkspace.id ? currentWorkspace : item);
      applyWorkspace(currentWorkspace);
    }
    setStatus(`Session cleared · ${result.total_rows} row(s) deleted`);
  } catch (error) {
    console.error(error);
    setStatus(error.message || "Database clear failed");
  }
}

async function boot() {
  renderInitialPanels();
  initResize();
  initCanvasPointerHandlers();
  clearERCanvas();
  try {
    await loadWorkspaces();
    await loadHistory();
    setStatus("Ready · enter a SQL query to begin");
  } catch (error) {
    console.error("Boot failed", error);
    setStatus(error.message || "Workspace initialization failed");
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const queryNode = $("query");
  queryNode?.addEventListener("input", () => {
    if (queryNode.value.trim()) return;
    currentResult = null;
    erModel = null;
    selectedElement = null;
    schemaLoadedByAction = false;
    erLayout = { version: 3, mode: erMode, nodes: [], zoom: 1, translate: [0, 0] };
    renderInitialPanels();
    clearERCanvas();
  });
  boot();
});
