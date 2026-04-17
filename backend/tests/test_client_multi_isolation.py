"""Multi-client isolation regression test.

Phase 2 Task P2-3: ``DeerFlowClient`` now captures its ``AppConfig`` in the
constructor instead of going through a process-global config.
This test pins the resulting invariant: two clients with different configs
can coexist without contending over shared state.

Before P2-3, the shared ``AppConfig._global`` caused the second client's
``init()`` to clobber the first client's config.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from deerflow.client import DeerFlowClient
from deerflow.config.app_config import AppConfig
from deerflow.config.memory_config import MemoryConfig
from deerflow.config.sandbox_config import SandboxConfig


@pytest.fixture
def disable_agent_creation(monkeypatch):
    """Prevent lazy agent creation — we only care about config access."""
    monkeypatch.setattr(DeerFlowClient, "_get_or_create_agent", MagicMock(), raising=False)


def test_two_clients_do_not_clobber_each_other(disable_agent_creation):
    """Two clients with distinct configs keep their own AppConfig."""
    cfg_a = AppConfig(
        sandbox=SandboxConfig(use="test"),
        memory=MemoryConfig(enabled=True),
    )
    cfg_b = AppConfig(
        sandbox=SandboxConfig(use="test"),
        memory=MemoryConfig(enabled=False),
    )

    client_a = DeerFlowClient(config=cfg_a)
    client_b = DeerFlowClient(config=cfg_b)

    # Identity: each client retains its own instance, not a shared ref
    assert client_a._app_config is cfg_a
    assert client_b._app_config is cfg_b

    # Semantic: memory flag differs
    assert client_a._app_config.memory.enabled is True
    assert client_b._app_config.memory.enabled is False


def test_client_config_precedes_path(disable_agent_creation, tmp_path):
    """When both config= and config_path= are given, config= wins."""
    cfg = AppConfig(sandbox=SandboxConfig(use="test"), log_level="debug")

    # config_path points at a file that doesn't exist — proves it's unused
    bogus_path = str(tmp_path / "nope.yaml")
    client = DeerFlowClient(config_path=bogus_path, config=cfg)

    assert client._app_config is cfg
    assert client._app_config.log_level == "debug"


def test_multi_client_gateway_dict_returns_distinct(disable_agent_creation):
    """get_mcp_config() reads from self._app_config, not process-global."""
    from deerflow.config.extensions_config import ExtensionsConfig, McpServerConfig

    ext_a = ExtensionsConfig(mcp_servers={"server-a": McpServerConfig(enabled=True)})
    ext_b = ExtensionsConfig(mcp_servers={"server-b": McpServerConfig(enabled=True)})

    cfg_a = AppConfig(sandbox=SandboxConfig(use="test"), extensions=ext_a)
    cfg_b = AppConfig(sandbox=SandboxConfig(use="test"), extensions=ext_b)

    client_a = DeerFlowClient(config=cfg_a)
    client_b = DeerFlowClient(config=cfg_b)

    servers_a = client_a.get_mcp_config()["mcp_servers"]
    servers_b = client_b.get_mcp_config()["mcp_servers"]

    assert set(servers_a.keys()) == {"server-a"}
    assert set(servers_b.keys()) == {"server-b"}
