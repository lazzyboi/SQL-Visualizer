let currentResult = null;
let erState = { nodes: [], links: [], zoom: 1, translate: [0, 0] };
let simulation = null;
let erZoom = null;
let savingTimer = null;
let csrfToken = "";

const defaultERState = () => ({ nodes: [], links: [], zoom: 1, translate: [0, 0] });

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;","\"":"&quot;"}[char]));
}

function setStatus(message) { document.getElementById("status").textContent = message; }

function activateTab(tab) {
  document.querySelectorAll(".tab").forEach(button => button.classList.toggle("active", button.dataset.tab === tab));
  document.querySelectorAll(".tab-panel").forEach(panel => panel.classList.toggle("active", panel.id === `tab-${tab}`));
}

document.querySelectorAll(".tab").forEach(button => button.addEventListener("click", () => {
  activateTab(button.dataset.tab);
  if (button.dataset.tab === "history") loadHistory();
  if (button.dataset.tab === "database") loadDatabaseSchema();
}));

async function ensureCsrf() {
  if (csrfToken) return csrfToken;
  const response = await fetch("/api/csrf");
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Session expired");
  csrfToken = data.token;
  return csrfToken;
}

async function runAnalysis() {
  const query = document.getElementById("query").value.trim();
  if (!query) { setStatus("Enter a query first"); return; }
  const button = document.getElementById("analyzeButton");
  button.disabled = true;
  setStatus("Analyzing…");
  try {
    await ensureCsrf();
    const response = await fetch("/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify({ query, er_state: defaultERState() })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Analysis failed");
    currentResult = data;
    erState = data.er_state || defaultERState();
    renderAll(data);
    activateTab("er");
    await loadHistory();
    setStatus(data.parsed?.validation_errors?.length ? "Review the query issues" : "Analysis complete");
  } catch (error) {
    setStatus(error.message);
  } finally {
    button.disabled = false;
  }
}

function renderAll(result) {
  drawER(result.parsed?.er, result.er_state || defaultERState());
  renderAnalysis(result.parsed);
  renderPerformance(result.performance);
  renderSecurity(result.security || []);
  renderOutput(result.output);
  renderAI(result.ai || {});
  loadDatabaseSchema();
}

function renderAnalysis(parsed) {
  const target = document.getElementById("analysisContent");
  const errors = parsed?.validation_errors || [];
  const html = [];
  if (errors.length) html.push(`<div class="content-card error"><h3>Query issues</h3><ul>${errors.map(error => `<li>${escapeHtml(error)}</li>`).join("")}</ul></div>`);
  html.push(`<div class="metric-card"><div><span class="metric-label">Base table</span><strong>${escapeHtml(parsed?.from || "—")}</strong></div><div><span class="metric-label">Selected expressions</span><strong>${parsed?.select?.length || 0}</strong></div><div><span class="metric-label">Joins</span><strong>${parsed?.joins?.length || 0}</strong></div></div>`);
  if (parsed?.select?.length) html.push(`<div class="content-card"><h3>SELECT</h3><ul>${parsed.select.map(item => `<li><code>${escapeHtml(item)}</code></li>`).join("")}</ul></div>`);
  if (parsed?.joins?.length) html.push(parsed.joins.map(join => `<div class="content-card"><h3>${escapeHtml(join.type)} JOIN · ${escapeHtml(join.table)}</h3><p><code>${escapeHtml(join.on)}</code></p></div>`).join(""));
  if (parsed?.where) html.push(`<div class="content-card"><h3>WHERE</h3><p><code>${escapeHtml(parsed.where)}</code></p></div>`);
  if (!parsed?.validation_errors?.length && !parsed?.from && !parsed?.select?.length) html.push(`<div class="content-card"><p>Run a SQL query to populate the analysis.</p></div>`);
  target.innerHTML = html.join("");
}

