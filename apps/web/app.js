const login = document.querySelector("#login");
const app = document.querySelector("#app");
const flash = document.querySelector("#flash");

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, { credentials: "same-origin", ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || "Richiesta non riuscita");
  return data;
}

const {
  driftCard,
  mappingRow,
  orderCard,
  orderDetail,
  customerDetail,
  inboxCard,
  auditCard,
} = globalThis.ieolEscape;

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
  document.querySelector("#session").hidden = false;
  document.querySelector("#who").textContent = `${me.username} · ${me.role}`;
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
  detail.innerHTML = customerDetail(customer);
  detail.querySelector("#back-orders").addEventListener("click", loadOrders);
  detail.querySelectorAll("button[data-order]").forEach((button) => {
    button.addEventListener("click", () => openOrder(button.dataset.order));
  });
}

async function loadInbox() {
  const data = await api("/api/inbox");
  const root = document.querySelector("#inbox");
  root.innerHTML = data.recommendations.map(inboxCard).join("") || "<p>Nessuna eccezione in inbox.</p>";
  root.querySelectorAll("article").forEach((card) => {
    card.querySelectorAll("button[data-act]").forEach((button) => {
      button.addEventListener("click", () => act(card.dataset.id, button.dataset.act, card));
    });
  });
}

async function act(id, action, card) {
  try {
    if (action === "modify") {
      const quantity = Number(card.querySelector("[data-qty]").value);
      await api(`/api/recommendations/${id}/modify`, { method: "POST", body: JSON.stringify({ quantity }) });
    } else if (action === "reject") {
      await api(`/api/recommendations/${id}/reject`, { method: "POST", body: JSON.stringify({ reason: "rifiutata dall'operatore" }) });
    } else if (action === "approve") {
      const result = await api(`/api/recommendations/${id}/approve`, { method: "POST" });
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
  }
}

async function loadAudit() {
  const data = await api("/api/audit");
  document.querySelector("#audit").innerHTML = data.events.map(auditCard).join("") || "<p>Nessun evento.</p>";
}

boot();
