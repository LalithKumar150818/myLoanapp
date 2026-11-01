import os
import re
import secrets
import uuid
import hashlib
import hmac
import time
from datetime import datetime
from functools import wraps
import json
import urllib.request
import urllib.parse
import urllib.error

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_
from werkzeug.security import check_password_hash, generate_password_hash

from translations import LANGS, TRANSLATIONS

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
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

MIN_LOAN, MAX_LOAN = 1000, 1000000
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
    email = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
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


def init_db():
    db.create_all()
    if not get_setting("fee"):
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

# ---------- Helpers ----------
OTP_LENGTH = 6
OTP_EXPIRY_SECONDS = 300
OTP_RESEND_SECONDS = 60
OTP_MAX_ATTEMPTS = 5
LAST_DEVELOPMENT_SMS_OTP = None


def normalize_indian_mobile(value):
    phone = re.sub(r"[\s\-()]", "", (value or "").strip())
    if phone.startswith("+91"):
        phone = phone[3:]
    elif phone.startswith("91") and len(phone) == 12:
        phone = phone[2:]
    return phone if re.fullmatch(r"[6-9]\d{9}", phone) else None


def otp_digest(phone, code):
    value = f"{phone}:{code}".encode("utf-8")
    key = app.config["SECRET_KEY"].encode("utf-8")
    return hmac.new(key, value, hashlib.sha256).hexdigest()


