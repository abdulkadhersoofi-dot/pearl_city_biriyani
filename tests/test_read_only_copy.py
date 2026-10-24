"""After the move to Google Cloud, the Render copy stays up read-only for
two weeks: switched on by committing deploy/render-read-only (Render sets
RENDER=true), it refuses every change and says where billing went, while
signing in and looking at records still work. Cloud Run never reads the
flag file."""

import app as app_module


def _login(client):
    return client.post("/auth/login", data={"email": "admin@acme.example.com", "password": "ClientSecret123"})


def test_the_render_copy_refuses_changes_but_can_be_looked_at(client, db, client_admin, monkeypatch, tmp_path):
    flag = tmp_path / "render-read-only"
    flag.write_text("")
    monkeypatch.setattr(app_module, "READ_ONLY_FLAG", str(flag))
    monkeypatch.setenv("RENDER", "true")
    assert _login(client).status_code == 302                       # signing in still works
    page = client.get("/invoices/")
    assert page.status_code == 200 and "This copy is read-only" in page.get_data(as_text=True)
    res = client.post("/customers/new", data={"name": "Someone"})
    assert res.status_code == 503 and "Nothing was saved here" in res.get_data(as_text=True)
    sync = client.post("/pos/offline/sync", json={"bills": []})
    assert sync.status_code == 503 and sync.get_json()["read_only"] is True   # the phone keeps its bills


def test_the_flag_file_does_nothing_off_render(client, db, client_admin, monkeypatch, tmp_path):
    flag = tmp_path / "render-read-only"
    flag.write_text("")
    monkeypatch.setattr(app_module, "READ_ONLY_FLAG", str(flag))
    monkeypatch.delenv("RENDER", raising=False)
    _login(client)
    assert "This copy is read-only" not in client.get("/invoices/").get_data(as_text=True)
