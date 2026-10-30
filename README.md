# Loan Mobile App (Flask)

## Run in VS Code
```
python -m venv venv
venv\Scripts\activate        # Windows   (Mac/Linux: source venv/bin/activate)
pip install -r requirements.txt
python app.py
```
Open http://127.0.0.1:5000

## Logins
- Users: /register then /login
- Admin: /admin/login  -> email `admin@loanapp.com`, password `Admin@12345`
  (auto-created on first run. CHANGE IT: set env vars ADMIN_EMAIL / ADMIN_PASSWORD before the FIRST run,
  or delete loan.db and re-run.)

## Admin first steps
Admin Panel -> gear icon (Settings): upload payment QR code, set application fee, edit conditions.

## Production notes
- Set SECRET_KEY env var, run behind HTTPS with gunicorn/waitress, set debug off.
- Uploads are stored in /uploads (not publicly served; only the owner/admin can open them).
