"use strict";

const state = {
  data: null,
  selectedIndex: 0,
  map: null,
  layers: [],
  manualMarker: null,
  selectedBulk: new Set(),
};

const $ = (id) => document.getElementById(id);

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function valueText(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

function appendOptions(select, values) {
  const existing = select.value;
  while (select.options.length > 1) select.remove(1);
  values.forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    select.appendChild(option);
  });
  select.value = existing;
}

function tableFor(fields) {
  const table = element("table", null, "field-table");
  const body = document.createElement("tbody");
  Object.entries(fields || {}).forEach(([name, value]) => {
    const row = document.createElement("tr");
    row.appendChild(element("th", name));
    const cell = element("td", valueText(value));
    row.appendChild(cell);
    body.appendChild(row);
  });
  table.appendChild(body);
  return table;
}

function summaryCard(value, label) {
  const card = element("div", null, "summary-card");
  card.appendChild(element("strong", value));
  card.appendChild(element("span", label));
  return card;
}

function renderSummary() {
  const summary = state.data.summary;
  const target = $("summary");
  clear(target);
  [
    [summary.human_review_listings, "Review listings"],
    [`${summary.completed_listings}/${summary.human_review_listings}`, "Listings completed"],
    [`${summary.completed_issues}/${summary.total_issues}`, "Issues completed"],
    [summary.geocoding_review_issues, "Geocode issues"],
    [summary.ai_review_issues, "AI issues"],
    [summary.pending_drafts, "Drafts"],
    [summary.accepted_unknowns, "Accepted unknowns"],
    [summary.recommended_next_action, "Operator next action"],
  ].forEach(([value, label]) => target.appendChild(summaryCard(value, label)));
  $("progress").textContent = `Resolved ${summary.completed_listings} of ${summary.human_review_listings} listings`;
  $("run-label").textContent = `Run ${state.data.run_id} · canonical ${state.data.canonical_sha256.slice(0, 12)}…`;
  $("mode-badge").textContent = state.data.read_only ? "Read only" : "Draft writes enabled";
  $("mode-badge").className = `badge ${state.data.read_only ? "warn" : "good"}`;
}

function renderFilters() {
  appendOptions($("category"), state.data.filters.categories);
  appendOptions($("field"), state.data.filters.fields);
  appendOptions($("reason"), state.data.filters.reasons);
  appendOptions($("decision-state"), state.data.filters.states);
}

function renderQueue() {
  const queue = $("queue");
  clear(queue);
  const listings = state.data.listings;
  if (state.selectedIndex >= listings.length) state.selectedIndex = Math.max(0, listings.length - 1);
  listings.forEach((listing, index) => {
    const item = document.createElement("li");
    const button = element("button");
    if (index === state.selectedIndex) button.classList.add("active");
    button.appendChild(element("strong", listing.address || listing.title || listing.listing_id));
    button.appendChild(element("small", `${listing.listing_id} · ${listing.issues.length} issue(s) · ${listing.review_status}`));
    button.addEventListener("click", () => {
      state.selectedIndex = index;
      renderQueue();
      renderSelected();
    });
    item.appendChild(button);
    queue.appendChild(item);
  });
  $("position").textContent = listings.length ? `${state.selectedIndex + 1} / ${listings.length}` : "0 / 0";
  $("previous").disabled = state.selectedIndex <= 0;
  $("next").disabled = state.selectedIndex >= listings.length - 1;
}

function section(title, content) {
  const card = element("section", null, "section-card");
  card.appendChild(element("h3", title));
  card.appendChild(content);
  return card;
}

function linkButton(label, href) {
  const link = element("a", label, "secondary");
  link.href = href;
  link.target = "_blank";
  link.rel = "noreferrer";
  return link;
}

function destroyMap() {
  if (state.map) state.map.remove();
  state.map = null;
  state.layers = [];
  state.manualMarker = null;
}

