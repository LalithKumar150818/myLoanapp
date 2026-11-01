import os
import shutil
import tempfile

import pytest

TEST_TEMP_ROOT = tempfile.mkdtemp(prefix="loan_app_pytest_")
TEST_DATABASE_FILE = os.path.join(TEST_TEMP_ROOT, "test.sqlite")
TEST_STARTUP_UPLOAD_DIR = os.path.join(TEST_TEMP_ROOT, "startup_uploads")

os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DATABASE_FILE.replace("\\", "/")
os.environ["SECRET_KEY"] = "test-only-secret-key"
os.environ["UPLOAD_DIR"] = TEST_STARTUP_UPLOAD_DIR
os.environ["SESSION_COOKIE_SECURE"] = "false"
os.environ["OTP_DEV_MODE"] = "true"

import app as application


def pytest_sessionfinish(session, exitstatus):
    with application.app.app_context():
        application.db.session.remove()
        application.db.engine.dispose()
    shutil.rmtree(TEST_TEMP_ROOT, ignore_errors=True)


def csrf_token(client):
    response = client.get("/")
    assert response.status_code == 200
    with client.session_transaction() as test_session:
        return test_session["_csrf"]


def verify_registration_otp(client, token, phone):
    response = client.post("/register/send-mobile-otp", data={"_csrf": token, "phone": phone})
    assert response.status_code == 200
    otp = application.LAST_DEVELOPMENT_SMS_OTP
    assert otp is not None
    response = client.post("/register/verify-mobile-otp", data={"_csrf": token, "phone": phone, "otp": otp})
    assert response.status_code == 200


@pytest.fixture()
def app_instance(tmp_path):
    application.app.config.update(TESTING=True)
    application.UPLOAD_DIR = str(tmp_path)

    with application.app.app_context():
        application.db.session.remove()
        application.db.drop_all()
        application.db.create_all()

    yield application.app

    with application.app.app_context():
        application.db.session.remove()
        application.db.drop_all()
        application.db.engine.dispose()


@pytest.fixture()
def client(app_instance):
    return app_instance.test_client()


def test_application_starts(app_instance):
    assert app_instance is not None
    assert app_instance.config["TESTING"] is True


def test_home_page_returns_successfully(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"Loan Mobile App" in response.data


def test_registration_and_login(client):
    token = csrf_token(client)
    verify_registration_otp(client, token, "9876543210")
    response = client.post("/register", data={
        "_csrf": token,
        "name": "Test User",
        "phone": "9876543210",
        "email": "test.user@example.com",
        "password": "TestPassword123!",
        "confirm_password": "TestPassword123!",
    })
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard")

    with application.app.app_context():
        user = application.User.query.filter_by(email="test.user@example.com").first()
        assert user is not None

    client.get("/logout")
    token = csrf_token(client)
    response = client.post("/login", data={
        "_csrf": token,
        "identifier": "test.user@example.com",
        "password": "TestPassword123!",
    })
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard")


def test_invalid_login_credentials_are_rejected(client):
    token = csrf_token(client)
    response = client.post("/login", data={
        "_csrf": token,
        "identifier": "missing@example.com",
        "password": "WrongPassword123!",
    })
    assert response.status_code == 200
    assert b"Invalid credentials." in response.data


def test_unauthenticated_user_cannot_access_admin_dashboard(client):
    response = client.get("/admin")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/admin/login")


def test_registered_user_cannot_access_admin_dashboard(client):
    token = csrf_token(client)
    verify_registration_otp(client, token, "9876543211")
    response = client.post("/register", data={
        "_csrf": token,
        "name": "Regular User",
        "phone": "9876543211",
        "email": "regular.user@example.com",
        "password": "TestPassword123!",
        "confirm_password": "TestPassword123!",
    })
    assert response.status_code == 302

    response = client.get("/admin")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/admin/login")


def test_customer_home_hides_admin_workflow_and_centered_apply_is_present(client):
    response = client.get("/")
    assert b"Register Customers" not in response.data
    assert b"Review Applications" not in response.data
    assert b"Verify Payments" not in response.data
    assert b"Update Status" not in response.data
    assert b"customer-hero" in response.data
    assert b"hero-apply" in response.data


def test_admin_dashboard_requires_admin_role(app_instance, client):
    with app_instance.app_context():
        admin = application.User(name="Admin User", phone="9000000000", email="admin@example.com",
                                 password_hash=application.generate_password_hash("AdminPassword123!"), is_admin=True)
        application.db.session.add(admin)
        application.db.session.commit()
        admin_id = admin.id

    token = csrf_token(client)
    response = client.post("/admin/login", data={
        "_csrf": token,
        "identifier": "admin@example.com",
        "password": "AdminPassword123!",
    })
    assert response.status_code == 200 or response.status_code == 302

    with client.session_transaction() as test_session:
        test_session["uid"] = admin_id

    response = client.get("/admin")
    assert response.status_code == 200
    assert b"Registered Customers" in response.data
    assert b"Total Applications" in response.data


def test_customer_dashboard_only_lists_their_application(app_instance, client):
    with app_instance.app_context():
        first = application.User(name="First Customer", phone="9111111111", email="first@example.com",
                                 password_hash=application.generate_password_hash("Password123!"), is_admin=False)
        second = application.User(name="Second Customer", phone="9222222222", email="second@example.com",
                                  password_hash=application.generate_password_hash("Password123!"), is_admin=False)
        application.db.session.add_all([first, second])
        application.db.session.flush()
        application.db.session.add_all([
            application.Application(user_id=first.id, applicant_name="First Application", relation_type="S/o",
                                    relation_name="Father", address="First customer address", loan_amount=10000,
                                    repayment_months=12, aadhaar_file="1.jpg", pan_file="2.jpg", photo_file="3.jpg",
                                    payment_file="4.jpg", fee_amount=36, status="Pending"),
            application.Application(user_id=second.id, applicant_name="Second Private Application", relation_type="S/o",
                                    relation_name="Father", address="Second customer address", loan_amount=20000,
                                    repayment_months=12, aadhaar_file="5.jpg", pan_file="6.jpg", photo_file="7.jpg",
                                    payment_file="8.jpg", fee_amount=36, status="Approved"),
        ])
        application.db.session.commit()
        first_id = first.id

    with client.session_transaction() as test_session:
        test_session["uid"] = first_id
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "₹10,000".encode("utf-8") in response.data
    assert "₹20,000".encode("utf-8") not in response.data
