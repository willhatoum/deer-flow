from unittest.mock import MagicMock, patch

from deerflow.agents.memory.queue import ConversationContext, MemoryUpdateQueue
from deerflow.config.app_config import AppConfig
from deerflow.config.memory_config import MemoryConfig
from deerflow.config.sandbox_config import SandboxConfig



# --- Phase 2 config-refactor test helper ---
# Memory APIs now take MemoryConfig / AppConfig explicitly. Tests construct a
# minimal config once and reuse it across call sites.
from deerflow.config.app_config import AppConfig as _TestAppConfig
from deerflow.config.memory_config import MemoryConfig as _TestMemoryConfig
from deerflow.config.sandbox_config import SandboxConfig as _TestSandboxConfig

_TEST_MEMORY_CONFIG = _TestMemoryConfig(enabled=True)
_TEST_APP_CONFIG = _TestAppConfig(sandbox=_TestSandboxConfig(use="test"), memory=_TEST_MEMORY_CONFIG)
# -------------------------------------------

def _make_config(**memory_overrides) -> AppConfig:
    return AppConfig(sandbox=SandboxConfig(use="test"), memory=MemoryConfig(**memory_overrides))


def test_queue_add_preserves_existing_correction_flag_for_same_thread() -> None:
    queue = MemoryUpdateQueue(_TEST_APP_CONFIG)

    with patch.object(queue, "_reset_timer"):
        queue.add(thread_id="thread-1", messages=["first"], correction_detected=True)
        queue.add(thread_id="thread-1", messages=["second"], correction_detected=False)

    assert len(queue._queue) == 1
    assert queue._queue[0].messages == ["second"]
    assert queue._queue[0].correction_detected is True


def test_process_queue_forwards_correction_flag_to_updater() -> None:
    queue = MemoryUpdateQueue(_TEST_APP_CONFIG)
    queue._queue = [
        ConversationContext(
            thread_id="thread-1",
            messages=["conversation"],
            agent_name="lead_agent",
            correction_detected=True,
        )
    ]
    mock_updater = MagicMock()
    mock_updater.update_memory.return_value = True

    with patch("deerflow.agents.memory.updater.MemoryUpdater", return_value=mock_updater):
        queue._process_queue()

    mock_updater.update_memory.assert_called_once_with(
        messages=["conversation"],
        thread_id="thread-1",
        agent_name="lead_agent",
        correction_detected=True,
        reinforcement_detected=False,
        user_id=None,
    )


def test_queue_add_preserves_existing_reinforcement_flag_for_same_thread() -> None:
    queue = MemoryUpdateQueue(_TEST_APP_CONFIG)

    with patch.object(queue, "_reset_timer"):
        queue.add(thread_id="thread-1", messages=["first"], reinforcement_detected=True)
        queue.add(thread_id="thread-1", messages=["second"], reinforcement_detected=False)

    assert len(queue._queue) == 1
    assert queue._queue[0].messages == ["second"]
    assert queue._queue[0].reinforcement_detected is True


def test_process_queue_forwards_reinforcement_flag_to_updater() -> None:
    queue = MemoryUpdateQueue(_TEST_APP_CONFIG)
    queue._queue = [
        ConversationContext(
            thread_id="thread-1",
            messages=["conversation"],
            agent_name="lead_agent",
            reinforcement_detected=True,
        )
    ]
    mock_updater = MagicMock()
    mock_updater.update_memory.return_value = True

    with patch("deerflow.agents.memory.updater.MemoryUpdater", return_value=mock_updater):
        queue._process_queue()

    mock_updater.update_memory.assert_called_once_with(
        messages=["conversation"],
        thread_id="thread-1",
        agent_name="lead_agent",
        correction_detected=False,
        reinforcement_detected=True,
        user_id=None,
    )
