-- Safe migration for Gmail temporary-password support.
-- Existing customer, loan, payment, and admin rows are preserved.
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN DEFAULT FALSE NOT NULL;
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS temp_password_expires_at TIMESTAMP;
