-- Safe, additive migration for reusable customer profiles and saved drafts.
-- Existing rows are preserved. Run once against PostgreSQL.

ALTER TABLE "user" ADD COLUMN IF NOT EXISTS address TEXT;
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS aadhaar_number VARCHAR(12);
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS pan_number VARCHAR(10);
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS profile_completed BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS registration_fee_paid BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE application ADD COLUMN IF NOT EXISTS application_reference VARCHAR(30);
ALTER TABLE application ADD COLUMN IF NOT EXISTS submission_token VARCHAR(36);
ALTER TABLE application ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP;

CREATE TABLE IF NOT EXISTS application_draft (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL UNIQUE REFERENCES "user"(id),
    data_json TEXT NOT NULL DEFAULT '{}',
    documents_json TEXT NOT NULL DEFAULT '{}',
    updated_at TIMESTAMP
);

UPDATE application
SET updated_at = COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)
WHERE updated_at IS NULL;

-- Customers with an existing application have already completed the original
-- registration fee in legacy installations.
UPDATE "user" u
SET registration_fee_paid = TRUE
WHERE u.is_admin = FALSE
  AND EXISTS (SELECT 1 FROM application a WHERE a.user_id = u.id);

-- References are stable and are only assigned to applications that lack one.
UPDATE application a
SET application_reference = 'MYL-' || TO_CHAR(COALESCE(a.created_at, CURRENT_TIMESTAMP), 'YYYYMMDD') || '-' || LPAD(a.id::TEXT, 6, '0')
WHERE a.application_reference IS NULL OR BTRIM(a.application_reference) = '';

CREATE UNIQUE INDEX IF NOT EXISTS uq_application_application_reference
    ON application(application_reference)
    WHERE application_reference IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_application_submission_token
    ON application(submission_token)
    WHERE submission_token IS NOT NULL;
