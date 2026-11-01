import os
import json
import re
import secrets
import uuid
from datetime import datetime, timedelta
import smtplib
from email.message import EmailMessage
from functools import wraps
from urllib.parse import quote

from dotenv import load_dotenv
from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import check_password_hash, generate_password_hash

from translations import LANGS, TRANSLATIONS

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"), override=False)
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(BASE_DIR, "uploads"))
os.makedirs(UPLOAD_DIR, exist_ok=True)

IS_PRODUCTION = (
    os.environ.get("RENDER") == "true"
    or os.environ.get("APP_ENV", "").lower() == "production"
    or os.environ.get("FLASK_ENV", "").lower() == "production"
    or bool(os.environ.get("DATABASE_URL") and os.environ.get("DATABASE_URL", "").startswith(("postgres://", "postgresql://", "postgresql+psycopg://")))
)

# ---------- Config ----------
def load_secret_key():
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    if IS_PRODUCTION:
        raise RuntimeError("SECRET_KEY must be configured in production")
    path = os.path.join(BASE_DIR, "secret.key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


def load_database_url():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return "sqlite:///" + os.path.join(BASE_DIR, "loan.db")
    if database_url.startswith("postgres://"):
        return "postgresql+psycopg://" + database_url[len("postgres://"):]
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url[len("postgresql://"):]
    return database_url


app = Flask(__name__)
app.config.update(
    SECRET_KEY=load_secret_key(),
    SQLALCHEMY_DATABASE_URI=load_database_url(),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
    MAX_CONTENT_LENGTH=25 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(os.environ.get("SESSION_COOKIE_SECURE", "true" if IS_PRODUCTION else "false").lower() == "true"),
)
db = SQLAlchemy(app)

MIN_LOAN, MAX_LOAN = 100, 10000000
MIN_MONTHS, MAX_MONTHS = 1, 60
STATUSES = ["Pending", "Under Review", "Approved", "Rejected"]
DEFAULT_CONDITIONS = (
    "1. All details and documents submitted must be true and belong to the applicant.\n"
    "2. The application fee is non-refundable.\n"
    "3. Submitting an application does not guarantee loan approval.\n"
    "4. The company may contact you to verify your details."
)

# ---------- Models ----------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(15), unique=True, nullable=False)
    country_code = db.Column(db.String(5), nullable=True, default="+91")
    email = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    must_change_password = db.Column(db.Boolean, default=False, nullable=False)
    temp_password_expires_at = db.Column(db.DateTime, nullable=True)
    address = db.Column(db.Text, nullable=True)
    profile_completed = db.Column(db.Boolean, default=False, nullable=False)
    registration_fee_paid = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    applications = db.relationship("Application", backref="user", lazy=True)


class Application(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    applicant_name = db.Column(db.String(120), nullable=False)
    relation_type = db.Column(db.String(5), nullable=False)  # S/o, D/o, W/o
    relation_name = db.Column(db.String(120), nullable=False)
    address = db.Column(db.Text, nullable=False)
    loan_amount = db.Column(db.Integer, nullable=False)
    repayment_months = db.Column(db.Integer, nullable=False)
    aadhaar_file = db.Column(db.String(200), nullable=False)
    pan_file = db.Column(db.String(200), nullable=False)
    photo_file = db.Column(db.String(200), nullable=False)
    payment_file = db.Column(db.String(200), nullable=False)
    fee_amount = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), default="Pending")
    admin_remarks = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    application_reference = db.Column(db.String(30), unique=True, nullable=True)
    submission_token = db.Column(db.String(36), unique=True, nullable=True)


class ApplicationDraft(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True)
    data_json = db.Column(db.Text, nullable=False, default="{}")
    documents_json = db.Column(db.Text, nullable=False, default="{}")
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    application_id = db.Column(db.Integer, db.ForeignKey("application.id"), nullable=True)
    payment_type = db.Column(db.String(50), default="Processing Fee")
    amount = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), default="Pending")
    reference = db.Column(db.String(150), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    user = db.relationship("User", backref="payments")
    application = db.relationship("Application", backref="payments")


class Setting(db.Model):
    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.Text, default="")


def get_setting(key, default=""):
    s = db.session.get(Setting, key)
    return s.value if s else default


def set_setting(key, value):
    s = db.session.get(Setting, key)
    if s:
        s.value = value
    else:
        db.session.add(Setting(key=key, value=value))
    db.session.commit()


