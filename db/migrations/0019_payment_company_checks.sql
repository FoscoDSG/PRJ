-- Vincoli sull'abbinamento. Non riscrive le righe già presenti: nessun
-- aggiornamento di dati. La 0017 resta com'è.

ALTER TABLE app.entities
  ADD CONSTRAINT entities_id_company_key UNIQUE (id, company_id);

COMMENT ON CONSTRAINT entities_id_company_key ON app.entities IS
  'Coppia usata dai riferimenti che esigono la stessa azienda. Non assegna l''azienda alle righe vecchie.';

ALTER TABLE app.payment_proposals
  ADD CONSTRAINT payment_proposals_payment_company_fkey
  FOREIGN KEY (payment_entity_id, company_id)
  REFERENCES app.entities (id, company_id);

ALTER TABLE app.payment_proposals
  ADD CONSTRAINT payment_proposals_invoice_company_fkey
  FOREIGN KEY (invoice_entity_id, company_id)
  REFERENCES app.entities (id, company_id);

ALTER TABLE app.payment_proposals
  ADD CONSTRAINT payment_proposals_allocation_company_fkey
  FOREIGN KEY (allocation_entity_id, company_id)
  REFERENCES app.entities (id, company_id);

COMMENT ON CONSTRAINT payment_proposals_payment_company_fkey ON app.payment_proposals IS
  'Incasso, fattura e assegnazione della proposta sono della stessa azienda della proposta.';

ALTER TABLE app.payment_decisions
  ADD CONSTRAINT payment_decisions_invoice_company_fkey
  FOREIGN KEY (invoice_entity_id, company_id)
  REFERENCES app.entities (id, company_id);

COMMENT ON CONSTRAINT payment_decisions_invoice_company_fkey ON app.payment_decisions IS
  'Se la decisione indica una fattura, quella fattura è della stessa azienda. Senza fattura il controllo non scatta.';

CREATE UNIQUE INDEX payment_decisions_one_revocation_idx
  ON app.payment_decisions (revokes_decision_id)
  WHERE revokes_decision_id IS NOT NULL;

COMMENT ON INDEX app.payment_decisions_one_revocation_idx IS
  'Una decisione si revoca una sola volta.';

ALTER TABLE app.payment_decisions
  ADD CONSTRAINT payment_decisions_reason_check
  CHECK (
    decision NOT IN ('correct', 'reject')
    OR char_length(btrim(coalesce(reason, ''))) >= 5
  );

COMMENT ON CONSTRAINT payment_decisions_reason_check ON app.payment_decisions IS
  'Correggi e Rifiuta esigono un motivo di almeno 5 caratteri. Conferma e revoca no.';
