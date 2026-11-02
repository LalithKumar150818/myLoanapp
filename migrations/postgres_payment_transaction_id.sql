-- Safe, additive migration for Payment.transaction_id and related payment columns.
-- Existing customer, loan application, admin, and payment rows are preserved.

ALTER TABLE payment ADD COLUMN IF NOT EXISTS payment_type VARCHAR(50) DEFAULT 'Processing Fee';
ALTER TABLE payment ADD COLUMN IF NOT EXISTS reference VARCHAR(150) DEFAULT '';
ALTER TABLE payment ADD COLUMN IF NOT EXISTS transaction_id VARCHAR(150);
ALTER TABLE payment ADD COLUMN IF NOT EXISTS screenshot_data BYTEA;
ALTER TABLE payment ADD COLUMN IF NOT EXISTS screenshot_mime VARCHAR(32);
ALTER TABLE payment ADD COLUMN IF NOT EXISTS payment_verified BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE payment ADD COLUMN IF NOT EXISTS created_at TIMESTAMP;
ALTER TABLE payment ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP;

UPDATE payment
SET payment_type = 'Processing Fee'
WHERE payment_type IS NULL;

UPDATE payment
SET reference = ''
WHERE reference IS NULL;

UPDATE payment
SET payment_verified = FALSE
WHERE payment_verified IS NULL;

UPDATE payment
SET created_at = COALESCE(created_at, CURRENT_TIMESTAMP),
    updated_at = COALESCE(updated_at, created_at, CURRENT_TIMESTAMP);

-- Existing NULL transaction IDs are valid and remain unoccupied.
-- PostgreSQL unique indexes allow multiple NULL values.
-- If duplicates already exist, remove the duplicate sources manually before creating this index.
CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_transaction_id
    ON payment(transaction_id)
    WHERE transaction_id IS NOT NULL;
