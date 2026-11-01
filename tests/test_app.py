import os
import shutil
import tempfile
import io
from unittest.mock import patch

import pytest

TEST_TEMP_ROOT = tempfile.mkdtemp(prefix="loan_app_pytest_")
TEST_DATABASE_FILE = os.path.join(TEST_TEMP_ROOT, "test.sqlite")
TEST_STARTUP_UPLOAD_DIR = os.path.join(TEST_TEMP_ROOT, "startup_uploads")

os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DATABASE_FILE.replace("\\", "/")
os.environ["SECRET_KEY"] = "test-only-secret-key"
os.environ["UPLOAD_DIR"] = TEST_STARTUP_UPLOAD_DIR
os.environ["SESSION_COOKIE_SECURE"] = "false"

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
    assert b"Mobile Loan App" in response.data


def test_registration_and_login(client):
    token = csrf_token(client)
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


def test_registration_phone_shows_fixed_country_code(client):
    response = client.get("/register")
    assert b"+91" in response.data
    assert b'name="country_code"' in response.data


def test_admin_can_upload_qr_and_payment_page_is_fixed_at_36(app_instance, client):
    with app_instance.app_context():
        admin = application.User(name="QR Admin", phone="9333333333", email="qr.admin@example.com",
                                 password_hash=application.generate_password_hash("AdminPassword123!"), is_admin=True)
        application.db.session.add(admin)
        application.db.session.commit()
        admin_id = admin.id

    with client.session_transaction() as test_session:
        test_session["uid"] = admin_id

    token = csrf_token(client)
    response = client.post("/admin/settings", data={
        "_csrf": token,
        "conditions": "Existing conditions",
        "upi_id": "merchant@bank",
        "qr": (io.BytesIO(b"\x89PNG\r\n\x1a\nfake-qr-image"), "payment.png"),
    }, content_type="multipart/form-data")
    assert response.status_code == 302

    response = client.get("/qr")
    assert response.status_code == 200

    with app_instance.app_context():
        user = application.User(name="QR Customer", phone="9444444444", email="qr.customer@example.com",
                                password_hash=application.generate_password_hash("Password123!"), is_admin=False)
        application.db.session.add(user)
        application.db.session.commit()
        user_id = user.id
    with client.session_transaction() as test_session:
        test_session["uid"] = user_id

    response = client.get("/apply")
    assert response.status_code == 200
    assert b"\xe2\x82\xb936" in response.data
    assert b"/qr" in response.data
    assert b"am=36" in response.data


def test_regular_user_cannot_upload_qr(app_instance, client):
    with app_instance.app_context():
        user = application.User(name="Regular User", phone="9555555555", email="regular.qr@example.com",
                                password_hash=application.generate_password_hash("Password123!"), is_admin=False)
        application.db.session.add(user)
        application.db.session.commit()
        user_id = user.id
    with client.session_transaction() as test_session:
        test_session["uid"] = user_id

    token = csrf_token(client)
    response = client.post("/admin/settings", data={
        "_csrf": token,
        "qr": (io.BytesIO(b"\x89PNG\r\n\x1a\nfake-qr-image"), "payment.png"),
    }, content_type="multipart/form-data")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/admin/login")


def test_admin_workflow_routes_and_customer_registration(app_instance, client):
    with app_instance.app_context():
        admin = application.User(name="Workflow Admin", phone="9666666666", email="workflow.admin@example.com",
                                 password_hash=application.generate_password_hash("AdminPassword123!"), is_admin=True)
        application.db.session.add(admin)
        application.db.session.commit()
        admin_id = admin.id
    with client.session_transaction() as test_session:
        test_session["uid"] = admin_id

    token = csrf_token(client)
    for route in ("/admin/register-customers", "/admin/applications", "/admin/payments", "/admin/update-status"):
        response = client.get(route)
        assert response.status_code == 200

    response = client.post("/admin/register-customers", data={
        "_csrf": token,
        "name": "Admin Created Customer",
        "phone": "9777777777",
        "email": "created.customer@example.com",
        "password": "CustomerPassword123!",
    })
    assert response.status_code == 200
    with app_instance.app_context():
        created = application.User.query.filter_by(email="created.customer@example.com").first()
        assert created is not None
        assert created.must_change_password is True
        assert created.temp_password_expires_at is not None


