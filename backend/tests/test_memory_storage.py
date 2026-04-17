
# --- Phase 2 config-refactor test helper ---
# Memory APIs now take MemoryConfig / AppConfig explicitly. Tests construct a
# minimal config once and reuse it across call sites.
from deerflow.config.app_config import AppConfig as _TestAppConfig
from deerflow.config.memory_config import MemoryConfig as _TestMemoryConfig
from deerflow.config.sandbox_config import SandboxConfig as _TestSandboxConfig

_TEST_MEMORY_CONFIG = _TestMemoryConfig(enabled=True)
_TEST_APP_CONFIG = _TestAppConfig(sandbox=_TestSandboxConfig(use="test"), memory=_TEST_MEMORY_CONFIG)
# -------------------------------------------

"""Tests for memory storage providers."""

import threading
from unittest.mock import MagicMock, patch

import pytest

from deerflow.agents.memory.storage import (
    FileMemoryStorage,
    MemoryStorage,
    create_empty_memory,
    get_memory_storage,
)
from deerflow.config.app_config import AppConfig
from deerflow.config.memory_config import MemoryConfig
from deerflow.config.sandbox_config import SandboxConfig


def _app_config(**memory_overrides) -> AppConfig:
    return AppConfig(sandbox=SandboxConfig(use="test"), memory=MemoryConfig(**memory_overrides))


class TestCreateEmptyMemory:
    """Test create_empty_memory function."""

    def test_returns_valid_structure(self):
        """Should return a valid empty memory structure."""
        memory = create_empty_memory()
        assert isinstance(memory, dict)
        assert memory["version"] == "1.0"
        assert "lastUpdated" in memory
        assert isinstance(memory["user"], dict)
        assert isinstance(memory["history"], dict)
        assert isinstance(memory["facts"], list)


class TestMemoryStorageInterface:
    """Test MemoryStorage abstract base class."""

    def test_abstract_methods(self):
        """Should raise TypeError when trying to instantiate abstract class."""

        class TestStorage(MemoryStorage):
            pass

        with pytest.raises(TypeError):
            TestStorage()


class TestFileMemoryStorage:
    """Test FileMemoryStorage implementation."""

    def test_get_memory_file_path_global(self, tmp_path):
        """Should return global memory file path when agent_name is None."""

        def mock_get_paths():
            mock_paths = MagicMock()
            mock_paths.memory_file = tmp_path / "memory.json"
            return mock_paths

        with patch("deerflow.agents.memory.storage.get_paths", side_effect=mock_get_paths):
            storage = FileMemoryStorage(_TEST_MEMORY_CONFIG)
            path = storage._get_memory_file_path(None)
            assert path == tmp_path / "memory.json"

    def test_get_memory_file_path_agent(self, tmp_path):
        """Should return per-agent memory file path when agent_name is provided."""

        def mock_get_paths():
            mock_paths = MagicMock()
            mock_paths.agent_memory_file.return_value = tmp_path / "agents" / "test-agent" / "memory.json"
            return mock_paths

        with patch("deerflow.agents.memory.storage.get_paths", side_effect=mock_get_paths):
            storage = FileMemoryStorage(_TEST_MEMORY_CONFIG)
            path = storage._get_memory_file_path("test-agent")
            assert path == tmp_path / "agents" / "test-agent" / "memory.json"

    @pytest.mark.parametrize("invalid_name", ["", "../etc/passwd", "agent/name", "agent\\name", "agent name", "agent@123", "agent_name"])
    def test_validate_agent_name_invalid(self, invalid_name):
        """Should raise ValueError for invalid agent names that don't match the pattern."""
        storage = FileMemoryStorage(_TEST_MEMORY_CONFIG)
        with pytest.raises(ValueError, match="Invalid agent name|Agent name must be a non-empty string"):
            storage._validate_agent_name(invalid_name)

    def test_load_creates_empty_memory(self, tmp_path):
        """Should create empty memory when file doesn't exist."""

        def mock_get_paths():
            mock_paths = MagicMock()
            mock_paths.memory_file = tmp_path / "non_existent_memory.json"
            return mock_paths

        with patch("deerflow.agents.memory.storage.get_paths", side_effect=mock_get_paths):
            storage = FileMemoryStorage(_TEST_MEMORY_CONFIG)
            memory = storage.load()
            assert isinstance(memory, dict)
            assert memory["version"] == "1.0"

    def test_save_writes_to_file(self, tmp_path):
        """Should save memory data to file."""
        memory_file = tmp_path / "memory.json"

        def mock_get_paths():
            mock_paths = MagicMock()
            mock_paths.memory_file = memory_file
            return mock_paths

        with patch("deerflow.agents.memory.storage.get_paths", side_effect=mock_get_paths):
            storage = FileMemoryStorage(_TEST_MEMORY_CONFIG)
            test_memory = {"version": "1.0", "facts": [{"content": "test fact"}]}
            result = storage.save(test_memory)
            assert result is True
            assert memory_file.exists()

    def test_reload_forces_cache_invalidation(self, tmp_path):
        """Should force reload from file and invalidate cache."""
        memory_file = tmp_path / "memory.json"
        memory_file.parent.mkdir(parents=True, exist_ok=True)
        memory_file.write_text('{"version": "1.0", "facts": [{"content": "initial fact"}]}')

        def mock_get_paths():
            mock_paths = MagicMock()
            mock_paths.memory_file = memory_file
            return mock_paths

        with patch("deerflow.agents.memory.storage.get_paths", side_effect=mock_get_paths):
            storage = FileMemoryStorage(_TEST_MEMORY_CONFIG)
            # First load
            memory1 = storage.load()
            assert memory1["facts"][0]["content"] == "initial fact"

            # Update file directly
            memory_file.write_text('{"version": "1.0", "facts": [{"content": "updated fact"}]}')

            # Reload should get updated data
            memory2 = storage.reload()
            assert memory2["facts"][0]["content"] == "updated fact"


