"""Add new application payment-management columns without recreating existing data."""
import os
import sqlite3


database_url = os.environ.get("DATABASE_URL", "")
database_path = os.path.join(os.path.dirname(__file__), "loan.db")

if database_url.startswith("sqlite:///"):
    database_path = database_url[len("sqlite:///"):].lstrip("/")

if not os.path.isfile(database_path):
    print(f"Database not found: {database_path}")
    raise SystemExit(0)

with sqlite3.connect(database_path) as connection:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(application)").fetchall()}
    if "updated_at" not in columns:
        connection.execute(
            "ALTER TABLE application ADD COLUMN updated_at DATETIME"
        )
        connection.execute(
            "UPDATE application SET updated_at = COALESCE(created_at, CURRENT_TIMESTAMP)"
        )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS payment (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            application_id INTEGER,
            payment_type VARCHAR(50) DEFAULT 'Processing Fee',
            amount INTEGER NOT NULL,
            status VARCHAR(20) DEFAULT 'Pending',
            reference VARCHAR(150) DEFAULT '',
            created_at DATETIME,
            updated_at DATETIME,
            FOREIGN KEY(user_id) REFERENCES user(id),
            FOREIGN KEY(application_id) REFERENCES application(id)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO payment (user_id, application_id, payment_type, amount, status, created_at, updated_at)
        SELECT a.user_id, a.id, 'Processing Fee', COALESCE(a.fee_amount, 36), 'Pending',
               COALESCE(a.created_at, CURRENT_TIMESTAMP), COALESCE(a.updated_at, CURRENT_TIMESTAMP)
        FROM application a
        LEFT JOIN payment p ON p.application_id = a.id
        WHERE p.id IS NULL
        """
    )
    connection.commit()

print(f"Database migration completed: {database_path}")
