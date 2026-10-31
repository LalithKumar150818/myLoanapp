import os
import re
import secrets
import uuid
from datetime import datetime
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request,
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


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        pw2 = request.form.get("confirm_password", "")
        errors = []
        if len(name) < 2:
            errors.append("Please enter your name.")
        if not re.fullmatch(r"[6-9]\d{9}", phone):
            errors.append("Enter a valid 10-digit Indian mobile number.")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            errors.append("Enter a valid email address.")
        if len(pw) < 8:
            errors.append("Password must be at least 8 characters.")
        if pw != pw2:
            errors.append("Passwords do not match.")
        if User.query.filter(or_(User.email == email, User.phone == phone)).first():
            errors.append("An account with this email or phone already exists.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("register.html", form=request.form)
        u = User(name=name, phone=phone, email=email, password_hash=generate_password_hash(pw))
        db.session.add(u)
        db.session.commit()
        session.clear()
        session["uid"] = u.id
        flash("Account created. Welcome!", "success")
        return redirect(url_for("dashboard"))
    return render_template("register.html", form={})


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
        try:
            amount = int(f.get("loan_amount", ""))
            months = int(f.get("repayment_months", ""))
        except ValueError:
            amount = months = 0
        if len(name) < 2: errors.append("Enter your name.")
        if rel_type not in ("S/o", "D/o", "W/o"): errors.append("Select S/o, D/o or W/o.")
        if len(rel_name) < 2: errors.append("Enter father/husband name.")
        if len(address) < 10: errors.append("Enter your full address.")
        if not MIN_LOAN <= amount <= MAX_LOAN:
            errors.append(f"Loan amount must be between ₹{MIN_LOAN:,} and ₹{MAX_LOAN:,}.")
        if not MIN_MONTHS <= months <= MAX_MONTHS:
            errors.append(f"Repayment period must be {MIN_MONTHS}-{MAX_MONTHS} months.")
        if not f.get("agree"):
            errors.append("You must agree to the conditions.")
        aadhaar = save_upload(request.files.get("aadhaar"), {"jpg", "jpeg", "png", "pdf"})
        pan = save_upload(request.files.get("pan"), {"jpg", "jpeg", "png", "pdf"})
        photo = save_upload(request.files.get("photo"), {"jpg", "jpeg", "png"})
        payment = save_upload(request.files.get("payment"), {"jpg", "jpeg", "png"})
        if not aadhaar: errors.append("Upload a valid Aadhaar file (JPG, PNG or PDF).")
        if not pan: errors.append("Upload a valid PAN file (JPG, PNG or PDF).")
        if not photo: errors.append("Upload a valid photo (JPG or PNG).")
        if not payment: errors.append("Upload the payment screenshot (JPG or PNG).")
        if errors:
            for n in (aadhaar, pan, photo, payment):  # clean up files saved before failure
                if n:
                    try: os.remove(os.path.join(UPLOAD_DIR, n))
                    except OSError: pass
            for e in errors:
                flash(e, "error")
            return render_template("apply.html", fee=fee, qr=qr, conditions=conditions, form=f)
        db.session.add(Application(
            user_id=g.user.id, applicant_name=name, relation_type=rel_type, relation_name=rel_name,
            address=address, loan_amount=amount, repayment_months=months, aadhaar_file=aadhaar,
            pan_file=pan, photo_file=photo, payment_file=payment, fee_amount=fee))
        db.session.commit()
        flash("Application submitted successfully!", "success")
        return redirect(url_for("dashboard"))
    return render_template("apply.html", fee=fee, qr=qr, conditions=conditions, form={})


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
        fee = request.form.get("fee", "").strip()
        if fee.isdigit() and 0 <= int(fee) <= 100000:
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
