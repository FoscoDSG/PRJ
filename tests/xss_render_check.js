const fs = require("fs");
const {
  PILL_CLASSES,
  escapeHtml,
  pill,
  driftCard,
  mappingRow,
  profileLabelText,
  tableTypeMarkup,
  orderCard,
  orderDetail,
  customerDetail,
  inboxCard,
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
  }),
  tableTypeMarkup({
    source_name: value,
    profile: value,
    profile_label: value,
    file_id: value,
  }),
  driftCard({ id: value, source_name: value, diff: { added: [value] } }),
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
    id: value,
    status: value,
    requires_cfo: false,
    risk: value,
    order_key: value,
    problem: value,
    expected_impact: value,
    confidence: value,
    blocked_reason: value,
    dry_run: { note: value },
    actions: [{ type: "createPurchaseOrder", params: { quantity: value } }],
  }, "approver"),
  auditCard({ action: value, actor: value, payload: { note: value } }),
  readableCard({ action: value, who: value, when: value, what: value, order: value, detail: value }),
  pill(value),
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
]);
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
  "mapping-actions",
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
if (!html.includes("Tipo non riconosciuto") || !html.includes(escapeHtml(value))) {
  console.error("table type markup did not escape");
  process.exit(2);
}

process.stdout.write("inert\n");
