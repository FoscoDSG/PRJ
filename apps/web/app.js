const login = document.querySelector("#login");
const app = document.querySelector("#app");
const flash = document.querySelector("#flash");
const state = { role: "", demoMode: false, ontology: null, todayProcess: "", objectType: "Order" };

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, { credentials: "same-origin", ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail;
    const message = typeof detail === "string" ? detail : "Richiesta non riuscita";
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return data;
}

const {
  escapeHtml: esc,
  driftCard,
  quarantineCard,
  identityCard,
  mappingRow,
  profileLabelText,
  orderCard,
  orderDetail,
  customerDetail,
  objectCard,
  objectDetail,
  inboxCard,
  measureText,
  readableCard,
  showFixtureButton,
  companyUploadNotice,
  canWriteDecisions,
  matchesIncasso,
  allowsAction,
} = globalThis.ieolEscape;

const auditState = { offset: 0, limit: 50 };

function showFlash(message) {
  flash.hidden = false;
  flash.textContent = message;
}

async function boot() {
  try {
    const me = await api("/api/me");
    enter(me);
  } catch {
    login.hidden = false;
    app.hidden = true;
  }
}

function enter(me) {
  login.hidden = true;
  app.hidden = false;
  state.role = me.role;
  state.demoMode = Boolean(me.demo_mode);
  state.hasCompany = me.has_company === true;
  document.querySelector("#session").hidden = false;
  document.querySelector("#who").textContent = `${me.username} · ${me.role}`;
  document.querySelector("#load-fixture").hidden = !showFixtureButton(state.role, state.demoMode);
  const canWrite = canWriteDecisions(state.hasCompany);
  document.querySelector("#upload-label").hidden = !canWrite;
  document.querySelector("#confirm-high").hidden = !canWrite;
  document.querySelector("#materialize").hidden = !canWrite;
  const companyNotice = document.querySelector("#company-notice");
  companyNotice.hidden = state.hasCompany;
  companyNotice.textContent = state.hasCompany ? "" : companyUploadNotice();
  show("inbox");
}

document.querySelector("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  const error = document.querySelector("#login-error");
  try {
    const me = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ username: form.get("username"), password: form.get("password") }),
    });
    error.hidden = true;
    enter(me);
  } catch (err) {
    error.hidden = false;
    error.textContent = err.message;
  }
});

document.querySelector("#logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  location.reload();
});

document.querySelectorAll("nav button").forEach((button) => {
  button.addEventListener("click", () => show(button.dataset.view));
});

function show(view) {
  document.querySelectorAll("nav button").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  for (const name of ["mapping", "orders", "inbox", "audit"]) {
    document.querySelector(`#view-${name}`).hidden = name !== view;
  }
  if (view === "mapping") loadMappings();
  if (view === "orders") loadObjects();
  if (view === "inbox") loadInbox();
  if (view === "audit") loadAudit();
}

document.querySelector("#upload-document").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  const body = new FormData();
  body.append("file", file);
  try {
    const result = await api("/api/ingest/document", { method: "POST", body });
    const pending = result.reasons && result.reasons.length ? ` Controlli: ${result.reasons.join("; ")}.` : " Resta in quarantena finché non lo rilasci.";
    showFlash(`Ordine ${result.order_key} letto dal ${result.source_kind}.${pending}`);
    loadMappings();
  } catch (err) {
    showFlash(err.message);
  }
  event.target.value = "";
});

document.querySelector("#load-fixture").addEventListener("click", async () => {
  try {
    const result = await api("/api/demo/load", { method: "POST" });
    showFlash(`Fixture caricata. Raccomandazioni: ${result.recommendations.length}. Le colonne ambigue restano in review.`);
    loadMappings();
  } catch (err) {
    showFlash(err.message);
  }
});

document.querySelector("#confirm-high").addEventListener("click", async () => {
  try {
    const result = await api("/api/mappings/confirm-confident", { method: "POST" });
    showFlash(`Confermati ${result.confirmed} suggerimenti forti.`);
    loadMappings();
  } catch (err) {
    showFlash(err.message);
  }
});