function renderMap(listing) {
  destroyMap();
  if (!window.L) {
    $("review-map").textContent = "Leaflet could not load. Coordinates and external map links remain available.";
    return;
  }
  const western = [state.data.western.latitude, state.data.western.longitude];
  state.map = window.L.map("review-map").setView(western, 12);
  window.L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "© OpenStreetMap contributors",
  }).addTo(state.map);
  window.L.marker(western).addTo(state.map).bindPopup("Western University reference");
  const cfg = state.data.configuration;
  window.L.rectangle([
    [cfg.london_min_latitude, cfg.london_min_longitude],
    [cfg.london_max_latitude, cfg.london_max_longitude],
  ], { color: "#4f2683", weight: 1, fillOpacity: 0.03 }).addTo(state.map);
  const geo = listing.geocoding;
  if (geo.latitude !== null && geo.longitude !== null) {
    const marker = window.L.marker([geo.latitude, geo.longitude]).addTo(state.map);
    const popup = element("div");
    popup.appendChild(element("strong", listing.address || listing.listing_id));
    popup.appendChild(element("p", `Confidence ${valueText(geo.confidence)}`));
    marker.bindPopup(popup);
    state.map.setView([geo.latitude, geo.longitude], 15);
  }
  state.map.on("click", (event) => {
    if (state.manualMarker) state.manualMarker.remove();
    state.manualMarker = window.L.marker(event.latlng, { draggable: true }).addTo(state.map);
    const updateInputs = (latlng) => {
      document.querySelectorAll("input[name='latitude']").forEach((input) => { input.value = latlng.lat.toFixed(6); });
      document.querySelectorAll("input[name='longitude']").forEach((input) => { input.value = latlng.lng.toFixed(6); });
    };
    updateInputs(event.latlng);
    state.manualMarker.on("dragend", () => updateInputs(state.manualMarker.getLatLng()));
  });
}

function mapSection(listing) {
  const wrap = element("div", null, "map-grid");
  const map = element("div");
  map.id = "review-map";
  wrap.appendChild(map);
  const data = element("div", null, "map-data");
  data.appendChild(tableFor(listing.geocoding));
  const actions = element("div", null, "map-actions");
  const geo = listing.geocoding;
  if (geo.latitude !== null && geo.longitude !== null) {
    actions.appendChild(linkButton("Google Maps", `https://www.google.com/maps?q=${geo.latitude},${geo.longitude}`));
    actions.appendChild(linkButton("OpenStreetMap", `https://www.openstreetmap.org/?mlat=${geo.latitude}&mlon=${geo.longitude}#map=17/${geo.latitude}/${geo.longitude}`));
    const copy = element("button", "Copy coordinates", "secondary");
    copy.type = "button";
    copy.addEventListener("click", () => navigator.clipboard?.writeText(`${geo.latitude}, ${geo.longitude}`));
    actions.appendChild(copy);
    const zoom = element("button", "Zoom to listing", "secondary");
    zoom.type = "button";
    zoom.addEventListener("click", () => state.map?.setView([geo.latitude, geo.longitude], 17));
    actions.appendChild(zoom);
  }
  data.appendChild(actions);
  data.appendChild(element("p", "Click the map to preview a manual coordinate marker; nothing is saved until a decision form is submitted.", "muted"));
  wrap.appendChild(data);
  return wrap;
}

function evidenceBox(label, value) {
  const box = element("div");
  box.appendChild(element("strong", label));
  box.appendChild(element("pre", valueText(value)));
  return box;
}

function inputField(name, label, value = "", type = "text") {
  const wrapper = element("label", label);
  const input = document.createElement("input");
  input.name = name;
  input.type = type;
  if (type === "number") input.step = "any";
  input.value = value ?? "";
  wrapper.appendChild(input);
  return wrapper;
}

function selectField(name, label, values, current = "") {
  const wrapper = element("label", label);
  const select = document.createElement("select");
  select.name = name;
  values.forEach(([value, text]) => {
    const option = element("option", text);
    option.value = value;
    option.selected = value === current;
    select.appendChild(option);
  });
  wrapper.appendChild(select);
  return wrapper;
}

