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
    assert b"Loan Mobile App" in response.data


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