document.querySelector("#materialize").addEventListener("click", async () => {
  try {
    await api("/api/materialize", { method: "POST" });
    await api("/api/agent/run", { method: "POST" });
    showFlash("Modello canonico e grafo aggiornati.");
  } catch (err) {
    showFlash(err.message);
  }
});

function renderProfileLabels(mappings, files) {
  const container = document.querySelector("#profile-labels");
  container.replaceChildren();
  const seen = new Map();
  for (const item of mappings || []) {
    if (!seen.has(item.source_name)) seen.set(item.source_name, item);
  }
  const bySource = new Map();
  for (const file of files || []) {
    if (file && file.source_name && !bySource.has(file.source_name)) {
      bySource.set(file.source_name, file);
    }
  }
  for (const item of seen.values()) {
    const row = document.createElement("p");
    row.className = "profile-label";
    const source = document.createElement("span");
    source.className = "profile-label-source";
    source.textContent = String(item.source_name ?? "");
    const text = document.createElement("span");
    text.className = "profile-label-text";
    text.textContent = profileLabelText(item);
    row.append(source, document.createTextNode(" "), text);
    container.append(row);
    const file = bySource.get(item.source_name);
    const fileActions = allowsAction(file, "correct") || allowsAction(file, "wrong") || allowsAction(file, "not_sure");
    if (file && file.file_id && Array.isArray(file.choices) && fileActions) {
      container.append(fileTypeActions(file));
    }
  }
}

function fileTypeActions(file) {
  const actions = document.createElement("div");
  actions.className = "file-type-actions";
  const ok = document.createElement("button");
  ok.type = "button";
  ok.textContent = "Corretto";
  ok.addEventListener("click", () => submitFileType(file.file_id, "correct"));
  if (allowsAction(file, "correct")) actions.append(ok);
  const wrong = document.createElement("button");
  wrong.type = "button";
  wrong.className = "ghost";
  wrong.textContent = "Non è questo tipo";
  const picker = document.createElement("label");
  picker.className = "file-type-choice";
  picker.hidden = true;
  picker.append(document.createTextNode("Tipo corretto "));
  const select = document.createElement("select");
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = "Scegli il tipo…";
  select.append(empty);
  for (const choice of file.choices) {
    const option = document.createElement("option");
    option.value = String(choice.code ?? "");
    option.textContent = String(choice.label ?? "");
    select.append(option);
  }
  const send = document.createElement("button");
  send.type = "button";
  send.textContent = "Registra tipo";
  send.disabled = true;
  select.addEventListener("change", () => {
    send.disabled = select.value === "";
  });
  picker.append(select, document.createTextNode(" "), send);
  wrong.addEventListener("click", () => {
    picker.hidden = false;
  });
  send.addEventListener("click", () => {
    const code = select.value;
    if (code === "") return;
    if (code === "not_sure") submitFileType(file.file_id, "not_sure");
    else submitFileType(file.file_id, "wrong", code);
  });
  if (allowsAction(file, "wrong") || allowsAction(file, "not_sure")) {
    if (actions.childNodes.length) actions.append(document.createTextNode(" "));
    actions.append(wrong, document.createTextNode(" "), picker);
  }
  return actions;
}