def send_sms(phone, message):
    global LAST_DEVELOPMENT_SMS_OTP
    provider = os.environ.get("SMS_PROVIDER", "").strip().lower()
    if not provider and os.environ.get("SMS_GATEWAY_URL"):
        provider = "webhook"
    if not provider and all(os.environ.get(name) for name in (
        "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"
    )):
        provider = "twilio"
    gateway_phone = "+91" + phone

    if provider == "twilio":
        account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
        auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
        sender = os.environ.get("TWILIO_FROM_NUMBER")
        if not all((account_sid, auth_token, sender)):
            raise RuntimeError("Twilio SMS configuration is incomplete")
        url = f"https://api.twilio.com/2010-04-01/Accounts/{urllib.parse.quote(account_sid)}/Messages.json"
        payload = urllib.parse.urlencode({"To": gateway_phone, "From": sender, "Body": message}).encode("utf-8")
        credentials = f"{account_sid}:{auth_token}".encode("utf-8")
        import base64
        authorization = base64.b64encode(credentials).decode("ascii")
        req = urllib.request.Request(url, data=payload, headers={
            "Authorization": "Basic " + authorization,
            "Content-Type": "application/x-www-form-urlencoded",
        }, method="POST")
        with urllib.request.urlopen(req, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
        if result.get("error_code") or result.get("status") in ("failed", "undelivered"):
            raise RuntimeError("Twilio rejected the SMS request")
        return

    sms_gateway = os.environ.get("SMS_GATEWAY_URL")
    if provider == "webhook" and sms_gateway:
        payload = json.dumps({
            "phone": gateway_phone,
            "mobile": gateway_phone,
            "message": message,
        }).encode("utf-8")
        req = urllib.request.Request(sms_gateway, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as response:
            response.read()
        return

    if os.environ.get("OTP_DEV_MODE", "").lower() == "true":
        otp_match = re.search(r"\b\d{6}\b", message)
        LAST_DEVELOPMENT_SMS_OTP = otp_match.group(0) if otp_match else None
        print(f"Development OTP SMS to {gateway_phone}: {message}")
        return

    raise RuntimeError("SMS provider is not configured")


@app.before_request
def before():
    uid = session.get("uid")
    g.user = db.session.get(User, uid) if uid else None
    if request.method == "POST":
        token = session.get("_csrf")
        if not token or token != request.form.get("_csrf"):
            abort(400, "Invalid or missing CSRF token. Go back and refresh the page.")


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

    return dict(t=t, csrf_token=session["_csrf"], user=g.user, lang=lang, LANGS=LANGS)


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


@app.route("/register/send-mobile-otp", methods=["POST"])
def send_mobile_otp():
    phone = normalize_indian_mobile(request.form.get("phone", ""))
    if not phone:
        return jsonify(ok=False, field="phone", message="Invalid mobile number. Please enter a valid number."), 400

    now = time.time()
    pending = session.get("mobile_otp") or {}
    if pending.get("phone") == phone and now < pending.get("resend_at", 0):
        retry_after = max(1, int(pending["resend_at"] - now))
        return jsonify(ok=False, message=f"Please wait {retry_after} seconds before requesting another OTP.", retry_after=retry_after), 429

    otp = f"{secrets.randbelow(900000) + 100000:06d}"
    try:
        send_sms(phone, f"Your Loan Mobile App verification OTP is {otp}. It expires in 5 minutes. Do not share it with anyone.")
    except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, TimeoutError, json.JSONDecodeError) as error:
        app.logger.error("SMS delivery failed for mobile verification: %s", error)
        return jsonify(ok=False, message="We could not send the OTP. Please try again shortly."), 503

    session["mobile_otp"] = {
        "phone": phone,
        "hash": otp_digest(phone, otp),
        "verified": False,
        "expires_at": now + OTP_EXPIRY_SECONDS,
        "resend_at": now + OTP_RESEND_SECONDS,
        "attempts": 0,
    }
    return jsonify(ok=True, message="OTP sent successfully. Please check your mobile.", resend_after=OTP_RESEND_SECONDS, expires_in=OTP_EXPIRY_SECONDS)


@app.route("/register/verify-mobile-otp", methods=["POST"])
def verify_mobile_otp():
    phone = normalize_indian_mobile(request.form.get("phone", ""))
    code = request.form.get("otp", "").strip()
    pending = session.get("mobile_otp") or {}
    if not phone or pending.get("phone") != phone:
        return jsonify(ok=False, message="The OTP is incorrect or does not match this mobile number."), 400
    if not re.fullmatch(r"\d{6}", code):
        return jsonify(ok=False, message="Enter the complete 6-digit OTP."), 400
    if time.time() >= pending.get("expires_at", 0):
        return jsonify(ok=False, message="The OTP has expired. Please request a new OTP.", expired=True), 400
    if pending.get("attempts", 0) >= OTP_MAX_ATTEMPTS:
        return jsonify(ok=False, message="Too many incorrect attempts. Please request a new OTP.", expired=True), 429
    if not hmac.compare_digest(pending.get("hash", ""), otp_digest(phone, code)):
        pending["attempts"] = pending.get("attempts", 0) + 1
        session["mobile_otp"] = pending
        remaining = OTP_MAX_ATTEMPTS - pending["attempts"]
        return jsonify(ok=False, message=f"The OTP is incorrect. {remaining} attempts remaining." if remaining else "Too many incorrect attempts. Please request a new OTP."), 400
    pending["verified"] = True
    session["mobile_otp"] = pending
    return jsonify(ok=True, message="Mobile number verified successfully.")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        pw2 = request.form.get("confirm_password", "")
        field_errors = {}
        if len(name) < 2:
            field_errors["name"] = "Please enter your name."
        if not re.fullmatch(r"[6-9]\d{9}", phone):
            field_errors["phone"] = "Enter a valid 10-digit Indian mobile number."
        otp_state = session.get("mobile_otp") or {}
        if not (otp_state.get("phone") == phone and otp_state.get("verified") is True):
            field_errors["otp"] = "Verify your mobile number with OTP before registration."
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            field_errors["email"] = "Enter a valid email address."
        if len(pw) < 8:
            field_errors["password"] = "Password must be at least 8 characters."
        if pw != pw2:
            field_errors["confirm_password"] = "Passwords do not match."
        if phone and User.query.filter(or_(User.email == email, User.phone == phone)).first():
            field_errors.setdefault("email", "An account with this email or phone already exists.")
        if field_errors:
            for message in field_errors.values():
                flash(message, "error")
            return render_template("register.html", form=request.form, field_errors=field_errors)
        u = User(name=name, phone=phone, email=email, password_hash=generate_password_hash(pw))
        db.session.add(u)
        db.session.commit()
        session.pop("mobile_otp", None)
        session.clear()
        session["uid"] = u.id
        flash("Account created. Welcome!", "success")
        return redirect(url_for("dashboard"))
    return render_template("register.html", form={}, field_errors={})


def do_login(admin):
    if request.method == "POST":
        ident = request.form.get("identifier", "").strip().lower()
        pw = request.form.get("password", "")
        u = User.query.filter(or_(User.email == ident, User.phone == ident)).first()
        if u and check_password_hash(u.password_hash, pw) and bool(u.is_admin) == admin:
            session.clear()
            session["uid"] = u.id
            return redirect(url_for("admin_dashboard" if admin else "dashboard"))
        flash("Invalid credentials.", "error")
    return render_template("login.html", admin=admin)


@app.route("/login", methods=["GET", "POST"])
def login():
    return do_login(False)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    return do_login(True)


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


@app.route("/apply", methods=["GET", "POST"])
@login_required
def apply_loan():
    if g.user.is_admin:
        return redirect(url_for("admin_dashboard"))
    fee = int(get_setting("fee", "36") or 36)
    qr = get_setting("qr_file")
    conditions = get_setting("conditions", DEFAULT_CONDITIONS)
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
        uploaded = session.setdefault("pending_application_files", {})
        document_fields = (
            ("aadhaar", {"jpg", "jpeg", "png", "pdf"}, "Upload a valid Aadhaar file (JPG, PNG or PDF)."),
            ("pan", {"jpg", "jpeg", "png", "pdf"}, "Upload a valid PAN file (JPG, PNG or PDF)."),
            ("photo", {"jpg", "jpeg", "png"}, "Upload a valid photo (JPG or PNG)."),
            ("payment", {"jpg", "jpeg", "png"}, "Upload the payment screenshot (JPG or PNG)."),
        )
        saved_files = {}
        field_errors = {}
        for field, allowed, error_message in document_fields:
            saved = save_upload(request.files.get(field), allowed)
            if saved:
                uploaded[field] = saved
                session["pending_application_files"] = uploaded
            if not uploaded.get(field):
                errors.append(error_message)
                field_errors[field] = error_message
            saved_files[field] = uploaded.get(field)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("apply.html", fee=fee, qr=qr, conditions=conditions, form=f,
                                   field_errors=field_errors, uploaded_files=uploaded)
        aadhaar, pan, photo, payment = (saved_files[n] for n in ("aadhaar", "pan", "photo", "payment"))
        db.session.add(Application(
            user_id=g.user.id, applicant_name=name, relation_type=rel_type, relation_name=rel_name,
            address=address, loan_amount=amount, repayment_months=months, aadhaar_file=aadhaar,
            pan_file=pan, photo_file=photo, payment_file=payment, fee_amount=fee))
        db.session.commit()
        session.pop("pending_application_files", None)
        flash("Application submitted successfully!", "success")
        return redirect(url_for("dashboard"))
    return render_template("apply.html", fee=fee, qr=qr, conditions=conditions, form={},
                           field_errors={}, uploaded_files=session.get("pending_application_files", {}))


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
    status = request.args.get("status")
    q = Application.query
    if status in STATUSES:
        q = q.filter_by(status=status)
    apps_ = q.order_by(Application.created_at.desc()).all()
    counts = {s: Application.query.filter_by(status=s).count() for s in STATUSES}
    return render_template("admin_dashboard.html", apps=apps_, counts=counts,
                           total=Application.query.count(), users=User.query.filter_by(is_admin=False).count(),
                           current=status)


@app.route("/admin/application/<int:app_id>", methods=["GET", "POST"])
@admin_required
def admin_application(app_id):
    a = db.session.get(Application, app_id) or abort(404)
    if request.method == "POST":
        status = request.form.get("status")
        if status not in STATUSES:
            abort(400)
        a.status = status
        a.admin_remarks = request.form.get("remarks", "").strip()[:1000]
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
        qr = save_upload(request.files.get("qr"), {"jpg", "jpeg", "png"})
        if request.files.get("qr") and request.files["qr"].filename and not qr:
            flash("QR must be a valid JPG or PNG image.", "error")
        elif qr:
            set_setting("qr_file", qr)
        flash("Settings saved.", "success")
        return redirect(url_for("admin_settings"))
    return render_template("admin_settings.html", fee=get_setting("fee", "36"),
                           conditions=get_setting("conditions", DEFAULT_CONDITIONS),
                           qr=get_setting("qr_file"))


with app.app_context():
    init_db()

if __name__ == "__main__":
    app.run(debug=not IS_PRODUCTION)
