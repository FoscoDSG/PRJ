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

  function withBreaks(value) {
    return escapeHtml(value).replaceAll(".", ".\u200b").replaceAll("_", "_\u200b");
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function pill(text, label) {
    const raw = text == null ? "" : String(text);
    const extra = PILL_CLASSES.has(raw) ? ` ${raw}` : "";
    const shown = label == null ? raw : label;
    return `<span class="pill${extra}">${escapeHtml(shown)}</span>`;
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
    const text = value == null ? "" : String(value).trim();
    if (!text) return "";
    const single = text.match(/^(\d{4})-(\d{2})-(\d{2})(?:[T\s].*)?$/);
    if (single) return `${single[3]}/${single[2]}/${single[1]}`;
    if (text.includes(",")) {
      return text.split(",").map((part) => formatDay(part.trim())).join(", ");
    }
    return text;
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

  // Una sola mappa per le parole che la pagina mostra. Le chiavi restano quelle del server.
  const PAGE_TEXT = {
    values: {
      direction: { incoming: "In entrata", outgoing: "In uscita" },
      status: {
        closed: "Chiuso",
        issued: "Emessa",
        received: "Ricevuto",
        proposed: "Proposto",
        confirmed: "Confermato",
        rejected: "Rifiutato",
        approved: "Approvato",
        executed: "Eseguito",
        failed: "Fallito",
        paid: "Pagato",
        pending: "In attesa",
      },
      byType: {
        Order: { status: { open: "Aperto", closed: "Chiuso" } },
        Invoice: { status: { open: "Aperta", closed: "Chiusa", issued: "Emessa", paid: "Pagata" } },
        CreditNote: { status: { open: "Aperta" } },
        Payment: { status: { open: "In corso", received: "Ricevuto" } },
        Shipment: { status: { open: "In corso" } },
      },
    },
    units: { margin: "percent" },
    fields: {
      name: "Nome",
      title: "Titolo",
      full_name: "Nome e cognome",
      amount: "Importo",
      source_key: "Chiave",
      status: "Stato",
      email: "Email",
      vat: "Partita IVA",
      company_name: "Ragione sociale",
      customer_vat: "Partita IVA cliente",
      customer_name: "Nome cliente",
      document_type: "Tipo documento",
      invoice_source_key: "Numero fattura",
      issued_at: "Data",
      reason: "Motivo",
      currency: "Valuta",
      due_at: "Scadenza",
      scadenze: "Scadenze",
      order_source_key: "Numero ordine",
      description: "Descrizione",
      line_amount: "Importo riga",
      quantity: "Quantità",
      unit_price: "Prezzo unitario",
      vat_rate: "Aliquota IVA",
      event_type: "Tipo evento",
      occurred_at: "Data evento",
      process_id: "Processo",
      step_id: "Passo",
      summary: "Sintesi",
      commessa: "Commessa",
      customer_source_key: "Cliente",
      margin: "Margine",
      ordered_at: "Data ordine",
      shortage_qty: "Quantità mancante",
      product_source_key: "Prodotto",
      shortage: "Mancanza",
      causale: "Causale",
      counterparty_name: "Nome controparte",
      counterparty_vat: "Partita IVA controparte",
      direction: "Direzione",
      iban: "IBAN",
      paid_at: "Data incasso",
      reference: "Riferimento",
      allocated_amount: "Importo assegnato",
      payment_source_key: "Incasso",
      cost: "Costo",
      shipped_at: "Data spedizione",
      available: "Disponibile",
      on_hand: "Giacenza",
      reserved: "Impegnato",
      warehouse_source_key: "Magazzino",
      delay_days: "Giorni di ritardo",
    },
    overrides: {
      "Order.amount": "Importo ordine",
      "Order.source_key": "Numero ordine",
    },
    headings: {
      links: "Collegamenti",
      facts: "Fatti",
      noLink: "Nessun collegamento",
      available: "Disponibile",
    },
    links: { out: "collegato a", in: "collegato da" },
    phrases: {
      places: { out: "effettua", in: "effettuato da" },
      contains: { out: "contiene", in: "contenuto in" },
      suppliedBy: { out: "fornito da", in: "fornisce" },
      storedAt: { out: "depositato in", in: "ospita" },
      triggered: { out: "genera", in: "generato da" },
      generates: { out: "genera", in: "generata da" },
      fulfills: { out: "evade", in: "evaso da" },
      allocates: { out: "assegna", in: "assegnato da" },
      settles: { out: "salda", in: "saldato da" },
      sameOrganization: { out: "stessa organizzazione di", in: "stessa organizzazione di" },
    },
    relations: {
      places: "effettua",
      contains: "contiene",
      suppliedBy: "fornito da",
      storedAt: "depositato in",
      triggered: "genera evento",
      generates: "genera",
      fulfills: "evade",
      allocates: "assegna",
      settles: "salda",
      sameOrganization: "stessa organizzazione",
    },
    types: {
      Customer: "Cliente",
      Product: "Prodotto",
      Order: "Ordine",
      OrderLine: "Riga d'ordine",
      Invoice: "Fattura",
      Payment: "Incasso",
      Shipment: "Spedizione",
      Supplier: "Fornitore",
      Warehouse: "Magazzino",
    },
    reservedDirection: "Direzione riservata",
  };

  function pageValue(name, value, typeName, ontology) {
    const raw = value == null ? "" : String(value);
    const fromType = valueLabel(ontology, typeName, name, raw);
    if (fromType) return fromType;
    const reserveType = PAGE_TEXT.values.byType[typeName];
    const reserveTyped = reserveType && reserveType[name];
    if (reserveTyped && Object.prototype.hasOwnProperty.call(reserveTyped, raw)) return reserveTyped[raw];
    if (name === "status" && raw === "open" && typeName && typeName !== "Order") return raw;
    const group = PAGE_TEXT.values[name];
    if (group && Object.prototype.hasOwnProperty.call(group, raw)) return group[raw];
    return raw;
  }

  function valueLabel(ontology, typeName, name, raw) {
    const book = ontology && ontology.value_labels;
    const typed = book && typeName && book[typeName];
    const labels = typed && name && typed[name];
    if (labels && Object.prototype.hasOwnProperty.call(labels, raw) && labels[raw] !== "") return labels[raw];
    return "";
  }

  function pageLabel(name, typeName, ontology) {
    const info = ontologyType(ontology, typeName);
    const fields = info && Array.isArray(info.fields) ? info.fields : [];
    const found = fields.find((item) => item && item.name === name);
    if (found && typeof found.label === "string" && found.label !== "") return found.label;
    const override = typeName && name ? PAGE_TEXT.overrides[`${typeName}.${name}`] : "";
    if (override) return override;
    const labels = PAGE_TEXT.fields;
    if (name && Object.prototype.hasOwnProperty.call(labels, name)) return labels[name];
    return name == null ? "" : String(name);
  }

  function pageRelation(name, ontology) {
    const rels = ontology && Array.isArray(ontology.relations) ? ontology.relations : [];
    const found = rels.find((item) => item && item.name === name);
    if (found && typeof found.label === "string" && found.label !== "") return found.label;
    const labels = PAGE_TEXT.relations;
    if (name && Object.prototype.hasOwnProperty.call(labels, name)) return labels[name];
    return name == null ? "" : String(name);
  }

  function pageType(name, ontology) {
    const info = ontologyType(ontology, name);
    if (info && typeof info.label === "string" && info.label !== "") return info.label;
    const labels = PAGE_TEXT.types;
    if (name && Object.prototype.hasOwnProperty.call(labels, name)) return labels[name];
    return name == null ? "" : String(name);
  }

  function unitOf(ontology, typeName, name) {
    const info = ontologyType(ontology, typeName);
    const fields = info && Array.isArray(info.fields) ? info.fields : [];
    const found = fields.find((item) => item && item.name === name);
    if (found && typeof found.unit === "string" && found.unit !== "") return found.unit;
    if (ontology) return "";
    const units = PAGE_TEXT.units || {};
    if (typeName && name && units[`${typeName}.${name}`]) return units[`${typeName}.${name}`];
    if (name && units[name]) return units[name];
    return "";
  }

  function percentText(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return String(value);
    const scaled = Math.round(number * 1000) / 10;
    const text = scaled.toFixed(1).replace(".", ",");
    const shown = text.endsWith(",0") ? text.slice(0, -2) : text;
    return `${shown} %`;
  }

  function decisionLabel(code) {
    return Object.prototype.hasOwnProperty.call(DECISION_LABELS, code) ? DECISION_LABELS[code] : String(code ?? "");
  }

  function defaultHistoryText(entry) {
    const label = decisionLabel(entry && entry.decision);
    const amount = entry && entry.allocated_amount != null && entry.allocated_amount !== ""
      ? ` · ${euroText(entry.allocated_amount)}`
      : "";
    const when = entry && entry.decided_at ? ` · ${formatDay(entry.decided_at)}` : "";
    const who = entry && entry.actor ? ` · ${entry.actor}` : "";
    return `${label}${amount}${who}${when}`;
  }

  function rejectedHistoryText(entry) {
    const who = entry && entry.actor != null ? String(entry.actor).trim() : "";
    const whenRaw = entry && entry.decided_at != null ? String(entry.decided_at).trim() : "";
    const when = whenRaw ? formatDay(whenRaw) : "";
    if (who && when) return `Rifiutato da ${who} il ${when}`;
    if (who) return `Rifiutato da ${who}`;
    if (when) return `Rifiutato il ${when}`;
    return "Rifiutato";
  }

  function decisionHistory(entries, kind) {
    const items = (entries || []).map((entry) => {
      const text = kind === "quarantine" && entry && entry.decision === "rejected"
        ? rejectedHistoryText(entry)
        : defaultHistoryText(entry);
      const line = entry && entry.revoked
        ? `<span class="revoked-decision">${escapeHtml(text)}</span>`
        : escapeHtml(text);
      const revoker = revokerLine(entry);
      const motive = entry && entry.revoke_reason
        ? `<span class="revoke-reason">${escapeHtml(entry.revoke_reason)}</span>`
        : "";
      return `<li>${line}${revoker}${motive}</li>`;
    });
    if (!items.length) return "";
    return `<ul class="decision-history">${items.join("")}</ul>`;
  }

  function revokerLine(entry) {
    if (!entry || !entry.revoked) return "";
    const who = entry.revoker != null ? String(entry.revoker).trim() : "";
    const whenRaw = entry.revoked_at != null ? String(entry.revoked_at).trim() : "";
    const when = whenRaw ? formatDay(whenRaw) : "";
    if (!who && !when) return "";
    if (who && when) {
      return `<span class="revoker-line">Annullata da ${escapeHtml(who)} il ${escapeHtml(when)}</span>`;
    }
    if (who) return `<span class="revoker-line">Annullata da ${escapeHtml(who)}</span>`;
    return `<span class="revoker-line">Annullata il ${escapeHtml(when)}</span>`;
  }

  function revokeFields(decisionId, kind) {
    return `<label class="revoke-reason">Motivo dell'annullamento <input data-revoke-reason="${escapeHtml(decisionId)}" data-revoke-kind="${escapeHtml(kind)}" required minlength="5" maxlength="500" /></label> <button type="button" class="ghost" data-revoke-${escapeHtml(kind)}="${escapeHtml(decisionId)}">Annulla</button>`;
  }

  function allows(item, action) {
    const list = item && item.allowed_actions;
    return Array.isArray(list) && list.includes(action);
  }

  function allowsAction(item, action) {
    return allows(item, action);
  }

  function fileActionBar(file) {
    const id = file && file.file_id != null ? file.file_id : "";
    const parts = [];
    if (allows(file, "correct")) {
      parts.push(`<button type="button" data-type-ok="${escapeHtml(id)}">Corretto</button>`);
    }
    if (allows(file, "wrong") || allows(file, "not_sure")) {
      parts.push(`<button type="button" class="ghost" data-type-wrong="${escapeHtml(id)}">Non è questo tipo</button>`);
    }
    if (!parts.length) return "";
    return `<div class="file-type-actions">${parts.join(" ")}</div>`;
  }

  function revokeBar(item, canWrite, kind, role) {
    if (item && item.executed) {
      const message = item.executed_message || "L'azione è già stata eseguita e non si può annullare da qui.";
      return `<p class="executed-note">${escapeHtml(message)}</p>`;
    }
    if (!allows(item, "revoke")) return "";
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
    if (kind === "quarantine" && item.status === "rejected") {
      return `<p class="executed-note">Il documento resta rifiutato e non torna in coda.</p>`;
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

  function driftCard(item, canWrite, role) {
    const button = allows(item, "acknowledge")
      ? `<button type="button" data-drift="${escapeHtml(item.id)}">Segna visto</button>`
      : "";
    return `<article class="card"><strong>Schema drift su ${escapeHtml(item.source_name)}</strong><pre>${escapeHtml(JSON.stringify(item.diff, null, 2))}</pre>${decisionHistory(item.history)}${button}${revokeBar(item, canWrite, "drift", role)}</article>`;
  }

  function quarantineCard(item, canWrite, role) {
    const reasons = (item.reasons || []).filter((reason) => reason != null && String(reason) !== "").join("; ");
    let note = "In attesa di conferma, nessuna scrittura ERP.";
    if (item.status === "rejected") note = "";
    else if (reasons) note = reasons;
    else if (item.status === "released") note = "Documento rilasciato.";
    const title = item.order_key || item.source_name;
    const release = allows(item, "release")
      ? `<button type="button" data-release="${escapeHtml(item.id)}">Rilascia</button>`
      : "";
    const reject = allows(item, "reject")
      ? `<label class="revoke-reason">Motivo del rifiuto <input data-quarantine-reason-input="${escapeHtml(item.id)}" required minlength="5" maxlength="500" /></label> <button type="button" class="ghost" data-reject-quarantine="${escapeHtml(item.id)}">Rifiuta</button>`
      : "";
    const reasonText = item && item.reason != null ? String(item.reason).trim() : "";
    const reasonSlot = item.status === "rejected" && reasonText
      ? `<p class="quarantine-reason" data-quarantine-reason="${escapeHtml(item.id)}"></p>`
      : "";
    const button = [release, reject].filter(Boolean).join(" ");
    const noteHtml = note ? `<p>${escapeHtml(note)}</p>` : "";
    return `<article class="card"><strong>Quarantena ${escapeHtml(title)}</strong>${noteHtml}${reasonSlot}${decisionHistory(item.history, "quarantine")}${keptEffect(item, "quarantine")}${button}${revokeBar(item, canWrite, "quarantine", role)}</article>`;
  }

  function identityCard(item, canWrite, role) {
    const confirm = allows(item, "confirm")
      ? `<button type="button" data-merge="${escapeHtml(item.id)}">Conferma fusione</button>`
      : "";
    const reject = allows(item, "reject")
      ? `<button type="button" class="ghost" data-split="${escapeHtml(item.id)}">Tieni separati</button>`
      : "";
    const buttons = [confirm, reject].filter(Boolean).join(" ");
    return `<article class="card"><strong>Identità ${escapeHtml(item.entity_type)}</strong><p>${escapeHtml(item.reason)} (${escapeHtml(item.score)})</p>${decisionHistory(item.history)}${keptEffect(item, "identity")}${buttons}${revokeBar(item, canWrite, "identity", role)}</article>`;
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

  function showFixtureButton(role, demoMode, ieolAdmin) {
    void role;
    return ieolAdmin === true && demoMode === true;
  }

  // Solo true abilita scrittura. La pagina nasconde i pulsanti di decisione.
  function canWriteDecisions(hasCompany) {
    return hasCompany === true;
  }

  // Testo fisso. La pagina lo assegna con textContent, non come HTML.
  function companyUploadNotice() {
    return "Non hai ancora un'azienda assegnata. Chiedi all'amministratore di collegarti.";
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

  function shownFactValue(name, value, typeName, ontology) {
    if (value != null && typeof value === "object") return JSON.stringify(value);
    if (value == null || value === "") return "";
    if (unitOf(ontology, typeName, name) === "percent" && isPlainNumber(value)) return percentText(value);
    if ((name === "amount" || MONEY_FIELDS.has(name)) && isPlainNumber(value)) return euroText(value);
    if (DATE_FIELDS.has(name)) return formatDay(value);
    if (name === "direction" || name === "status") return pageValue(name, value, typeName, ontology);
    return String(value);
  }

  function factList(facts, typeName, ontology) {
    const items = (facts || []).map((fact) => {
      const name = fact && fact.property != null ? String(fact.property) : "";
      const label = pageLabel(name, typeName, ontology);
      const value = shownFactValue(name, fact && fact.value, typeName, ontology);
      return `<li><span hidden><code>${escapeHtml(name)}</code></span>${escapeHtml(label)}: ${escapeHtml(value)}<p>${escapeHtml(provenanceLabel(fact.source_class))}</p><p class="fact-confidence">${escapeHtml(confidenceLabel(fact))}</p></li>`;
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

  function mappingRow(item, canWrite, role) {
    const confirm = allows(item, "confirm")
      ? `<button type="button" data-confirm="${escapeHtml(item.id)}">Conferma</button>`
      : "";
    const reject = allows(item, "reject")
      ? `<button type="button" class="ghost" data-reject="${escapeHtml(item.id)}">Rifiuta</button>`
      : "";
    const pair = [confirm, reject].filter(Boolean).join(" ");
    const revoke = allows(item, "revoke") && item.origin === "learned" && item.decision_id
      ? `<label class="revoke-reason">Motivo dell'annullamento <input data-revoke-reason="${escapeHtml(item.decision_id)}" required minlength="5" maxlength="500" /></label> <button type="button" class="ghost" data-revoke="${escapeHtml(item.decision_id)}">Annulla correzione</button>`
      : "";
    const decided = revokeBar({
      history: item.history,
      executed: item.executed,
      executed_message: item.executed_message,
      allowed_actions: item.allowed_actions,
    }, canWrite, "mapping", role);
    const actions = [pair, revoke, decided].filter(Boolean).join(" ");
    const shown = suggestionBand(item);
    const note = shown.note
      ? `<p class="suggestion-note">${escapeHtml(shown.note)}</p>`
      : "";
    return `<tr>
        <td class="mapping-source" data-label="${MAPPING_HEADERS.source}">${withBreaks(item.source_name)}</td>
        <td class="mapping-column" data-label="${MAPPING_HEADERS.column}"><code>${withBreaks(item.source_field)}</code></td>
        <td class="mapping-destination" data-label="${MAPPING_HEADERS.destination}">${withBreaks(item.target_type)}.\u200b${withBreaks(item.target_property)}</td>
        <td class="mapping-confidence" data-label="${MAPPING_HEADERS.suggestion}"><p class="origin-line">${escapeHtml(originText(item))}</p><p class="suggestion-band">${escapeHtml(shown.band)}</p>${note}</td>
        <td class="mapping-status" data-label="${MAPPING_HEADERS.status}">${pill(item.status)}${decisionHistory(item.history)}${confirmRevokedNotice(item)}</td>
        <td class="mapping-actions">${actions}</td>
      </tr>`;
  }

  function orderCard(order, ontology) {
    const status = order.status || "n/d";
    const amount = isPlainNumber(order.amount) ? euroText(order.amount) : order.amount;
    const marginUnit = unitOf(ontology, "Order", "margin");
    const margin = order.margin == null || order.margin === ""
      ? "n/d"
      : (isPlainNumber(order.margin) && marginUnit === "percent" ? percentText(order.margin) : order.margin);
    return `<article class="card">
      <h3 class="card-title">Ordine ${escapeHtml(order.business_key)}</h3>
      <div class="meta">${pill(status, pageValue("status", status, "Order", ontology))}<span class="pill">${escapeHtml(pageLabel("shortage_qty", "Order", ontology))} ${escapeHtml(order.shortage_qty ?? 0)}</span></div>
      <p>${escapeHtml(order.customer_name || "Cliente non collegato")} · ${escapeHtml(pageLabel("amount", ontology ? "Order" : "", ontology))} ${escapeHtml(amount)} · ${escapeHtml(pageLabel("margin", "Order", ontology))} ${escapeHtml(margin)}</p>
      <button type="button" data-order="${escapeHtml(order.id)}">Apri scheda</button>
    </article>`;
  }

  function orderEdgeText(edge, ontology) {
    const rel = edge && edge.rel;
    const way = edge && edge.direction;
    const phrase = relationPhrase(ontology, rel, way);
    const typeLabel = pageType(edge && edge.other_type, ontology);
    const key = edge && edge.other_key;
    if (phrase) return [phrase, typeLabel, key].filter((part) => part !== "" && part != null).join(" ");
    const fallback = way === "in" ? PAGE_TEXT.links.in : way === "out" ? PAGE_TEXT.links.out : "";
    return [fallback, pageRelation(rel, ontology), typeLabel, key]
      .filter((part) => part !== "" && part != null)
      .join(" ");
  }

  function orderDetail(order, ontology) {
    const edges = (order.edges || [])
      .map((edge) => `<li>${escapeHtml(orderEdgeText(edge, ontology))}</li>`)
      .join("");
    const lines = (order.lines || [])
      .map((line) => {
        const available = line.stock ? line.stock.available : "n/d";
        const formula = line.stock ? line.stock.formula : order.stock_formula;
        return `<li>${escapeHtml(line.product)}: ${escapeHtml(pageLabel("quantity"))} ${escapeHtml(line.quantity)}, ${escapeHtml(pageLabel("shortage"))} ${escapeHtml(line.shortage)}. ${escapeHtml(PAGE_TEXT.headings.available)} ${escapeHtml(available)} <span class="formula">(${escapeHtml(formula)})</span></li>`;
      })
      .join("");
    const customer = order.customer_id
      ? `<button type="button" id="open-customer" data-customer="${escapeHtml(order.customer_id)}">Scheda cliente</button>`
      : "";
    const empty = `<li>${escapeHtml(PAGE_TEXT.headings.noLink)}</li>`;
    return `
    <p><button type="button" id="back-orders" class="ghost">Torna agli ordini</button></p>
    <h2>Ordine ${escapeHtml(order.business_key)}</h2>
    <p class="formula">${escapeHtml(order.stock_formula)}</p>
    <ul>${lines}</ul>
    <h3>${escapeHtml(PAGE_TEXT.headings.links)}</h3>
    <ul class="edges">${edges || empty}</ul>
    ${customer}
    <h3>${escapeHtml(PAGE_TEXT.headings.facts)}</h3>
    ${factList(order.facts, "Order", ontology)}
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
    ${allows(customer, "unmask") ? `<label>Motivo dello smascheramento <input data-reason required minlength="5" maxlength="500" /></label><p><button type="button" id="unmask-pii" data-entity="${escapeHtml(customer.id)}">Mostra dati personali</button></p><pre id="unmask-result"></pre>` : ""}
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

  function criterionWords(code) {
    const key = code == null ? "" : String(code);
    return Object.prototype.hasOwnProperty.call(CRITERION_LABELS, key) ? CRITERION_LABELS[key] : key;
  }

  function failedMarkup(failed) {
    if (!Array.isArray(failed) || !failed.length) return "";
    const bits = failed.map((item) => escapeHtml(criterionWords(item)));
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

  const MONEY_FIELDS = new Set([
    "amount",
    "unit_price",
    "line_amount",
    "allocated_amount",
    "cost",
    "unit_cost",
    "total_cost",
    "line_cost",
    "standard_cost",
    "price",
  ]);
  const DATE_FIELDS = new Set([
    "issued_at",
    "due_at",
    "paid_at",
    "ordered_at",
    "occurred_at",
    "shipped_at",
    "scadenze",
  ]);

  function isPlainNumber(value) {
    if (typeof value === "number") return Number.isFinite(value);
    return typeof value === "string" && /^-?\d+(\.\d+)?$/.test(value.trim());
  }

  function displayMoney(value) {
    if (value == null || value === "") return "";
    if (!isPlainNumber(value)) return String(value);
    return euroText(value);
  }

  function displayDate(value) {
    if (value == null || value === "") return "";
    return formatDay(value);
  }

  function displayField(field, typeName, ontology) {
    const name = field && field.name ? String(field.name) : "";
    const value = field && field.value;
    if (value == null || value === "") return "";
    if (field.masked) {
      if (name === "direction") return PAGE_TEXT.reservedDirection;
      return String(value);
    }
    if (unitOf(ontology, typeName, name) === "percent" && isPlainNumber(value)) return percentText(value);
    if (MONEY_FIELDS.has(name)) return displayMoney(value);
    if (DATE_FIELDS.has(name)) return displayDate(value);
    if (name === "direction" || name === "status") return pageValue(name, value, typeName, ontology);
    return String(value);
  }

  function paymentHeading(payment) {
    if (payment && typeof payment.label === "string" && payment.label !== "") return payment.label;
    return isIncoming(payment) ? "Incasso" : "Pagamento";
  }

  function matchLine(label, value) {
    return `<li>${escapeHtml(label)} ${escapeHtml(value ?? "")}</li>`;
  }

  function matchBlocks(item) {
    const payment = item && item.payment && typeof item.payment === "object" ? item.payment : {};
    const invoice = item && item.invoice && typeof item.invoice === "object" ? item.invoice : {};
    const allocation = item && item.allocation && typeof item.allocation === "object" ? item.allocation : {};
    return `<h3 class="section-heading">${escapeHtml(paymentHeading(payment))}</h3>
      <ul class="fact-list">
        ${matchLine("Numero", payment.number)}
        ${matchLine("Data", displayDate(payment.paid_at))}
        ${matchLine("Importo", displayMoney(payment.amount))}
      </ul>
      <h3 class="section-heading">Fattura proposta</h3>
      <ul class="fact-list">
        ${matchLine("Numero", invoice.number)}
        ${matchLine("Importo", displayMoney(invoice.amount))}
        ${matchLine("Scadenza", displayDate(invoice.due_at))}
      </ul>
      <h3 class="section-heading">Assegnazione</h3>
      <ul class="fact-list">
        ${matchLine("Importo assegnato", displayMoney(allocation.amount))}
        ${matchLine("Residuo attuale", euroText(allocation.remaining))}
      </ul>
      <h3 class="section-heading">Criteri</h3>
      ${criteriaMarkup(item && item.criteria)}`;
  }

  function paymentButtons(item, role, canWrite) {
    const parts = [];
    if (canWrite === false) parts.push(`<p>${escapeHtml(companyUploadNotice())}</p>`);
    const confirm = allows(item, "confirm");
    const correct = allows(item, "correct");
    const reject = allows(item, "reject");
    if (!confirm && !correct && !reject) return parts.join("");
    if (correct) {
      parts.push(`<label>Fattura <input data-invoice value="${escapeHtml(invoiceNumberValue(item))}" /></label>`);
      parts.push(`<label>Importo assegnato <input data-amount value="${escapeHtml(allocationAmountValue(item))}" /></label>`);
      parts.push(`<label>Motivo della correzione <input data-correct-reason required minlength="5" maxlength="500" /></label>`);
    }
    if (reject) {
      parts.push(`<label>Motivo del rifiuto <input data-reject-reason required minlength="5" maxlength="500" /></label>`);
    }
    const buttons = [
      confirm ? `<button type="button" data-act="confirm-match">Conferma</button>` : "",
      correct ? `<button type="button" class="ghost" data-act="correct-match">Correggi</button>` : "",
      reject ? `<button type="button" class="ghost" data-act="reject-match">Rifiuta</button>` : "",
    ].filter(Boolean).join("\n        ");
    parts.push(`<div class="decision-bar">\n        ${buttons}\n      </div>`);
    return parts.join("\n      ");
  }

  function recommendationButtons(item, canWrite) {
    const parts = [];
    if (canWrite === false) parts.push(`<p>${escapeHtml(companyUploadNotice())}</p>`);
    const approve = allows(item, "approve");
    const modify = allows(item, "modify");
    const reject = allows(item, "reject");
    if (approve || modify || reject) {
      const blocked = item.blocked_reason ? String(item.blocked_reason) : "";
      const approveButton = approve
        ? (blocked
          ? `<button type="button" data-act="approve" disabled>Approva</button> <span class="blocked-reason">${escapeHtml(blocked)}</span>`
          : `<button type="button" data-act="approve">Approva</button>`)
        : "";
      const fields = [];
      if (modify) fields.push(`<label>Quantità acquisto <input data-qty value="${escapeHtml((item.actions || []).find((action) => action.type === "createPurchaseOrder")?.params.quantity ?? "")}" /></label>`);
      if (reject) fields.push(`<label>Motivo del rifiuto <input data-reason required minlength="5" maxlength="500" /></label>`);
      const buttons = [
        approveButton,
        modify ? `<button type="button" class="ghost" data-act="modify">Modifica</button>` : "",
        reject ? `<button type="button" class="ghost" data-act="reject">Rifiuta</button>` : "",
      ].filter(Boolean).join("\n        ");
      parts.push(`${fields.join("\n      ")}\n      <div class="decision-bar">\n        ${buttons}\n      </div>`);
    }
    if (allows(item, "execute")) {
      parts.push(`<div class="decision-bar"><button type="button" data-act="execute">Esegui</button></div>`);
    }
    return parts.join("");
  }

  function inboxCard(item, role, canWrite) {
    const preview = item.preview || {};
    const payloads = preview.payloads || item.dry_run || [];
    const evidence = (item.evidence || [])
      .map((fact) => `<li>${escapeHtml(fact.property)}: ${escapeHtml(fact.value)}<p>${escapeHtml(provenanceLabel(fact.source_class))}</p></li>`)
      .join("");
    const cfo = item.requires_cfo ? '<span class="pill high">CFO</span>' : "";
    const payment = item.kind === "payment_match";
    const buttons = payment ? paymentButtons(item, role, canWrite) : recommendationButtons(item, canWrite);
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
      ${revokeBar(item, canWrite, payment ? "payment" : "inbox", role)}
      ${buttons}
    </article>`;
  }

  function ontologyType(ontology, name) {
    const types = ontology && Array.isArray(ontology.types) ? ontology.types : [];
    return types.find((item) => item && item.name === name) || null;
  }

  function directionOf(object) {
    const fields = object && Array.isArray(object.fields) ? object.fields : [];
    const found = fields.find((field) => field && field.name === "direction" && !field.masked);
    return found && found.value != null ? String(found.value) : "";
  }

  function directionMasked(object) {
    const fields = object && Array.isArray(object.fields) ? object.fields : [];
    return fields.some((field) => field && field.name === "direction" && field.masked === true);
  }

  function matchesIncasso(object) {
    return Boolean(object && object.incoming === true && !directionMasked(object));
  }

  function matchesPagamento(object) {
    return Boolean(object && object.incoming === false && !directionMasked(object));
  }

  function isIncoming(payment) {
    let direction = payment;
    if (payment && typeof payment === "object") {
      direction = Object.prototype.hasOwnProperty.call(payment, "direction") ? payment.direction : "";
    }
    return direction !== "outgoing";
  }

  function paymentWord(direction, ontology) {
    const labels = ontology && ontology.payment_labels;
    const key = isIncoming(direction) ? "incoming" : "outgoing";
    if (labels && typeof labels[key] === "string" && labels[key] !== "") return labels[key];
    return key === "outgoing" ? "Pagamento" : "Incasso";
  }

  function objectTypeLabel(object, ontology) {
    if (object && object.type === "Payment") {
      if (directionMasked(object)) {
        if (typeof object.type_label === "string" && object.type_label !== "") return object.type_label;
        return "Movimento";
      }
      return paymentWord(directionOf(object), ontology);
    }
    const info = ontologyType(ontology, object && object.type);
    if (info && info.label) return info.label;
    if (object && typeof object.type_label === "string" && object.type_label !== "") return object.type_label;
    return pageType(object && object.type, ontology);
  }

  function fieldCaption(ontology, typeName, field, object) {
    if (directionMasked(object) && field && typeof field.label === "string" && field.label !== "") {
      return field.label;
    }
    if (typeName === "Payment" && !isIncoming(directionOf(object))) {
      const maps = ontology && ontology.payment_field_labels;
      const outgoing = maps && maps.outgoing;
      const name = field && field.name;
      if (outgoing && name && typeof outgoing[name] === "string" && outgoing[name] !== "") return outgoing[name];
      const fallback = {
        amount: "Importo pagamento",
        source_key: "Numero pagamento",
        paid_at: "Data pagamento",
      };
      if (name && fallback[name]) return fallback[name];
    }
    const info = ontologyType(ontology, typeName);
    const fields = info && Array.isArray(info.fields) ? info.fields : [];
    const found = fields.find((item) => item && item.name === field.name);
    if (found && typeof found.label === "string" && found.label !== "") return found.label;
    if (field && typeof field.label === "string" && field.label !== "") return field.label;
    return pageLabel(field && field.name, typeName, ontology);
  }

  function relationPhrase(ontology, name, way) {
    if (way !== "in" && way !== "out") return "";
    const rels = ontology && Array.isArray(ontology.relations) ? ontology.relations : [];
    const found = rels.find((item) => item && item.name === name);
    if (found && typeof found[way] === "string" && found[way] !== "") return found[way];
    const reserve = PAGE_TEXT.phrases && name ? PAGE_TEXT.phrases[name] : null;
    if (reserve && typeof reserve[way] === "string" && reserve[way] !== "") return reserve[way];
    return "";
  }

  function relationLabel(ontology, name) {
    const rels = ontology && Array.isArray(ontology.relations) ? ontology.relations : [];
    const found = rels.find((item) => item && item.name === name);
    if (found && typeof found.label === "string" && found.label !== "") return found.label;
    return name == null ? "" : String(name);
  }

  function linkedTypeLabel(ontology, link) {
    if (link && link.type === "Payment") {
      if (typeof link.type_label === "string" && link.type_label !== "") return link.type_label;
      return paymentWord("", ontology);
    }
    const info = ontologyType(ontology, link && link.type);
    if (info && info.label) return info.label;
    if (link && typeof link.type_label === "string" && link.type_label !== "") return link.type_label;
    return (link && link.type) || "";
  }

  function linkWay(link) {
    if (link && link.direction === "in") return PAGE_TEXT.links.in;
    if (link && link.direction === "out") return PAGE_TEXT.links.out;
    return "";
  }

  function linkText(ontology, link) {
    const typeLabel = linkedTypeLabel(ontology, link);
    const key = link && link.key != null ? link.key : "";
    const phrase = relationPhrase(ontology, link && link.rel, link && link.direction);
    if (phrase) return [phrase, typeLabel, key].filter((part) => part !== "" && part != null).join(" ");
    const rel = relationLabel(ontology, link && link.rel);
    return [linkWay(link), rel, typeLabel, key].filter((part) => part !== "" && part != null).join(" ");
  }

  function objectFields(object, ontology) {
    const rows = (object && object.fields || []).map((field) => {
      if (field && field.name === "direction" && field.masked) {
        return `<li>${escapeHtml(PAGE_TEXT.reservedDirection)}</li>`;
      }
      const caption = fieldCaption(ontology, object.type, field, object);
      const value = displayField(field, object && object.type, ontology);
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
      return `<li><a href="#" data-object="${escapeHtml(link && link.id)}">${escapeHtml(text)}</a></li>`;
    }).join("");
  }

  const DECISION_NOUNS = {
    confirm: "della conferma",
    correct: "della correzione",
    reject: "del rifiuto",
    approve: "dell'approvazione",
    modify: "della modifica",
  };

  function revocationLine(entry, history) {
    const who = entry && entry.actor ? String(entry.actor) : "";
    const when = formatDay(entry && entry.at);
    const targetId = entry && entry.revokes_decision_id;
    const target = targetId
      ? (history || []).find((item) => item && item.id === targetId && item.decision !== "revoked")
      : null;
    const noun = target && Object.prototype.hasOwnProperty.call(DECISION_NOUNS, target.decision)
      ? DECISION_NOUNS[target.decision]
      : "";
    if (!target || noun === "") {
      return escapeHtml(["Annullamento di una decisione precedente", who, when].filter((part) => part !== "").join(" · "));
    }
    const amount = target.amount != null && target.amount !== "" ? euroText(target.amount) : "";
    const day = formatDay(target.at);
    let phrase = `Annullamento ${noun}`;
    if (amount) phrase += ` di ${amount}`;
    if (day) phrase += ` del ${day}`;
    if (who) phrase += ` · ${who}`;
    if (when) phrase += ` · ${when}`;
    return escapeHtml(phrase);
  }

  function historyLine(entry, history) {
    const code = entry && entry.decision ? String(entry.decision) : "";
    if (code === "revoked") return revocationLine(entry, history);
    const label = code && Object.prototype.hasOwnProperty.call(DECISION_LABELS, code)
      ? DECISION_LABELS[code]
      : (entry && entry.label != null ? String(entry.label) : code);
    const amount = entry && entry.amount != null && entry.amount !== "" ? euroText(entry.amount) : "";
    const who = entry && entry.actor ? String(entry.actor) : "";
    const when = formatDay(entry && entry.at);
    const text = escapeHtml([label, amount, who, when].filter((part) => part !== "").join(" · "));
    if (entry && entry.revoked) {
      return `<span class="revoked-decision">${text}</span> annullata`;
    }
    return text;
  }

  function objectCard(object, ontology) {
    const typeLabel = objectTypeLabel(object, ontology);
    const links = objectLinks(object, ontology);
    return `<article class="card">
      <h3 class="card-title">${escapeHtml(typeLabel)} ${escapeHtml(object && object.business_key || "")}</h3>
      <ul class="fact-list">${objectFields(object, ontology)}</ul>
      ${links ? `<h3 class="section-heading">Collegamenti</h3><ul class="edges">${links}</ul>` : ""}
      <button type="button" data-object="${escapeHtml(object && object.id)}">Apri scheda</button>
    </article>`;
  }

  function objectDetail(object, ontology) {
    const typeLabel = objectTypeLabel(object, ontology);
    const links = objectLinks(object, ontology);
    const entries = (object && object.history) || [];
    const history = entries.map((entry) => {
      return `<li>${historyLine(entry, entries)}</li>`;
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
    allowsAction,
    matchesIncasso,
    matchesPagamento,
    fileActionBar,
    measureText,
    auditCard,
    readableCard,
  };
});