async function submitFileType(fileId, decision, correctType) {
  const body = { decision };
  if (correctType) body.correct_type = correctType;
  try {
    await api(`/api/files/${encodeURIComponent(fileId)}/type`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    showFlash("Tipo di file registrato.");
    loadMappings();
  } catch (err) {
    showFlash(err.message);
  }
}

async function loadMappings() {
  const data = await api("/api/mappings");
  const canWrite = canWriteDecisions(state.hasCompany);
  document.querySelector("#mapping-table").classList.toggle("mappings-readonly", !canWrite);
  const drift = document.querySelector("#drift");
  drift.innerHTML = data.drift.map((item) => driftCard(item, canWrite, state.role)).join("");
  drift.querySelectorAll("button[data-drift]").forEach((button) => {
    button.addEventListener("click", async () => {
      await api(`/api/drift/${button.dataset.drift}/acknowledge`, { method: "POST" });
      loadMappings();
    });
  });
  bindRevokes(drift, loadMappings);
  const quarantine = document.querySelector("#quarantine");
  const held = await api("/api/quarantine");
  quarantine.innerHTML = held.documents.map((item) => quarantineCard(item, canWrite, state.role)).join("");
  for (const item of held.documents) {
    const slot = quarantine.querySelector(`[data-quarantine-reason="${CSS.escape(String(item.id))}"]`);
    if (slot) slot.textContent = item.reason == null ? "" : String(item.reason);
  }
  quarantine.querySelectorAll("button[data-release]").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        await api(`/api/quarantine/${button.dataset.release}/release`, { method: "POST" });
        showFlash("Documento rilasciato nel modello.");
        loadMappings();
      } catch (err) {
        showFlash(err.message);
      }
    });
  });
  quarantine.querySelectorAll("button[data-reject-quarantine]").forEach((button) => {
    button.addEventListener("click", async () => {
      const field = quarantine.querySelector(
        `[data-quarantine-reason-input="${CSS.escape(button.dataset.rejectQuarantine)}"]`,
      );
      const reason = field ? field.value.trim() : "";
      if (reason.length < 5) {
        showFlash("Scrivi il motivo del rifiuto (almeno 5 caratteri).");
        return;
      }
      try {
        await api(`/api/quarantine/${button.dataset.rejectQuarantine}/reject`, {
          method: "POST",
          body: JSON.stringify({ reason }),
        });
        showFlash("Documento rifiutato.");
        loadMappings();
      } catch (err) {
        showFlash(err.message);
      }
    });
  });
  bindRevokes(quarantine, loadMappings);
  const identity = document.querySelector("#identity");
  const reviews = await api("/api/identity-reviews");
  identity.innerHTML = reviews.reviews.map((item) => identityCard(item, canWrite, state.role)).join("");
  identity.querySelectorAll("button[data-merge]").forEach((button) => {
    button.addEventListener("click", async () => {
      await api(`/api/identity-reviews/${button.dataset.merge}/confirm`, { method: "POST" });
      loadMappings();
    });
  });
  identity.querySelectorAll("button[data-split]").forEach((button) => {
    button.addEventListener("click", async () => {
      await api(`/api/identity-reviews/${button.dataset.split}/reject`, { method: "POST" });
      loadMappings();
    });
  });
  bindRevokes(identity, loadMappings);
  renderProfileLabels(data.mappings, data.files);
  const body = document.querySelector("#mappings");
  body.innerHTML = data.mappings.map((item) => mappingRow(item, canWriteDecisions(state.hasCompany), state.role)).join("");
  body.querySelectorAll("button[data-confirm]").forEach((button) => {
    button.addEventListener("click", async () => {
      await api(`/api/mappings/${button.dataset.confirm}/confirm`, { method: "POST" });
      loadMappings();
    });
  });
  body.querySelectorAll("button[data-reject]").forEach((button) => {
    button.addEventListener("click", async () => {
      await api(`/api/mappings/${button.dataset.reject}/reject`, { method: "POST" });
      loadMappings();
    });
  });
  bindRevokes(body, loadMappings);
}

function revokeReason(button, id) {
  const root = button.closest("article, tr") || button.parentElement;
  const field = root ? root.querySelector(`[data-revoke-reason="${CSS.escape(id)}"]`) : null;
  const reason = field ? field.value.trim() : "";
  if (reason.length < 5) {
    showFlash("Scrivi il motivo dell'annullamento (almeno 5 caratteri).");
    return "";
  }
  return reason;
}

async function postRevoke(url, reason, reload) {
  try {
    await api(url, { method: "POST", body: JSON.stringify({ reason }) });
    showFlash("Decisione annullata. Resta nello storico, barrata.");
    reload();
  } catch (err) {
    showFlash(err.message);
  }
}