class TestGetMemoryStorage:
    """Test get_memory_storage function."""

    @pytest.fixture(autouse=True)
    def reset_storage_instance(self):
        """Reset the global storage instance before and after each test."""
        import deerflow.agents.memory.storage as storage_mod

        storage_mod._storage_instance = None
        yield
        storage_mod._storage_instance = None

    def test_returns_file_memory_storage_by_default(self):
        """Should return FileMemoryStorage by default."""
        storage = get_memory_storage(_TEST_MEMORY_CONFIG)
        assert isinstance(storage, FileMemoryStorage)

    def test_falls_back_to_file_memory_storage_on_error(self):
        """Should fall back to FileMemoryStorage if configured storage fails to load."""
        storage = get_memory_storage(_TEST_MEMORY_CONFIG)
        assert isinstance(storage, FileMemoryStorage)

    def test_returns_singleton_instance(self):
        """Should return the same instance on subsequent calls."""
        storage1 = get_memory_storage(_TEST_MEMORY_CONFIG)
        storage2 = get_memory_storage(_TEST_MEMORY_CONFIG)
        assert storage1 is storage2

    def test_get_memory_storage_thread_safety(self):
        """Should safely initialize the singleton even with concurrent calls."""
        results = []

        def get_storage():
            # get_memory_storage is called concurrently from multiple threads while
            # AppConfig.get is patched once around thread creation. This verifies
            # that the singleton initialization remains thread-safe.
            results.append(get_memory_storage(_TEST_MEMORY_CONFIG))

        threads = [threading.Thread(target=get_storage) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All results should be the exact same instance
        assert len(results) == 10
        assert all(r is results[0] for r in results)

    def test_get_memory_storage_invalid_class_fallback(self):
        """Should fall back to FileMemoryStorage if the configured class is not actually a class."""
        # Using a built-in function instead of a class
        storage = get_memory_storage(_TEST_MEMORY_CONFIG)
        assert isinstance(storage, FileMemoryStorage)

    def test_get_memory_storage_non_subclass_fallback(self):
        """Should fall back to FileMemoryStorage if the configured class is not a subclass of MemoryStorage."""
        # Using 'dict' as a class that is not a MemoryStorage subclass
        storage = get_memory_storage(_TEST_MEMORY_CONFIG)
        assert isinstance(storage, FileMemoryStorage)
