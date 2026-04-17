
# --- Phase 2 config-refactor test helper ---
# Memory APIs now take MemoryConfig / AppConfig explicitly. Tests construct a
# minimal config once and reuse it across call sites.
from deerflow.config.app_config import AppConfig as _TestAppConfig
from deerflow.config.memory_config import MemoryConfig as _TestMemoryConfig
from deerflow.config.sandbox_config import SandboxConfig as _TestSandboxConfig

_TEST_MEMORY_CONFIG = _TestMemoryConfig(enabled=True)
_TEST_APP_CONFIG = _TestAppConfig(sandbox=_TestSandboxConfig(use="test"), memory=_TEST_MEMORY_CONFIG)
# -------------------------------------------

"""Tests for user_id propagation through memory queue."""
from unittest.mock import MagicMock, patch

import pytest

from deerflow.agents.memory.queue import ConversationContext, MemoryUpdateQueue
from deerflow.config.app_config import AppConfig
from deerflow.config.memory_config import MemoryConfig


@pytest.fixture(autouse=True)
def _enable_memory(monkeypatch):
    """Ensure MemoryUpdateQueue.add() doesn't early-return on disabled memory."""
    config = MagicMock(spec=AppConfig)
    config.memory = MemoryConfig(enabled=True)


def test_conversation_context_has_user_id():
    ctx = ConversationContext(thread_id="t1", messages=[], user_id="alice")
    assert ctx.user_id == "alice"


def test_conversation_context_user_id_default_none():
    ctx = ConversationContext(thread_id="t1", messages=[])
    assert ctx.user_id is None


def test_queue_add_stores_user_id():
    q = MemoryUpdateQueue(_TEST_APP_CONFIG)
    with patch.object(q, "_reset_timer"):
        q.add(thread_id="t1", messages=["msg"], user_id="alice")
    assert len(q._queue) == 1
    assert q._queue[0].user_id == "alice"
    q.clear()


def test_queue_process_passes_user_id_to_updater():
    q = MemoryUpdateQueue(_TEST_APP_CONFIG)
    with patch.object(q, "_reset_timer"):
        q.add(thread_id="t1", messages=["msg"], user_id="alice")

    mock_updater = MagicMock()
    mock_updater.update_memory.return_value = True
    with patch("deerflow.agents.memory.updater.MemoryUpdater", return_value=mock_updater):
        q._process_queue()

    mock_updater.update_memory.assert_called_once()
    call_kwargs = mock_updater.update_memory.call_args.kwargs
    assert call_kwargs["user_id"] == "alice"