function bindRevokes(root, reload) {
  const routes = [
    ["button[data-revoke]", (button) => button.dataset.revoke, (id) => `/api/mapping-decisions/${id}/revoke`],
    ["button[data-revoke-mapping]", (button) => button.dataset.revokeMapping, (id) => `/api/mapping-decisions/${id}/revoke`],
    ["button[data-revoke-inbox]", (button) => button.dataset.revokeInbox, (id) => `/api/decisions/${id}/revoke`],
    ["button[data-revoke-payment]", (button) => button.dataset.revokePayment, (id) => `/api/payment-decisions/${id}/revoke`],
    ["button[data-revoke-quarantine]", (button) => button.dataset.revokeQuarantine, (id) => `/api/quarantine/${id}/revoke`],
    ["button[data-revoke-identity]", (button) => button.dataset.revokeIdentity, (id) => `/api/identity-reviews/${id}/revoke`],
    ["button[data-revoke-drift]", (button) => button.dataset.revokeDrift, (id) => `/api/drift/${id}/revoke`],
  ];
  for (const [selector, idFor, urlFor] of routes) {
    root.querySelectorAll(selector).forEach((button) => {
      button.addEventListener("click", async () => {
        const id = idFor(button);
        const reason = revokeReason(button, id);
        if (!reason) return;
        await postRevoke(urlFor(id), reason, reload);
      });
    });
  }
}

const OBJECT_PAGE_TYPES = ["Order", "Invoice", "Payment"];

function openCount(items, processId) {
  return items.filter((item) => {
    if (!item || item.disposition !== "open") return false;
    if (!processId) return true;
    return Boolean(item.process && item.process.id === processId);
  }).length;
}

function renderProcessFilters(processes, items) {
  const root = document.querySelector("#process-filters");
  root.replaceChildren();
  const choices = [{ id: "", label: "Tutti" }].concat(
    (processes || []).map((process) => ({
      id: process.id,
      label: process.label || process.id,
    })),
  );
  for (const choice of choices) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ghost";
    button.setAttribute("aria-pressed", state.todayProcess === choice.id ? "true" : "false");
    button.textContent = `${choice.label} · ${openCount(items, choice.id)}`;
    button.addEventListener("click", () => {
      state.todayProcess = choice.id;
      loadInbox();
    });
    root.append(button);
  }
}

async function ensureOntology() {
  if (!state.ontology) state.ontology = await api("/api/ontology");
  return state.ontology;
}

async function loadObjects() {
  const ontology = await ensureOntology();
  const types = OBJECT_PAGE_TYPES
    .map((name) => (ontology.types || []).find((item) => item && item.name === name))
    .filter(Boolean);
  if (types.length && !types.some((item) => item.name === state.objectType)) {
    state.objectType = types[0].name;
  }
  renderObjectTypes(types);
  const list = document.querySelector("#order-list");
  const detail = document.querySelector("#order-detail");
  detail.hidden = true;
  list.hidden = false;
  let data = { objects: [] };
  try {
    data = await api(`/api/objects?type=${encodeURIComponent(state.objectType)}`);
  } catch (err) {
    list.replaceChildren();
    const note = document.createElement("p");
    note.textContent = err.message;
    list.append(note);
    return;
  }
  let objects = data.objects || [];
  if (state.objectType === "Payment") objects = objects.filter((object) => matchesIncasso(object));
  if (!objects.length) {
    list.replaceChildren();
    const note = document.createElement("p");
    note.textContent = "Nessun oggetto di questo tipo.";
    list.append(note);
    return;
  }
  list.innerHTML = objects.map((object) => objectCard(object, ontology)).join("");
  bindObjectOpen(list);
}

function renderObjectTypes(types) {
  const root = document.querySelector("#object-types");
  root.replaceChildren();
  for (const typeDef of types) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ghost";
    button.setAttribute("aria-pressed", state.objectType === typeDef.name ? "true" : "false");
    button.textContent = typeDef.label || typeDef.name;
    button.addEventListener("click", () => {
      state.objectType = typeDef.name;
      loadObjects();
    });
    root.append(button);
  }
}

function bindObjectOpen(root) {
  root.querySelectorAll("[data-object]").forEach((node) => {
    node.addEventListener("click", (event) => {
      if (node.tagName === "A") event.preventDefault();
      openObject(node.dataset.object);
    });
  });
}

async function openObject(id) {
  const object = await api(`/api/objects/${id}`);
  const ontology = await ensureOntology();
  document.querySelector("#order-list").hidden = true;
  const detail = document.querySelector("#order-detail");
  detail.hidden = false;
  detail.innerHTML = objectDetail(object, ontology);
  detail.querySelector("#back-objects").addEventListener("click", loadObjects);
  bindObjectOpen(detail);
}

