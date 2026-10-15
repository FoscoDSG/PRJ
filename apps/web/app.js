const login = document.querySelector("#login");
const app = document.querySelector("#app");
const flash = document.querySelector("#flash");
const state = { role: "", demoMode: false };

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
  mappingRow,
  orderCard,
  orderDetail,
  customerDetail,
  inboxCard,
  readableCard,
  showFixtureButton,
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
  document.querySelector("#session").hidden = false;
  document.querySelector("#who").textContent = `${me.username} · ${me.role}`;
  document.querySelector("#load-fixture").hidden = !showFixtureButton(state.role, state.demoMode);
  show("mapping");
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
  if (view === "orders") loadOrders();
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
    showFlash(`Confermati ${result.confirmed} mapping sopra soglia.`);
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

async function loadMappings() {
  const data = await api("/api/mappings");
  const drift = document.querySelector("#drift");
  drift.innerHTML = data.drift
    .filter((item) => item.status === "open")
    .map(driftCard)
    .join("");
  drift.querySelectorAll("button[data-drift]").forEach((button) => {
    button.addEventListener("click", async () => {
      await api(`/api/drift/${button.dataset.drift}/acknowledge`, { method: "POST" });
      loadMappings();
    });
  });
  const quarantine = document.querySelector("#quarantine");
  const held = await api("/api/quarantine");
  quarantine.innerHTML = held.documents
    .filter((item) => item.status === "open")
    .map((item) => `<article class="card"><strong>Quarantena ${esc(item.order_key || item.source_name)}</strong><p>${esc((item.reasons || []).join("; ") || "In attesa di conferma, nessuna scrittura ERP.")}</p><button type="button" data-release="${esc(item.id)}">Rilascia</button></article>`)
    .join("");
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
  const identity = document.querySelector("#identity");
  const reviews = await api("/api/identity-reviews");
  identity.innerHTML = reviews.reviews
    .filter((item) => item.status === "open")
    .map((item) => `<article class="card"><strong>Identità ${esc(item.entity_type)}</strong><p>${esc(item.reason)} (${esc(item.score)})</p><button type="button" data-merge="${esc(item.id)}">Conferma fusione</button> <button type="button" class="ghost" data-split="${esc(item.id)}">Tieni separati</button></article>`)
    .join("");
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
  const body = document.querySelector("#mappings");
  body.innerHTML = data.mappings.map(mappingRow).join("");
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
}

async function loadOrders() {
  const data = await api("/api/orders");
  const list = document.querySelector("#order-list");
  document.querySelector("#order-detail").hidden = true;
  list.hidden = false;
  list.innerHTML = data.orders.map(orderCard).join("") || "<p>Nessun ordine. Carica la fixture e aggiorna il modello.</p>";
  list.querySelectorAll("button[data-order]").forEach((button) => {
    button.addEventListener("click", () => openOrder(button.dataset.order));
  });
}

async function openOrder(id) {
  const order = await api(`/api/orders/${id}`);
  document.querySelector("#order-list").hidden = true;
  const detail = document.querySelector("#order-detail");
  detail.hidden = false;
  detail.innerHTML = orderDetail(order);
  detail.querySelector("#back-orders").addEventListener("click", loadOrders);
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
  detail.innerHTML = customerDetail(customer, state.role);
  detail.querySelector("#back-orders").addEventListener("click", loadOrders);
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
  const root = document.querySelector("#inbox");
  root.innerHTML = data.recommendations.map((item) => inboxCard(item, state.role)).join("") || "<p>Nessuna eccezione in inbox.</p>";
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
