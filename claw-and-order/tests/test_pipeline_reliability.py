import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, func, create_engine, text, inspect

from backend.main import app, _migrate_event_source_id, _backfill_event_counts
from backend.models import Event, TeamEventCount, DiscordOutbox, DashboardOutbox
from backend.models import Team
from backend.services import auth
from reporter.reporter import Reporter, RetryBuffer, PermanentEventError
from scripts.provision_tokens import main as provision_teams
from tests.conftest import VALID_TOKEN, TEST_ADMIN_SECRET, TEST_EXPORT_SECRET, _session_factory


def test_retry_buffer_keeps_all_records_and_replays_in_order(tmp_path):
    path = tmp_path / "retry.jsonl"
    buffer = RetryBuffer(str(path), max_events=2)
    for index in range(5):
        buffer.append({"source_id": str(index)})
    assert len(path.read_text().splitlines()) == 5

    sent = []
    assert buffer.drain(lambda item: sent.append(item["source_id"]) or len(sent) < 3) == 2
    assert sent == ["0", "1", "2"]
    assert [json.loads(line)["source_id"] for line in path.read_text().splitlines()] == ["2", "3", "4"]

    sent.clear()
    assert buffer.drain(lambda item: sent.append(item["source_id"]) or True) == 3
    assert sent == ["2", "3", "4"]
    assert not path.exists()


def test_permanent_rejection_is_audited_and_does_not_block_following_events(tmp_path):
    path = tmp_path / "retry.jsonl"
    buffer = RetryBuffer(str(path))
    for source_id in ("bad", "good"):
        buffer.append({"source_id": source_id})

    def post(item):
        if item["source_id"] == "bad":
            raise PermanentEventError(422, "invalid payload")
        return True

    assert buffer.drain(post) == 1
    assert not path.exists()
    rejection = json.loads((tmp_path / "retry.jsonl.rejected.jsonl").read_text())
    assert rejection["payload"]["source_id"] == "bad"
    assert rejection["status_code"] == 422


def test_reporter_classifies_permanent_and_retryable_http_errors(tmp_path):
    cfg = {
        "backend_url": "http://127.0.0.1:8000", "bearer_token": "test", "team_id": "team-1",
        "telemetry_log": str(tmp_path / "telemetry.jsonl"),
        "retry_buffer": str(tmp_path / "retry.jsonl"),
        "technique_map": str(Path(__file__).parents[1] / "reporter" / "technique_map.json"),
    }
    reporter = Reporter(cfg)
    reporter._session.post = lambda *args, **kwargs: SimpleNamespace(status_code=409, text="source conflict")
    assert reporter.send_event({"source_id": "bad"}) is False
    assert not reporter.buffer.has_content
    assert json.loads((tmp_path / "retry.jsonl.rejected.jsonl").read_text())["status_code"] == 409

    reporter._session.post = lambda *args, **kwargs: SimpleNamespace(status_code=401, text="invalid token")
    assert reporter.send_event({"source_id": "retry"}) is False
    assert reporter.buffer.has_content
    assert json.loads((tmp_path / "retry.jsonl").read_text())["source_id"] == "retry"


def test_existing_event_history_migrates_without_10k_cutoff():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE events (id INTEGER PRIMARY KEY, team_id TEXT)"))
        connection.execute(text("CREATE TABLE team_event_counts (team_id TEXT PRIMARY KEY, event_count BIGINT)"))
        connection.execute(text("INSERT INTO events (team_id) VALUES (:team_id)"),
                           [{"team_id": "new-class-1"} for _ in range(12_000)])
        _migrate_event_source_id(connection)
        _backfill_event_counts(connection)
        count = connection.scalar(text("SELECT event_count FROM team_event_counts WHERE team_id = 'new-class-1'"))
        assert count == 12_000
        assert "source_id" in {column["name"] for column in inspect(connection).get_columns("events")}
    engine.dispose()


def test_reporter_resumes_cursor_and_source_id_survives_restart(tmp_path):
    telemetry = tmp_path / "telemetry.jsonl"
    telemetry.write_text('old event\n')
    config = {
        "backend_url": "http://127.0.0.1:8000", "bearer_token": "test", "team_id": "new-class-1",
        "telemetry_log": str(telemetry), "cursor_file": str(tmp_path / "cursor.json"),
        "retry_buffer": str(tmp_path / "retry.jsonl"),
        "technique_map": str(Path(__file__).parents[1] / "reporter" / "technique_map.json"),
    }
    reporter = Reporter(config)
    file = reporter._open_telemetry()
    assert file.read() == ""
    with telemetry.open("a") as out:
        out.write('{"type":"tool.end","success":true}\n')
    offset = file.tell()
    line = file.readline().strip()
    source_id = reporter._source_id(file, offset, line)
    file.close()  # crash before cursor update

    resumed = Reporter(config)
    file = resumed._open_telemetry()
    assert file.tell() == offset
    assert resumed._source_id(file, offset, file.readline().strip()) == source_id
    resumed._save_cursor(file)
    file.close()
    file = Reporter(config)._open_telemetry()
    assert file.read() == ""
    file.close()

    rotated = tmp_path / "next.jsonl"
    rotated.write_text('{"type":"tool.end","success":true}\n')
    os.replace(rotated, telemetry)
    file = Reporter(config)._open_telemetry()
    assert file.tell() == 0
    file.close()


