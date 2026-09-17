import { chooseGoalView, layoutProcessGraph } from "./atlas-layout.js";
import { emptyDraftPlan, loadDraftPlan, saveDraftPlan } from "./atlas-draft-store.js";
import {
  chooseInitialGoal,
  emptyData,
  normalizePayload,
  selectAuthoritativeRun,
} from "./atlas-model.js";

(() => {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const ROLES = ["apex", "septum", "hyphae", "stem", "cap"];
  const ROLE_LABELS = {
    apex: "APEX",
    septum: "SEPTUM",
    hyphae: "HYPHAE",
    stem: "STEM",
    cap: "CAP",
    other: "OTHER",
  };

  const state = {
    data: emptyData(),
    selectedGoal: "",
    selectedGraphId: "",
    tab: "details",
    layers: {
      process: false,
      evidence: true,
      lifecycle: false,
    },
    loading: true,
    graph: {
      x: 0,
      y: 0,
      scale: 1,
      width: 1000,
      height: 720,
      positions: new Map(),
      visibleNodes: [],
      visibleEdges: [],
    },
    pan: null,
    plan: emptyDraftPlan(),
    draftRelations: [],
    toastTimer: null,
  };

  const dom = {};

  document.addEventListener("DOMContentLoaded", init);

  function init() {
    Object.assign(dom, {
      app: byId("app"),
      repoLabel: byId("repoLabel"),
      globalSearch: byId("globalSearch"),
      processSearch: byId("processSearch"),
      roleFilter: byId("roleFilter"),
      statusFilter: byId("statusFilter"),
      lifecycleFilter: byId("lifecycleFilter"),
      reloadButton: byId("reloadButton"),
      exportButton: byId("exportButton"),
      planCount: byId("planCount"),
      searchToggle: byId("searchToggle"),
      processToggle: byId("processToggle"),
      processRail: byId("processRail"),
      railClose: byId("railClose"),
      railBackdrop: byId("railBackdrop"),
      processSummary: byId("processSummary"),
      processList: byId("processList"),
      processTitle: byId("processTitle"),
      processMeta: byId("processMeta"),
      processInfo: byId("processInfo"),
      processLayerToggle: byId("processLayerToggle"),
      evidenceLayerToggle: byId("evidenceLayerToggle"),
      lifecycleLayerToggle: byId("lifecycleLayerToggle"),
      lineageWarning: byId("lineageWarning"),
      graphViewport: byId("graphViewport"),
      graphSvg: byId("graphSvg"),
      graphTransform: byId("graphTransform"),
      laneLayer: byId("laneLayer"),
      edgeLayer: byId("edgeLayer"),
      nodeLayer: byId("nodeLayer"),
      graphEmpty: byId("graphEmpty"),
      minimap: byId("minimap"),
      zoomInButton: byId("zoomInButton"),
      zoomOutButton: byId("zoomOutButton"),
      fitButton: byId("fitButton"),
      fitButtonTop: byId("fitButtonTop"),
      inspector: byId("inspector"),
      inspectorEmpty: byId("inspectorEmpty"),
      inspectorContent: byId("inspectorContent"),
      inspectorNodeId: byId("inspectorNodeId"),
      inspectorSubtitle: byId("inspectorSubtitle"),
      inspectorRoleDot: byId("inspectorRoleDot"),
      inspectorClose: byId("inspectorClose"),
      detailsPanel: byId("detailsPanel"),
      relationsPanel: byId("relationsPanel"),
      draftPanel: byId("draftPanel"),
      draftNote: byId("draftNote"),
      draftRole: byId("draftRole"),
      draftStatus: byId("draftStatus"),
      draftRelationType: byId("draftRelationType"),
      draftRelationTarget: byId("draftRelationTarget"),
      addRelationButton: byId("addRelationButton"),
      plannedRelations: byId("plannedRelations"),
      addToPlanButton: byId("addToPlanButton"),
      visibleNodeCount: byId("visibleNodeCount"),
      visibleEdgeCount: byId("visibleEdgeCount"),
      warningCount: byId("warningCount"),
      diagnosticsButton: byId("diagnosticsButton"),
      diagnosticsDialog: byId("diagnosticsDialog"),
      diagnosticsClose: byId("diagnosticsClose"),
      diagnosticsList: byId("diagnosticsList"),
      serverUrl: byId("serverUrl"),
      toast: byId("toast"),
    });

    dom.serverUrl.textContent = window.location.origin;
    bindEvents();
    renderPlanCount();
    loadGraph();
  }

  function bindEvents() {
    dom.reloadButton.addEventListener("click", () => loadGraph(true));
    dom.exportButton.addEventListener("click", exportPlan);
    dom.globalSearch.addEventListener("input", renderFilteredView);
    dom.processSearch.addEventListener("input", renderProcesses);
    dom.roleFilter.addEventListener("change", renderFilteredView);
    dom.statusFilter.addEventListener("change", renderFilteredView);
    dom.lifecycleFilter.addEventListener("change", renderFilteredView);
    dom.processToggle.addEventListener("click", () => setRailOpen(true));
    dom.searchToggle.addEventListener("click", toggleMobileSearch);
    dom.railClose.addEventListener("click", () => setRailOpen(false));
    dom.railBackdrop.addEventListener("click", () => setRailOpen(false));
    dom.processInfo.addEventListener("click", () => showToast("Process lineage is explicit. Evidence and lifecycle overlays never create or reposition process nodes."));
    dom.processLayerToggle.addEventListener("click", () => toggleLayer("process"));
    dom.evidenceLayerToggle.addEventListener("click", () => toggleLayer("evidence"));
    dom.lifecycleLayerToggle.addEventListener("click", () => toggleLayer("lifecycle"));
    dom.fitButton.addEventListener("click", (event) => { event.stopPropagation(); fitGraph(); });
    dom.fitButtonTop.addEventListener("click", (event) => { event.stopPropagation(); fitGraph(); });
    dom.zoomInButton.addEventListener("click", (event) => { event.stopPropagation(); zoomGraph(1.18); });
    dom.zoomOutButton.addEventListener("click", (event) => { event.stopPropagation(); zoomGraph(0.84); });
    dom.graphSvg.addEventListener("wheel", onGraphWheel, { passive: false });
    dom.graphSvg.addEventListener("pointerdown", onGraphPointerDown);
    dom.graphSvg.addEventListener("pointermove", onGraphPointerMove);
    dom.graphSvg.addEventListener("pointerup", endGraphPan);
    dom.graphSvg.addEventListener("pointercancel", endGraphPan);
    dom.inspectorClose.addEventListener("click", clearSelection);
    document.querySelectorAll("[role=tab]").forEach((tab) => {
      tab.addEventListener("click", () => setTab(tab.dataset.tab));
      tab.addEventListener("keydown", onTabKeydown);
    });
    dom.addRelationButton.addEventListener("click", addDraftRelation);
    dom.addToPlanButton.addEventListener("click", addCurrentDraftToPlan);
    dom.diagnosticsButton.addEventListener("click", openDiagnostics);
    dom.diagnosticsClose.addEventListener("click", () => dom.diagnosticsDialog.close());
    dom.diagnosticsDialog.addEventListener("click", (event) => {
      if (event.target === dom.diagnosticsDialog) dom.diagnosticsDialog.close();
    });
    document.addEventListener("keydown", onGlobalKeydown);
    window.addEventListener("resize", debounce(() => {
      if (state.graph.visibleNodes.length) fitGraph();
    }, 120));
  }

  async function loadGraph(isReload = false) {
    state.loading = true;
    dom.reloadButton.disabled = true;
    dom.reloadButton.setAttribute("aria-busy", "true");
    dom.processMeta.textContent = isReload ? "Refreshing source artifacts…" : "Loading artifact graph…";
    try {
      const response = await fetch("/api/v1/graph", {
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      if (!response.ok) throw new Error(`Graph request failed (${response.status})`);
      const payload = normalizePayload(await response.json());
      state.data = payload;
      state.layers.process = payload.hasAuthoritativeLineage;
      state.layers.evidence = !payload.hasAuthoritativeLineage;
      state.layers.lifecycle = false;
      state.plan = loadDraftPlan({
        repositoryId: payload.repositoryId,
        repository: payload.repository,
      });
      const selectedStillExists = payload.processes.some((process) => process.goal === state.selectedGoal);
      if (!selectedStillExists) state.selectedGoal = chooseInitialGoal(payload);
      if (state.selectedGoal) {
        const goalNodes = payload.nodes
          .filter((node) => node.goal === state.selectedGoal)
          .sort((left, right) => Number(right.lifecycle.current) - Number(left.lifecycle.current) || nodeSort(left, right));
        const view = chooseGoalView({
          goal: state.selectedGoal,
          processPreferred: state.layers.process,
          processRuns: payload.processRuns,
          processVertices: payload.processVertices,
          nodes: goalNodes,
        });
        state.layers.process = view.process;
        if (view.evidenceFallback) state.layers.evidence = true;
        state.selectedGraphId = view.graphId;
      }
      renderAll();
      if (isReload) showToast(`Reloaded ${payload.nodes.length} artifacts with ${payload.diagnostics.length} warnings.`);
    } catch (error) {
      state.data = emptyData();
      state.selectedGoal = "";
      state.selectedGraphId = "";
      renderAll();
      dom.processMeta.textContent = "The local graph endpoint could not be loaded.";
      showToast(error instanceof Error ? error.message : "Unable to load the graph.", true);
    } finally {
      state.loading = false;
      dom.reloadButton.disabled = false;
      dom.reloadButton.removeAttribute("aria-busy");
    }
  }

  function renderAll() {
    dom.repoLabel.textContent = state.data.repository || "local repository";
    dom.repoLabel.title = state.data.repository || "local repository";
    renderProcesses();
    renderGraph(true);
    renderInspector();
    renderDiagnostics();
    renderPlanCount();
    renderLayerControls();
  }

  function toggleLayer(layer) {
    if (layer === "process" && !selectedProcessRun()) return;
    state.layers[layer] = !state.layers[layer];
    if (!state.layers.process && !state.layers.evidence && !state.layers.lifecycle) {
      state.layers[layer] = true;
    }
    if (layer === "process" && state.layers.process) {
      const view = chooseGoalView({
        goal: state.selectedGoal,
        processPreferred: true,
        processRuns: state.data.processRuns,
        processVertices: state.data.processVertices,
        nodes: filteredNodesForGoal(),
      });
      state.selectedGraphId = view.graphId;
    }
    renderLayerControls();
    renderGraph(false);
    renderInspector();
  }

  function renderLayerControls() {
    const processAvailable = Boolean(selectedProcessRun());
    dom.processLayerToggle.disabled = !processAvailable;
    dom.processLayerToggle.setAttribute("aria-pressed", String(processAvailable && state.layers.process));
    dom.evidenceLayerToggle.setAttribute("aria-pressed", String(state.layers.evidence));
    dom.lifecycleLayerToggle.setAttribute("aria-pressed", String(state.layers.lifecycle));
    const legacy = state.data.schemaVersion < 2;
    const unavailable = !processAvailable;
    dom.lineageWarning.hidden = !unavailable;
    dom.lineageWarning.textContent = legacy
      ? "Lineage unavailable: schema-v1 payload. Showing persisted evidence relationships without inferred hierarchy."
      : "No sealed, current process run is available. Untracked artifacts remain visible without inferred hierarchy.";
  }

  function renderFilteredView() {
    renderProcesses();
    const visibleNodes = filteredNodesForGoal();
    if (!visibleNodes.some((node) => node.graphId === state.selectedGraphId)) {
      state.selectedGraphId = visibleNodes[0]?.graphId || "";
    }
    renderGraph(true);
    renderInspector();
  }

  function renderProcesses() {
    clear(dom.processList);
    const processQuery = dom.processSearch.value.trim().toLowerCase();
    const globalQuery = dom.globalSearch.value.trim().toLowerCase();
    const role = dom.roleFilter.value;
    const status = dom.statusFilter.value;
    const processes = state.data.processes.filter((process) => {
      if (processQuery && !process.goal.toLowerCase().includes(processQuery)) return false;
      const processNodes = state.data.nodes.filter((node) => node.goal === process.goal);
      return processNodes.some((node) => {
        if (role !== "all" && displayRole(node.role) !== role) return false;
        if (status !== "all" && node.status.toLowerCase() !== status) return false;
        if (globalQuery && !nodeSearchText(node).includes(globalQuery) && !process.goal.toLowerCase().includes(globalQuery)) return false;
        return true;
      });
    });

    dom.processSummary.textContent = `${processes.length} ${plural(processes.length, "process", "processes")}`;
    for (const process of processes) {
      const nodes = state.data.nodes.filter((node) => node.goal === process.goal);
      const currentCount = nodes.filter((node) => node.lifecycle.current).length;
      const representativeRole = representativeProcessRole(nodes);
      const button = element("button", "process-item");
      button.type = "button";
      button.setAttribute("aria-current", String(process.goal === state.selectedGoal));
      button.title = process.goal;
      button.addEventListener("click", () => selectGoal(process.goal));
      const dot = element("span", `process-dot ${roleClass(representativeRole)}`);
      const copy = element("span", "process-copy");
      copy.append(element("span", "process-name", process.goal || "Untitled process"));
      copy.append(element("span", "process-role", ROLE_LABELS[representativeRole] || "MIXED"));
      const count = element("span", "process-count", `${currentCount}/${nodes.length}`);
      button.append(dot, copy, count);
      dom.processList.append(button);
    }

    if (!processes.length) {
      dom.processList.append(element("p", "empty-copy", "No processes match the current filters."));
    }
  }

  function selectGoal(goal) {
    state.selectedGoal = goal;
    const goalNodes = filteredNodesForGoal(goal)
      .sort((left, right) => Number(right.lifecycle.current) - Number(left.lifecycle.current) || nodeSort(left, right));
    const view = chooseGoalView({
      goal,
      processPreferred: state.layers.process,
      processRuns: state.data.processRuns,
      processVertices: state.data.processVertices,
      nodes: goalNodes,
    });
    state.layers.process = view.process;
    if (view.evidenceFallback) state.layers.evidence = true;
    state.selectedGraphId = view.graphId;
    setRailOpen(false);
    renderProcesses();
    renderGraph(true);
    renderInspector();
    renderLayerControls();
  }

  function filteredNodesForGoal(goal = state.selectedGoal) {
    const query = dom.globalSearch.value.trim().toLowerCase();
    const role = dom.roleFilter.value;
    const status = dom.statusFilter.value;
    const lifecycle = dom.lifecycleFilter.value;
    return state.data.nodes
      .filter((node) => node.goal === goal)
      .filter((node) => role === "all" || displayRole(node.role) === role)
      .filter((node) => status === "all" || node.status.toLowerCase() === status)
      .filter((node) => matchesLifecycle(node, lifecycle))
      .filter((node) => !query || nodeSearchText(node).includes(query))
      .sort(nodeSort);
  }

  function renderGraph(shouldFit = false) {
    clear(dom.laneLayer);
    clear(dom.edgeLayer);
    clear(dom.nodeLayer);
    clear(dom.minimap);

    const projection = graphProjection();
    const { vertices, edges, processEdges, run } = projection;
    const layout = layoutProcessGraph({ vertices, processEdges });
    state.graph.positions = layout.positions;
    state.graph.width = layout.width;
    state.graph.height = layout.height;
    state.graph.visibleNodes = vertices;
    state.graph.visibleEdges = edges;

    dom.graphEmpty.hidden = vertices.length > 0;
    dom.visibleNodeCount.textContent = String(vertices.length);
    dom.visibleEdgeCount.textContent = String(edges.length);
    dom.warningCount.textContent = String(state.data.diagnostics.length);

    const process = state.data.processes.find((item) => item.goal === state.selectedGoal);
    dom.processTitle.textContent = state.selectedGoal || "Process graph";
    dom.processMeta.textContent = process
      ? `${vertices.length} visible ${plural(vertices.length, "vertex", "vertices")} · ${edges.length} relationships${run ? ` · sealed run ${run.runId}` : " · lineage untracked"}`
      : state.loading ? "Loading artifact graph…" : "Select a process to inspect its artifacts.";

    if (!vertices.length) {
      updateTransform();
      return;
    }

    renderLanes(layout);
    renderEdges(edges, layout.positions);
    renderNodes(vertices, layout.positions);
    renderMinimap(vertices, layout.positions, layout);
    if (shouldFit) requestAnimationFrame(fitGraph);
    else updateTransform();
  }

  function selectedProcessRun(goal = state.selectedGoal) {
    return selectAuthoritativeRun(state.data.processRuns, goal);
  }

  function graphProjection() {
    const nodes = filteredNodesForGoal();
    const visibleGraphIds = new Set(nodes.map((node) => node.graphId));
    const run = state.layers.process ? selectedProcessRun() : null;
    let vertices;
    let processEdges = [];
    let layoutIdByGraphId = new Map();

    if (run) {
      const rawVertices = state.data.processVertices
        .filter((vertex) => vertex.runId === run.runId)
        .filter((vertex) => vertex.kind === "skip" || visibleGraphIds.has(vertex.graphId));
      vertices = rawVertices.map((vertex) => {
        if (vertex.kind === "skip") {
          return {
            vertexId: vertex.vertexId,
            layoutId: vertex.vertexId,
            graphId: "",
            id: `${ROLE_LABELS[displayRole(vertex.role)] || vertex.role.toUpperCase()} skipped`,
            role: vertex.role,
            status: "skipped",
            kind: "skip",
            sequence: vertex.sequence,
            skip: vertex.skip,
          };
        }
        const node = state.data.nodeByGraphId.get(vertex.graphId);
        return node ? { ...node, vertexId: vertex.vertexId, layoutId: vertex.vertexId, kind: "node", sequence: vertex.sequence } : null;
      }).filter(Boolean);
      layoutIdByGraphId = new Map(vertices.filter((vertex) => vertex.graphId).map((vertex) => [vertex.graphId, vertex.layoutId]));
      const visibleVertexIds = new Set(vertices.map((vertex) => vertex.layoutId));
      processEdges = state.data.processEdges
        .filter((edge) => edge.runId === run.runId && edge.authoritative)
        .filter((edge) => visibleVertexIds.has(edge.source) && visibleVertexIds.has(edge.target));
    } else {
      vertices = nodes.map((node, sequence) => ({
        ...node,
        vertexId: node.graphId,
        layoutId: node.graphId,
        kind: "node",
        sequence,
      }));
      layoutIdByGraphId = new Map(vertices.map((vertex) => [vertex.graphId, vertex.layoutId]));
    }

    const overlays = state.data.edges
      .filter((edge) => (edge.type === "depends_on" && state.layers.evidence) || (edge.type === "supersedes" && state.layers.lifecycle))
      .map((edge) => ({
        ...edge,
        source: layoutIdByGraphId.get(edge.source) || "",
        target: layoutIdByGraphId.get(edge.target) || "",
      }))
      .filter((edge) => edge.source && edge.target);
    return { vertices, processEdges, edges: [...processEdges, ...overlays], run };
  }

  function renderLanes(layout) {
    layout.roles.forEach((role, index) => {
      const y = index * layout.laneHeight;
      const band = svgElement("rect", "lane-band");
      setAttributes(band, { x: 0, y, width: layout.width, height: layout.laneHeight });
      const label = svgElement("text", `lane-label ${roleClass(role)}`);
      setAttributes(label, { x: 18, y: y + 57 });
      label.textContent = ROLE_LABELS[role] || role.toUpperCase();
      const count = svgElement("text", "lane-count");
      setAttributes(count, { x: 18, y: y + 77 });
      const roleCount = (layout.byRole.get(role) || []).length;
      count.textContent = `${roleCount} ${plural(roleCount, "node", "nodes")}`;
      dom.laneLayer.append(band, label, count);
    });
  }

  function renderEdges(edges, positions) {
    const selected = new Set(
      state.graph.visibleNodes
        .filter((vertex) => vertex.graphId === state.selectedGraphId)
        .map((vertex) => vertex.layoutId),
    );
    for (const edge of edges) {
      const source = positions.get(edge.source);
      const target = positions.get(edge.target);
      if (!source || !target) continue;
      let pathData;
      if (edge.type === "process") {
        const x1 = source.x + source.width / 2;
        const y1 = source.y + source.height;
        const x2 = target.x + target.width / 2;
        const y2 = target.y;
        const curve = Math.max(34, Math.abs(y2 - y1) * 0.46);
        pathData = `M ${x1} ${y1} C ${x1} ${y1 + curve}, ${x2} ${y2 - curve}, ${x2} ${y2}`;
      } else {
        const sourceOnLeft = source.x <= target.x;
        const x1 = sourceOnLeft ? source.x + source.width : source.x;
        const y1 = source.y + source.height / 2;
        const x2 = sourceOnLeft ? target.x : target.x + target.width;
        const y2 = target.y + target.height / 2;
        const curve = Math.max(42, Math.abs(x2 - x1) * 0.42);
        const c1 = sourceOnLeft ? x1 + curve : x1 - curve;
        const c2 = sourceOnLeft ? x2 - curve : x2 + curve;
        pathData = `M ${x1} ${y1} C ${c1} ${y1}, ${c2} ${y2}, ${x2} ${y2}`;
      }
      const path = svgElement("path", `graph-edge ${edge.type}`);
      setAttributes(path, {
        d: pathData,
        "data-edge-id": edge.id,
      });
      if (selected.size) {
        const related = selected.has(edge.source) || selected.has(edge.target);
        path.classList.toggle("is-related", related);
        path.classList.toggle("is-muted", !related);
      }
      const title = svgElement("title");
      const label = edge.type === "process" ? "Process" : edge.type === "depends_on" ? "Evidence dependency" : "Lifecycle replacement";
      title.textContent = `${label}: ${edge.sourceNodeId || nodeLabel(edge.source)} → ${edge.targetNodeId || nodeLabel(edge.target)}`;
      path.append(title);
      dom.edgeLayer.append(path);
    }
  }

  function renderNodes(nodes, positions) {
    nodes.forEach((node, index) => {
      const position = positions.get(node.layoutId);
      if (!position) return;
      if (node.kind === "skip") {
        const group = svgElement("g", `graph-node is-skip ${roleClass(node.role)}`);
        group.setAttribute("aria-label", `${ROLE_LABELS[displayRole(node.role)]} skipped: ${node.skip?.code || "reason unavailable"}`);
        group.setAttribute("transform", `translate(${position.x} ${position.y})`);
        const shell = svgElement("rect", "node-shell");
        setAttributes(shell, { x: 0, y: 0, width: position.width, height: position.height, rx: 8 });
        const accent = svgElement("rect", "node-accent");
        setAttributes(accent, { x: 0, y: 0, width: 4, height: position.height, rx: 2 });
        const title = svgElement("text", "node-title");
        setAttributes(title, { x: 13, y: 27 });
        title.textContent = node.id;
        const meta = svgElement("text", "node-meta");
        setAttributes(meta, { x: 13, y: 45 });
        meta.textContent = node.skip?.code || "skip reason unavailable";
        const tooltip = svgElement("title");
        tooltip.textContent = `${node.id}\n${node.skip?.code || "reason unavailable"}\n${JSON.stringify(node.skip?.evidence || {})}`;
        group.append(shell, accent, title, meta, tooltip);
        dom.nodeLayer.append(group);
        return;
      }
      const group = svgElement("g", `graph-node ${roleClass(node.role)} lifecycle-${slug(node.lifecycle.classification)}`);
      group.setAttribute("tabindex", "0");
      group.setAttribute("role", "button");
      group.setAttribute("aria-label", `${node.id}, ${displayRole(node.role)}, ${node.status}, ${node.lifecycle.classification}`);
      group.setAttribute("transform", `translate(${position.x} ${position.y})`);
      group.dataset.graphId = node.graphId;
      group.classList.toggle("is-selected", node.graphId === state.selectedGraphId);
      group.classList.toggle("is-inactive", !node.lifecycle.current);
      if (state.selectedGraphId) {
        const selectedLayoutIds = new Set(state.graph.visibleNodes.filter((vertex) => vertex.graphId === state.selectedGraphId).map((vertex) => vertex.layoutId));
        const related = node.graphId === state.selectedGraphId || state.graph.visibleEdges.some((edge) =>
          (selectedLayoutIds.has(edge.source) && edge.target === node.layoutId) ||
          (selectedLayoutIds.has(edge.target) && edge.source === node.layoutId));
        group.classList.toggle("is-muted", !related);
      }

      const shell = svgElement("rect", "node-shell");
      setAttributes(shell, { x: 0, y: 0, width: position.width, height: position.height, rx: 8 });
      const accent = svgElement("rect", "node-accent");
      setAttributes(accent, { x: 0, y: 0, width: 4, height: position.height, rx: 2 });
      const order = svgElement("text", "node-index");
      setAttributes(order, { x: 13, y: 17 });
      order.textContent = String(index + 1).padStart(2, "0");
      const title = svgElement("text", "node-title");
      setAttributes(title, { x: 13, y: 35 });
      title.textContent = truncateMiddle(node.id, 29);
      const meta = svgElement("text", "node-meta");
      setAttributes(meta, { x: 13, y: 51 });
      meta.textContent = `${ROLE_LABELS[displayRole(node.role)] || "OTHER"} · ${node.status || "unknown"}`;
      const stateMark = svgElement("circle", "lifecycle-mark");
      setAttributes(stateMark, { cx: position.width - 13, cy: 13, r: 4 });
      const tooltip = svgElement("title");
      tooltip.textContent = `${node.id}\n${node.lifecycle.classification}\n${node.firstFact || "No fact summary"}`;
      group.append(shell, accent, order, title, meta, stateMark, tooltip);
      group.addEventListener("click", (event) => {
        event.stopPropagation();
        selectNode(node.graphId, true);
      });
      group.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          selectNode(node.graphId, true);
        }
      });
      dom.nodeLayer.append(group);
    });
  }

  function renderMinimap(nodes, positions, layout) {
    const viewWidth = 170;
    const viewHeight = 90;
    const scale = Math.min(viewWidth / layout.width, viewHeight / layout.height);
    dom.minimap.setAttribute("viewBox", `0 0 ${viewWidth} ${viewHeight}`);
    for (const node of nodes) {
      const position = positions.get(node.layoutId);
      const rect = svgElement("rect", `minimap-node ${roleClass(node.role)}`);
      setAttributes(rect, {
        x: position.x * scale,
        y: position.y * scale,
        width: Math.max(3, position.width * scale),
        height: Math.max(2, position.height * scale),
        rx: 1,
      });
      dom.minimap.append(rect);
    }
    const viewport = svgElement("rect", "minimap-viewport");
    viewport.id = "minimapViewport";
    dom.minimap.append(viewport);
    updateMinimapViewport(scale, viewWidth, viewHeight);
  }

  function updateMinimapViewport(explicitScale, viewWidth = 170, viewHeight = 90) {
    const viewport = byId("minimapViewport");
    if (!viewport || !dom.graphViewport.clientWidth) return;
    const miniScale = explicitScale || Math.min(viewWidth / state.graph.width, viewHeight / state.graph.height);
    const x = Math.max(0, -state.graph.x / state.graph.scale * miniScale);
    const y = Math.max(0, -state.graph.y / state.graph.scale * miniScale);
    const width = Math.min(viewWidth, dom.graphViewport.clientWidth / state.graph.scale * miniScale);
    const height = Math.min(viewHeight, dom.graphViewport.clientHeight / state.graph.scale * miniScale);
    setAttributes(viewport, { x, y, width, height });
  }

  function selectNode(graphId, restoreGraphFocus = false) {
    const node = state.data.nodeByGraphId.get(graphId);
    if (!node) return;
    state.selectedGraphId = graphId;
    if (node.goal !== state.selectedGoal) state.selectedGoal = node.goal;
    renderProcesses();
    renderGraph(false);
    renderInspector();
    if (restoreGraphFocus) {
      requestAnimationFrame(() => {
        const selected = [...dom.nodeLayer.querySelectorAll(".graph-node")]
          .find((element) => element.dataset.graphId === graphId);
        selected?.focus({ preventScroll: true });
      });
    }
  }

  function clearSelection() {
    state.selectedGraphId = "";
    renderGraph(false);
    renderInspector();
  }

  function renderInspector() {
    const node = selectedNode();
    dom.inspector.classList.toggle("has-selection", Boolean(node));
    dom.inspectorEmpty.hidden = Boolean(node);
    dom.inspectorContent.hidden = !node;
    if (!node) return;

    dom.inspectorNodeId.textContent = node.id;
    dom.inspectorNodeId.title = node.id;
    dom.inspectorSubtitle.textContent = `${ROLE_LABELS[displayRole(node.role)] || "OTHER"} · ${node.status || "unknown"}`;
    dom.inspectorRoleDot.className = `role-dot ${roleClass(node.role)}`;
    ROLES.concat("other").forEach((role) => dom.inspectorContent.classList.remove(`role-${role}`));
    dom.inspectorContent.classList.add(roleClass(node.role));
    renderDetails(node);
    renderRelations(node);
    renderDraft(node);
    setTab(state.tab);
  }

  function renderDetails(node) {
    clear(dom.detailsPanel);
    dom.detailsPanel.append(
      detailsSection("Lifecycle", lifecycleContent(node), node.lifecycle.classification),
      detailsSection("Facts", factsContent(node), node.facts.length),
      detailsSection("Evidence", paragraphOrEmpty(node.evidence)),
      detailsSection("Identity", identityContent(node)),
      detailsSection("Topics", topicsContent(node)),
      detailsSection("Questions", listOrEmpty(node.questions)),
      detailsSection("Next", listOrEmpty(node.next)),
      detailsSection("Trace", paragraphOrEmpty(node.trace)),
      detailsSection("Reflection", paragraphOrEmpty(node.reflection)),
      detailsSection("Source refs", sourceRefsContent(node), node.sourceRefs.length),
      detailsSection("Flow history", flowHistoryContent(node), node.flowHistory.length),
    );
  }

  function lifecycleContent(node) {
    const wrap = element("div", "lifecycle-list");
    const entries = [
      ["Parsed", node.lifecycle.parsed],
      ["Flow committed", node.lifecycle.flowCommitted],
      ["Source present", node.lifecycle.sourcePresent],
      ["Source current", node.lifecycle.sourceCurrent],
      ["Eligible current", node.lifecycle.eligibleCurrent],
      ["Superseded", node.lifecycle.superseded],
      ["Search visible", node.lifecycle.searchVisible],
      ["CAP eligible", node.lifecycle.capEligible],
    ];
    for (const [label, value] of entries) {
      const row = element("div", `lifecycle-row${value ? " is-true" : ""}`);
      row.append(element("span", "boolean-mark", value ? "✓" : "—"), element("span", "", label));
      wrap.append(row);
    }
    return wrap;
  }

  function factsContent(node) {
    if (!node.facts.length) return element("p", "empty-copy", "No facts recorded.");
    const list = element("ul", "fact-list");
    for (const fact of node.facts) {
      const item = element("li", "", fact.text);
      if (fact.line) item.title = `Artifact line ${fact.line}`;
      list.append(item);
    }
    return list;
  }

  function identityContent(node) {
    const list = element("dl", "detail-grid");
    const entries = [
      ["Role", node.rawRole || node.role || "none"],
      ["Status", node.status || "none"],
      ["Confidence", node.confidence || "none"],
      ["Version", node.version || "none"],
      ["Goal", node.goal || "none"],
      ["Parent goal", node.parentGoalId || "none"],
      ["Producing agent", node.producingAgent || "none"],
      ["Updated", formatTimestamp(node.updated)],
      ["Artifact", node.artifactPath || "none"],
    ];
    for (const [term, value] of entries) {
      const pair = element("div", "detail-pair");
      pair.append(element("dt", "", term), element("dd", term === "Artifact" ? "path-value" : "", String(value)));
      list.append(pair);
    }
    return list;
  }

  function topicsContent(node) {
    if (!node.topics.length) return element("p", "empty-copy", "No topics recorded.");
    const list = element("div", "topic-list");
    node.topics.forEach((topic) => list.append(element("span", "topic-token", topic)));
    return list;
  }

  function sourceRefsContent(node) {
    if (!node.sourceRefs.length) return element("p", "empty-copy", "No source references recorded.");
    const list = element("ul", "source-list");
    node.sourceRefs.forEach((reference) => {
      const item = element("li", `source-reference${reference.current ? " is-current" : ""}`);
      item.title = reference.reason || (reference.current ? "Reference is current" : "Reference is not current");
      item.append(element("span", "source-state"), element("code", "", reference.reference));
      list.append(item);
    });
    return list;
  }

  function flowHistoryContent(node) {
    if (!node.flowHistory.length) return element("p", "empty-copy", "No matching flow snapshot found.");
    const list = element("ul", "flow-list");
    node.flowHistory.forEach((record) => {
      const item = element("li", "flow-record");
      const path = `${record.path || "flow"}${record.line ? `:${record.line}` : ""}`;
      item.append(element("code", "", path), element("span", "", `${record.status || "unknown"} · ${formatTimestamp(record.timestamp)}`));
      list.append(item);
    });
    return list;
  }

  function renderRelations(node) {
    clear(dom.relationsPanel);
    const run = selectedProcessRun(node.goal);
    const processVertex = run && state.data.processVertices.find((vertex) => vertex.runId === run.runId && vertex.graphId === node.graphId);
    const processConnected = processVertex
      ? state.data.processEdges.filter((edge) => edge.runId === run.runId && edge.authoritative && (edge.source === processVertex.vertexId || edge.target === processVertex.vertexId))
      : [];
    const processUpstream = [];
    const processDownstream = [];
    for (const edge of processConnected) {
      const outgoing = edge.source === processVertex.vertexId;
      const otherVertexId = outgoing ? edge.target : edge.source;
      const otherVertex = state.data.processVertices.find((vertex) => vertex.vertexId === otherVertexId);
      const otherNode = otherVertex ? state.data.nodeByGraphId.get(otherVertex.graphId) : null;
      const skippedLabel = otherVertex?.kind === "skip"
        ? `${ROLE_LABELS[displayRole(otherVertex.role)]} skipped (${otherVertex.skip?.code || "reason unavailable"})`
        : "";
      const relation = { graphId: otherVertex?.graphId || "", label: otherNode?.id || skippedLabel || otherVertex?.nodeId || otherVertexId };
      (outgoing ? processDownstream : processUpstream).push(relation);
    }
    dom.relationsPanel.append(
      detailsSection("Process upstream", processRelationList(processUpstream, "upstream"), processUpstream.length),
      detailsSection("Process downstream", processRelationList(processDownstream, "downstream"), processDownstream.length),
    );

    const evidence = state.data.edges.filter((edge) => edge.type === "depends_on" && (edge.source === node.graphId || edge.target === node.graphId));
    const lifecycle = state.data.edges.filter((edge) => edge.type === "supersedes" && (edge.source === node.graphId || edge.target === node.graphId));
    dom.relationsPanel.append(
      detailsSection("Evidence dependencies", artifactRelationList(evidence, node, "evidence"), evidence.length),
      detailsSection("Lifecycle replacements", artifactRelationList(lifecycle, node, "lifecycle"), lifecycle.length),
    );

    const raw = element("dl", "detail-grid");
    [
      ["Depends on", joinOrNone(node.dependsOn)],
      ["Supersedes", joinOrNone(node.supersedes)],
      ["Consumes", node.consumes || "none"],
      ["Blocks", node.blocks || "none"],
    ].forEach(([term, value]) => {
      const pair = element("div", "detail-pair");
      pair.append(element("dt", "", term), element("dd", "", value));
      raw.append(pair);
    });
    dom.relationsPanel.append(detailsSection("Raw fields", raw));

    const missing = state.data.diagnostics.filter((item) => item.nodeId === node.id || item.graphId === node.graphId);
    if (missing.length) {
      const list = element("ul", "plain-list");
      missing.forEach((item) => list.append(element("li", "plain-text", item.message)));
      dom.relationsPanel.append(detailsSection("Node diagnostics", list, missing.length));
    }
  }

  function processRelationList(relations, direction) {
    if (!relations.length) return element("p", "empty-copy", "None recorded in the sealed run.");
    const list = element("div", "relation-list");
    for (const relation of relations) {
      const row = element(relation.graphId ? "button" : "div", "relation-row");
      if (relation.graphId) {
        row.type = "button";
        row.addEventListener("click", () => selectNode(relation.graphId));
      }
      row.append(
        element("span", "relation-type", "process"),
        element("span", "relation-node", relation.label),
        element("span", "relation-direction", direction),
      );
      list.append(row);
    }
    return list;
  }

  function artifactRelationList(edges, node, layer) {
    if (!edges.length) return element("p", "empty-copy", `No ${layer} relationships recorded.`);
    const list = element("div", "relation-list");
    for (const edge of edges) {
      const outgoing = edge.source === node.graphId;
      const otherGraphId = outgoing ? edge.target : edge.source;
      const otherNode = state.data.nodeByGraphId.get(otherGraphId);
      const row = element("button", "relation-row");
      row.type = "button";
      row.addEventListener("click", () => selectNode(otherGraphId));
      row.append(
        element("span", "relation-type", layer),
        element("span", "relation-node", otherNode ? otherNode.id : otherGraphId),
        element("span", "relation-direction", outgoing ? "downstream" : "upstream"),
        element("span", "relation-declaration", `stored: ${edge.rawDirection || "not recorded"} · declared by ${edge.declaringNode || "unknown"}`),
      );
      list.append(row);
    }
    return list;
  }

  function renderDraft(node) {
    const existing = state.plan.proposals.find((proposal) => proposal.source.graphId === node.graphId);
    dom.draftNote.value = existing?.note || "";
    dom.draftRole.value = existing?.proposed.role || "";
    dom.draftStatus.value = existing?.proposed.status || "";
    state.draftRelations = existing ? existing.proposed.relations.map((relation) => ({ ...relation })) : [];
    dom.addToPlanButton.querySelector("span").textContent = existing ? "Update plan" : "Add to plan";
    renderDraftTargets(node);
    renderPlannedRelations();
  }

  function renderDraftTargets(node) {
    clear(dom.draftRelationTarget);
    const candidates = state.data.nodes.filter((candidate) => candidate.graphId !== node.graphId).sort(nodeSort);
    if (!candidates.length) {
      const option = element("option", "", "No other artifacts available");
      option.value = "";
      dom.draftRelationTarget.append(option);
      dom.addRelationButton.disabled = true;
      return;
    }
    dom.addRelationButton.disabled = false;
    candidates.forEach((candidate) => {
      const option = element("option", "", `${candidate.id} · ${shortGoal(candidate.goal)}`);
      option.value = candidate.graphId;
      dom.draftRelationTarget.append(option);
    });
  }

  function addDraftRelation() {
    const targetGraphId = dom.draftRelationTarget.value;
    if (!targetGraphId) return;
    const type = dom.draftRelationType.value;
    if (state.draftRelations.some((relation) => relation.type === type && relation.targetGraphId === targetGraphId)) {
      showToast("That relation is already in this draft.");
      return;
    }
    const target = state.data.nodeByGraphId.get(targetGraphId);
    state.draftRelations.push({ type, targetGraphId, targetNodeId: target?.id || targetGraphId });
    renderPlannedRelations();
  }

  function renderPlannedRelations() {
    clear(dom.plannedRelations);
    state.draftRelations.forEach((relation, index) => {
      const row = element("div", "planned-relation");
      row.append(
        element("span", "relation-type", relation.type === "depends_on" ? "depends" : "supersedes"),
        element("code", "", relation.targetNodeId),
      );
      const remove = element("button", "", "×");
      remove.type = "button";
      remove.setAttribute("aria-label", `Remove relation to ${relation.targetNodeId}`);
      remove.addEventListener("click", () => {
        state.draftRelations.splice(index, 1);
        renderPlannedRelations();
      });
      row.append(remove);
      dom.plannedRelations.append(row);
    });
  }

  function addCurrentDraftToPlan() {
    const node = selectedNode();
    if (!node) return;
    const note = dom.draftNote.value.trim();
    const role = dom.draftRole.value;
    const status = dom.draftStatus.value;
    if (!note && !role && !status && !state.draftRelations.length) {
      showToast("Add a note or proposed change before saving this draft.");
      dom.draftNote.focus();
      return;
    }
    const proposal = {
      source: {
        graphId: node.graphId,
        nodeId: node.id,
        goal: node.goal,
        artifactPath: node.artifactPath,
      },
      current: {
        role: node.rawRole || node.role,
        status: node.status,
        dependsOn: [...node.dependsOn],
        supersedes: [...node.supersedes],
      },
      proposed: {
        role: role || null,
        status: status || null,
        relations: state.draftRelations.map((relation) => ({ ...relation })),
      },
      note,
      updatedAt: new Date().toISOString(),
    };
    const existingIndex = state.plan.proposals.findIndex((item) => item.source.graphId === node.graphId);
    if (existingIndex >= 0) state.plan.proposals.splice(existingIndex, 1, proposal);
    else state.plan.proposals.push(proposal);
    savePlan();
    renderPlanCount();
    dom.addToPlanButton.querySelector("span").textContent = "Update plan";
    showToast(existingIndex >= 0 ? "Draft updated in the process plan." : "Draft added to the process plan.");
  }

  function renderPlanCount() {
    const count = state.plan.proposals.length;
    dom.planCount.textContent = String(count);
    dom.planCount.hidden = count === 0;
    dom.exportButton.disabled = count === 0;
    dom.exportButton.title = count ? `Export ${count} ${plural(count, "proposal", "proposals")}` : "Add a draft before exporting";
  }

  function exportPlan() {
    if (!state.plan.proposals.length) return;
    const exported = {
      schemaVersion: 1,
      kind: "mycelium-process-adjustment-plan",
      repository: state.data.repository,
      exportedAt: new Date().toISOString(),
      readOnlySource: true,
      proposals: state.plan.proposals,
    };
    const text = `${JSON.stringify(exported, null, 2)}\n`;
    const blob = new Blob([text], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    const stamp = exported.exportedAt.replace(/[-:]/g, "").replace(/\..+/, "").replace("T", "-");
    link.href = url;
    link.download = `mycelium-process-plan-${stamp}.json`;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    showToast(`Exported ${state.plan.proposals.length} ${plural(state.plan.proposals.length, "proposal", "proposals")} as JSON.`);
  }

  function setTab(name) {
    state.tab = ["details", "relations", "draft"].includes(name) ? name : "details";
    document.querySelectorAll("[role=tab]").forEach((tab) => {
      const selected = tab.dataset.tab === state.tab;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
    });
    dom.detailsPanel.hidden = state.tab !== "details";
    dom.relationsPanel.hidden = state.tab !== "relations";
    dom.draftPanel.hidden = state.tab !== "draft";
  }

  function onTabKeydown(event) {
    if (!event.key.startsWith("Arrow")) return;
    const tabs = [...document.querySelectorAll("[role=tab]")];
    const current = tabs.indexOf(event.currentTarget);
    const direction = event.key === "ArrowRight" || event.key === "ArrowDown" ? 1 : -1;
    const next = tabs[(current + direction + tabs.length) % tabs.length];
    event.preventDefault();
    setTab(next.dataset.tab);
    next.focus();
  }

  function renderDiagnostics() {
    clear(dom.diagnosticsList);
    if (!state.data.diagnostics.length) {
      dom.diagnosticsList.append(element("p", "empty-copy", "No artifact diagnostics were reported."));
      return;
    }
    state.data.diagnostics.forEach((diagnostic) => {
      const item = element("article", "diagnostic-item");
      const copy = element("div");
      copy.append(
        element("div", "diagnostic-kind", diagnostic.kind || diagnostic.severity || "warning"),
        element("p", "diagnostic-message", diagnostic.message || "Artifact warning"),
      );
      if (diagnostic.path) {
        copy.append(element("code", "diagnostic-path", `${diagnostic.path}${diagnostic.line ? `:${diagnostic.line}` : ""}`));
      }
      item.append(element("span", "diagnostic-icon"), copy);
      dom.diagnosticsList.append(item);
    });
  }

  function openDiagnostics() {
    renderDiagnostics();
    if (typeof dom.diagnosticsDialog.showModal === "function") dom.diagnosticsDialog.showModal();
    else dom.diagnosticsDialog.setAttribute("open", "");
  }

  function onGraphPointerDown(event) {
    if (event.button !== 0 || event.target.closest(".graph-node")) return;
    state.pan = { pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, x: state.graph.x, y: state.graph.y };
    dom.graphSvg.setPointerCapture(event.pointerId);
    dom.graphSvg.classList.add("grabbing");
  }

  function onGraphPointerMove(event) {
    if (!state.pan || state.pan.pointerId !== event.pointerId) return;
    state.graph.x = state.pan.x + event.clientX - state.pan.startX;
    state.graph.y = state.pan.y + event.clientY - state.pan.startY;
    updateTransform();
  }

  function endGraphPan(event) {
    if (!state.pan || state.pan.pointerId !== event.pointerId) return;
    state.pan = null;
    dom.graphSvg.classList.remove("grabbing");
    if (dom.graphSvg.hasPointerCapture(event.pointerId)) dom.graphSvg.releasePointerCapture(event.pointerId);
  }

  function onGraphWheel(event) {
    event.preventDefault();
    const factor = event.deltaY < 0 ? 1.1 : 0.9;
    const rect = dom.graphSvg.getBoundingClientRect();
    zoomGraph(factor, event.clientX - rect.left, event.clientY - rect.top);
  }

  function zoomGraph(factor, centerX, centerY) {
    if (!state.graph.visibleNodes.length) return;
    const oldScale = state.graph.scale;
    const nextScale = clamp(oldScale * factor, 0.22, 2.6);
    const x = Number.isFinite(centerX) ? centerX : dom.graphViewport.clientWidth / 2;
    const y = Number.isFinite(centerY) ? centerY : dom.graphViewport.clientHeight / 2;
    const graphX = (x - state.graph.x) / oldScale;
    const graphY = (y - state.graph.y) / oldScale;
    state.graph.scale = nextScale;
    state.graph.x = x - graphX * nextScale;
    state.graph.y = y - graphY * nextScale;
    updateTransform();
  }

  function fitGraph() {
    if (!state.graph.visibleNodes.length || !dom.graphViewport.clientWidth) return;
    const width = dom.graphViewport.clientWidth;
    const height = dom.graphViewport.clientHeight;
    const bottomReserve = width <= 760 ? 68 : 116;
    const usableHeight = Math.max(180, height - bottomReserve);
    const scale = clamp(Math.min((width - 32) / state.graph.width, (usableHeight - 24) / state.graph.height), 0.24, 1.18);
    state.graph.scale = scale;
    state.graph.x = Math.max(12, (width - state.graph.width * scale) / 2);
    state.graph.y = width <= 760 ? 12 : Math.max(10, (usableHeight - state.graph.height * scale) / 2);
    updateTransform();
  }

  function updateTransform() {
    dom.graphTransform.setAttribute("transform", `translate(${state.graph.x} ${state.graph.y}) scale(${state.graph.scale})`);
    updateMinimapViewport();
  }

  function setRailOpen(open) {
    dom.processRail.classList.toggle("is-open", open);
    dom.processToggle.setAttribute("aria-expanded", String(open));
    dom.railBackdrop.hidden = !open;
  }

  function toggleMobileSearch() {
    const open = !dom.globalSearch.closest(".search-box").classList.contains("is-open");
    dom.globalSearch.closest(".search-box").classList.toggle("is-open", open);
    dom.searchToggle.setAttribute("aria-expanded", String(open));
    if (open) dom.globalSearch.focus();
  }

  function onGlobalKeydown(event) {
    const target = event.target;
    const isField = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement;
    if (event.key === "/" && !isField) {
      event.preventDefault();
      dom.globalSearch.focus();
    }
    if (event.key === "Escape") {
      setRailOpen(false);
      dom.globalSearch.closest(".search-box").classList.remove("is-open");
      dom.searchToggle.setAttribute("aria-expanded", "false");
      if (dom.diagnosticsDialog.open) dom.diagnosticsDialog.close();
      if (window.innerWidth <= 760 && state.selectedGraphId) clearSelection();
    }
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && state.tab === "draft") addCurrentDraftToPlan();
  }

  function selectedNode() {
    return state.data.nodeByGraphId.get(state.selectedGraphId) || null;
  }

  function matchesLifecycle(node, filter) {
    if (filter === "all") return true;
    if (filter === "current") return node.lifecycle.current;
    if (filter === "attention") return !node.lifecycle.current;
    return node.lifecycle.classification === filter;
  }

  function nodeSearchText(node) {
    return [
      node.id,
      node.goal,
      node.role,
      node.rawRole,
      node.status,
      node.confidence,
      node.topics.join(" "),
      node.facts.map((fact) => fact.text).join(" "),
      node.evidence,
      node.questions.join(" "),
      node.next.join(" "),
      node.trace,
      node.reflection,
      node.consumes,
      node.blocks,
      node.parentGoalId,
      node.producingAgent,
      node.sourceRefs.map((reference) => reference.reference).join(" "),
      node.artifactPath,
    ].join(" ").toLowerCase();
  }

  function nodeSort(a, b) {
    const roleDifference = roleOrder(a.role) - roleOrder(b.role);
    if (roleDifference) return roleDifference;
    return a.updated.localeCompare(b.updated) || a.id.localeCompare(b.id) || a.graphId.localeCompare(b.graphId);
  }

  function representativeProcessRole(nodes) {
    for (const role of ["hyphae", "stem", "cap", "septum", "apex"]) {
      if (nodes.some((node) => displayRole(node.role) === role)) return role;
    }
    return "other";
  }

  function detailsSection(title, content, count) {
    const section = element("section", "detail-section");
    const heading = element("div", "section-heading");
    heading.append(element("h3", "", title));
    if (count !== undefined && count !== null && count !== "") {
      const countText = typeof count === "number" ? String(count) : String(count).replace(/-/g, " ");
      heading.append(element("span", title === "Lifecycle" ? `lifecycle-badge lifecycle-${slug(String(count))}` : "section-count", countText));
    }
    section.append(heading, content);
    return section;
  }

  function listOrEmpty(values) {
    if (!values.length) return element("p", "empty-copy", "None recorded.");
    const list = element("ul", "plain-list");
    values.forEach((value) => list.append(element("li", "plain-text", value)));
    return list;
  }

  function paragraphOrEmpty(value) {
    return value && value.toLowerCase() !== "none"
      ? element("p", "plain-text", value)
      : element("p", "empty-copy", "None recorded.");
  }

  function savePlan() {
    try {
      saveDraftPlan({
        plan: state.plan,
        repositoryId: state.data.repositoryId,
        repository: state.data.repository,
      });
    } catch (_error) {
      showToast("The browser could not persist this draft. Export it before closing Atlas.", true);
    }
  }

  function showToast(message, isError = false) {
    clearTimeout(state.toastTimer);
    dom.toast.textContent = message;
    dom.toast.hidden = false;
    dom.toast.dataset.kind = isError ? "error" : "status";
    state.toastTimer = window.setTimeout(() => { dom.toast.hidden = true; }, isError ? 5200 : 3200);
  }

  function byId(id) {
    return document.getElementById(id);
  }

  function element(tag, className = "", text = "") {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== "") node.textContent = text;
    return node;
  }

  function svgElement(tag, className = "") {
    const node = document.createElementNS(SVG_NS, tag);
    if (className) node.setAttribute("class", className);
    return node;
  }

  function setAttributes(node, attributes) {
    for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
  }

  function clear(node) {
    node.replaceChildren();
  }

  function displayRole(role) {
    const normalized = String(role || "").toLowerCase();
    return ROLES.includes(normalized) ? normalized : "other";
  }

  function roleClass(role) {
    return `role-${displayRole(role)}`;
  }

  function roleOrder(role) {
    const index = ROLES.indexOf(displayRole(role));
    return index < 0 ? ROLES.length : index;
  }

  function nodeLabel(graphId) {
    return state.data.nodeByGraphId.get(graphId)?.id
      || state.graph.visibleNodes.find((vertex) => vertex.layoutId === graphId)?.id
      || graphId;
  }

  function joinOrNone(values) {
    return values.length ? values.join(", ") : "none";
  }

  function formatTimestamp(value) {
    if (!value) return "none";
    const date = new Date(value);
    if (Number.isNaN(date.valueOf())) return value;
    return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
  }

  function shortGoal(goal) {
    return goal.length > 34 ? `${goal.slice(0, 31)}…` : goal;
  }

  function truncateMiddle(value, maxLength) {
    if (value.length <= maxLength) return value;
    const head = Math.ceil((maxLength - 1) * 0.62);
    const tail = maxLength - 1 - head;
    return `${value.slice(0, head)}…${value.slice(-tail)}`;
  }

  function slug(value) {
    return String(value || "unknown").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  }

  function plural(count, singular, pluralForm) {
    return count === 1 ? singular : pluralForm;
  }

  function clamp(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, value));
  }

  function debounce(callback, delay) {
    let timer;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => callback(...args), delay);
    };
  }
})();
