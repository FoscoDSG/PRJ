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

function showFlash(message) {
  flash.hidden = false;
  flash.textContent = message;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[character]));
}

function pill(text) {
  return `<span class="pill ${esc(text)}">${esc(text)}</span>`;
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
    .map((item) => `<article class="card"><strong>Schema drift su ${esc(item.source_name)}</strong><pre>${esc(JSON.stringify(item.diff, null, 2))}</pre><button type="button" data-drift="${esc(item.id)}">Segna visto</button></article>`)
    .join("");
  drift.querySelectorAll("button[data-drift]").forEach((button) => {
    button.addEventListener("click", async () => {
      await api(`/api/drift/${button.dataset.drift}/acknowledge`, { method: "POST" });
      loadMappings();
    });
  });
  const body = document.querySelector("#mappings");
  body.innerHTML = data.mappings
    .map((item) => {
      const actions = item.status === "proposed"
        ? `<button type="button" data-confirm="${esc(item.id)}">Conferma</button> <button type="button" class="ghost" data-reject="${esc(item.id)}">Rifiuta</button>`
        : "";
      return `<tr>
        <td>${esc(item.source_name)}</td>
        <td><code>${esc(item.source_field)}</code></td>
        <td>${esc(item.target_type)}.${esc(item.target_property)}</td>
        <td>${esc(Number(item.confidence).toFixed(2))}</td>
        <td>${pill(item.status)}</td>
        <td>${actions}</td>
      </tr>`;
    })
    .join("");
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
  list.innerHTML = data.orders
    .map((order) => `<article class="card">
      <h3>Ordine ${esc(order.business_key)}</h3>
      <div class="meta">${pill(order.status || "n/d")}<span class="pill">shortage ${esc(order.shortage_qty ?? 0)}</span></div>
      <p>${esc(order.customer_name || "Cliente non collegato")} · importo ${esc(order.amount)} · margine ${esc(order.margin ?? "n/d")}</p>
      <button type="button" data-order="${esc(order.id)}">Apri scheda</button>
    </article>`)
    .join("") || "<p>Nessun ordine. Carica la fixture e aggiorna il modello.</p>";
  list.querySelectorAll("button[data-order]").forEach((button) => {
    button.addEventListener("click", () => openOrder(button.dataset.order));
  });
}

async function openOrder(id) {
  const order = await api(`/api/orders/${id}`);
  document.querySelector("#order-list").hidden = true;
  const detail = document.querySelector("#order-detail");
  detail.hidden = false;
  const edges = order.edges
    .map((edge) => `<li>${edge.direction === "in" ? "←" : "→"} ${esc(edge.rel)} ${esc(edge.other_type)} ${esc(edge.other_key)}</li>`)
    .join("");
  const lines = order.lines
    .map((line) => `<li>${esc(line.product)}: quantità ${esc(line.quantity)}, shortage ${esc(line.shortage)}. Stock disponibile ${esc(line.stock ? line.stock.available : "n/d")} <span class="formula">(${esc(line.stock ? line.stock.formula : order.stock_formula)})</span></li>`)
    .join("");
  const customer = order.customer_id
    ? `<button type="button" id="open-customer" data-customer="${esc(order.customer_id)}">Scheda cliente</button>`
    : "";
  detail.innerHTML = `
    <p><button type="button" id="back-orders" class="ghost">Torna agli ordini</button></p>
    <h2>Ordine ${esc(order.business_key)}</h2>
    <p class="formula">${esc(order.stock_formula)}</p>
    <ul>${lines}</ul>
    <h3>Grafo</h3>
    <ul class="edges">${edges || "<li>Nessun arco</li>"}</ul>
    ${customer}
    <h3>Fatti</h3>
    <pre>${esc(JSON.stringify(order.facts, null, 2))}</pre>
  `;
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
  const orders = customer.orders.map((order) => `<li><button type="button" data-order="${esc(order.id)}">Ordine ${esc(order.business_key)}</button></li>`).join("");
  detail.innerHTML = `
    <p><button type="button" id="back-orders" class="ghost">Torna agli ordini</button></p>
    <h2>${esc(customer.company_name || customer.business_key)}</h2>
    <ul>${orders}</ul>
    <h3>Grafo</h3>
    <ul class="edges">${customer.edges.map((edge) => `<li>${esc(edge.rel)} ${esc(edge.other_type)} ${esc(edge.other_key)}</li>`).join("")}</ul>
    <pre>${esc(JSON.stringify(customer.facts, null, 2))}</pre>
  `;
  detail.querySelector("#back-orders").addEventListener("click", loadOrders);
  detail.querySelectorAll("button[data-order]").forEach((button) => {
    button.addEventListener("click", () => openOrder(button.dataset.order));
  });
}

async function loadInbox() {
  const data = await api("/api/inbox");
  const root = document.querySelector("#inbox");
  root.innerHTML = data.recommendations.map((item) => `
    <article class="card" data-id="${esc(item.id)}">
      <div class="meta">${pill(item.status)}${item.requires_cfo ? '<span class="pill high">CFO</span>' : ""}<span class="pill">${esc(item.risk)}</span></div>
      <h3>Ordine ${esc(item.order_key)}</h3>
      <p>${esc(item.problem)}</p>
      <p>Impatto atteso: ${esc(item.expected_impact)}. Confidence ${esc(item.confidence)}.</p>
      ${item.dry_run ? `<h3>Payload ERP</h3><pre>${esc(JSON.stringify(item.dry_run, null, 2))}</pre>` : ""}
      <label>Quantità acquisto <input data-qty value="${esc(item.actions.find((action) => action.type === "createPurchaseOrder")?.params.quantity || "")}" /></label>
      <div>
        <button type="button" data-act="approve">Approva</button>
        <button type="button" class="ghost" data-act="modify">Modifica</button>
        <button type="button" class="ghost" data-act="reject">Rifiuta</button>
        <button type="button" data-act="execute">Esegui</button>
      </div>
    </article>
  `).join("") || "<p>Nessuna eccezione in inbox.</p>";
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
  document.querySelector("#audit").innerHTML = data.events
    .map((event) => `<article class="card"><div class="meta">${pill(event.action)}<span class="pill">${esc(event.actor || "system")}</span></div><pre>${esc(JSON.stringify(event.payload, null, 2))}</pre></article>`)
    .join("") || "<p>Nessun evento.</p>";
}

boot();
