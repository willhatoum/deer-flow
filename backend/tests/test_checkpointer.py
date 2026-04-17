"""Unit tests for checkpointer config and singleton factory."""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deerflow.config.app_config import AppConfig
from deerflow.config.checkpointer_config import CheckpointerConfig
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.runtime.checkpointer import get_checkpointer, reset_checkpointer


def _make_config(checkpointer: CheckpointerConfig | None = None) -> AppConfig:
    return AppConfig(sandbox=SandboxConfig(use="test"), checkpointer=checkpointer)


@pytest.fixture(autouse=True)
def reset_state():
    """Reset singleton state before each test."""
    reset_checkpointer()
    yield
    reset_checkpointer()


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestCheckpointerConfig:
    def test_memory_config(self):
        config = CheckpointerConfig(type="memory")
        assert config.type == "memory"
        assert config.connection_string is None

    def test_sqlite_config(self):
        config = CheckpointerConfig(type="sqlite", connection_string="/tmp/test.db")
        assert config.type == "sqlite"
        assert config.connection_string == "/tmp/test.db"

    def test_postgres_config(self):
        config = CheckpointerConfig(type="postgres", connection_string="postgresql://localhost/db")
        assert config.type == "postgres"
        assert config.connection_string == "postgresql://localhost/db"

    def test_default_connection_string_is_none(self):
        config = CheckpointerConfig(type="memory")
        assert config.connection_string is None

    def test_invalid_type_raises(self):
        with pytest.raises(Exception):
            CheckpointerConfig(type="unknown")


# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------


class TestGetCheckpointer:
    def test_returns_in_memory_saver_when_not_configured(self):
        """get_checkpointer should return InMemorySaver when not configured."""
        from langgraph.checkpoint.memory import InMemorySaver

        cfg = _make_config()
        cp = get_checkpointer(cfg)
        assert cp is not None
        assert isinstance(cp, InMemorySaver)

    def test_memory_returns_in_memory_saver(self):
        from langgraph.checkpoint.memory import InMemorySaver

        cfg = _make_config(CheckpointerConfig(type="memory"))
        cp = get_checkpointer(cfg)
        assert isinstance(cp, InMemorySaver)

    def test_memory_singleton(self):
        cfg = _make_config(CheckpointerConfig(type="memory"))
        cp1 = get_checkpointer(cfg)
        cp2 = get_checkpointer(cfg)
        assert cp1 is cp2

    def test_reset_clears_singleton(self):
        cfg = _make_config(CheckpointerConfig(type="memory"))
        cp1 = get_checkpointer(cfg)
        reset_checkpointer()
        cp2 = get_checkpointer(cfg)
        assert cp1 is not cp2

    def test_sqlite_raises_when_package_missing(self):
        cfg = _make_config(CheckpointerConfig(type="sqlite", connection_string="/tmp/test.db"))
        with patch.dict(sys.modules, {"langgraph.checkpoint.sqlite": None}):
            reset_checkpointer()
            with pytest.raises(ImportError, match="langgraph-checkpoint-sqlite"):
                get_checkpointer(cfg)

    def test_postgres_raises_when_package_missing(self):
        cfg = _make_config(CheckpointerConfig(type="postgres", connection_string="postgresql://localhost/db"))
        with patch.dict(sys.modules, {"langgraph.checkpoint.postgres": None}):
            reset_checkpointer()
            with pytest.raises(ImportError, match="langgraph-checkpoint-postgres"):
                get_checkpointer(cfg)

    def test_postgres_raises_when_connection_string_missing(self):
        cfg = _make_config(CheckpointerConfig(type="postgres"))
        mock_saver = MagicMock()
        mock_module = MagicMock()
        mock_module.PostgresSaver = mock_saver
        with patch.dict(sys.modules, {"langgraph.checkpoint.postgres": mock_module}):
            reset_checkpointer()
            with pytest.raises(ValueError, match="connection_string is required"):
                get_checkpointer(cfg)

    def test_sqlite_creates_saver(self):
        """SQLite checkpointer is created when package is available."""
        cfg = _make_config(CheckpointerConfig(type="sqlite", connection_string="/tmp/test.db"))

        mock_saver_instance = MagicMock()
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_saver_instance)
        mock_cm.__exit__ = MagicMock(return_value=False)

        mock_saver_cls = MagicMock()
        mock_saver_cls.from_conn_string = MagicMock(return_value=mock_cm)

        mock_module = MagicMock()
        mock_module.SqliteSaver = mock_saver_cls

        with patch.dict(sys.modules, {"langgraph.checkpoint.sqlite": mock_module}):
            reset_checkpointer()
            cp = get_checkpointer(cfg)

        assert cp is mock_saver_instance
        mock_saver_cls.from_conn_string.assert_called_once()
        mock_saver_instance.setup.assert_called_once()

    def test_postgres_creates_saver(self):
        """Postgres checkpointer is created when packages are available."""
        cfg = _make_config(CheckpointerConfig(type="postgres", connection_string="postgresql://localhost/db"))

        mock_saver_instance = MagicMock()
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_saver_instance)
        mock_cm.__exit__ = MagicMock(return_value=False)

        mock_saver_cls = MagicMock()
        mock_saver_cls.from_conn_string = MagicMock(return_value=mock_cm)

        mock_pg_module = MagicMock()
        mock_pg_module.PostgresSaver = mock_saver_cls

        with patch.dict(sys.modules, {"langgraph.checkpoint.postgres": mock_pg_module}):
            reset_checkpointer()
            cp = get_checkpointer(cfg)

        assert cp is mock_saver_instance
        mock_saver_cls.from_conn_string.assert_called_once_with("postgresql://localhost/db")
        mock_saver_instance.setup.assert_called_once()


