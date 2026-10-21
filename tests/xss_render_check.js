const fs = require("fs");
const {
  PILL_CLASSES,
  escapeHtml,
  pill,
  driftCard,
  quarantineCard,
  identityCard,
  mappingRow,
  profileLabelText,
  tableTypeMarkup,
  orderCard,
  orderDetail,
  customerDetail,
  inboxCard,
  objectCard,
  objectDetail,
  measureText,
  auditCard,
  readableCard,
} = require("../apps/web/escape.js");

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const value = input.value;
if (!value.includes("<") || !value.includes('"') || !value.includes("onerror")) {
  console.error("payload is not an attribute and markup breakout");
  process.exit(2);
}

const html = [
  customerDetail(input.customer),
  mappingRow({
    id: value,
    source_name: input.source_name,
    source_field: input.source_field,
    target_type: "Customer",
    target_property: "company_name",
    confidence: 0.42,
    status: "proposed",
    profile: value,
    profile_label: value,
    origin: "learned",
    origin_text: value,
    decision_id: value,
    actor: value,
    decided_at: value,
    band: "forte",
    band_note: value,
    history: [{
      id: value,
      decision: "confirm",
      actor: value,
      decided_at: value,
      revoked: true,
      revoker: value,
      revoked_at: value,
      revoke_reason: value,
    }],
  }),
  tableTypeMarkup({
    source_name: value,
    profile: value,
    profile_label: value,
    file_id: value,
  }),
  driftCard({
    id: value,
    source_name: value,
    diff: { added: [value] },
    status: "acknowledged",
    history: [{
      id: value,
      decision: "acknowledged",
      actor: value,
      decided_at: value,
      revoked: true,
      revoker: value,
      revoked_at: value,
      revoke_reason: value,
    }],
  }),
  quarantineCard({
    id: "released-empty",
    source_name: "archivio",
    order_key: "2002",
    reasons: [],
    status: "released",
    history: [{
      id: "qd",
      decision: "released",
      actor: "mapper",
      decided_at: "01/01/2026 10:00",
      revoked: true,
      revoker: "mapper",
      revoked_at: "01/01/2026 10:05",
      revoke_reason: "motivo del rilascio",
    }],
  }),
  quarantineCard({
    id: value,
    source_name: value,
    order_key: value,
    reasons: [value],
    status: "released",
    history: [{
      id: value,
      decision: "released",
      actor: value,
      decided_at: value,
      revoked: true,
      revoker: value,
      revoked_at: value,
      revoke_reason: value,
    }],
  }),
  identityCard({
    id: value,
    entity_type: value,
    reason: value,
    score: value,
    status: "confirmed",
    history: [{
      id: value,
      decision: "confirmed",
      actor: value,
      decided_at: value,
      revoked: true,
      revoker: value,
      revoked_at: value,
      revoke_reason: value,
    }],
  }),
  orderCard({
    id: value,
    business_key: value,
    status: value,
    shortage_qty: value,
    customer_name: value,
    amount: value,
    margin: value,
  }),
  orderDetail({
    business_key: value,
    stock_formula: value,
    customer_id: value,
    facts: [{ property: "company_name", value }],
    lines: [{ product: value, quantity: value, shortage: value, stock: { available: value, formula: value } }],
    edges: [{ direction: "out", rel: value, other_type: value, other_key: value }],
  }),
  inboxCard({
    id: value,
    status: value,
    requires_cfo: false,
    risk: value,
    order_key: value,
    problem: value,
    expected_impact: value,
    confidence: value,
    dry_run: { note: value },
    actions: [{ type: "createPurchaseOrder", params: { quantity: value } }],
  }),
  inboxCard({
    id: "proposed-bar",
    status: "proposed",
    requires_cfo: false,
    risk: "low",
    order_key: "1024",
    problem: "manca",
    expected_impact: "niente",
    confidence: 0.5,
    disposition: "closed",
    blocked_reason: value,
    actions: [{ type: "createPurchaseOrder", params: { quantity: 1 } }],
  }, "approver"),
  inboxCard({
    id: "revoked-approval",
    status: "revoked",
    requires_cfo: false,
    risk: "low",
    order_key: "1024",
    problem: "manca",
    expected_impact: "niente",
    confidence: 0.5,
    actions: [{ type: "createPurchaseOrder", params: { quantity: 1 } }],
    history: [{
      id: "ap",
      decision: "approve",
      actor: "approver",
      decided_at: "01/01/2026 10:00",
      revoked: true,
      revoker: "approver",
      revoked_at: "01/01/2026 10:05",
      revoke_reason: "motivo",
    }],
  }, "approver"),
  inboxCard({
    id: value,
    kind: "payment_match",
    status: "proposed",
    requires_cfo: false,
    risk: value,
    order_key: value,
    headline: value,
    problem: value,
    expected_impact: value,
    confidence: value,
    confidence_method: "rule",
    confidence_rule: value,
    invoice_id: value,
    allocated_amount: value,
    criteria: { amount: true, customer: false, reference: true, extra: value },
    failed_criteria: [value, "amount"],
    process: { id: value, label: value, package: value },
    step: { id: value, label: value },
    actions: [{ type: "allocatePayment", params: { invoice: value, allocated_amount: value } }],
    history: [{
      id: value,
      decision: "correct",
      actor: value,
      decided_at: value,
      revoked: false,
    }],
  }, "approver", true),
  inboxCard({
    id: value,
    status: value,
    requires_cfo: false,
    risk: value,
    order_key: value,
    problem: value,
    expected_impact: value,
    confidence: value,
    blocked_reason: value,
    process: { id: value, label: value, package: value },
    step: { id: value, label: value },
    dry_run: { note: value },
    actions: [{ type: "createPurchaseOrder", params: { quantity: value } }],
    executed: true,
    executed_message: value,
    history: [{
      id: value,
      decision: "approve",
      actor: value,
      decided_at: value,
      revoked: false,
      revoker: value,
      revoked_at: value,
      revoke_reason: value,
    }],
  }, "approver"),
  objectCard({
    id: "obj-1",
    type: "Invoice",
    business_key: value,
    fields: [
      { name: "amount", label: "WRONG", value: 122, classification: "confidential", masked: false },
      { name: "note", label: value, value: "visibile", classification: "confidential", masked: false },
      { name: "iban", label: "IBAN", value: "riservato", masked: true },
      { name: "hostile", label: "Nota", value: value, masked: false },
    ],
    remaining: 90,
    history: [{ decision: "confirm", label: "confirm", amount: 40, actor: "approver", at: "2026-04-02T10:00:00" }],
    links: [{ rel: "settles", type: "Payment", key: value, id: "obj-1" }],
  }, {
    types: [
      { name: "Invoice", label: "Fattura", fields: [{ name: "amount", label: "Importo fattura" }] },
      { name: "Payment", label: "Incasso", fields: [] },
    ],
    relations: [{ name: "settles" }],
  }),
  auditCard({ action: value, actor: value, payload: { note: value } }),
  readableCard({ action: value, who: value, when: value, what: value, order: value, detail: value }),
  pill(value),
  objectDetail({
    type: "Invoice",
    business_key: "FT-2026-0187",
    fields: [],
    links: [],
    history: [
      { decision: "confirm", amount: 1500, actor: "approver", at: "2026-04-02T10:00:00" },
      { decision: "confirm", amount: 940, actor: value, at: "2026-04-03T10:00:00", revoked: true },
      { decision: "revoked", actor: "approver", at: "2026-04-04T09:00:00", revoked: true },
      { decision: "correct", amount: 940, actor: "approver", at: "2026-04-04T11:00:00", revoked: false },
    ],
  }, {
    types: [{ name: "Invoice", label: "Fattura" }],
    relations: [],
  }),
].join("\n");