def test_payment_verification_and_application_status(app_instance, client):
    with app_instance.app_context():
        admin = application.User(name="Status Admin", phone="9888888888", email="status.admin@example.com",
                                 password_hash=application.generate_password_hash("AdminPassword123!"), is_admin=True)
        user = application.User(name="Status Customer", phone="9999999999", email="status.customer@example.com",
                                password_hash=application.generate_password_hash("Password123!"), is_admin=False)
        application.db.session.add_all([admin, user])
        application.db.session.flush()
        loan = application.Application(user_id=user.id, applicant_name="Status Customer", relation_type="S/o",
                                       relation_name="Father", address="Customer address here", loan_amount=10000,
                                       repayment_months=12, aadhaar_file="a.jpg", pan_file="p.jpg",
                                       photo_file="photo.jpg", payment_file="pay.jpg", fee_amount=36)
        application.db.session.add(loan)
        application.db.session.flush()
        payment = application.Payment(user_id=user.id, application_id=loan.id, amount=36)
        application.db.session.add(payment)
        application.db.session.commit()
        admin_id, loan_id, payment_id = admin.id, loan.id, payment.id

    with client.session_transaction() as test_session:
        test_session["uid"] = admin_id
    token = csrf_token(client)
    response = client.post("/admin/application/%d" % loan_id, data={
        "_csrf": token, "action": "approve", "remarks": "Documents verified"
    })
    assert response.status_code == 302
    response = client.post("/admin/payments", data={
        "_csrf": token, "payment_id": payment_id, "status": "Verified", "reference": "UPI123"
    })
    assert response.status_code == 302
    with app_instance.app_context():
        assert application.db.session.get(application.Application, loan_id).status == "Approved"
        assert application.db.session.get(application.Payment, payment_id).status == "Verified"


def test_share_temporary_password_only_changes_password_after_email(app_instance, client):
    with app_instance.app_context():
        admin = application.User(name="Mail Admin", phone="9444444444", email="mail.admin@example.com",
                                 password_hash=application.generate_password_hash("AdminPassword123!"), is_admin=True)
        customer = application.User(name="Mail Customer", phone="9555555555", email="mail.customer@example.com",
                                    password_hash=application.generate_password_hash("ExistingPassword123!"))
        application.db.session.add_all([admin, customer])
        application.db.session.commit()
        admin_id, customer_id = admin.id, customer.id

    with client.session_transaction() as test_session:
        test_session["uid"] = admin_id
    token = csrf_token(client)
    with patch.object(application, "send_temporary_password_email", return_value=None) as send_email:
        response = client.post("/admin/customers/%d/share" % customer_id, data={"_csrf": token})
    assert response.status_code == 302
    assert send_email.call_count == 1
    sent_customer, sent_password = send_email.call_args.args
    assert sent_password not in response.data.decode("utf-8")
    with app_instance.app_context():
        assert application.check_password_hash(
            application.db.session.get(application.User, customer_id).password_hash, sent_password
        )


