import pytest
from flask.testing import FlaskClient

from websole.app import app, configure


@pytest.fixture()
def client():
    yield app.test_client()


def test_login(client: FlaskClient):
    configure(webpass="0000")
    resp = client.get("/console", follow_redirects=True)
    assert b"<title>Log in" in resp.data
    resp = client.post("/login", data={"webpass": "0000"})
    assert resp.status_code == 302
    configure(webpass="")
    resp = client.get("/console", follow_redirects=True)
    assert b"<title>Console" in resp.data