function renderDynamicFields(container, action, issue, listing) {
  clear(container);
  if (action === "correct_value") {
    if (issue.field === "is_sublet") {
      container.appendChild(selectField("selected_value", "Sublet status", [["true", "Explicitly true"], ["false", "Explicitly false"], ["", "Unknown"]]));
    } else {
      container.appendChild(inputField("selected_value", "Corrected value", valueText(issue.original_value) === "—" ? "" : issue.original_value));
    }
  } else if (action === "accepted_as_unknown") {
    const choices = issue.field === "map_ready"
      ? [["coordinates_unknown", "Coordinates unknown"], ["address_unknown", "Address unknown"]]
      : issue.field === "price_monthly"
        ? [["period_ambiguous", "Period ambiguous"], ["genuinely_missing", "Price genuinely missing"]]
        : [["accepted_unknown", "Value unknown"]];
    container.appendChild(selectField("unknown_kind", "Unknown classification", choices));
  } else if (action === "correct_geocode") {
    container.appendChild(inputField("latitude", "Latitude", listing.geocoding.latitude, "number"));
    container.appendChild(inputField("longitude", "Longitude", listing.geocoding.longitude, "number"));
    container.appendChild(inputField("out_of_bounds_reason", "Out-of-bounds override reason"));
  } else if (action === "correct_address") {
    container.appendChild(inputField("corrected_address", "Corrected address", listing.address));
    container.appendChild(inputField("latitude", "Validated latitude (optional)", listing.geocoding.latitude, "number"));
    container.appendChild(inputField("longitude", "Validated longitude (optional)", listing.geocoding.longitude, "number"));
    const check = element("label", "Coordinates explicitly validated");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.name = "coordinates_validated";
    check.prepend(input);
    container.appendChild(check);
    container.appendChild(inputField("out_of_bounds_reason", "Out-of-bounds override reason"));
  } else if (action === "price_correction") {
    ["price_text", "price_numeric", "price_period", "price_monthly"].forEach((name) => {
      container.appendChild(inputField(name, name, listing.pricing[name]));
    });
  }
}

function formPayload(form, issue, listing) {
  const values = new FormData(form);
  const action = values.get("action");
  let status = "human_review_required";
  if (["accept_current", "correct_value", "accept_current_geocode", "correct_geocode", "correct_address", "price_correction"].includes(action)) status = "human_approved";
  if (action === "accepted_as_unknown") status = "accepted_as_unknown";
  if (action === "exclude") status = "excluded";
  let selectedValue = values.get("selected_value");
  if (issue.field === "is_sublet" && selectedValue !== null) {
    selectedValue = selectedValue === "true" ? true : selectedValue === "false" ? false : null;
  }
  const payload = {
    base_decision_id: issue.base_decision_id,
    listing_id: listing.listing_id,
    input_fingerprint: issue.input_fingerprint,
    reviewer_name: $("reviewer").value.trim(),
    status,
    action,
    selected_value: selectedValue,
    review_note: values.get("review_note") || "",
    supporting_text: values.get("supporting_text") ? [values.get("supporting_text")] : [],
    unknown_kind: values.get("unknown_kind"),
    corrected_address: values.get("corrected_address"),
    latitude: values.get("latitude"),
    longitude: values.get("longitude"),
    coordinates_validated: values.get("coordinates_validated") === "on",
    out_of_bounds_reason: values.get("out_of_bounds_reason"),
  };
  if (action === "price_correction") {
    payload.price_fields = {};
    ["price_text", "price_numeric", "price_period", "price_monthly"].forEach((name) => {
      payload.price_fields[name] = values.get(name);
    });
  }
  if (issue.human_decision?.decision_id) payload.supersedes_decision_id = issue.human_decision.decision_id;
  return payload;
}

async function submitDecision(form, issue, listing) {
  const status = form.querySelector(".form-status");
  status.className = "form-status";
  status.textContent = "Saving…";
  try {
    const response = await fetch("/api/decisions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decisions: [formPayload(form, issue, listing)] }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Decision save failed");
    status.classList.add("success");
    status.textContent = result.idempotent ? "Identical decision already saved." : "Draft saved. It has not been applied.";
    await loadState(false);
  } catch (error) {
    status.classList.add("error");
    status.textContent = error.message;
  }
}