async function openOrder(id) {
  const order = await api(`/api/orders/${id}`);
  document.querySelector("#order-list").hidden = true;
  const detail = document.querySelector("#order-detail");
  detail.hidden = false;
  detail.innerHTML = orderDetail(order);
  detail.querySelector("#back-orders").addEventListener("click", loadObjects);
  const customerButton = detail.querySelector("#open-customer");
  if (customerButton) {
    customerButton.addEventListener("click", () => openCustomer(customerButton.dataset.customer));
  }
}

async function openCustomer(id) {
  const customer = await api(`/api/customers/${id}`);
  const detail = document.querySelector("#order-detail");
  document.querySelector("#order-list").hidden = true;
  detail.hidden = false;
  detail.innerHTML = customerDetail(customer, state.role, canWriteDecisions(state.hasCompany));
  detail.querySelector("#back-orders").addEventListener("click", loadObjects);
  const unmask = detail.querySelector("#unmask-pii");
  if (unmask) {
    unmask.addEventListener("click", async () => {
      try {
        const reason = detail.querySelector("[data-reason]").value.trim();
        if (reason.length < 5) {
          showFlash("Scrivi un motivo di almeno 5 caratteri.");
          return;
        }
        const result = await api("/api/pii/unmask", {
          method: "POST",
          body: JSON.stringify({ entity_id: unmask.dataset.entity, reason }),
        });
        const target = detail.querySelector("#unmask-result");
        target.textContent = (result.properties || [])
          .map((item) => `${item.property}: ${item.value}`)
          .join("\n");
      } catch (err) {
        showFlash(err.message);
      }
    });
  }
  detail.querySelectorAll("button[data-order]").forEach((button) => {
    button.addEventListener("click", () => openOrder(button.dataset.order));
  });
}

async function loadInbox() {
  const data = await api("/api/inbox");
  const ontology = await ensureOntology();
  const items = data.recommendations || [];
  renderProcessFilters(ontology.processes || [], items);
  const measure = document.querySelector("#match-measure");
  if (measure) {
    try {
      measure.textContent = measureText(await api("/api/payment-match/measure"));
    } catch (err) {
      measure.textContent = measureText(null);
    }
  }
  const visible = state.todayProcess
    ? items.filter((item) => item.process && item.process.id === state.todayProcess)
    : items;
  const root = document.querySelector("#inbox");
  const canWrite = canWriteDecisions(state.hasCompany);
  if (!visible.length) {
    root.replaceChildren();
    const note = document.createElement("p");
    note.textContent = "Nessuna scheda in questo processo.";
    root.append(note);
    return;
  }
  const openItems = [];
  const decidedItems = [];
  for (const item of visible) {
    if (item && item.disposition === "closed") decidedItems.push(item);
    else openItems.push(item);
  }
  const openHtml = openItems.map((item) => inboxCard(item, state.role, canWrite)).join("");
  const decidedHtml = decidedItems.length
    ? `<h3 class="section-heading">Già decise</h3>${decidedItems.map((item) => inboxCard(item, state.role, canWrite)).join("")}`
    : "";
  root.innerHTML = openHtml + decidedHtml;
  bindRevokes(root, loadInbox);
  root.querySelectorAll("article").forEach((card) => {
    card.querySelectorAll("button[data-act]").forEach((button) => {
      button.addEventListener("click", () => act(card.dataset.id, button.dataset.act, card));
    });
  });
}