def test_share_temporary_password_failure_preserves_existing_password(app_instance, client):
    with app_instance.app_context():
        admin = application.User(name="Mail Admin", phone="9666666666", email="mail.failure.admin@example.com",
                                 password_hash=application.generate_password_hash("AdminPassword123!"), is_admin=True)
        customer = application.User(name="Mail Customer", phone="9777777777", email="mail.failure.customer@example.com",
                                    password_hash=application.generate_password_hash("ExistingPassword123!"))
        application.db.session.add_all([admin, customer])
        application.db.session.commit()
        admin_id, customer_id, original_hash = admin.id, customer.id, customer.password_hash

    with client.session_transaction() as test_session:
        test_session["uid"] = admin_id
    token = csrf_token(client)
    with patch.object(application, "send_temporary_password_email",
                      side_effect=application.smtplib.SMTPAuthenticationError(535, b"authentication failed")):
        response = client.post("/admin/customers/%d/share" % customer_id, data={"_csrf": token})
    assert response.status_code == 302
    with application.app.app_context():
        preserved = application.db.session.get(application.User, customer_id)
        assert preserved.password_hash == original_hash
        assert preserved.must_change_password is False


def test_expired_temporary_password_is_rejected(app_instance, client):
    with app_instance.app_context():
        user = application.User(name="Expired Customer", phone="9888888888", email="expired@example.com",
                                password_hash=application.generate_password_hash("Temporary123!"),
                                must_change_password=True,
                                temp_password_expires_at=application.datetime.utcnow() - application.timedelta(hours=1))
        application.db.session.add(user)
        application.db.session.commit()

    token = csrf_token(client)
    response = client.post("/login", data={
        "_csrf": token, "identifier": "expired@example.com", "password": "Temporary123!"
    })
    assert b"Temporary password has expired" in response.data


def test_customer_can_edit_owned_application(app_instance, client):
    with app_instance.app_context():
        customer = application.User(name="Edit Customer", phone="9550000000", email="edit.customer@example.com",
                                    password_hash=application.generate_password_hash("Password123!"))
        application.db.session.add(customer)
        application.db.session.flush()
        loan = application.Application(
            user_id=customer.id, applicant_name="Original Name", relation_type="S/o",
            relation_name="Original Father", address="Original address with enough text",
            loan_amount=10000, repayment_months=12, aadhaar_file="a.jpg", pan_file="p.jpg",
            photo_file="photo.jpg", payment_file="pay.jpg", fee_amount=36)
        application.db.session.add(loan)
        application.db.session.commit()
        customer_id, loan_id = customer.id, loan.id

    with client.session_transaction() as test_session:
        test_session["uid"] = customer_id
    token = csrf_token(client)
    response = client.post("/applications/%d/edit" % loan_id, data={
        "_csrf": token, "applicant_name": "Updated Name", "relation_type": "S/o",
        "relation_name": "Updated Father", "address": "Updated address with enough text",
        "loan_amount": 25000, "repayment_months": 24, "agree": "1"
    }, content_type="multipart/form-data")
    assert response.status_code == 302
    with app_instance.app_context():
        updated = application.db.session.get(application.Application, loan_id)
        assert updated.applicant_name == "Updated Name"
        assert updated.loan_amount == 25000
        assert updated.aadhaar_file == "a.jpg"


def test_customer_cannot_edit_another_customers_application(app_instance, client):
    with app_instance.app_context():
        customer = application.User(name="Owner", phone="9550000001", email="owner@example.com",
                                    password_hash=application.generate_password_hash("Password123!"))
        other = application.User(name="Other", phone="9550000002", email="other@example.com",
                                 password_hash=application.generate_password_hash("Password123!"))
        application.db.session.add_all([customer, other])
        application.db.session.flush()
        loan = application.Application(
            user_id=other.id, applicant_name="Other Customer", relation_type="S/o",
            relation_name="Father", address="Other customer address here",
            loan_amount=15000, repayment_months=12, aadhaar_file="o-a.jpg", pan_file="o-p.jpg",
            photo_file="o-photo.jpg", payment_file="o-pay.jpg", fee_amount=36)
        application.db.session.add(loan)
        application.db.session.commit()
        customer_id, loan_id = customer.id, loan.id

    with client.session_transaction() as test_session:
        test_session["uid"] = customer_id
    response = client.get("/applications/%d/edit" % loan_id)
    assert response.status_code == 404