function issueCard(issue, listing) {
  const fragment = $("issue-template").content.cloneNode(true);
  const card = fragment.querySelector(".issue-card");
  card.querySelector(".issue-field").textContent = issue.field;
  card.querySelector(".issue-reason").textContent = issue.reason_code;
  card.querySelector(".issue-state").textContent = issue.ui_state;
  const evidence = card.querySelector(".evidence-grid");
  evidence.appendChild(evidenceBox("Current value", issue.original_value));
  evidence.appendChild(evidenceBox("Proposed value", issue.proposed_value));
  evidence.appendChild(evidenceBox("Evidence", issue.evidence));
  evidence.appendChild(evidenceBox("Supporting text", issue.supporting_text));
  evidence.appendChild(evidenceBox("Conflicting text", issue.conflicting_text));
  evidence.appendChild(evidenceBox("Fingerprint", issue.input_fingerprint));

  const bulk = element("label", " Select for safe bulk preview", "check");
  const bulkCheck = document.createElement("input");
  bulkCheck.type = "checkbox";
  bulkCheck.checked = state.selectedBulk.has(issue.base_decision_id);
  bulkCheck.addEventListener("change", () => {
    if (bulkCheck.checked) state.selectedBulk.add(issue.base_decision_id);
    else state.selectedBulk.delete(issue.base_decision_id);
  });
  bulk.prepend(bulkCheck);
  card.querySelector(".issue-heading").appendChild(bulk);

  const form = card.querySelector(".decision-form");
  const action = form.elements.action;
  const allowedActions = issue.field === "map_ready"
    ? new Set(["leave_unresolved", "accepted_as_unknown", "accept_current_geocode", "correct_geocode", "correct_address", "exclude"])
    : issue.field === "price_monthly"
      ? new Set(["leave_unresolved", "accept_current", "accepted_as_unknown", "price_correction", "exclude"])
      : new Set(["leave_unresolved", "accept_current", "correct_value", "accepted_as_unknown", "exclude"]);
  [...action.options].forEach((option) => {
    if (!allowedActions.has(option.value)) option.remove();
  });
  const dynamic = form.querySelector(".dynamic-fields");
  action.addEventListener("change", () => renderDynamicFields(dynamic, action.value, issue, listing));
  renderDynamicFields(dynamic, action.value, issue, listing);
  if (issue.human_decision) {
    form.elements.review_note.value = issue.human_decision.review_note || "";
    form.elements.supporting_text.value = (issue.human_decision.supporting_text || []).join("\n");
  }
  if (state.data.read_only) {
    form.querySelector("button[type='submit']").disabled = true;
    form.querySelector(".form-status").textContent = "Read-only mode: decisions cannot be saved.";
  }
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submitDecision(form, issue, listing);
  });
  return fragment;
}

async function previewBulk(action) {
  if (!state.selectedBulk.size) return window.alert("Select one or more issues first.");
  const response = await fetch("/api/bulk-preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, base_decision_ids: [...state.selectedBulk] }),
  });
  const result = await response.json();
  if (!response.ok) return window.alert(result.detail || "Bulk preview failed");
  const message = `${result.safe ? "SAFE PREVIEW" : "UNAVAILABLE"}\n${result.affected_listing_count} listing(s), ${result.affected_issue_count} issue(s)\nReason: ${result.shared_reason || "mixed"}\nFields: ${result.fields.join(", ")}\n${result.evidence_criteria}`;
  window.alert(message);
  if (!result.safe || state.data.read_only) return;
  if (!window.confirm(`${message}\n\nSave these decisions? They will not be applied.`)) return;
  const save = await fetch("/api/bulk-decisions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ...result,
      confirm: true,
      reviewer_name: $("reviewer").value.trim(),
      review_note: "Confirmed homogeneous bulk decision",
    }),
  });
  const saved = await save.json();
  if (!save.ok) return window.alert(saved.detail || "Bulk save failed");
  state.selectedBulk.clear();
  await loadState(false);
}

