/* Safe HTML for server-provided strings. Class tokens are whitelisted. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  root.ieolEscape = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const PILL_CLASSES = new Set([
    "proposed",
    "confirmed",
    "rejected",
    "open",
    "acknowledged",
    "detected",
    "approved",
    "modified",
    "dry_run_ok",
    "executed",
    "failed",
    "expired",
    "high",
    "low",
    "medium",
    "ok",
    "warn",
    "proposal",
    "human_decision",
    "dry_run",
    "external_call",
    "outcome",
    "n/d",
  ]);

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function pill(text) {
    const raw = text == null ? "" : String(text);
    const extra = PILL_CLASSES.has(raw) ? ` ${raw}` : "";
    return `<span class="pill${extra}">${escapeHtml(raw)}</span>`;
  }

  function driftCard(item) {
    return `<article class="card"><strong>Schema drift su ${escapeHtml(item.source_name)}</strong><pre>${escapeHtml(JSON.stringify(item.diff, null, 2))}</pre><button type="button" data-drift="${escapeHtml(item.id)}">Segna visto</button></article>`;
  }

  function mappingRow(item) {
    const actions = item.status === "proposed"
      ? `<button type="button" data-confirm="${escapeHtml(item.id)}">Conferma</button> <button type="button" class="ghost" data-reject="${escapeHtml(item.id)}">Rifiuta</button>`
      : "";
    return `<tr>
        <td>${escapeHtml(item.source_name)}</td>
        <td><code>${escapeHtml(item.source_field)}</code></td>
        <td>${escapeHtml(item.target_type)}.${escapeHtml(item.target_property)}</td>
        <td>${escapeHtml(Number(item.confidence).toFixed(2))}</td>
        <td>${pill(item.status)}</td>
        <td>${actions}</td>
      </tr>`;
  }

  function orderCard(order) {
    return `<article class="card">
      <h3>Ordine ${escapeHtml(order.business_key)}</h3>
      <div class="meta">${pill(order.status || "n/d")}<span class="pill">shortage ${escapeHtml(order.shortage_qty ?? 0)}</span></div>
      <p>${escapeHtml(order.customer_name || "Cliente non collegato")} · importo ${escapeHtml(order.amount)} · margine ${escapeHtml(order.margin ?? "n/d")}</p>
      <button type="button" data-order="${escapeHtml(order.id)}">Apri scheda</button>
    </article>`;
  }

  function orderDetail(order) {
    const edges = order.edges
      .map((edge) => `<li>${edge.direction === "in" ? "←" : "→"} ${escapeHtml(edge.rel)} ${escapeHtml(edge.other_type)} ${escapeHtml(edge.other_key)}</li>`)
      .join("");
    const lines = order.lines
      .map((line) => {
        const available = line.stock ? line.stock.available : "n/d";
        const formula = line.stock ? line.stock.formula : order.stock_formula;
        return `<li>${escapeHtml(line.product)}: quantità ${escapeHtml(line.quantity)}, shortage ${escapeHtml(line.shortage)}. Stock disponibile ${escapeHtml(available)} <span class="formula">(${escapeHtml(formula)})</span></li>`;
      })
      .join("");
    const customer = order.customer_id
      ? `<button type="button" id="open-customer" data-customer="${escapeHtml(order.customer_id)}">Scheda cliente</button>`
      : "";
    return `
    <p><button type="button" id="back-orders" class="ghost">Torna agli ordini</button></p>
    <h2>Ordine ${escapeHtml(order.business_key)}</h2>
    <p class="formula">${escapeHtml(order.stock_formula)}</p>
    <ul>${lines}</ul>
    <h3>Grafo</h3>
    <ul class="edges">${edges || "<li>Nessun arco</li>"}</ul>
    ${customer}
    <h3>Fatti</h3>
    <pre>${escapeHtml(JSON.stringify(order.facts, null, 2))}</pre>
  `;
  }

  function customerDetail(customer) {
    const orders = customer.orders
      .map((order) => `<li><button type="button" data-order="${escapeHtml(order.id)}">Ordine ${escapeHtml(order.business_key)}</button></li>`)
      .join("");
    const edges = customer.edges
      .map((edge) => `<li>${escapeHtml(edge.rel)} ${escapeHtml(edge.other_type)} ${escapeHtml(edge.other_key)}</li>`)
      .join("");
    return `
    <p><button type="button" id="back-orders" class="ghost">Torna agli ordini</button></p>
    <h2>${escapeHtml(customer.company_name || customer.business_key)}</h2>
    <ul>${orders}</ul>
    <h3>Grafo</h3>
    <ul class="edges">${edges}</ul>
    <pre>${escapeHtml(JSON.stringify(customer.facts, null, 2))}</pre>
  `;
  }

  function inboxCard(item, role) {
    const purchase = item.actions.find((action) => action.type === "createPurchaseOrder");
    const qty = purchase?.params.quantity ?? "";
    const preview = item.preview || {};
    const payloads = preview.payloads || item.dry_run || [];
    const evidence = (item.evidence || [])
      .map((fact) => `<li>${escapeHtml(fact.property)}: ${escapeHtml(fact.value)}</li>`)
      .join("");
    const canDecide = role === "approver" || role === "admin";
    const cfo = item.requires_cfo ? '<span class="pill high">CFO</span>' : "";
    const executeButton = item.status === "dry_run_ok" || item.status === "executed"
      ? '<button type="button" data-act="execute">Esegui</button>'
      : "";
    const buttons = canDecide
      ? `<label>Quantità acquisto <input data-qty value="${escapeHtml(qty)}" /></label>
      <div>
        <button type="button" data-act="approve">Approva</button>
        <button type="button" class="ghost" data-act="modify">Modifica</button>
        <button type="button" class="ghost" data-act="reject">Rifiuta</button>
        ${executeButton}
      </div>`
      : "<p>Sola lettura: l'operatore non approva.</p>";
    return `<article class="card" data-id="${escapeHtml(item.id)}">
      <div class="meta">${pill(item.status)}${cfo}<span class="pill">${escapeHtml(item.risk)}</span></div>
      <h3>Ordine ${escapeHtml(item.order_key)}</h3>
      <p>${escapeHtml(item.problem)}</p>
      <p class="formula">${escapeHtml(preview.formula || "")}</p>
      <ul>${evidence}</ul>
      <h3>Payload ERP / email</h3>
      <pre>${escapeHtml(JSON.stringify(payloads, null, 2))}</pre>
      ${buttons}
    </article>`;
  }

  function auditCard(event) {
    return `<article class="card"><div class="meta">${pill(event.action)}<span class="pill">${escapeHtml(event.actor || "system")}</span></div><pre>${escapeHtml(JSON.stringify(event.payload, null, 2))}</pre></article>`;
  }

  return {
    PILL_CLASSES,
    escapeHtml,
    pill,
    driftCard,
    mappingRow,
    orderCard,
    orderDetail,
    customerDetail,
    inboxCard,
    auditCard,
  };
});
