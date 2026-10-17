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
    "pii_unmask",
    "n/d",
  ]);

  const PROFILE_LABELS = {
    customer: "Probabile anagrafica clienti",
    order: "Probabile export ordini",
    order_line: "Probabile righe d'ordine",
    product: "Probabile anagrafica prodotti",
    stock: "Probabile giacenze",
    supplier: "Probabile anagrafica fornitori",
    shipment: "Probabile spedizioni",
  };
  const UNKNOWN_PROFILE_LABEL = "Tipo non riconosciuto";

  function profileLabelText(item) {
    const allowed = new Set(Object.values(PROFILE_LABELS));
    allowed.add(UNKNOWN_PROFILE_LABEL);
    const fromServer = item && typeof item === "object" ? item.profile_label : "";
    if (typeof fromServer === "string" && allowed.has(fromServer)) {
      return fromServer;
    }
    const code = item && typeof item === "object" ? item.profile : "";
    if (Object.prototype.hasOwnProperty.call(PROFILE_LABELS, code)) {
      return PROFILE_LABELS[code];
    }
    return UNKNOWN_PROFILE_LABEL;
  }

  // Usato solo dai test anti-XSS. La pagina assegna il testo con textContent.
  function tableTypeMarkup(item) {
    const source = item && typeof item === "object" ? item.source_name : "";
    const fileId = item && typeof item === "object" ? item.file_id : "";
    const options = fileTypeChoices()
      .map((choice) => `<option value="${escapeHtml(choice.code)}">${escapeHtml(choice.label)}</option>`)
      .join("");
    return `<p class="profile-label"><span class="profile-label-source">${escapeHtml(source)}</span> <span class="profile-label-text">${escapeHtml(profileLabelText(item))}</span></p><div class="file-type-actions"><button type="button" data-type-ok="${escapeHtml(fileId)}">Corretto</button> <button type="button" class="ghost" data-type-wrong="${escapeHtml(fileId)}">Non è questo tipo</button> <label class="file-type-choice">Tipo corretto <select data-type-choice="${escapeHtml(fileId)}">${options}</select></label></div>`;
  }

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

  const PROVENANCE_LABELS = {
    system_of_record: "sistema di riferimento",
    operational: "operativa",
    external: "esterna",
    manual: "manuale",
  };

  function provenanceLabel(sourceClass) {
    if (sourceClass == null || sourceClass === "" || sourceClass === "unknown") {
      return "provenienza sconosciuta";
    }
    return PROVENANCE_LABELS[sourceClass] || "provenienza sconosciuta";
  }

  function decimalScore(confidence) {
    const number = Number(confidence);
    if (!Number.isFinite(number)) return null;
    return number.toFixed(2).replace(".", ",");
  }

  function confidenceLabel(fact) {
    const method = fact && typeof fact === "object" ? fact.confidence_method : null;
    if (method === "human") {
      return "rettifica manuale";
    }
    const confidence = fact && typeof fact === "object" ? fact.confidence : fact;
    if (method === "rule" && confidence != null && confidence !== "") {
      const phrase = measuredThreshold(fact);
      if (phrase) return phrase;
      const score = decimalScore(confidence);
      if (score != null) {
        const rule = fact.confidence_rule ? ` · ${fact.confidence_rule}` : "";
        return `${score} · punteggio di regola, non calibrato${rule}`;
      }
    }
    return "confidenza non disponibile";
  }

  function recommendationConfidence(confidence) {
    if (confidence == null || confidence === "") {
      return "confidenza non disponibile";
    }
    const number = Number(confidence);
    if (!Number.isFinite(number)) {
      return "confidenza non disponibile";
    }
    return number.toFixed(2).replace(".", ",");
  }

  function showFixtureButton(role, demoMode) {
    return role === "admin" && demoMode === true;
  }

  function measuredThreshold(item) {
    if (!item || typeof item !== "object" || item.measured !== true) return null;
    const support = Number(item.support);
    if (!Number.isFinite(support)) return null;
    return `soglia misurata su ${support} casi`;
  }

  function mappingConfidence(item) {
    const phrase = measuredThreshold(item);
    if (phrase) return phrase;
    const confidence = item && typeof item === "object" ? item.confidence : item;
    if (confidence == null || confidence === "") {
      return "confidenza non disponibile";
    }
    const number = Number(confidence);
    if (!Number.isFinite(number)) {
      return "confidenza non disponibile";
    }
    return number.toFixed(2);
  }

  function factValue(value) {
    if (value != null && typeof value === "object") {
      return JSON.stringify(value);
    }
    return value;
  }

  function factList(facts) {
    const items = (facts || []).map((fact) => {
      return `<li><code>${escapeHtml(fact.property)}</code>: ${escapeHtml(factValue(fact.value))}<p>${escapeHtml(provenanceLabel(fact.source_class))}</p><p class="fact-confidence">${escapeHtml(confidenceLabel(fact))}</p></li>`;
    });
    return `<ul class="edges fact-list">${items.join("") || "<li>Nessun fatto</li>"}</ul>`;
  }

  function originText(item) {
    if (item && item.origin === "learned" && item.origin_text) {
      return String(item.origin_text);
    }
    return "Da regola";
  }

  function suggestionBand(item) {
    if (item && item.band === "forte") {
      return {
        band: "Suggerimento forte",
        note: "Indicazione automatica, da verificare",
      };
    }
    return { band: "Da verificare", note: "" };
  }

  function fileTypeChoices() {
    const choices = Object.keys(PROFILE_LABELS).map((code) => ({
      code,
      label: PROFILE_LABELS[code],
    }));
    choices.push({ code: "non_so", label: "Non so" });
    return choices;
  }

  function mappingRow(item) {
    const confirm = item.status === "proposed"
      ? `<button type="button" data-confirm="${escapeHtml(item.id)}">Conferma</button> <button type="button" class="ghost" data-reject="${escapeHtml(item.id)}">Rifiuta</button>`
      : "";
    const revoke = item.origin === "learned" && item.decision_id
      ? `<button type="button" class="ghost" data-revoke="${escapeHtml(item.decision_id)}">Annulla correzione</button>`
      : "";
    const actions = [confirm, revoke].filter(Boolean).join(" ");
    const shown = suggestionBand(item);
    const note = shown.note
      ? `<p class="suggestion-note">${escapeHtml(shown.note)}</p>`
      : "";
    return `<tr>
        <td>${escapeHtml(item.source_name)}</td>
        <td><code>${escapeHtml(item.source_field)}</code></td>
        <td>${escapeHtml(item.target_type)}.${escapeHtml(item.target_property)}</td>
        <td class="mapping-confidence"><p class="origin-line">${escapeHtml(originText(item))}</p><p class="suggestion-band">${escapeHtml(shown.band)}</p>${note}</td>
        <td>${pill(item.status)}</td>
        <td>${actions}</td>
      </tr>`;
  }

  function orderCard(order) {
    return `<article class="card">
      <h3 class="card-title">Ordine ${escapeHtml(order.business_key)}</h3>
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
    ${factList(order.facts)}
  `;
  }

  function customerDetail(customer, role) {
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
    <h3>Fatti</h3>
    ${factList(customer.facts)}
    ${role === "admin" ? `<label>Motivo dello smascheramento <input data-reason required minlength="5" maxlength="500" /></label><p><button type="button" id="unmask-pii" data-entity="${escapeHtml(customer.id)}">Mostra dati personali</button></p><pre id="unmask-result"></pre>` : ""}
  `;
  }

  function readableCard(item) {
    return `<article class="card"><div class="meta">${pill(item.action)}<span class="pill">${escapeHtml(item.who || "")}</span></div><p class="audit-when">${escapeHtml(item.when || "")}</p><p class="audit-what">${escapeHtml(item.what || item.action || "")}</p><p class="audit-order">Ordine ${escapeHtml(item.order || "n/d")}</p><p class="audit-detail">${escapeHtml(item.detail || "")}</p></article>`;
  }

  function inboxCard(item, role) {
    const purchase = (item.actions || []).find((action) => action.type === "createPurchaseOrder");
    const qty = purchase?.params.quantity ?? "";
    const preview = item.preview || {};
    const payloads = preview.payloads || item.dry_run || [];
    const evidence = (item.evidence || [])
      .map((fact) => `<li>${escapeHtml(fact.property)}: ${escapeHtml(fact.value)}<p>${escapeHtml(provenanceLabel(fact.source_class))}</p></li>`)
      .join("");
    const canDecide = role === "approver" || role === "admin";
    const cfo = item.requires_cfo ? '<span class="pill high">CFO</span>' : "";
    const blocked = item.blocked_reason ? String(item.blocked_reason) : "";
    const executeButton = item.status === "dry_run_ok" || item.status === "executed"
      ? '<button type="button" data-act="execute">Esegui</button>'
      : "";
    const approveButton = blocked
      ? `<button type="button" data-act="approve" disabled>Approva</button> <span class="blocked-reason">${escapeHtml(blocked)}</span>`
      : `<button type="button" data-act="approve">Approva</button>`;
    const buttons = canDecide
      ? `<label>Quantità acquisto <input data-qty value="${escapeHtml(qty)}" /></label>
      <label>Motivo del rifiuto <input data-reason required minlength="5" maxlength="500" /></label>
      <div class="decision-bar">
        ${approveButton}
        <button type="button" class="ghost" data-act="modify">Modifica</button>
        <button type="button" class="ghost" data-act="reject">Rifiuta</button>
        ${executeButton}
      </div>`
      : "<p>Sola lettura: l'operatore non approva.</p>";
    return `<article class="card" data-id="${escapeHtml(item.id)}" data-hash="${escapeHtml(item.evidence_hash)}">
      <div class="meta">${pill(item.status)}${cfo}<span class="pill">${escapeHtml(item.risk)}</span></div>
      <h3 class="card-title">Ordine ${escapeHtml(item.order_key)}</h3>
      <h3 class="section-heading">Anteprima di approvazione</h3>
      <p>${escapeHtml(item.problem)}</p>
      <p class="confidence-badge">${escapeHtml(confidenceLabel(item))}</p>
      <p class="formula">${escapeHtml(preview.formula || "")}</p>
      <ul class="fact-list">${evidence}</ul>
      <h3 class="section-heading">Cosa verrà approvato</h3>
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
    provenanceLabel,
    confidenceLabel,
    recommendationConfidence,
    mappingConfidence,
    showFixtureButton,
    factList,
    driftCard,
    mappingRow,
    profileLabelText,
    tableTypeMarkup,
    fileTypeChoices,
    originText,
    orderCard,
    orderDetail,
    customerDetail,
    inboxCard,
    auditCard,
    readableCard,
  };
});