if (html.includes(value) || html.includes("<img") || html.includes("<script")) {
  console.error("raw payload was interpolated");
  process.exit(2);
}
if (!html.includes(escapeHtml(value))) {
  console.error("escaped payload missing");
  process.exit(2);
}

const allowedTags = new Set([
  "span", "button", "article", "strong", "pre", "tr", "td", "code",
  "h3", "div", "p", "ul", "li", "label", "input", "h2", "select", "option",
]);
const structural = new Set([
  "pill", "card", "meta", "ghost", "formula", "edges",
  "profile-label", "profile-label-source", "profile-label-text",
  "card-title", "section-heading", "mapping-confidence", "fact-list",
  "fact-confidence", "confidence-badge", "decision-bar", "blocked-reason",
  "audit-when", "audit-what", "audit-order", "audit-detail",
  "origin-line", "suggestion-band", "suggestion-note",
  "file-type-actions", "file-type-choice", "mapping-actions",
  "mapping-source", "mapping-column", "mapping-destination", "mapping-status",
  "decision-history", "revoked-decision", "revoker-line", "revoke-reason", "executed-note",
  "process-step", "match-criteria", "failed-criteria", "decided",
]);
const allowedLabels = new Set(["Fonte", "Colonna", "Destinazione", "Suggerimento", "Stato"]);
const tagRe = /<\/?([a-zA-Z][a-zA-Z0-9]*)\b([^>]*)>/g;
const attrRe = /([^\s=/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g;
const seenClasses = new Set();
let cursor = 0;
let tag;
while ((tag = tagRe.exec(html))) {
  const between = html.slice(cursor, tag.index);
  if (/[<>]/.test(between)) {
    console.error("unescaped bracket in text");
    process.exit(2);
  }
  cursor = tag.index + tag[0].length;
  const name = tag[1].toLowerCase();
  if (!allowedTags.has(name)) {
    console.error(`unexpected tag ${name}`);
    process.exit(2);
  }
  const attrs = tag[2];
  if (/[<>]/.test(attrs)) {
    console.error("raw bracket in attribute");
    process.exit(2);
  }
  attrRe.lastIndex = 0;
  let attr;
  while ((attr = attrRe.exec(attrs))) {
    const attrName = attr[1].toLowerCase();
    if (attrName.startsWith("on")) {
      console.error(`event handler attribute ${attrName}`);
      process.exit(2);
    }
    if (attrName === "data-label") {
      const label = attr[2] ?? attr[3] ?? attr[4] ?? "";
      if (!allowedLabels.has(label)) {
        console.error(`data-label not allowed: ${label}`);
        process.exit(2);
      }
      continue;
    }
    if (attrName !== "class") continue;
    const classValue = attr[2] ?? attr[3] ?? attr[4] ?? "";
    for (const token of classValue.split(/\s+/)) {
      if (!token) continue;
      seenClasses.add(token);
      if (!structural.has(token) && !PILL_CLASSES.has(token)) {
        console.error(`class not allowed: ${token}`);
        process.exit(2);
      }
    }
  }
}
if (/[<>]/.test(html.slice(cursor))) {
  console.error("unescaped bracket in text");
  process.exit(2);
}

const requiredClasses = [
  "card-title", "section-heading", "mapping-confidence", "fact-list",
  "fact-confidence", "confidence-badge", "decision-bar", "blocked-reason",
  "audit-when", "audit-what", "audit-order", "audit-detail",
  "origin-line", "suggestion-band", "suggestion-note", "file-type-actions",
  "file-type-choice", "mapping-actions",
  "mapping-source", "mapping-column", "mapping-destination", "mapping-status",
  "decision-history", "revoked-decision", "revoker-line", "revoke-reason", "executed-note",
  "process-step", "match-criteria", "failed-criteria",
];
for (const token of requiredClasses) {
  if (!seenClasses.has(token)) {
    console.error(`expected class missing from render: ${token}`);
    process.exit(2);
  }
}

const hostile = pill(value);
if (!hostile.startsWith('<span class="pill">') || hostile.includes("<img")) {
  console.error("pill class was not whitelisted");
  process.exit(2);
}
const known = pill("confirmed");
if (known !== '<span class="pill confirmed">confirmed</span>') {
  console.error("known pill class dropped");
  process.exit(2);
}

const poisoned = profileLabelText({ profile: value, profile_label: value, source_name: value });
if (poisoned !== "Tipo non riconosciuto" || poisoned.includes(value) || poisoned.includes("<")) {
  console.error("profile label echoed file content");
  process.exit(2);
}
const probable = profileLabelText({ profile: "order", profile_label: value });
if (probable !== "Probabile export ordini" || /%|\d/.test(probable) || probable.includes(value)) {
  console.error("profile label was not the fixed phrase");
  process.exit(2);
}
const fromServer = profileLabelText({ profile: "order", profile_label: "Probabile export ordini" });
if (fromServer !== "Probabile export ordini") {
  console.error("fixed server label was dropped");
  process.exit(2);
}
const fromColumn = profileLabelText({ profile: "order", profile_label: "Order Date", source_name: value });
if (fromColumn !== "Probabile export ordini" || fromColumn.includes("Order Date")) {
  console.error("column name became the profile label");
  process.exit(2);
}
if (!html.includes("Il documento resta rilasciato.")) {
  console.error("quarantine revoke notice missing");
  process.exit(2);
}
if (!html.includes("I clienti restano uniti.")) {
  console.error("merge revoke notice missing");
  process.exit(2);
}
if (!html.includes("Al prossimo caricamento la colonna torna da decidere; i dati già caricati restano.")) {
  console.error("revoked confirmation notice missing");
  process.exit(2);
}
if (!html.includes("Documento rilasciato.")) {
  console.error("released state missing");
  process.exit(2);
}
if (html.includes("In attesa di conferma")) {
  console.error("released document still looks pending");
  process.exit(2);
}
const bareProcess = inboxCard({
  id: "bare-process",
  status: "detected",
  risk: "low",
  order_key: "9",
  problem: "manca",
  expected_impact: "",
  confidence: null,
}, "operator");
if (bareProcess.includes(" · ") || bareProcess.includes("process-step")) {
  console.error("missing process rendered a lone separator");
  process.exit(2);
}
const stepOnly = inboxCard({
  id: "step-only",
  status: "detected",
  risk: "low",
  order_key: "9",
  problem: "manca",
  expected_impact: "",
  confidence: null,
  step: { id: "shortage", label: "Disponibilità insufficiente" },
}, "operator");
if (!stepOnly.includes("process-step") || stepOnly.includes(" · ") || !stepOnly.includes("Disponibilità insufficiente")) {
  console.error("a step without a process still printed a separator");
  process.exit(2);
}
const secretInvoice = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
const paymentCard = inboxCard({
  id: "pay-card",
  kind: "payment_match",
  status: "proposed",
  risk: "low",
  headline: "Incasso",
  problem: "abbina",
  confidence: 1,
  confidence_method: "rule",
  invoice_id: secretInvoice,
  allocated_amount: "10",
  payment: { number: "INC-9", paid_at: "2026-04-02", amount: 122 },
  invoice: { number: "FT-9", amount: 200, due_at: "2026-04-01" },
  allocation: { amount: 20, remaining: 90 },
  criteria: { amount: true, customer: false, reference: true },
  process: { id: "order_to_cash", label: "Ordine e incasso" },
  step: { id: "match_proposed", label: "Proposta" },
  evidence_hash: "abc",
}, "approver", true);
if (!paymentCard.includes("data-correct-reason") || !paymentCard.includes("data-reject-reason")) {
  console.error("correct and reject need separate reason fields");
  process.exit(2);
}
if (paymentCard.includes(" data-reason")) {
  console.error("confirm still shares one reason field");
  process.exit(2);
}
if (paymentCard.includes("Cosa verrà approvato") || paymentCard.includes("Anteprima di approvazione")) {
  console.error("payment card still shows the approval payload");
  process.exit(2);
}
for (const heading of ["Incasso", "Fattura proposta", "Assegnazione", "Criteri", "Conferma", "Correggi", "Rifiuta"]) {
  if (!paymentCard.includes(heading)) {
    console.error(`payment card missing ${heading}`);
    process.exit(2);
  }
}
if (!paymentCard.includes('value="FT-9"') || paymentCard.includes(secretInvoice)) {
  console.error("correct form used the internal invoice id");
  process.exit(2);
}
if (!paymentCard.includes("sì") || !paymentCard.includes("no") || !paymentCard.includes("Residuo attuale") || !paymentCard.includes("90,00 €")) {
  console.error("payment card did not show criteria and the remaining balance");
  process.exit(2);
}
if (!html.includes("Residuo attuale") || !html.includes("90,00 €")) {
  console.error("invoice card hid the remaining balance");
  process.exit(2);
}
const historyCard = objectDetail({
  type: "Invoice",
  business_key: "FT-9",
  fields: [],
  links: [{ rel: "settles", type: "Payment", key: "INC-9", id: "pay-9" }],
  history: [{ decision: "confirm", label: "confirm", amount: 40, actor: "approver", at: "2026-04-02T10:00:00" }],
}, {
  types: [{ name: "Invoice", label: "Fattura" }, { name: "Payment", label: "Incasso" }],
  relations: [{ name: "settles" }],
});
if (historyCard.includes(">confirm<") || !historyCard.includes("Confermata") || !historyCard.includes("40,00 €") || !historyCard.includes("02/04/2026")) {
  console.error("history still shows a raw decision code");
  process.exit(2);
}
if (historyCard.includes("revoked-decision") || historyCard.includes("annullata")) {
  console.error("a valid history line was marked annullata");
  process.exit(2);
}
const story = html.slice(html.indexOf("FT-2026-0187"));
const historyList = story.split('<ul class="decision-history">')[1].split("</ul>")[0];
const storyLines = [...historyList.matchAll(/<li>([\s\S]*?)<\/li>/g)].map((match) => match[1]);
if (storyLines.length !== 4) {
  console.error("invoice history did not keep every decision");
  process.exit(2);
}
const [kept, struck, cancellation, replacement] = storyLines;
if (kept.includes("revoked-decision") || kept.includes("annullata") || !kept.includes("1.500,00 €")) {
  console.error("a valid history line was marked annullata");
  process.exit(2);
}
for (const line of [struck, cancellation]) {
  const marked = line.indexOf("annullata");
  if (!line.includes('class="revoked-decision"') || marked < 0 || marked < line.indexOf("</span>")) {
    console.error("revoked history line is not marked annullata");
    process.exit(2);
  }
}
if (!struck.includes("940,00 €") || struck.includes(value) || !struck.includes(escapeHtml(value))) {
  console.error("revoked history line leaked or dropped the amount");
  process.exit(2);
}
if (replacement.includes("revoked-decision") || replacement.includes("annullata") || !replacement.includes("Corretta")) {
  console.error("a valid history line was marked annullata");
  process.exit(2);
}
if (!historyCard.includes('data-object="pay-9"')) {
  console.error("linked object is not clickable");
  process.exit(2);
}
if (!html.includes("Importo fattura: 122") || html.includes("WRONG")) {
  console.error("object card ignored the ontology label or hid a visible amount");
  process.exit(2);
}
if (!html.includes("visibile") || !html.includes("riservato")) {
  console.error("object card did not show the server value");
  process.exit(2);
}
if (!html.includes("Cosa verrà approvato")) {
  console.error("order card lost its approval preview");
  process.exit(2);
}
const measuredLine = measureText({
  recall: value,
  unmatched_share: value,
  recall_label: value,
  unmatched_label: value,
});
if (
  measuredLine.includes("<") ||
  measuredLine.includes(value) ||
  !measuredLine.includes("richiamo sui casi controllati") ||
  !measuredLine.includes("quota di incassi lasciati senza abbinamento")
) {
  console.error("measure line echoed a server string");
  process.exit(2);
}
const shownLine = measureText({ recall: 1, unmatched_share: 0.5 });
if (shownLine !== "richiamo sui casi controllati: 1. quota di incassi lasciati senza abbinamento: 0,5") {
  console.error("recall is not shown next to the unmatched share");
  process.exit(2);
}
const revokedCard = html.split('id="revoked-approval"').pop();
if (revokedCard.includes(">Esegui<") || revokedCard.includes(">Approva<")) {
  console.error("revoked approval still offers a decision");
  process.exit(2);
}
if (!html.includes("Tipo non riconosciuto") || !html.includes(escapeHtml(value))) {
  console.error("table type markup did not escape");
  process.exit(2);
}

process.stdout.write("inert\n");