function renderSelected() {
  const panel = $("review-panel");
  clear(panel);
  const listing = state.data.listings[state.selectedIndex];
  if (!listing) {
    panel.appendChild(element("div", "No listings match the current filters.", "empty-state"));
    destroyMap();
    return;
  }
  const identity = element("section", null, "identity-card");
  const heading = element("div", null, "identity-header");
  const title = element("div");
  title.appendChild(element("h2", listing.title || listing.address || listing.listing_id));
  const source = element("a", listing.source_url || "No source URL");
  if (listing.source_url) {
    source.href = listing.source_url;
    source.target = "_blank";
    source.rel = "noreferrer";
  }
  title.appendChild(source);
  heading.appendChild(title);
  heading.appendChild(element("span", listing.review_status, `badge ${listing.review_status === "complete" ? "good" : "warn"}`));
  identity.appendChild(heading);
  identity.appendChild(element("p", `${listing.listing_id} · ${listing.address || "Address unknown"}`, "muted"));
  const chips = element("div", null, "chip-row");
  listing.review_categories.forEach((category) => chips.appendChild(element("span", category, "badge")));
  identity.appendChild(chips);
  identity.appendChild(element("p", `Fingerprint: ${listing.input_fingerprint_status} · Last decision: ${listing.last_decision_timestamp || "none"}`, "muted"));
  panel.appendChild(identity);

  const callout = element("p", "May–Aug indicates summer availability and does not automatically mean sublet.", "rule-callout");
  panel.appendChild(callout);

  const evidence = element("div", null, "evidence-layout");
  const original = element("div");
  original.appendChild(section("Original description", element("div", listing.description || "No description", "description")));
  original.appendChild(section("Structured website fields", tableFor(listing.structured_fields)));
  original.appendChild(section("Deterministically parsed fields", tableFor(listing.parsed_fields)));
  const enrichment = element("div");
  enrichment.appendChild(section("Current reviewed fields", tableFor(listing.current_reviewed_fields)));
  enrichment.appendChild(section("Manual corrections", tableFor(listing.manual_corrections)));
  enrichment.appendChild(section("AI proposals and concise evidence", tableFor(listing.ai_fields)));
  evidence.appendChild(original);
  evidence.appendChild(enrichment);
  panel.appendChild(evidence);
  panel.appendChild(section("Pricing", tableFor(listing.pricing)));
  panel.appendChild(section("Availability and lease interpretation", tableFor(listing.availability)));
  panel.appendChild(section("Geocoding and map", mapSection(listing)));

  const issueTitle = element("div", null, "issues-title queue-header");
  issueTitle.appendChild(element("h2", `Review decisions (${listing.issues.length})`));
  const bulkActions = element("div", null, "map-actions");
  const unknown = element("button", "Preview bulk unknown", "secondary");
  unknown.type = "button";
  unknown.addEventListener("click", () => previewBulk("accepted_as_unknown"));
  const current = element("button", "Preview bulk current", "secondary");
  current.type = "button";
  current.addEventListener("click", () => previewBulk("accept_current"));
  bulkActions.appendChild(unknown);
  bulkActions.appendChild(current);
  issueTitle.appendChild(bulkActions);
  panel.appendChild(issueTitle);
  listing.issues.forEach((issue) => panel.appendChild(issueCard(issue, listing)));
  panel.appendChild(section("Audit timeline", tableFor(Object.fromEntries(listing.history.map((event, index) => [`${index + 1}. ${event.at_utc || "unknown time"}`, `${event.event}: ${event.status || event.reason || ""}`])))));
  panel.appendChild(element("p", `Drafts are not applied automatically. Apply with: python -m pipeline.operator apply-review-decisions --run-id ${state.data.run_id}`, "rule-callout"));
  window.setTimeout(() => renderMap(listing), 0);
}

function filterQuery() {
  const params = new URLSearchParams();
  const mappings = [
    ["search", "search"], ["category", "category"], ["field", "field"],
    ["reason", "reason"], ["decision-state", "decision_state"],
    ["map-ready", "map_ready"], ["max-confidence", "maximum_confidence"],
  ];
  mappings.forEach(([id, name]) => {
    const value = $(id).value;
    if (value !== "") params.set(name, value);
  });
  if ($("missing-price").checked) params.set("missing_price", "true");
  return params.toString();
}

async function loadState(resetIndex = true) {
  const panel = $("review-panel");
  try {
    const response = await fetch(`/api/state?${filterQuery()}`);
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Dashboard load failed");
    state.data = result;
    if (resetIndex) state.selectedIndex = 0;
    if (!$("reviewer").value) $("reviewer").value = result.default_reviewer || "";
    renderSummary();
    renderFilters();
    renderQueue();
    renderSelected();
  } catch (error) {
    clear(panel);
    panel.appendChild(element("div", error.message, "error-state"));
  }
}

function bindControls() {
  $("previous").addEventListener("click", () => {
    if (state.selectedIndex > 0) state.selectedIndex -= 1;
    renderQueue(); renderSelected();
  });
  $("next").addEventListener("click", () => {
    if (state.selectedIndex < state.data.listings.length - 1) state.selectedIndex += 1;
    renderQueue(); renderSelected();
  });
  $("refresh").addEventListener("click", () => loadState(false));
  ["category", "field", "reason", "decision-state", "map-ready", "max-confidence", "missing-price"].forEach((id) => {
    $(id).addEventListener("change", () => loadState(true));
  });
  let searchTimer;
  $("search").addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => loadState(true), 180);
  });
  document.addEventListener("keydown", (event) => {
    if (event.target.matches("input, textarea, select")) return;
    if (event.key === "ArrowLeft") $("previous").click();
    if (event.key === "ArrowRight") $("next").click();
  });
}

bindControls();
loadState(true);
