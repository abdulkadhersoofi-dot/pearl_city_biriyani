"""The Google Cloud deploy is code in the repository (checked as files - it
runs in Google Cloud, not here): the settings every script reads, the
push pipeline (staging, then production only once it is live, then
Firebase Hosting), the connection budget, the guarded SQL helper, and the
phones' server address published with the app."""

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _settings() -> dict:
    out = {}
    for line in (ROOT / "deploy/gcp/settings.env").read_text().splitlines():
        m = re.match(r'^([A-Z_]+)="?([^"#]*)"?', line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def test_every_script_is_valid_shell():
    for script in sorted((ROOT / "scripts/gcp").glob("*.sh")) + [ROOT / "deploy/migrate.sh", ROOT / "deploy/entrypoint.sh"]:
        assert subprocess.run(["bash", "-n", str(script)]).returncode == 0, script


def test_the_connection_budget_fits_the_database():
    s = _settings()
    per_instance = int(s["WORKERS"]) * (int(s["POOL_SIZE"]) + int(s["MAX_OVERFLOW"]))
    assert int(s["CONCURRENCY"]) == int(s["WORKERS"]) * int(s["THREADS"]) and int(s["POOL_SIZE"]) == int(s["THREADS"])
    worst = int(s["MAX_INSTANCES"]) * per_instance + int(s["RESERVED_CONNECTIONS"])
    assert worst <= int(s["SQL_MAX_CONNECTIONS"]) * 0.8          # 20% headroom
    assert int(s["MAX_INSTANCES"]) * int(s["CONCURRENCY"]) > 48     # not capped at the old 6 x 8
    assert int(s["MIN_INSTANCES"]) >= 2


def test_production_waits_for_its_data_and_a_healthy_candidate():
    release = (ROOT / "scripts/gcp/release.sh").read_text()
    assert 'PRODUCTION_ENABLED" != "1"' in release
    assert "--no-traffic --tag candidate" in release and "update-traffic" in release
    assert release.index("jobs execute") < release.index("gcloud run deploy")      # migrate before deploy
    assert _settings()["PRODUCTION_ENABLED"] in ("0", "1")
    assert "SUPER_ADMIN_PASSWORD=arfa-super-admin-password" in release   # the first login, for the job only
    assert release.index("JOB_SECRETS") < release.index("gcloud run deploy")
    build = (ROOT / "cloudbuild.yaml").read_text()
    assert build.index("id: staging") < build.index("id: production") < build.index("id: hosting")


def test_firebase_rewrites_every_path_to_cloud_run_in_mumbai():
    config = json.loads((ROOT / "firebase.json").read_text())
    targets = {h["target"]: h for h in config["hosting"]}
    assert targets["production"]["rewrites"][0] == {"source": "**", "run": {"serviceId": "arfa-web", "region": "asia-south1"}}
    assert targets["staging"]["rewrites"][0]["run"]["serviceId"] == "arfa-web-staging"
    assert not any(p.name == "index.html" for p in (ROOT / "deploy/firebase/public").iterdir())  # nothing shadows "/"


def test_the_sql_helper_refuses_changes_without_being_told():
    sql = (ROOT / "scripts/gcp/sql.sh").read_text()
    assert "--destructive" in sql and "gcloud sql backups create" in sql
    assert re.search(r"update\|delete\|drop\|truncate\|alter", sql)


def test_no_secret_is_committed():
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert not any(name.endswith((".pem", "key.json")) or name == ".env" for name in tracked)
    for name in ("deploy/gcp/settings.env", "cloudbuild.yaml", "firebase.json", ".firebaserc"):
        text = (ROOT / name).read_text()
        assert not re.search(r"postgres(ql)?://[^@\s]+:[^@\s]+@", text) and "BEGIN PRIVATE KEY" not in text


def test_the_phones_can_be_moved_to_a_new_address_without_reinstalling():
    assert "server_url" in json.loads((ROOT / "android/server.json").read_text())
    assert "server_url" in (ROOT / ".github/workflows/android-apk.yml").read_text()
    activity = (ROOT / "android/app/src/main/java/com/arfa/pos/MainActivity.kt").read_text()
    assert "startMove()" in activity and "restoreCarried()" in activity and '"AndroidMove"' in activity
    assert "/healthz" in activity                       # never moves to an address that doesn't answer
    assert "server.offer(" in (ROOT / "android/app/src/main/java/com/arfa/pos/UpdateChecker.kt").read_text()
