"""The optional login: off by default, one form to turn on, cookies that expire."""

from __future__ import annotations

import time

from cleanarr import auth, config


def test_password_hashing():
    h, salt = auth.hash_password("correct horse")
    assert auth.check_password("correct horse", h, salt)
    assert not auth.check_password("wrong horse", h, salt)
    assert not auth.check_password("x", "", "")
    assert not auth.check_password("x", h, "!!not base64!!")
    assert auth.hash_password("correct horse", salt)[0] == h


def test_tokens():
    secret = auth.new_secret()
    token = auth.issue("amy", secret)
    assert auth.verify(token, secret, "amy")
    assert not auth.verify(token, secret, "bob")                 # renamed account
    assert not auth.verify(token, auth.new_secret(), "amy")      # rotated key
    assert not auth.verify(token + "x", secret, "amy")
    assert not auth.verify("garbage", secret, "amy")
    assert not auth.verify("", secret, "amy")


def test_tokens_expire(monkeypatch):
    secret = auth.new_secret()
    token = auth.issue("amy", secret)
    later = time.time() + auth.SESSION_DAYS * 86400 + 10
    monkeypatch.setattr(auth.time, "time", lambda: later)
    assert not auth.verify(token, secret, "amy")


def test_open_paths():
    assert auth.is_open("/")
    assert auth.is_open("/api/auth/state")
    assert auth.is_open("/static/app.js")
    assert auth.is_open("/api/health")
    assert not auth.is_open("/api/settings")
    assert not auth.is_open("/api/jobs")


def test_no_login_by_default(client):
    assert client.get("/api/auth/state").json() == {"configured": False, "username": ""}
    assert client.get("/api/settings").status_code == 200


def test_setup_login_logout_flow(client):
    assert client.post("/api/auth/setup", json={"username": "ab", "password": "12345678"}
                       ).status_code == 400
    assert client.post("/api/auth/setup", json={"username": "amy", "password": "short"}
                       ).status_code == 400
    r = client.post("/api/auth/setup", json={"username": "amy", "password": "long enough"})
    assert r.status_code == 200
    assert client.get("/api/settings").status_code == 200        # cookie was set

    # A second setup cannot take the instance over.
    assert client.post("/api/auth/setup", json={"username": "eve", "password": "long enough"}
                       ).status_code == 409

    client.post("/api/auth/logout")
    client.cookies.clear()
    assert client.get("/api/settings").status_code == 401
    assert client.get("/api/health").status_code == 200
    assert client.get("/").status_code == 200

    bad = client.post("/api/auth/login", json={"username": "amy", "password": "nope nope"})
    assert bad.status_code == 401
    assert bad.json()["detail"] == "wrong username or password"
    wrong_user = client.post("/api/auth/login", json={"username": "bob", "password": "long enough"})
    assert wrong_user.json()["detail"] == "wrong username or password"
    assert client.post("/api/auth/login", json={"username": "amy", "password": "long enough"}
                       ).status_code == 200
    assert client.get("/api/settings").status_code == 200


def test_settings_never_leak_the_hash(client):
    client.post("/api/auth/setup", json={"username": "amy", "password": "long enough"})
    data = client.get("/api/settings").json()
    for secret in ("auth_hash", "auth_salt", "auth_secret"):
        assert secret not in data
    assert data["auth_enabled"] is True


def test_password_change_ends_other_sessions(client):
    from fastapi.testclient import TestClient
    from cleanarr import main

    client.post("/api/auth/setup", json={"username": "amy", "password": "long enough"})
    other = TestClient(main.app)
    other.post("/api/auth/login", json={"username": "amy", "password": "long enough"})
    assert other.get("/api/jobs").status_code == 200

    assert client.post("/api/auth/change", json={"current": "wrong", "password": "new password"}
                       ).status_code == 401
    assert client.post("/api/auth/change", json={"current": "long enough", "password": "short"}
                       ).status_code == 400
    r = client.post("/api/auth/change", json={"current": "long enough", "password": "new password"})
    assert r.status_code == 200
    assert client.get("/api/jobs").status_code == 200       # this one got a new cookie
    assert other.get("/api/jobs").status_code == 401        # that one did not


def test_disable_needs_the_password(client):
    client.post("/api/auth/setup", json={"username": "amy", "password": "long enough"})
    assert client.post("/api/auth/change", json={"current": "nope", "disable": True}
                       ).status_code == 401
    r = client.post("/api/auth/change", json={"current": "long enough", "disable": True})
    assert r.json()["disabled"] is True
    client.cookies.clear()
    assert client.get("/api/settings").status_code == 200
    assert config.load().auth_secret == ""