async function act(id, action, card) {
  try {
    const evidenceHash = card.dataset.hash;
    if (action === "modify") {
      const quantity = Number(card.querySelector("[data-qty]").value);
      await api(`/api/recommendations/${id}/modify`, {
        method: "POST",
        body: JSON.stringify({ decision: "modify", evidence_hash: evidenceHash, quantity }),
      });
    } else if (action === "reject") {
      const reason = card.querySelector("[data-reason]").value.trim();
      if (reason.length < 5) {
        showFlash("Scrivi un motivo di almeno 5 caratteri.");
        return;
      }
      await api(`/api/recommendations/${id}/reject`, {
        method: "POST",
        body: JSON.stringify({
          decision: "reject",
          evidence_hash: evidenceHash,
          reason,
        }),
      });
    } else if (action === "approve") {
      const result = await api(`/api/recommendations/${id}/approve`, {
        method: "POST",
        body: JSON.stringify({ decision: "approve", evidence_hash: evidenceHash }),
      });
      showFlash("Dry-run pronto. Il payload è nella scheda, nessuna scrittura ERP.");
      if (result.dry_run) {
        const block = document.createElement("pre");
        block.textContent = JSON.stringify(result.dry_run, null, 2);
        card.appendChild(block);
      }
    } else if (action === "execute") {
      const result = await api(`/api/recommendations/${id}/execute`, { method: "POST" });
      showFlash(result.replayed ? "Esecuzione già presente: nessuna seconda scrittura." : "Azione eseguita sul sandbox e registrata nel grafo.");
    } else if (action === "confirm-match") {
      await api(`/api/payment-proposals/${id}/confirm`, {
        method: "POST",
        body: JSON.stringify({ decision: "confirm", evidence_hash: evidenceHash }),
      });
    } else if (action === "reject-match") {
      const reason = card.querySelector("[data-reject-reason]").value.trim();
      if (reason.length < 5) {
        showFlash("Scrivi un motivo di almeno 5 caratteri.");
        return;
      }
      await api(`/api/payment-proposals/${id}/reject`, {
        method: "POST",
        body: JSON.stringify({ decision: "reject", evidence_hash: evidenceHash, reason }),
      });
    } else if (action === "correct-match") {
      const reason = card.querySelector("[data-correct-reason]").value.trim();
      const invoiceId = card.querySelector("[data-invoice]").value.trim();
      const amount = Number(card.querySelector("[data-amount]").value);
      if (reason.length < 5 || invoiceId === "" || !Number.isFinite(amount) || amount <= 0) {
        showFlash("Indica fattura, importo e un motivo di almeno 5 caratteri.");
        return;
      }
      await api(`/api/payment-proposals/${id}/correct`, {
        method: "POST",
        body: JSON.stringify({
          decision: "correct",
          evidence_hash: evidenceHash,
          invoice_number: invoiceId,
          allocated_amount: amount,
          reason,
        }),
      });
    }
    loadInbox();
  } catch (err) {
    showFlash(err.message);
    if (err.status === 409) {
      await loadInbox();
    }
  }
}

async function loadAudit() {
  const params = new URLSearchParams();
  const order = document.querySelector("#audit-order").value.trim();
  const user = document.querySelector("#audit-user").value.trim();
  const batch = document.querySelector("#audit-batch").value.trim();
  const dateFrom = document.querySelector("#audit-from").value;
  const dateTo = document.querySelector("#audit-to").value;
  if (order) params.set("order", order);
  if (user) params.set("user", user);
  if (batch) params.set("batch", batch);
  if (dateFrom) params.set("date_from", dateFrom);
  if (dateTo) params.set("date_to", dateTo);
  params.set("limit", String(auditState.limit));
  params.set("offset", String(auditState.offset));
  const root = document.querySelector("#audit");
  try {
    const data = await api(`/api/audit/readable?${params.toString()}`);
    root.innerHTML = data.events.map(readableCard).join("") || "<p>Nessun evento.</p>";
    const page = document.querySelector("#audit-page");
    const shown = data.offset + data.events.length;
    page.textContent = data.total ? `${data.offset + 1}–${shown} di ${data.total}` : "0 eventi";
  } catch (err) {
    root.innerHTML = `<p>${esc(err.message)}</p>`;
    document.querySelector("#audit-page").textContent = "";
  }
}

document.querySelector("#audit-apply").addEventListener("click", () => {
  auditState.offset = 0;
  loadAudit();
});
document.querySelector("#audit-prev").addEventListener("click", () => {
  auditState.offset = Math.max(0, auditState.offset - auditState.limit);
  loadAudit();
});
document.querySelector("#audit-next").addEventListener("click", () => {
  auditState.offset += auditState.limit;
  loadAudit();
});

boot();
