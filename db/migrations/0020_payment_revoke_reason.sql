-- L'annullamento esige lo stesso motivo di Correggi e Rifiuta.
-- La conferma può restare senza motivo. Non riscrive le righe già presenti.
--
-- Allowlist, riga esatta:
--   ALTER TABLE app.payment_decisions DROP CONSTRAINT IF EXISTS payment_decisions_reason_check

ALTER TABLE app.payment_decisions DROP CONSTRAINT IF EXISTS payment_decisions_reason_check;

ALTER TABLE app.payment_decisions
  ADD CONSTRAINT payment_decisions_reason_check
  CHECK (
    decision = 'confirm'
    OR char_length(btrim(coalesce(reason, ''))) >= 5
  );

COMMENT ON CONSTRAINT payment_decisions_reason_check ON app.payment_decisions IS
  'Correggi, Rifiuta e Annulla esigono un motivo di almeno 5 caratteri. La conferma può restare senza motivo.';
