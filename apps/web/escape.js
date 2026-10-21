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
    "corrected",
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
    invoice: "Probabile fatture",
    payment: "Probabile incassi",
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
    const choices = item && Array.isArray(item.choices) ? item.choices : [];
    const options = [
      `<option value="">Scegli il tipo…</option>`,
      ...choices.map((choice) => `<option value="${escapeHtml(choice.code)}">${escapeHtml(choice.label)}</option>`),
    ].join("");
    return `<p class="profile-label"><span class="profile-label-source">${escapeHtml(source)}</span> <span class="profile-label-text">${escapeHtml(profileLabelText(item))}</span></p><div class="file-type-actions"><button type="button" data-type-ok="${escapeHtml(fileId)}">Corretto</button> <button type="button" class="ghost" data-type-wrong="${escapeHtml(fileId)}">Non è questo tipo</button> <label class="file-type-choice">Tipo corretto <select data-type-choice="${escapeHtml(fileId)}">${options}</select> <button type="button" disabled>Registra tipo</button></label></div>`;
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

  const DECISION_LABELS = {
    confirm: "Confermata",
    correct: "Corretta",
    reject: "Rifiutata",
    approve: "Approvata",
    modify: "Modificata",
    released: "Rilasciata",
    acknowledged: "Segnata come vista",
    confirmed: "Fusione confermata",
    rejected: "Rifiutata",
    revoked: "Annullata",
  };

  function formatDay(value) {
    const text = value == null ? "" : String(value);
    const match = text.match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (!match) return text;
    return `${match[3]}/${match[2]}/${match[1]}`;
  }

  function euroText(value) {
    if (value == null || value === "") return "";
    const number = Number(value);
    if (!Number.isFinite(number)) return String(value);
    const negative = number < 0;
    const fixed = Math.abs(number).toFixed(2).split(".");
    const whole = fixed[0].replace(/\B(?=(\d{3})+(?!\d))/g, ".");
    return `${negative ? "-" : ""}${whole},${fixed[1]} €`;
  }

  function decisionLabel(code) {
    return Object.prototype.hasOwnProperty.call(DECISION_LABELS, code) ? DECISION_LABELS[code] : String(code ?? "");
  }

  function decisionHistory(entries) {
    const items = (entries || []).map((entry) => {
      const label = decisionLabel(entry && entry.decision);
      const amount = entry && entry.allocated_amount != null && entry.allocated_amount !== ""
        ? ` · ${euroText(entry.allocated_amount)}`
        : "";
      const when = entry && entry.decided_at ? ` · ${formatDay(entry.decided_at)}` : "";
      const who = entry && entry.actor ? ` · ${entry.actor}` : "";
      const text = `${label}${amount}${who}${when}`;
      const line = entry && entry.revoked
        ? `<span class="revoked-decision">${escapeHtml(text)}</span>`
        : escapeHtml(text);
      const revoker = entry && entry.revoked
        ? `<span class="revoker-line">Annullata da ${escapeHtml(entry.revoker)} il ${escapeHtml(formatDay(entry.revoked_at))}</span>`
        : "";
      const motive = entry && entry.revoke_reason
        ? `<span class="revoke-reason">${escapeHtml(entry.revoke_reason)}</span>`
        : "";
      return `<li>${line}${revoker}${motive}</li>`;
    });
    if (!items.length) return "";
    return `<ul class="decision-history">${items.join("")}</ul>`;
  }

  function revokeFields(decisionId, kind) {
    return `<label class="revoke-reason">Motivo dell'annullamento <input data-revoke-reason="${escapeHtml(decisionId)}" data-revoke-kind="${escapeHtml(kind)}" required minlength="5" maxlength="500" /></label> <button type="button" class="ghost" data-revoke-${escapeHtml(kind)}="${escapeHtml(decisionId)}">Annulla</button>`;
  }

  function revokeBar(item, canWrite, kind) {
    if (canWrite === false) return "";
    if (item && item.executed) {
      const message = item.executed_message || "L'azione è già stata eseguita e non si può annullare da qui.";
      return `<p class="executed-note">${escapeHtml(message)}</p>`;
    }
    const open = (item && item.history || []).filter((entry) => entry && entry.id && !entry.revoked);
    if (!open.length) return "";
    return open.map((entry) => revokeFields(entry.id, kind)).join(" ");
  }

  function keptEffect(item, kind) {
    const revoked = (item && item.history || []).some((entry) => entry && entry.revoked);
    if (!revoked) return "";
    if (kind === "quarantine" && item.status === "released") {
      return `<p class="executed-note">Il documento resta rilasciato.</p>`;
    }
    if (kind === "identity" && item.status === "confirmed") {
      return `<p class="executed-note">I clienti restano uniti.</p>`;
    }
    return "";
  }

  function confirmRevokedNotice(item) {
    const hit = (item && item.history || []).some((entry) => entry && entry.revoked && entry.decision === "confirm");
    if (!hit) return "";
    return `<p class="executed-note">Al prossimo caricamento la colonna torna da decidere; i dati già caricati restano.</p>`;
  }

  function driftCard(item, canWrite) {
    const queued = !item.status || item.status === "open";
    const button = canWrite !== false && queued
      ? `<button type="button" data-drift="${escapeHtml(item.id)}">Segna visto</button>`
      : "";
    return `<article class="card"><strong>Schema drift su ${escapeHtml(item.source_name)}</strong><pre>${escapeHtml(JSON.stringify(item.diff, null, 2))}</pre>${decisionHistory(item.history)}${button}${revokeBar(item, canWrite, "drift")}</article>`;
  }

  function quarantineCard(item, canWrite) {
    const reasons = (item.reasons || []).filter((reason) => reason != null && String(reason) !== "").join("; ");
    let note = "In attesa di conferma, nessuna scrittura ERP.";
    if (reasons) note = reasons;
    else if (item.status === "released") note = "Documento rilasciato.";
    const title = item.order_key || item.source_name;
    const queued = !item.status || item.status === "open";
    const button = canWrite !== false && queued
      ? `<button type="button" data-release="${escapeHtml(item.id)}">Rilascia</button>`
      : "";
    return `<article class="card"><strong>Quarantena ${escapeHtml(title)}</strong><p>${escapeHtml(note)}</p>${decisionHistory(item.history)}${keptEffect(item, "quarantine")}${button}${revokeBar(item, canWrite, "quarantine")}</article>`;
  }

  function identityCard(item, canWrite) {
    const queued = !item.status || item.status === "open";
    const buttons = canWrite !== false && queued
      ? `<button type="button" data-merge="${escapeHtml(item.id)}">Conferma fusione</button> <button type="button" class="ghost" data-split="${escapeHtml(item.id)}">Tieni separati</button>`
      : "";
    return `<article class="card"><strong>Identità ${escapeHtml(item.entity_type)}</strong><p>${escapeHtml(item.reason)} (${escapeHtml(item.score)})</p>${decisionHistory(item.history)}${keptEffect(item, "identity")}${buttons}${revokeBar(item, canWrite, "identity")}</article>`;
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

  // Solo true abilita scrittura. La pagina nasconde i pulsanti di decisione.
  function canWriteDecisions(hasCompany) {
    return hasCompany === true;
  }

  // Testo fisso. La pagina lo assegna con textContent, non come HTML.
  function companyUploadNotice() {
    return "Il tuo utente non è ancora collegato a un'azienda: chiedi all'amministratore";
  }

  function measuredThreshold(item) {
    if (!item || typeof item !== "object" || item.measured !== true) return null;
    const support = Number(item.support);
    if (!Number.isFinite(support)) return null;
    return `soglia misurata su ${support} casi`;
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
      const note = item.band_note ? String(item.band_note) : "";
      return { band: "Suggerimento forte", note };
    }
    return { band: "Da verificare", note: "" };
  }

  const MAPPING_HEADERS = {
    source: "Fonte",
    column: "Colonna",
    destination: "Destinazione",
    suggestion: "Suggerimento",
    status: "Stato",
  };

  function mappingRow(item, canWrite) {
    const writable = canWrite !== false;
    const confirm = writable && item.status === "proposed"
      ? `<button type="button" data-confirm="${escapeHtml(item.id)}">Conferma</button> <button type="button" class="ghost" data-reject="${escapeHtml(item.id)}">Rifiuta</button>`
      : "";
    const revoke = writable && item.origin === "learned" && item.decision_id
      ? `<label class="revoke-reason">Motivo dell'annullamento <input data-revoke-reason="${escapeHtml(item.decision_id)}" required minlength="5" maxlength="500" /></label> <button type="button" class="ghost" data-revoke="${escapeHtml(item.decision_id)}">Annulla correzione</button>`
      : "";
    const decided = revokeBar({ history: item.history, executed: item.executed, executed_message: item.executed_message }, canWrite, "mapping");
    const actions = [confirm, revoke, decided].filter(Boolean).join(" ");
    const shown = suggestionBand(item);
    const note = shown.note
      ? `<p class="suggestion-note">${escapeHtml(shown.note)}</p>`
      : "";
    return `<tr>
        <td class="mapping-source" data-label="${MAPPING_HEADERS.source}">${escapeHtml(item.source_name)}</td>
        <td class="mapping-column" data-label="${MAPPING_HEADERS.column}"><code>${escapeHtml(item.source_field)}</code></td>
        <td class="mapping-destination" data-label="${MAPPING_HEADERS.destination}">${escapeHtml(item.target_type)}.${escapeHtml(item.target_property)}</td>
        <td class="mapping-confidence" data-label="${MAPPING_HEADERS.suggestion}"><p class="origin-line">${escapeHtml(originText(item))}</p><p class="suggestion-band">${escapeHtml(shown.band)}</p>${note}</td>
        <td class="mapping-status" data-label="${MAPPING_HEADERS.status}">${pill(item.status)}${decisionHistory(item.history)}${confirmRevokedNotice(item)}</td>
        <td class="mapping-actions">${actions}</td>
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

  function customerDetail(customer, role, canWrite) {
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
    ${role === "admin" && canWrite !== false ? `<label>Motivo dello smascheramento <input data-reason required minlength="5" maxlength="500" /></label><p><button type="button" id="unmask-pii" data-entity="${escapeHtml(customer.id)}">Mostra dati personali</button></p><pre id="unmask-result"></pre>` : ""}
  `;
  }

  function readableCard(item) {
    return `<article class="card"><div class="meta">${pill(item.action)}<span class="pill">${escapeHtml(item.who || "")}</span></div><p class="audit-when">${escapeHtml(item.when || "")}</p><p class="audit-what">${escapeHtml(item.what || item.action || "")}</p><p class="audit-order">Ordine ${escapeHtml(item.order || "n/d")}</p><p class="audit-detail">${escapeHtml(item.detail || "")}</p></article>`;
  }

  const CRITERION_LABELS = {
    amount: "Importo",
    customer: "Cliente",
    reference: "Riferimento in causale",
  };

  function criteriaMarkup(criteria) {
    if (!criteria || typeof criteria !== "object") return "";
    const parts = ["amount", "customer", "reference"].map((key) => {
      const flag = criteria[key] === true ? "sì" : "no";
      return `${CRITERION_LABELS[key]}: ${flag}`;
    });
    return `<p class="match-criteria">${escapeHtml(parts.join(" · "))}</p>`;
  }

  function failedMarkup(failed) {
    if (!Array.isArray(failed) || !failed.length) return "";
    const bits = failed.map((item) => escapeHtml(item));
    return `<p class="failed-criteria">${bits.join(", ")}</p>`;
  }

  function invoiceNumberValue(item) {
    const invoice = item && item.invoice;
    if (invoice && invoice.number != null && invoice.number !== "") return invoice.number;
    return "";
  }

  function allocationAmountValue(item) {
    const allocation = item && item.allocation;
    if (allocation && allocation.amount != null && allocation.amount !== "") return allocation.amount;
    return item && item.allocated_amount != null ? item.allocated_amount : "";
  }

  function matchLine(label, value) {
    return `<li>${escapeHtml(label)} ${escapeHtml(value ?? "")}</li>`;
  }

  function matchBlocks(item) {
    const payment = item && item.payment && typeof item.payment === "object" ? item.payment : {};
    const invoice = item && item.invoice && typeof item.invoice === "object" ? item.invoice : {};
    const allocation = item && item.allocation && typeof item.allocation === "object" ? item.allocation : {};
    return `<h3 class="section-heading">Incasso</h3>
      <ul class="fact-list">
        ${matchLine("Numero", payment.number)}
        ${matchLine("Data", payment.paid_at)}
        ${matchLine("Importo", payment.amount)}
      </ul>
      <h3 class="section-heading">Fattura proposta</h3>
      <ul class="fact-list">
        ${matchLine("Numero", invoice.number)}
        ${matchLine("Importo", invoice.amount)}
        ${matchLine("Scadenza", invoice.due_at)}
      </ul>
      <h3 class="section-heading">Assegnazione</h3>
      <ul class="fact-list">
        ${matchLine("Importo assegnato", allocation.amount)}
        ${matchLine("Residuo attuale", euroText(allocation.remaining))}
      </ul>
      <h3 class="section-heading">Criteri</h3>
      ${criteriaMarkup(item && item.criteria)}`;
  }

  function paymentButtons(item, role, canWrite) {
    if (canWrite === false) {
      return `<p>${escapeHtml(companyUploadNotice())}</p>`;
    }
    if (role !== "approver" && role !== "admin") {
      return "<p>Sola lettura: l'operatore non approva.</p>";
    }
    if (item.status !== "proposed") return "";
    return `<label>Fattura <input data-invoice value="${escapeHtml(invoiceNumberValue(item))}" /></label>
      <label>Importo assegnato <input data-amount value="${escapeHtml(allocationAmountValue(item))}" /></label>
      <label>Motivo della correzione <input data-correct-reason required minlength="5" maxlength="500" /></label>
      <label>Motivo del rifiuto <input data-reject-reason required minlength="5" maxlength="500" /></label>
      <div class="decision-bar">
        <button type="button" data-act="confirm-match">Conferma</button>
        <button type="button" class="ghost" data-act="correct-match">Correggi</button>
        <button type="button" class="ghost" data-act="reject-match">Rifiuta</button>
      </div>`;
  }

  function inboxCard(item, role, canWrite) {
    const purchase = (item.actions || []).find((action) => action.type === "createPurchaseOrder");
    const qty = purchase?.params.quantity ?? "";
    const preview = item.preview || {};
    const payloads = preview.payloads || item.dry_run || [];
    const evidence = (item.evidence || [])
      .map((fact) => `<li>${escapeHtml(fact.property)}: ${escapeHtml(fact.value)}<p>${escapeHtml(provenanceLabel(fact.source_class))}</p></li>`)
      .join("");
    const cfo = item.requires_cfo ? '<span class="pill high">CFO</span>' : "";
    const blocked = item.blocked_reason ? String(item.blocked_reason) : "";
    const pending = item.status === "proposed" || item.status === "detected";
    const approvalRevoked = (item.history || []).some((entry) => entry && entry.revoked);
    const executeButton = !approvalRevoked && (item.status === "dry_run_ok" || item.status === "executed")
      ? '<button type="button" data-act="execute">Esegui</button>'
      : "";
    const approveButton = blocked
      ? `<button type="button" data-act="approve" disabled>Approva</button> <span class="blocked-reason">${escapeHtml(blocked)}</span>`
      : `<button type="button" data-act="approve">Approva</button>`;
    const payment = item.kind === "payment_match";
    let buttons;
    if (payment) {
      buttons = paymentButtons(item, role, canWrite);
    } else if (canWrite === false) {
      buttons = `<p>${escapeHtml(companyUploadNotice())}</p>`;
    } else if (role === "operator") {
      buttons = "<p>Sola lettura: l'operatore non approva.</p>";
    } else if (role === "approver" || role === "admin") {
      const decide = pending
        ? `<label>Quantità acquisto <input data-qty value="${escapeHtml(qty)}" /></label>
      <label>Motivo del rifiuto <input data-reason required minlength="5" maxlength="500" /></label>
      <div class="decision-bar">
        ${approveButton}
        <button type="button" class="ghost" data-act="modify">Modifica</button>
        <button type="button" class="ghost" data-act="reject">Rifiuta</button>
      </div>`
        : "";
      const run = executeButton ? `<div class="decision-bar">${executeButton}</div>` : "";
      buttons = `${decide}${run}`;
    } else {
      buttons = "<p>Sola lettura: l'operatore non approva.</p>";
    }
    const title = item && typeof item.headline === "string" && item.headline !== ""
      ? item.headline
      : `Ordine ${item.order_key ?? ""}`;
    const processLabel = item && item.process && typeof item.process === "object" ? item.process.label : "";
    const stepLabel = item && item.step && typeof item.step === "object" ? item.step.label : "";
    const processBits = [processLabel, stepLabel].filter((part) => typeof part === "string" && part !== "");
    const processLine = processBits.length
      ? `<p class="process-step">${escapeHtml(processBits.join(" · "))}</p>`
      : "";
    const body = payment
      ? `<p>${escapeHtml(item.problem)}</p>
      <p class="confidence-badge">${escapeHtml(confidenceLabel(item))}</p>
      ${matchBlocks(item)}`
      : `<h3 class="section-heading">Anteprima di approvazione</h3>
      <p>${escapeHtml(item.problem)}</p>
      <p class="confidence-badge">${escapeHtml(confidenceLabel(item))}</p>
      <p class="formula">${escapeHtml(preview.formula || "")}</p>
      <ul class="fact-list">${evidence}</ul>
      <h3 class="section-heading">Cosa verrà approvato</h3>
      <pre>${escapeHtml(JSON.stringify(payloads, null, 2))}</pre>`;
    const decided = item && item.disposition === "closed" ? " decided" : "";
    return `<article class="card${decided}" data-id="${escapeHtml(item.id)}" data-hash="${escapeHtml(item.evidence_hash)}">
      <div class="meta">${pill(item.status)}${cfo}<span class="pill">${escapeHtml(item.risk)}</span></div>
      <h3 class="card-title">${escapeHtml(title)}</h3>
      ${processLine}
      ${payment ? "" : criteriaMarkup(item.criteria)}
      ${failedMarkup(item.failed_criteria)}
      ${body}
      ${decisionHistory(item.history)}
      ${revokeBar(item, canWrite, payment ? "payment" : "inbox")}
      ${buttons}
    </article>`;
  }

  function ontologyType(ontology, name) {
    const types = ontology && Array.isArray(ontology.types) ? ontology.types : [];
    return types.find((item) => item && item.name === name) || null;
  }

  function fieldCaption(ontology, typeName, field) {
    const info = ontologyType(ontology, typeName);
    const fields = info && Array.isArray(info.fields) ? info.fields : [];
    const found = fields.find((item) => item && item.name === field.name);
    if (found && typeof found.label === "string" && found.label !== "") return found.label;
    if (field && typeof field.label === "string" && field.label !== "") return field.label;
    return field && field.name ? String(field.name) : "";
  }

  function linkText(ontology, link) {
    const rels = ontology && Array.isArray(ontology.relations) ? ontology.relations : [];
    const known = rels.some((item) => item && item.name === link.rel);
    const rel = known ? link.rel : (link && link.rel) || "";
    const info = ontologyType(ontology, link && link.type);
    const typeLabel = info && info.label ? info.label : (link && link.type) || "";
    const key = link && link.key != null ? link.key : "";
    return [rel, typeLabel, key].filter((part) => part !== "" && part != null).join(" ");
  }

  function objectFields(object, ontology) {
    const rows = (object && object.fields || []).map((field) => {
      const caption = fieldCaption(ontology, object.type, field);
      const value = field && field.value != null ? field.value : "";
      return `<li>${escapeHtml(caption)}: ${escapeHtml(value)}</li>`;
    }).join("");
    return `${rows}${remainingLine(object)}`;
  }

  function remainingLine(object) {
    if (!object || object.type !== "Invoice" || object.remaining == null || object.remaining === "") return "";
    return `<li>${escapeHtml("Residuo attuale")}: ${escapeHtml(euroText(object.remaining))}</li>`;
  }

  function objectLinks(object, ontology) {
    return (object && object.links || []).map((link) => {
      const text = linkText(ontology, link);
      return `<li><button type="button" data-object="${escapeHtml(link && link.id)}">${escapeHtml(text)}</button></li>`;
    }).join("");
  }

  function historyLine(entry) {
    const code = entry && entry.decision ? String(entry.decision) : "";
    const label = code && Object.prototype.hasOwnProperty.call(DECISION_LABELS, code)
      ? DECISION_LABELS[code]
      : (entry && entry.label != null ? String(entry.label) : code);
    const amount = entry && entry.amount != null && entry.amount !== "" ? euroText(entry.amount) : "";
    const who = entry && entry.actor ? String(entry.actor) : "";
    const when = formatDay(entry && entry.at);
    return [label, amount, who, when].filter((part) => part !== "").join(" · ");
  }

  function objectCard(object, ontology) {
    const info = ontologyType(ontology, object && object.type);
    const typeLabel = info && info.label ? info.label : (object && object.type) || "";
    const links = objectLinks(object, ontology);
    return `<article class="card">
      <h3 class="card-title">${escapeHtml(typeLabel)} ${escapeHtml(object && object.business_key || "")}</h3>
      <ul class="fact-list">${objectFields(object, ontology)}</ul>
      ${links ? `<h3 class="section-heading">Collegamenti</h3><ul class="edges">${links}</ul>` : ""}
      <button type="button" data-object="${escapeHtml(object && object.id)}">Apri scheda</button>
    </article>`;
  }

  function objectDetail(object, ontology) {
    const info = ontologyType(ontology, object && object.type);
    const typeLabel = info && info.label ? info.label : (object && object.type) || "";
    const links = objectLinks(object, ontology);
    const history = (object && object.history || []).map((entry) => {
      return `<li>${escapeHtml(historyLine(entry))}</li>`;
    }).join("");
    return `<p><button type="button" id="back-objects" class="ghost">Torna agli oggetti</button></p>
      <h2>${escapeHtml(typeLabel)} ${escapeHtml(object && object.business_key || "")}</h2>
      <ul class="fact-list">${objectFields(object, ontology)}</ul>
      ${links ? `<h3 class="section-heading">Collegamenti</h3><ul class="edges">${links}</ul>` : ""}
      ${history ? `<h3 class="section-heading">Storia</h3><ul class="decision-history">${history}</ul>` : ""}`;
  }

  function plainRatio(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "n/d";
    return String(number).replace(".", ",");
  }

  function measureText(report) {
    const recall = plainRatio(report && report.recall);
    const share = plainRatio(report && report.unmatched_share);
    return `richiamo sui casi controllati: ${recall}. quota di incassi lasciati senza abbinamento: ${share}`;
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
    showFixtureButton,
    companyUploadNotice,
    canWriteDecisions,
    factList,
    driftCard,
    quarantineCard,
    identityCard,
    decisionHistory,
    mappingRow,
    profileLabelText,
    tableTypeMarkup,
    originText,
    orderCard,
    orderDetail,
    customerDetail,
    objectCard,
    objectDetail,
    inboxCard,
    measureText,
    auditCard,
    readableCard,
  };
});