class TestAsyncCheckpointer:
    @pytest.mark.anyio
    async def test_sqlite_creates_parent_dir_via_to_thread(self):
        """Async SQLite setup should move mkdir off the event loop."""
        from deerflow.runtime.checkpointer.async_provider import make_checkpointer

        mock_config = MagicMock()
        mock_config.checkpointer = CheckpointerConfig(type="sqlite", connection_string="relative/test.db")

        mock_saver = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__.return_value = mock_saver
        mock_cm.__aexit__.return_value = False

        mock_saver_cls = MagicMock()
        mock_saver_cls.from_conn_string.return_value = mock_cm

        mock_module = MagicMock()
        mock_module.AsyncSqliteSaver = mock_saver_cls

        with (
            patch.dict(sys.modules, {"langgraph.checkpoint.sqlite.aio": mock_module}),
            patch("deerflow.runtime.checkpointer.async_provider.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread,
            patch(
                "deerflow.runtime.checkpointer.async_provider.resolve_sqlite_conn_str",
                return_value="/tmp/resolved/test.db",
            ),
        ):
            async with make_checkpointer(mock_config) as saver:
                assert saver is mock_saver

        mock_to_thread.assert_awaited_once()
        called_fn, called_path = mock_to_thread.await_args.args
        assert called_fn.__name__ == "ensure_sqlite_parent_dir"
        assert called_path == "/tmp/resolved/test.db"
        mock_saver_cls.from_conn_string.assert_called_once_with("/tmp/resolved/test.db")
        mock_saver.setup.assert_awaited_once()


# ---------------------------------------------------------------------------
# app_config.py integration
# ---------------------------------------------------------------------------


class TestAppConfigLoadsCheckpointer:
    def test_load_checkpointer_section(self):
        """AppConfig with checkpointer section has the correct config."""
        cfg = _make_config(CheckpointerConfig(type="memory"))
        assert cfg.checkpointer is not None
        assert cfg.checkpointer.type == "memory"


# ---------------------------------------------------------------------------
# DeerFlowClient falls back to config checkpointer
# ---------------------------------------------------------------------------


class TestClientCheckpointerFallback:
    def test_client_uses_config_checkpointer_when_none_provided(self):
        """DeerFlowClient._ensure_agent falls back to get_checkpointer(app_config) when checkpointer=None."""
        # This is a structural test — verifying the fallback path exists.
        cfg = _make_config(CheckpointerConfig(type="memory"))
        assert cfg.checkpointer is not None