def ensure_database_columns():
    """Add newly introduced columns without recreating existing tables."""
    with db.engine.connect() as connection:
        if db.engine.url.get_backend_name() == "sqlite":
            existing_tables = {
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if "user" not in existing_tables or "application" not in existing_tables:
                return
            application_columns = {
                row._mapping["name"]
                for row in connection.exec_driver_sql("PRAGMA table_info(application)").fetchall()
            }
            if application_columns and "updated_at" not in application_columns:
                connection.exec_driver_sql("ALTER TABLE application ADD COLUMN updated_at DATETIME")
                connection.exec_driver_sql("UPDATE application SET updated_at = COALESCE(created_at, CURRENT_TIMESTAMP)")
            if application_columns and "application_reference" not in application_columns:
                connection.exec_driver_sql("ALTER TABLE application ADD COLUMN application_reference VARCHAR(30)")
            if application_columns and "submission_token" not in application_columns:
                connection.exec_driver_sql("ALTER TABLE application ADD COLUMN submission_token VARCHAR(36)")
            user_columns = {
                row._mapping["name"]
                for row in connection.exec_driver_sql("PRAGMA table_info(user)").fetchall()
            }
            if user_columns and "must_change_password" not in user_columns:
                connection.exec_driver_sql("ALTER TABLE user ADD COLUMN must_change_password BOOLEAN DEFAULT 0 NOT NULL")
            if user_columns and "temp_password_expires_at" not in user_columns:
                connection.exec_driver_sql("ALTER TABLE user ADD COLUMN temp_password_expires_at DATETIME")
            if user_columns and "country_code" not in user_columns:
                connection.exec_driver_sql("ALTER TABLE user ADD COLUMN country_code VARCHAR(5)")
                connection.exec_driver_sql("UPDATE user SET country_code = CASE WHEN substr(phone, 1, 3) = '+91' THEN '+91' ELSE country_code END")
            for column, definition in (
                ("address", "TEXT"),
                ("profile_completed", "BOOLEAN DEFAULT 0 NOT NULL"),
                ("registration_fee_paid", "BOOLEAN DEFAULT 0 NOT NULL"),
            ):
                if column not in user_columns:
                    connection.exec_driver_sql(f"ALTER TABLE user ADD COLUMN {column} {definition}")
            connection.commit()
        else:
            from sqlalchemy import inspect
            existing_application_columns = {column["name"] for column in inspect(db.engine).get_columns("application")}
            if "application_reference" not in existing_application_columns:
                connection.exec_driver_sql("ALTER TABLE application ADD COLUMN application_reference VARCHAR(30)")
            if "submission_token" not in existing_application_columns:
                connection.exec_driver_sql("ALTER TABLE application ADD COLUMN submission_token VARCHAR(36)")
            existing_user_columns = {column["name"] for column in inspect(db.engine).get_columns("user")}
            if "must_change_password" not in existing_user_columns:
                connection.exec_driver_sql("ALTER TABLE \"user\" ADD COLUMN must_change_password BOOLEAN DEFAULT FALSE NOT NULL")
            if "temp_password_expires_at" not in existing_user_columns:
                connection.exec_driver_sql("ALTER TABLE \"user\" ADD COLUMN temp_password_expires_at TIMESTAMP")
            if "country_code" not in existing_user_columns:
                connection.exec_driver_sql("ALTER TABLE \"user\" ADD COLUMN country_code VARCHAR(5)")
            for column, definition in (
                ("address", "TEXT"),
                ("profile_completed", "BOOLEAN DEFAULT FALSE NOT NULL"),
                ("registration_fee_paid", "BOOLEAN DEFAULT FALSE NOT NULL"),
            ):
                if column not in existing_user_columns:
                    connection.exec_driver_sql(f"ALTER TABLE \"user\" ADD COLUMN {column} {definition}")
            connection.commit()


def init_db():
    db.create_all()
    set_setting("fee", "36")
    if not get_setting("conditions"):
        set_setting("conditions", DEFAULT_CONDITIONS)
    if not User.query.filter_by(is_admin=True).first():
        if IS_PRODUCTION:
            admin_email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
            admin_password = os.environ.get("ADMIN_PASSWORD")
            if not admin_email or not admin_password:
                raise RuntimeError("ADMIN_EMAIL and ADMIN_PASSWORD must be configured before creating a production admin")
        else:
            admin_email = os.environ.get("ADMIN_EMAIL", "admin@loanapp.local").strip().lower()
            admin_password = os.environ.get("ADMIN_PASSWORD", "LocalAdmin!12345")
        db.session.add(User(name="Administrator", phone="0000000000", email=admin_email,
                            password_hash=generate_password_hash(admin_password), is_admin=True))
        db.session.commit()
    for user in User.query.filter_by(is_admin=False).all():
        if Application.query.filter_by(user_id=user.id).first() and not user.registration_fee_paid:
            user.registration_fee_paid = True
    for application in Application.query.filter(Application.application_reference.is_(None)).all():
        application.application_reference = application_reference(application.id)
    for application in Application.query.outerjoin(Payment, Payment.application_id == Application.id).filter(Payment.id.is_(None)).all():
        db.session.add(Payment(user_id=application.user_id, application_id=application.id,
                               payment_type="Processing Fee", amount=application.fee_amount or 36))
    db.session.commit()

# ---------- Helpers ----------
def normalize_e164(value, default_country="+91"):
    raw = (value or "").strip()
    if raw.startswith("+"):
        normalized = "+" + re.sub(r"\D", "", raw)
    else:
        digits = re.sub(r"\D", "", raw)
        country_digits = re.sub(r"\D", "", default_country or "+91")
        normalized = "+" + country_digits + digits
    if re.fullmatch(r"\+\d{8,15}", normalized):
        return normalized
    return None


def normalize_indian_mobile(value):
    phone = re.sub(r"[\s\-()]", "", (value or "").strip())
    if phone.startswith("+91"):
        phone = phone[3:]
    elif phone.startswith("91") and len(phone) == 12:
        phone = phone[2:]
    return phone if re.fullmatch(r"[6-9]\d{9}", phone) else None


COUNTRY_CODES = [
    ("India", "+91"), ("United States", "+1"), ("United Kingdom", "+44"),
    ("Australia", "+61"), ("Bangladesh", "+880"), ("Brazil", "+55"),
    ("Canada", "+1"), ("China", "+86"), ("France", "+33"), ("Germany", "+49"),
    ("Indonesia", "+62"), ("Ireland", "+353"), ("Italy", "+39"), ("Japan", "+81"),
    ("Malaysia", "+60"), ("Nepal", "+977"), ("Netherlands", "+31"),
    ("New Zealand", "+64"), ("Nigeria", "+234"), ("Pakistan", "+92"),
    ("Philippines", "+63"), ("Saudi Arabia", "+966"), ("Singapore", "+65"),
    ("South Africa", "+27"), ("South Korea", "+82"), ("Spain", "+34"),
    ("Sri Lanka", "+94"), ("UAE", "+971"), ("UAE", "+971"), ("Vietnam", "+84"),
]


def normalize_country_code(value):
    code = "+" + re.sub(r"\D", "", value or "")
    return code if any(code == dialing_code for _, dialing_code in COUNTRY_CODES) else None


def normalize_national_phone(value, country_code="+91"):
    digits = re.sub(r"\D", "", value or "")
    dialing_digits = country_code.lstrip("+")
    if digits.startswith(dialing_digits) and len(digits) > len(dialing_digits):
        digits = digits[len(dialing_digits):]
    return digits if digits.isdigit() and 7 <= len(digits) <= 15 else None


@app.before_request
def before():
    uid = session.get("uid")
    g.user = db.session.get(User, uid) if uid else None
    if request.method == "POST":
        token = session.get("_csrf")
        if not token or token != request.form.get("_csrf"):
            abort(400, "Invalid or missing CSRF token. Go back and refresh the page.")
    if g.user and not g.user.is_admin and g.user.must_change_password:
        allowed = {"change_password", "logout", "static"}
        if request.endpoint not in allowed:
            return redirect(url_for("change_password"))


@app.after_request
def secure_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    return resp


@app.context_processor
def inject():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    lang = session.get("lang", "en")

    def t(key):
        return TRANSLATIONS.get(lang, {}).get(key) or TRANSLATIONS["en"].get(key, key)

    return dict(t=t, csrf_token=session["_csrf"], user=g.user, lang=lang, LANGS=LANGS,
                now=datetime.utcnow(), country_choices=COUNTRY_CODES)


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not g.user:
            flash("Please login first.", "error")
            return redirect(url_for("login"))
        return f(*a, **kw)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not g.user or not g.user.is_admin:
            return redirect(url_for("admin_login"))
        return f(*a, **kw)
    return wrapper


def generate_temporary_password():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%^&*"
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(16))
        if (any(char.islower() for char in password)
                and any(char.isupper() for char in password)
                and any(char.isdigit() for char in password)
                and any(char in "!@#$%^&*" for char in password)):
            return password