@pytest.mark.asyncio
async def test_provisioning_adds_teams_without_rotating_existing_tokens(tmp_path):
    database = tmp_path / "teams.db"
    definitions = tmp_path / "teams.json"
    output = tmp_path / "configs"
    base = {"display_name": "Team", "wan_ip": "203.0.113.1",
            "dmz_cidr": "10.10.1.0/24", "lan_cidr": "10.10.2.0/24"}
    definitions.write_text(json.dumps([{**base, "id": "class-a-1"}, {**base, "id": "class-b-1"}]))
    url = f"sqlite+aiosqlite:///{database}"
    assert await provision_teams(str(definitions), db_url=url, destination=output) == 2
    original_token = json.loads((output / "config_class-a-1.json").read_text())["bearer_token"]
    with sqlite3.connect(database) as connection:
        original_hash = connection.execute("SELECT bearer_token_hash FROM teams WHERE id='class-a-1'").fetchone()[0]

    definitions.write_text(json.dumps([{**base, "id": "class-a-1", "display_name": "Renamed"},
                                       {**base, "id": "class-b-1"}, {**base, "id": "class-c-1"}]))
    assert await provision_teams(str(definitions), db_url=url, destination=output) == 1
    assert json.loads((output / "config_class-a-1.json").read_text())["bearer_token"] == original_token
    assert (output / "config_class-c-1.json").stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 3
        assert connection.execute("SELECT bearer_token_hash FROM teams WHERE id='class-a-1'").fetchone()[0] == original_hash
        assert connection.execute("SELECT display_name FROM teams WHERE id='class-a-1'").fetchone()[0] == "Renamed"


@pytest.mark.asyncio
async def test_duplicate_source_does_not_double_count_or_alert():
    payload = {
        "team_id": "team-alpha", "command": "sqlmap -u http://10.50.1.100/login",
        "technique": "sqli", "target_ip": "10.50.1.100", "result": "success",
        "source_id": "reporter-event-123",
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/api/events", headers={"Authorization": f"Bearer {VALID_TOKEN}"}, json=payload)
        duplicate = await client.post("/api/events", headers={"Authorization": f"Bearer {VALID_TOKEN}"}, json=payload)
        conflict = await client.post("/api/events", headers={"Authorization": f"Bearer {VALID_TOKEN}"},
                                     json={**payload, "target_ip": "10.50.1.101"})
    assert first.status_code == 201
    assert duplicate.status_code == 200
    assert duplicate.json()["event_id"] == first.json()["event_id"]
    assert conflict.status_code == 409
    async with _session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 1
        assert await session.scalar(select(TeamEventCount.event_count)) == 1
        assert await session.scalar(select(func.count()).select_from(DiscordOutbox)) == 1
        assert await session.scalar(select(func.count()).select_from(DashboardOutbox)) == 1


@pytest.mark.asyncio
async def test_admin_event_export_is_authenticated_and_cursor_ordered():
    payload = {
        "team_id": "team-alpha", "command": "nmap 10.50.1.100", "technique": "port_scan",
        "target_ip": "10.50.1.100", "result": "success", "source_id": "export-1",
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        unauthorized = await client.get("/api/admin/events/export")
        assert unauthorized.status_code == 422
        admin_only = await client.get("/api/admin/events/export",
                                      headers={"X-Admin-Secret": TEST_ADMIN_SECRET})
        assert admin_only.status_code == 422
        wrong_export = await client.get("/api/admin/events/export",
                                        headers={"X-Export-Secret": "wrong"})
        assert wrong_export.status_code == 401
        await client.post("/api/events", headers={"Authorization": f"Bearer {VALID_TOKEN}"}, json=payload)
        await client.post("/api/events", headers={"Authorization": f"Bearer {VALID_TOKEN}"},
                          json={**payload, "source_id": "export-2"})
        headers = {"X-Export-Secret": TEST_EXPORT_SECRET}
        first = (await client.get("/api/admin/events/export?limit=1", headers=headers)).json()
        replay = (await client.get("/api/admin/events/export?limit=1", headers=headers)).json()
        ack = await client.post("/api/admin/events/ack", headers=headers,
                                json={"event_ids": [first["events"][0]["id"]]})
        second = (await client.get("/api/admin/events/export?limit=1", headers=headers)).json()
        await client.post("/api/admin/events/ack", headers=headers,
                          json={"event_ids": [second["events"][0]["id"]]})
        drained = (await client.get("/api/admin/events/export", headers=headers)).json()
    assert first["has_more"] is True
    assert first["pending"] == 2
    assert replay["events"][0]["id"] == first["events"][0]["id"]
    assert ack.status_code == 200
    assert len(first["events"]) == 1
    assert len(second["events"]) == 1
    assert second["events"][0]["id"] > first["events"][0]["id"]
    assert "command" not in second["events"][0]
    assert drained["pending"] == 0
    async with _session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(DashboardOutbox)) == 2


@pytest.mark.asyncio
async def test_legacy_token_upgrades_once_then_uses_fast_verifier(monkeypatch):
    payload = {
        "team_id": "team-alpha", "command": "nmap 10.50.1.100", "technique": "port_scan",
        "target_ip": "10.50.1.100", "result": "failure", "source_id": "token-upgrade-1",
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/api/events", headers={"Authorization": f"Bearer {VALID_TOKEN}"}, json=payload)
        assert first.status_code == 201
        async with _session_factory() as session:
            team = await session.get(Team, "team-alpha")
            assert team.bearer_token_hash.startswith("sha256:")

        def should_not_check_bcrypt(*args):
            raise AssertionError("bcrypt was called again")

        monkeypatch.setattr(auth.bcrypt, "checkpw", should_not_check_bcrypt)
        second = await client.post("/api/events", headers={"Authorization": f"Bearer {VALID_TOKEN}"},
                                   json={**payload, "source_id": "token-upgrade-2"})
        assert second.status_code == 201