function renderPerformance(performance) {
  document.getElementById("performanceStatus").textContent = performance?.available ? "Ready" : "Unavailable";
  document.getElementById("performanceSteps").textContent = performance?.plan?.length ?? "—";
  const target = document.getElementById("performanceContent");
  if (!performance?.available) {
    target.innerHTML = `<div class="content-card"><h3>Performance</h3><p>${escapeHtml(performance?.message || "No execution plan available.")}</p></div>`;
    return;
  }
  const insightHtml = (performance.insights || []).map(item => `<div class="content-card"><p>${escapeHtml(item)}</p></div>`).join("");
  const rows = (performance.plan || []).map(step => `<tr><td>${escapeHtml(step.id)}</td><td>${escapeHtml(step.parent)}</td><td>${escapeHtml(step.detail)}</td></tr>`).join("");
  target.innerHTML = `${insightHtml}<div class="content-card"><h3>SQLite execution plan</h3><table><thead><tr><th>ID</th><th>Parent</th><th>Detail</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function renderSecurity(security) {
  const target = document.getElementById("securityContent");
  if (!security?.length) {
    target.innerHTML = `<div class="content-card success"><h3>Security</h3><p>No pattern-based findings for this query.</p></div>`;
    return;
  }
  target.innerHTML = security.map(item => `<div class="content-card warn"><h3>${escapeHtml(item.pattern)}</h3><p>${escapeHtml(item.message)}</p></div>`).join("");
}

function renderOutput(output) {
  const target = document.getElementById("outputContent");
  if (!output?.available) {
    target.innerHTML = `<div class="content-card"><span class="badge">Sandbox</span><h2>Output</h2><p>${escapeHtml(output?.message || "No execution result available.")}</p></div>`;
    return;
  }
  if (!output.rows?.length) {
    target.innerHTML = `<div class="content-card"><h2>Output</h2><p>${escapeHtml(output.message || "The query returned no rows.")}</p></div>`;
    return;
  }
  const head = output.columns.map(column => `<th>${escapeHtml(column)}</th>`).join("");
  const body = output.rows.map(row => `<tr>${output.columns.map(column => `<td>${escapeHtml(row[column])}</td>`).join("")}</tr>`).join("");
  target.innerHTML = `<div class="content-card output-table-card"><h2>Output</h2><p>${escapeHtml(output.message || "")}</p><div class="table-scroll"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div></div>`;
}

function renderAI(ai) {
  document.getElementById("aiTitle").textContent = ai?.provider ? `AI source: ${ai.provider}` : "AI Tutor";
  renderAIList("aiSummary", ai?.summary || [], "summary");
  renderAIList("aiTeaching", ai?.teaching || [], "teaching");
  renderAIList("aiSuggestions", ai?.suggestions || [], "suggestions");
}

function renderAIList(id, items, kind) {
  const target = document.getElementById(id);
  target.innerHTML = items.length ? items.map(item => `<div class="ai-item ${kind === "suggestions" ? "warn" : ""}">${escapeHtml(item)}</div>`).join("") : `<div class="ai-item">Nothing to add here yet.</div>`;
}

function stopERSimulation() {
  if (simulation) { simulation.stop(); simulation = null; }
}

function applyERState(nodes) {
  nodes.forEach(node => {
    if (Number.isFinite(node.x) && Number.isFinite(node.y)) { node.fx = node.x; node.fy = node.y; }
  });
}

function drawER(schema, savedState = {}) {
  const svg = d3.select("#erGraph");
  stopERSimulation();
  svg.selectAll("*").remove();
  const empty = document.getElementById("erEmpty");
  const tables = schema?.tables || [];
  const relationships = schema?.relationships || [];
  empty.style.display = tables.length ? "none" : "grid";
  if (!tables.length) { erState = defaultERState(); return; }

  const savedNodes = new Map((savedState.nodes || []).map(node => [node.id, node]));
  const hasSavedLayout = tables.every(table => savedNodes.has(table.id));
  const nodes = tables.map((table, index) => {
    const saved = savedNodes.get(table.id);
    return { ...table, x: saved?.x ?? 180 + (index % 4) * 250, y: saved?.y ?? 150 + Math.floor(index / 4) * 220, fx: null, fy: null };
  });
  const nodeMap = new Map(nodes.map(node => [node.id, node]));
  const links = relationships.map((link, index) => ({ ...link, id: `${link.source}-${link.target}-${index}`, source: nodeMap.get(link.source), target: nodeMap.get(link.target) })).filter(link => link.source && link.target);
  erState = {
    nodes: nodes.map(node => ({ id: node.id, x: node.x, y: node.y })),
    links: links.map(link => ({ id: link.id, source: link.source.id, target: link.target.id, label: link.label })),
    zoom: Number.isFinite(savedState.zoom) ? savedState.zoom : 1,
    translate: Array.isArray(savedState.translate) ? savedState.translate : [0, 0]
  };

  const root = svg.append("g").attr("class", "viewport");
  const linkLayer = root.append("g").attr("class", "links");
  const nodeLayer = root.append("g").attr("class", "nodes");
  linkLayer.selectAll("line").data(links, d => d.id).join("line").attr("class", "er-link");
  const nodeSelection = nodeLayer.selectAll("g.er-node").data(nodes, d => d.id).join(enter => enter.append("g").attr("class", "er-node"));

  nodeSelection.each(function(d) {
    const node = d3.select(this);
    const width = Math.max(180, d.name.length * 9 + 50);
    const height = 56 + d.columns.length * 24;
    d._width = width; d._height = height;
    node.selectAll("*").remove();
    node.append("rect").attr("width", width).attr("height", height).attr("rx", 10).attr("x", -width / 2).attr("y", -height / 2);
    node.append("rect").attr("class", "header").attr("width", width).attr("height", 36).attr("rx", 10).attr("x", -width / 2).attr("y", -height / 2);
    node.append("text").attr("x", 0).attr("y", -height / 2 + 24).attr("text-anchor", "middle").text(d.name);
    d.columns.forEach((column, index) => node.append("text").attr("class", "column").attr("x", -width / 2 + 12).attr("y", -height / 2 + 58 + index * 20).text(column));
  });

  const updatePositions = () => {
    nodeSelection.attr("transform", d => `translate(${d.x},${d.y})`);
    linkLayer.selectAll("line").attr("x1", d => d.source.x).attr("y1", d => d.source.y).attr("x2", d => d.target.x).attr("y2", d => d.target.y);
  };

  nodeSelection.call(d3.drag()
    .on("start", (event, d) => { event.sourceEvent.stopPropagation(); d.fx = d.x; d.fy = d.y; })
    .on("drag", (event, d) => { d.fx = event.x; d.fy = event.y; d.x = event.x; d.y = event.y; updatePositions(); })
    .on("end", (_event, d) => { d.fx = d.x; d.fy = d.y; updatePositions(); syncERState(nodes, links); scheduleERSave(); })
  );

  erZoom = d3.zoom().scaleExtent([0.35, 2.5]).on("zoom", event => {
    root.attr("transform", event.transform);
    erState.zoom = event.transform.k;
    erState.translate = [event.transform.x, event.transform.y];
    scheduleERSave();
  });
  svg.call(erZoom);
  svg.call(erZoom.transform, d3.zoomIdentity.translate(erState.translate[0], erState.translate[1]).scale(erState.zoom));

  if (hasSavedLayout) {
    applyERState(nodes);
    updatePositions();
    syncERState(nodes, links);
    return;
  }

  simulation = d3.forceSimulation(nodes)
    .force("link", d3.forceLink(links).id(d => d.id).distance(260).strength(0.45))
    .force("charge", d3.forceManyBody().strength(-900))
    .force("center", d3.forceCenter(600, 350))
    .force("collision", d3.forceCollide().radius(d => Math.max(130, Math.max(d._width, d._height) / 2 + 30)))
    .alphaDecay(0.08)
    .on("tick", updatePositions)
    .on("end", () => { applyERState(nodes); updatePositions(); syncERState(nodes, links); simulation.stop(); simulation = null; scheduleERSave(); });
}

function syncERState(nodes, links) {
  erState.nodes = nodes.map(node => ({ id: node.id, x: Number(node.x), y: Number(node.y) }));
  erState.links = links.map(link => ({ id: link.id, source: link.source.id, target: link.target.id, label: link.label }));
}

function scheduleERSave() { clearTimeout(savingTimer); savingTimer = setTimeout(saveCurrentERState, 900); }

async function saveCurrentERState() {
  if (!currentResult?.id) return;
  try { await ensureCsrf(); await fetch(`/history/${currentResult.id}/state`, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken }, body: JSON.stringify({ er_state: erState }) }); } catch (_) {}
}

function resetERLayout() {
  if (!currentResult?.parsed?.er) return;
  stopERSimulation();
  erState = defaultERState();
  drawER(currentResult.parsed.er, erState);
  scheduleERSave();
}

async function loadHistory() {
  const list = document.getElementById("historyList");
  try {
    const response = await fetch("/history?limit=100");
    const items = await response.json();
    if (!response.ok) throw new Error(items.error || "Unable to load history");
    if (!items.length) { list.innerHTML = `<div class="content-card"><p>No saved queries yet.</p></div>`; return; }
    list.innerHTML = items.map(item => {
      const firstLine = (item.query || "(empty query)").replace(/\s+/g, " ").trim();
      const tables = item.parsed?.tables?.join(", ") || "No tables detected";
      const issue = item.parsed?.validation_errors?.length ? "Needs review" : "Analyzed";
      return `<div class="history-card">
        <div class="history-top"><span class="badge">${escapeHtml(issue)}</span><span class="history-date">${escapeHtml(new Date(item.created_at || "").toLocaleString())}</span></div>
        <div class="history-query">${escapeHtml(firstLine)}</div>
        <div class="history-meta"><span>Tables: ${escapeHtml(tables)}</span><span>AI: ${escapeHtml(item.ai?.provider || "fallback")}</span></div>
        <button class="secondary history-restore" onclick="restoreHistory('${escapeHtml(item.id)}')">Restore this state</button>
      </div>`;
    }).join("");
  } catch (error) { list.innerHTML = `<div class="content-card error"><p>${escapeHtml(error.message)}</p></div>`; }
}

async function restoreHistory(id) {
  setStatus("Restoring…");
  try {
    const response = await fetch(`/history/${id}`);
    const item = await response.json();
    if (!response.ok) throw new Error(item.error || "Could not restore history");
    currentResult = item;
    document.getElementById("query").value = item.query || "";
    erState = item.er_state || defaultERState();
    renderAll(item);
    activateTab("er");
    setStatus("Historical state restored");
  } catch (error) { setStatus(error.message); }
}

async function loadDatabaseSchema() {
  const target = document.getElementById("databaseContent");
  if (!target) return;
  try {
    const response = await fetch("/api/database/schema");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to load database schema");
    target.innerHTML = data.map(table => `<div class="schema-card"><div class="schema-heading"><div><h3>${escapeHtml(table.name)}</h3><span>${escapeHtml(table.rows)} row(s)</span></div></div><div class="schema-columns">${table.columns.map(column => `<div class="schema-column"><strong>${escapeHtml(column.name)}</strong><span>${escapeHtml(column.type || "TEXT")}${column.primary_key ? " · PK" : ""}</span></div>`).join("")}</div></div>`).join("");
  } catch (error) { target.innerHTML = `<div class="content-card error"><p>${escapeHtml(error.message)}</p></div>`; }
}

loadHistory();
loadDatabaseSchema();