def normalize_customer_phone(value):
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits if re.fullmatch(r"[6-9]\d{9}", digits) else None


def display_customer_phone(phone):
    if phone and phone.startswith("+"):
        matched = sorted({code for _, code in COUNTRY_CODES}, key=len, reverse=True)
        for code in matched:
            if phone.startswith(code):
                return f"{code} {phone[len(code):]}"
    normalized = normalize_customer_phone(phone)
    return "+91 " + normalized if normalized else phone


SIGNATURES = {"jpg": b"\xff\xd8", "jpeg": b"\xff\xd8", "png": b"\x89PNG", "pdf": b"%PDF"}


def save_upload(file, allowed):
    """Validate and save an uploaded file with a random name. Returns the name or None."""
    if not file or not file.filename:
        return None
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in allowed:
        return None
    head = file.stream.read(8)
    file.stream.seek(0)
    if not head.startswith(SIGNATURES[ext]):
        return None
    name = f"{uuid.uuid4().hex}.{ext}"
    file.save(os.path.join(UPLOAD_DIR, name))
    return name

# ---------- Public / user routes ----------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/lang/<code>")
def set_lang(code):
    if code in LANGS:
        session["lang"] = code
    return redirect(request.referrer or url_for("index"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        submitted_phone = request.form.get("phone", "").strip()
        country_code = normalize_country_code(request.form.get("country_code", "+91"))
        national_phone = normalize_national_phone(submitted_phone, country_code or "+91")
        phone = (country_code or "+91") + national_phone if country_code and national_phone else None
        email = request.form.get("email", "").strip().lower()
        address = request.form.get("address", "").strip()
        pw = request.form.get("password", "")
        pw2 = request.form.get("confirm_password", "")
        field_errors = {}
        if len(name) < 2:
            field_errors["name"] = "Please enter your name."
        if not phone:
            field_errors["phone"] = "Invalid mobile number. Please enter a valid number."
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            field_errors["email"] = "Enter a valid email address."
        if len(pw) < 8:
            field_errors["password"] = "Password must be at least 8 characters."
        if pw != pw2:
            field_errors["confirm_password"] = "Passwords do not match."
        legacy_phone = phone[len(country_code):] if phone and country_code else phone
        existing_phone_candidates = [phone, legacy_phone]
        if phone and User.query.filter(or_(User.email == email, User.phone.in_(existing_phone_candidates))).first():
            field_errors.setdefault("email", "An account with this email or phone already exists.")
        if field_errors:
            for message in field_errors.values():
                flash(message, "error")
            return render_template("register.html", form=request.form, field_errors=field_errors)
        u = User(name=name, phone=phone, country_code=country_code, email=email,
                 address=address, profile_completed=bool(address),
                 password_hash=generate_password_hash(pw))
        db.session.add(u)
        db.session.commit()
        session.clear()
        session["uid"] = u.id
        flash("Account created. Welcome!", "success")
        return redirect(url_for("dashboard"))
    return render_template("register.html", form={}, field_errors={})


def do_login(admin):
    if request.method == "POST":
        ident = request.form.get("identifier", "").strip().lower()
        pw = request.form.get("password", "")
        phone_candidates = [ident, "+" + re.sub(r"\D", "", ident)]
        if re.fullmatch(r"[6-9]\d{9}", ident):
            phone_candidates.append("+91" + ident)
        u = User.query.filter(or_(User.email == ident, User.phone.in_(phone_candidates))).first()
        if u and check_password_hash(u.password_hash, pw) and bool(u.is_admin) == admin:
            if not admin and u.must_change_password and (
                    not u.temp_password_expires_at or u.temp_password_expires_at < datetime.utcnow()):
                flash("Temporary password has expired. Please contact admin.", "error")
                return render_template("login.html", admin=False)
            session.clear()
            session["uid"] = u.id
            return redirect(url_for("admin_dashboard" if admin else "dashboard"))
        flash("Invalid credentials.", "error")
    return render_template("login.html", admin=admin)


@app.route("/login", methods=["GET", "POST"])
def login():
    return do_login(False)


def smtp_message(customer, subject, body):
    smtp_host = os.environ.get("MAIL_SERVER", os.environ.get("SMTP_HOST", "")).strip()
    smtp_port = int(os.environ.get("MAIL_PORT", os.environ.get("SMTP_PORT", "587")))
    smtp_user = os.environ.get("MAIL_USERNAME", os.environ.get("SMTP_USERNAME", "")).strip()
    smtp_password = os.environ.get("MAIL_PASSWORD", os.environ.get("SMTP_PASSWORD", ""))
    sender = os.environ.get("MAIL_DEFAULT_SENDER", os.environ.get("SMTP_FROM", smtp_user)).strip()
    use_tls = os.environ.get("MAIL_USE_TLS", os.environ.get("SMTP_USE_TLS", "true")).lower() == "true"
    if not smtp_host or not smtp_user or not smtp_password or not sender:
        raise RuntimeError("Email service is not configured.")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = customer.email
    message.set_content(body)
    server = smtplib.SMTP(smtp_host, smtp_port, timeout=20)
    try:
        if use_tls:
            server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(message)
    finally:
        server.quit()


def phone_digits_match(left, right):
    left_digits = re.sub(r"\D", "", left or "")
    right_digits = re.sub(r"\D", "", right or "")
    return bool(left_digits and right_digits) and left_digits[-10:] == right_digits[-10:]


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        customer = User.query.filter(db.func.lower(User.email) == email, User.is_admin == False).first()
        if not customer or not phone_digits_match(customer.phone, phone):
            flash("No account found with this email.", "error")
        elif password != confirm_password:
            flash("Passwords do not match.", "error")
        elif len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
        else:
            customer.password_hash = generate_password_hash(password)
            customer.must_change_password = False
            customer.temp_password_expires_at = None
            db.session.commit()
            flash("Password updated successfully", "success")
            return redirect(url_for("login"))
    return render_template("forgot_password.html", form=request.form if request.method == "POST" else {})


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    return do_login(True)


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if g.user.is_admin or not g.user.must_change_password:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
        elif password != confirm_password:
            flash("Passwords do not match.", "error")
        elif check_password_hash(g.user.password_hash, password):
            flash("New password must differ from the temporary password.", "error")
        else:
            g.user.password_hash = generate_password_hash(password)
            g.user.must_change_password = False
            g.user.temp_password_expires_at = None
            db.session.commit()
            flash("Password changed successfully.", "success")
            return redirect(url_for("dashboard"))
    return render_template("change_password.html")


@app.route("/logout")
def logout():
    lang = session.get("lang")
    session.clear()
    if lang:
        session["lang"] = lang
    return redirect(url_for("index"))


@app.route("/dashboard")
@login_required
def dashboard():
    if g.user.is_admin:
        return redirect(url_for("admin_dashboard"))
    apps_ = Application.query.filter_by(user_id=g.user.id).order_by(Application.created_at.desc()).all()
    return render_template("dashboard.html", apps=apps_)


def application_reference(application_id):
    return f"MYL-{datetime.utcnow():%Y%m%d}-{application_id:06d}"


@app.route("/apply/draft", methods=["POST"])
@login_required
def save_application_draft_route():
    if g.user.is_admin:
        abort(403)
    uploaded_documents = {}
    document_fields = {
        "aadhaar": {"jpg", "jpeg", "png", "pdf"},
        "pan": {"jpg", "jpeg", "png", "pdf"},
        "photo": {"jpg", "jpeg", "png"},
        "payment": {"jpg", "jpeg", "png"},
    }
    for field, allowed in document_fields.items():
        saved = save_upload(request.files.get(field), allowed)
        if saved:
            uploaded_documents[field] = saved
    save_application_draft(g.user, request.form, uploaded_documents)
    return jsonify({"success": True, "message": "Draft saved successfully."})


def load_application_draft(user):
    draft = ApplicationDraft.query.filter_by(user_id=user.id).first()
    if not draft:
        return {}, {}
    try:
        return json.loads(draft.data_json or "{}"), json.loads(draft.documents_json or "{}")
    except (TypeError, ValueError):
        return {}, {}


def save_application_draft(user, form_data, uploaded_documents=None):
    draft = ApplicationDraft.query.filter_by(user_id=user.id).first() or ApplicationDraft(user_id=user.id)
    draft.data_json = json.dumps({
        key: str(form_data.get(key, "")).strip()
        for key in ("applicant_name", "relation_type", "relation_name", "address", "loan_amount", "repayment_months")
    }, ensure_ascii=False)
    _, documents = load_application_draft(user)
    if uploaded_documents:
        documents.update(uploaded_documents)
    draft.documents_json = json.dumps(documents, ensure_ascii=False)
    db.session.add(draft)
    db.session.commit()


@app.route("/apply", methods=["GET", "POST"])
@login_required
def apply_loan():
    if g.user.is_admin:
        return redirect(url_for("admin_dashboard"))
    set_setting("fee", "36")
    fee = 36
    qr = get_setting("qr_file")
    conditions = get_setting("conditions", DEFAULT_CONDITIONS)
    draft_data, draft_documents = load_application_draft(g.user)
    if not draft_data:
        draft_data = {
            "applicant_name": g.user.name,
            "address": g.user.address or "",
        }
    registration_fee_required = not g.user.registration_fee_paid
    registration_fee_ready = bool(qr) if registration_fee_required else True
    if not qr and registration_fee_required:
        if request.method == "POST" and registration_fee_required:
            flash("Payment cannot start until the administrator uploads the QR code.", "error")
        return render_template("apply.html", fee=fee, qr=None, conditions=conditions, form=request.form,
                               field_errors={}, uploaded_files=draft_documents,
                               qr_ready=registration_fee_ready, upi_id=get_setting("upi_id", "").strip(),
                               registration_fee_required=registration_fee_required)
    if request.method == "GET":
        return render_template("apply.html", fee=fee, qr=qr, conditions=conditions, form=draft_data,
                               field_errors={}, uploaded_files=draft_documents, qr_ready=True,
                               upi_id=get_setting("upi_id", "").strip(),
                               registration_fee_required=registration_fee_required,
                               submission_token=uuid.uuid4().hex)
    if request.method == "POST":
        f = request.form
        name = f.get("applicant_name", "").strip()
        rel_type = f.get("relation_type", "")
        rel_name = f.get("relation_name", "").strip()
        address = f.get("address", "").strip()
        errors = []
        field_errors = {}
        try:
            amount = int(f.get("loan_amount", ""))
            months = int(f.get("repayment_months", ""))
        except ValueError:
            amount = months = 0
        if len(name) < 2:
            errors.append("Enter your name."); field_errors["applicant_name"] = "Enter your name."
        if rel_type not in ("S/o", "D/o", "W/o"):
            errors.append("Select S/o, D/o or W/o."); field_errors["relation_type"] = "Select S/o, D/o or W/o."
        if len(rel_name) < 2:
            errors.append("Enter father/husband name."); field_errors["relation_name"] = "Enter father/husband name."
        if len(address) < 10:
            errors.append("Enter your full address."); field_errors["address"] = "Enter your full address."
        if not MIN_LOAN <= amount <= MAX_LOAN:
            errors.append(f"Loan amount must be between ₹{MIN_LOAN:,} and ₹{MAX_LOAN:,}."); field_errors["loan_amount"] = "Check the loan amount."
        if not MIN_MONTHS <= months <= MAX_MONTHS:
            errors.append(f"Repayment period must be {MIN_MONTHS}-{MAX_MONTHS} months."); field_errors["repayment_months"] = "Check the repayment period."
        if not f.get("agree"):
            errors.append("You must agree to the conditions."); field_errors["agree"] = "You must agree to the conditions."
        uploaded = draft_documents.copy()
        document_fields = (
            ("aadhaar", {"jpg", "jpeg", "png", "pdf"}, "Upload a valid Aadhaar file (JPG, PNG or PDF)."),
            ("pan", {"jpg", "jpeg", "png", "pdf"}, "Upload a valid PAN file (JPG, PNG or PDF)."),
            ("photo", {"jpg", "jpeg", "png"}, "Upload a valid photo (JPG or PNG)."),
            ("payment", {"jpg", "jpeg", "png"}, "Upload the payment screenshot (JPG or PNG)."),
        )
        saved_files = {}
        for field, allowed, error_message in document_fields:
            saved = save_upload(request.files.get(field), allowed)
            if saved:
                uploaded[field] = saved
                session["pending_application_files"] = uploaded
            if not uploaded.get(field) and (field != "payment" or registration_fee_required):
                errors.append(error_message)
                field_errors[field] = error_message
            saved_files[field] = uploaded.get(field)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("apply.html", fee=fee, qr=qr, conditions=conditions, form=f,
                                   field_errors=field_errors, uploaded_files=uploaded)
        aadhaar, pan, photo, payment = (saved_files[n] if saved_files.get(n) else uploaded.get(n) for n in ("aadhaar", "pan", "photo", "payment"))
        if not registration_fee_required:
            payment = payment or ""
        submission_token = request.form.get("submission_token", "").strip()
        existing_application = Application.query.filter_by(submission_token=submission_token).first() if submission_token else None
        if existing_application:
            flash(f"Application submitted successfully. Reference: {existing_application.application_reference}.", "success")
            return redirect(url_for("dashboard"))
        loan_application = Application(
            user_id=g.user.id, applicant_name=name, relation_type=rel_type, relation_name=rel_name,
            address=address, loan_amount=amount, repayment_months=months, aadhaar_file=aadhaar,
            pan_file=pan, photo_file=photo, payment_file=payment,
            fee_amount=(fee if registration_fee_required else 0))
        db.session.add(loan_application)
        db.session.flush()
        loan_application.application_reference = application_reference(loan_application.id)
        loan_application.submission_token = submission_token or uuid.uuid4().hex
        if registration_fee_required:
            g.user.registration_fee_paid = True
            db.session.add(Payment(
                user_id=g.user.id, application_id=loan_application.id,
                payment_type="Registration Fee", amount=fee, status="Pending",
                reference=loan_application.application_reference))
        db.session.delete(ApplicationDraft.query.filter_by(user_id=g.user.id).first())
        session.pop("pending_application_files", None)
        db.session.commit()
        flash(f"Application submitted successfully. Reference: {loan_application.application_reference}.", "success")
        return redirect(url_for("dashboard"))
    return render_template("apply.html", fee=fee, qr=qr, conditions=conditions, form={},
                           field_errors={}, uploaded_files=session.get("pending_application_files", {}),
                           qr_ready=True, upi_id=get_setting("upi_id", "").strip())


@app.route("/files/<name>")
@login_required
def files(name):
    if not re.fullmatch(r"[0-9a-f]{32}\.(jpg|jpeg|png|pdf)", name):
        abort(404)
    if not g.user.is_admin:
        owns = Application.query.filter(
            Application.user_id == g.user.id,
            or_(Application.aadhaar_file == name, Application.pan_file == name,
                Application.photo_file == name, Application.payment_file == name)).first()
        if not owns:
            abort(404)
    return send_from_directory(UPLOAD_DIR, name)


@app.route("/applications/<int:application_id>/edit", methods=["GET", "POST"])
@login_required
def edit_application(application_id):
    if g.user.is_admin:
        return redirect(url_for("admin_dashboard"))
    loan_application = db.session.get(Application, application_id)
    if loan_application is None or loan_application.user_id != g.user.id:
        abort(404)

    existing_files = {
        "aadhaar": loan_application.aadhaar_file,
        "pan": loan_application.pan_file,
        "photo": loan_application.photo_file,
        "payment": loan_application.payment_file,
    }
    if request.method == "GET":
        form = {
            "applicant_name": loan_application.applicant_name,
            "relation_type": loan_application.relation_type,
            "relation_name": loan_application.relation_name,
            "address": loan_application.address,
            "loan_amount": loan_application.loan_amount,
            "repayment_months": loan_application.repayment_months,
            "agree": True,
        }
        return render_template("apply.html", form=form, field_errors={}, uploaded_files=existing_files,
                               editing=True, application=loan_application,
                               conditions=get_setting("conditions", DEFAULT_CONDITIONS),
                               qr_ready=True, upi_id=get_setting("upi_id", "").strip())

    f = request.form
    name = f.get("applicant_name", "").strip()
    relation_type = f.get("relation_type", "")
    relation_name = f.get("relation_name", "").strip()
    address = f.get("address", "").strip()
    field_errors = {}
    errors = []
    try:
        amount = int(f.get("loan_amount", ""))
        months = int(f.get("repayment_months", ""))
    except ValueError:
        amount = months = 0
    if len(name) < 2:
        errors.append("Enter your name."); field_errors["applicant_name"] = "Enter your name."
    if relation_type not in ("S/o", "D/o", "W/o"):
        errors.append("Select S/o, D/o or W/o."); field_errors["relation_type"] = "Select S/o, D/o or W/o."
    if len(relation_name) < 2:
        errors.append("Enter father/husband name."); field_errors["relation_name"] = "Enter father/husband name."
    if len(address) < 10:
        errors.append("Enter your full address."); field_errors["address"] = "Enter your full address."
    if not MIN_LOAN <= amount <= MAX_LOAN:
        errors.append(f"Loan amount must be between ₹{MIN_LOAN:,} and ₹{MAX_LOAN:,}."); field_errors["loan_amount"] = "Check the loan amount."
    if not MIN_MONTHS <= months <= MAX_MONTHS:
        errors.append(f"Repayment period must be {MIN_MONTHS}-{MAX_MONTHS} months."); field_errors["repayment_months"] = "Check the repayment period."
    if not f.get("agree"):
        errors.append("You must agree to the conditions."); field_errors["agree"] = "You must agree to the conditions."

    document_fields = (
        ("aadhaar", {"jpg", "jpeg", "png", "pdf"}),
        ("pan", {"jpg", "jpeg", "png", "pdf"}),
        ("photo", {"jpg", "jpeg", "png"}),
        ("payment", {"jpg", "jpeg", "png"}),
    )
    uploaded_files = existing_files.copy()
    for field, allowed in document_fields:
        saved = save_upload(request.files.get(field), allowed)
        if saved:
            uploaded_files[field] = saved
        if not uploaded_files.get(field):
            errors.append(f"Upload a valid {field} file.")
            field_errors[field] = "Upload a valid file."

    if errors:
        for error in errors:
            flash(error, "error")
        return render_template("apply.html", form=f, field_errors=field_errors,
                               uploaded_files=uploaded_files, editing=True, application=loan_application,
                               conditions=get_setting("conditions", DEFAULT_CONDITIONS),
                               qr_ready=True, upi_id=get_setting("upi_id", "").strip())

    loan_application.applicant_name = name
    loan_application.relation_type = relation_type
    loan_application.relation_name = relation_name
    loan_application.address = address
    loan_application.loan_amount = amount
    loan_application.repayment_months = months
    loan_application.aadhaar_file = uploaded_files["aadhaar"]
    loan_application.pan_file = uploaded_files["pan"]
    loan_application.photo_file = uploaded_files["photo"]
    loan_application.payment_file = uploaded_files["payment"]
    db.session.commit()
    flash("Application updated successfully!", "success")
    return redirect(url_for("dashboard"))


@app.route("/qr")
@login_required
def qr_image():
    name = get_setting("qr_file")
    if not name:
        abort(404)
    return send_from_directory(UPLOAD_DIR, name)

# ---------- Admin routes ----------
@app.route("/admin")
@admin_required
def admin_dashboard():
    status_param = request.args.get("status", "").strip().lower()
    status_lookup = {
        "pending": "Pending",
        "under-review": "Under Review",
        "approved": "Approved",
        "rejected": "Rejected",
    }
    status = status_lookup.get(status_param)
    q = Application.query
    if status in STATUSES:
        q = q.filter_by(status=status)
    apps_ = q.order_by(Application.created_at.desc()).all()
    counts = {s: Application.query.filter_by(status=s).count() for s in STATUSES}
    return render_template("admin_dashboard.html", apps=apps_, counts=counts,
                           total=Application.query.count(), users=User.query.filter_by(is_admin=False).count(),
                           current=status)


@app.route("/admin/register-customers", methods=["GET", "POST"])
@admin_required
def admin_register_customers():
    generated_password = None
    generated_customer = None
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        submitted_phone = request.form.get("phone", "").strip()
        country_code = normalize_country_code(request.form.get("country_code", "+91"))
        national_phone = normalize_national_phone(submitted_phone, country_code or "+91")
        phone = (country_code + national_phone) if country_code and national_phone else None
        email = request.form.get("email", "").strip().lower()
        if len(name) < 2:
            flash("Customer name must be at least 2 characters.", "error")
        elif not phone:
            flash("Enter a valid Indian mobile number.", "error")
        elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            flash("Enter a valid email address.", "error")
        elif User.query.filter(or_(User.email == email, User.phone.in_([phone, national_phone]))).first():
            flash("A customer with this email or phone already exists.", "error")
        else:
            generated_password = generate_temporary_password()
            generated_customer = User(
                name=name, phone=phone, country_code=country_code, email=email,
                password_hash=generate_password_hash(generated_password),
                is_admin=False, must_change_password=True,
                temp_password_expires_at=datetime.utcnow() + timedelta(hours=24),
            )
            db.session.add(generated_customer)
            db.session.commit()
            flash("Customer registered successfully.", "success")
    customers = User.query.filter_by(is_admin=False).order_by(User.created_at.desc()).all()
    edit_id = request.args.get("edit", type=int)
    edit_customer = db.session.get(User, edit_id) if edit_id else None
    if edit_customer and edit_customer.is_admin:
        edit_customer = None
    return render_template(
        "admin_register_customers.html", customers=customers, form=request.form,
        generated_password=generated_password, generated_customer=generated_customer,
        edit_customer=edit_customer, display_phone=display_customer_phone,
    )


@app.route("/admin/customers/<int:user_id>/edit", methods=["POST"])
@login_required
@admin_required
def admin_edit_customer(user_id):
    customer = db.session.get(User, user_id)
    if customer is None or customer.is_admin:
        abort(404)
    name = request.form.get("name", "").strip()
    country_code = normalize_country_code(request.form.get("country_code", customer.country_code or "+91"))
    national_phone = normalize_national_phone(request.form.get("phone", ""), country_code or "+91")
    phone = (country_code + national_phone) if country_code and national_phone else None
    email = request.form.get("email", "").strip().lower()
    if len(name) < 2:
        flash("Customer name must be at least 2 characters.", "error")
    elif phone is None:
        flash("Enter a valid Indian mobile number.", "error")
    elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        flash("Enter a valid email address.", "error")
    elif User.query.filter(User.id != customer.id, or_(User.email == email, User.phone.in_([phone, national_phone]))).first():
        flash("Another customer uses this email or phone.", "error")
    else:
        customer.name = name
        customer.phone = phone
        customer.country_code = country_code
        customer.email = email
        db.session.commit()
        flash("Customer details updated.", "success")
    return redirect(url_for("admin_register_customers"))


@app.route("/admin/customers/<int:user_id>/delete", methods=["POST"])
@login_required
@admin_required
def admin_delete_customer(user_id):
    customer = db.session.get(User, user_id)
    if customer is None or customer.is_admin:
        abort(404)
    if customer.applications:
        flash("This customer has loan applications and cannot be deleted.", "error")
    else:
        db.session.delete(customer)
        db.session.commit()
        flash("Customer deleted successfully.", "success")
    return redirect(url_for("admin_register_customers"))


def assign_temporary_password(customer):
    return generate_temporary_password()


def send_temporary_password_email(customer, password):
    smtp_host = os.environ.get("MAIL_SERVER", os.environ.get("SMTP_HOST", "")).strip()
    smtp_port = int(os.environ.get("MAIL_PORT", os.environ.get("SMTP_PORT", "587")))
    smtp_user = os.environ.get("MAIL_USERNAME", os.environ.get("SMTP_USERNAME", "")).strip()
    smtp_password = os.environ.get("MAIL_PASSWORD", os.environ.get("SMTP_PASSWORD", ""))
    sender = os.environ.get("MAIL_DEFAULT_SENDER", os.environ.get("SMTP_FROM", smtp_user)).strip()
    use_tls = os.environ.get(
        "MAIL_USE_TLS", os.environ.get("SMTP_USE_TLS", "true")
    ).lower() == "true"
    login_url = request.host_url.rstrip("/") + url_for("login")

    if not smtp_host or not sender:
        raise RuntimeError("Email sharing is not configured.")
    if not smtp_user or not smtp_password:
        raise RuntimeError("Email authentication is not configured.")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", customer.email or ""):
        raise RuntimeError("The customer has no valid email address.")

    message = EmailMessage()
    message["Subject"] = "Your temporary login password"
    message["From"] = sender
    message["To"] = customer.email
    latest_application = Application.query.filter_by(user_id=customer.id).order_by(Application.created_at.desc()).first()
    reference = f"LA-{latest_application.id}" if latest_application else f"Customer-{customer.id}"
    message.set_content(
        f"Hello {customer.name},\n\n"
        f"Your temporary password is: {password}\n"
        "This password is valid for 24 hours only.\n"
        f"Login with this email: {customer.email}\n"
        f"Login URL: {login_url}\n\n"
        "For your security, change this password after logging in."
        f"\nAccount reference: {reference}"
    )

    server = smtplib.SMTP(smtp_host, smtp_port, timeout=20)
    try:
        if use_tls:
            server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(message)
    finally:
        server.quit()


@app.route("/admin/customers/<int:user_id>/share", methods=["POST"])
@app.route("/admin/customers/<int:user_id>/generate-password", methods=["POST"])
@login_required
@admin_required
def admin_share_customer_password(user_id):
    customer = db.session.get(User, user_id)
    if customer is None or customer.is_admin:
        abort(404)

    now_epoch = datetime.utcnow().timestamp()
    last_request = session.get("temporary_password_last_request")
    if last_request and now_epoch - float(last_request) < 30:
        flash("Please wait before sending another temporary password.", "error")
        return redirect(url_for("admin_register_customers"))

    password = assign_temporary_password(customer)
    try:
        send_temporary_password_email(customer, password)
        customer.password_hash = generate_password_hash(password)
        customer.must_change_password = True
        customer.temp_password_expires_at = datetime.utcnow() + timedelta(hours=24)
        db.session.commit()
        session["temporary_password_last_request"] = now_epoch
        flash(f"Temporary password sent to {customer.email}.", "success")
    except (RuntimeError, smtplib.SMTPException, OSError, ValueError, SQLAlchemyError):
        db.session.rollback()
        flash("Temporary password email could not be sent. The existing password was not changed.", "error")
    return redirect(url_for("admin_register_customers"))


@app.route("/admin/applications", methods=["GET", "POST"])
@admin_required
def admin_review_applications():
    if request.method == "POST":
        application = db.session.get(Application, request.form.get("application_id", type=int))
        if application is None:
            abort(404)
        application.status = "Under Review"
        application.updated_at = datetime.utcnow()
        db.session.commit()
        flash("Application moved to Under Review.", "success")
        return redirect(url_for("admin_review_applications"))
    applications = Application.query.filter_by(status="Pending").order_by(Application.created_at.desc()).all()
    return render_template("admin_applications.html", applications=applications)


@app.route("/admin/payments", methods=["GET", "POST"])
@admin_required
def admin_payments():
    if request.method == "POST":
        payment = db.session.get(Payment, request.form.get("payment_id", type=int))
        if payment is None:
            abort(404)
        status = request.form.get("status")
        if status not in ("Verified", "Rejected"):
            abort(400)
        payment.status = status
        payment.reference = request.form.get("reference", "").strip()[:150]
        payment.updated_at = datetime.utcnow()
        db.session.commit()
        flash("Payment verification status updated.", "success")
        return redirect(url_for("admin_payments"))
    payments = Payment.query.order_by(Payment.created_at.desc()).all()
    return render_template("admin_payments.html", payments=payments)


@app.route("/admin/update-status", methods=["GET", "POST"])
@admin_required
def admin_update_status():
    if request.method == "POST":
        application = db.session.get(Application, request.form.get("application_id", type=int))
        if application is None:
            abort(404)
        status = request.form.get("status")
        if status not in STATUSES:
            abort(400)
        application.status = status
        application.admin_remarks = request.form.get("remarks", "").strip()[:1000]
        application.updated_at = datetime.utcnow()
        db.session.commit()
        flash("Application status updated.", "success")
        return redirect(url_for("admin_update_status"))
    applications = Application.query.order_by(Application.created_at.desc()).all()
    return render_template("admin_update_status.html", applications=applications, statuses=STATUSES)


@app.route("/admin/application/<int:app_id>", methods=["GET", "POST"])
@admin_required
def admin_application(app_id):
    a = db.session.get(Application, app_id) or abort(404)
    if request.method == "POST":
        status = request.form.get("action") or request.form.get("status")
        status_lookup = {"approve": "Approved", "reject": "Rejected", "under_review": "Under Review"}
        status = status_lookup.get(status, status)
        if status not in STATUSES:
            abort(400)
        a.status = status
        a.admin_remarks = request.form.get("remarks", "").strip()[:1000]
        a.updated_at = datetime.utcnow()
        db.session.commit()
        flash("Application updated.", "success")
        return redirect(url_for("admin_application", app_id=a.id))
    return render_template("admin_application.html", a=a, statuses=STATUSES)


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        fee = "36"
        set_setting("fee", fee)
        set_setting("conditions", request.form.get("conditions", "").strip()[:5000])
        upi_id = request.form.get("upi_id", "").strip()
        if upi_id and not re.fullmatch(r"[A-Za-z0-9.\-_]{2,256}@[A-Za-z0-9.\-_]{2,64}", upi_id):
            flash("Enter a valid UPI ID, such as merchant@bank.", "error")
            return redirect(url_for("admin_settings"))
        set_setting("upi_id", upi_id)
        qr = save_upload(request.files.get("qr"), {"jpg", "jpeg", "png"})
        if request.files.get("qr") and request.files["qr"].filename and not qr:
            flash("QR must be a valid JPG or PNG image.", "error")
        elif qr:
            set_setting("qr_file", qr)
        if not get_setting("qr_file"):
            flash("A payment QR code is required before saving payment settings.", "error")
            return redirect(url_for("admin_settings"))
        flash("Settings saved.", "success")
        return redirect(url_for("admin_settings"))
    return render_template("admin_settings.html", fee=get_setting("fee", "36"),
                           conditions=get_setting("conditions", DEFAULT_CONDITIONS),
                           qr=get_setting("qr_file"), upi_id=get_setting("upi_id", "").strip())


with app.app_context():
    ensure_database_columns()
    init_db()

if __name__ == "__main__":
    app.run(debug=not IS_PRODUCTION)
