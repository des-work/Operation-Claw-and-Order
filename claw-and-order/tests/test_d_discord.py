"""Phase D tests — Discord worker queue dispatch (no real bot connection)."""

import asyncio
import pytest
from backend.services.discord_worker import _queue, enqueue, _format_message


@pytest.mark.asyncio
async def test_enqueue_puts_message_on_queue():
    """enqueue() should place the message on the internal asyncio.Queue."""
    # Drain any leftover messages
    while not _queue.empty():
        _queue.get_nowait()

    msg = {
        "type": "milestone",
        "data": {
            "team_id": "red-1",
            "key": "dmz_compromise",
            "score": 100,
            "phase": 1,
            "technique": "nmap_scan",
        },
    }
    await enqueue(msg)
    assert not _queue.empty()

    dequeued = _queue.get_nowait()
    assert dequeued["type"] == "milestone"
    assert dequeued["data"]["team_id"] == "red-1"
    assert dequeued["data"]["key"] == "dmz_compromise"


@pytest.mark.asyncio
async def test_format_milestone_message():
    """Milestone messages should format with emoji, team, label, score."""
    msg = {
        "type": "milestone",
        "data": {
            "team_id": "red-2",
            "key": "lan_pivot",
            "score": 80,
            "phase": 2,
            "technique": "ssh_login",
        },
    }
    text = _format_message(msg)
    assert "red-2" in text
    assert "LAN pivot" in text
    assert "80" in text
    assert "🚨" in text


@pytest.mark.asyncio
async def test_format_phase_change_message():
    """Phase change messages should format with phase number and label."""
    msg = {
        "type": "phase_change",
        "data": {
            "phase": 2,
            "label": "Internal Recon",
        },
    }
    text = _format_message(msg)
    assert "Phase 2" in text
    assert "Internal Recon" in text
    assert "📢" in text


@pytest.mark.asyncio
async def test_format_vm_offline_message():
    """VM offline alert should name the team."""
    msg = {
        "type": "vm_offline",
        "data": {"team_id": "red-3"},
    }
    text = _format_message(msg)
    assert "red-3" in text
    assert "silent" in text.lower() or "offline" in text.lower() or "5+" in text


@pytest.mark.asyncio
async def test_format_unknown_type_fallback():
    """Unknown message types should still produce something sensible."""
    msg = {
        "type": "custom_alert",
        "data": {"info": "test"},
    }
    text = _format_message(msg)
    assert "custom_alert" in text


@pytest.mark.asyncio
async def test_multiple_enqueue_ordering():
    """Messages should dequeue in FIFO order."""
    while not _queue.empty():
        _queue.get_nowait()

    await enqueue({"type": "a", "data": {}})
    await enqueue({"type": "b", "data": {}})
    await enqueue({"type": "c", "data": {}})

    assert _queue.get_nowait()["type"] == "a"
    assert _queue.get_nowait()["type"] == "b"
    assert _queue.get_nowait()["type"] == "c"
    assert _queue.empty()
