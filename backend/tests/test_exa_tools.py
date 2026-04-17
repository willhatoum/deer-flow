"""Unit tests for the Exa community tools."""

import json
from unittest.mock import MagicMock, patch

import pytest

# --- Phase 2 test helper: injected runtime for community tools ---
from types import SimpleNamespace as _P2NS
from deerflow.config.app_config import AppConfig as _P2AppConfig
from deerflow.config.sandbox_config import SandboxConfig as _P2SandboxConfig
from deerflow.config.deer_flow_context import DeerFlowContext as _P2Ctx
_P2_APP_CONFIG = _P2AppConfig(sandbox=_P2SandboxConfig(use="test"))
_P2_RUNTIME = _P2NS(context=_P2Ctx(app_config=_P2_APP_CONFIG, thread_id="test-thread"))


def _runtime_with_config(config):
    """Build a runtime carrying a custom (possibly mocked) app_config.

    ``DeerFlowContext`` is a frozen dataclass typed as ``AppConfig`` but
    dataclasses don't enforce the type at runtime — handing a Mock through
    lets tests exercise the tool's ``get_tool_config`` lookup without going
    through a process-global config.
    """
    ctx = _P2Ctx.__new__(_P2Ctx)
    object.__setattr__(ctx, "app_config", config)
    object.__setattr__(ctx, "thread_id", "test-thread")
    object.__setattr__(ctx, "agent_name", None)
    return _P2NS(context=ctx)
# -------------------------------------------------------------------



@pytest.fixture
def mock_app_config():
    """Fixture retained as a pass-through: tests inject config via runtime directly."""
    yield


@pytest.fixture
def mock_exa_client():
    """Mock the Exa client."""
    with patch("deerflow.community.exa.tools.Exa") as mock_exa_cls:
        mock_client = MagicMock()
        mock_exa_cls.return_value = mock_client
        yield mock_client


class TestWebSearchTool:
    def test_basic_search(self, mock_app_config, mock_exa_client):
        """Test basic web search returns normalized results."""
        mock_result_1 = MagicMock()
        mock_result_1.title = "Test Title 1"
        mock_result_1.url = "https://example.com/1"
        mock_result_1.highlights = ["This is a highlight about the topic."]

        mock_result_2 = MagicMock()
        mock_result_2.title = "Test Title 2"
        mock_result_2.url = "https://example.com/2"
        mock_result_2.highlights = ["First highlight.", "Second highlight."]

        mock_response = MagicMock()
        mock_response.results = [mock_result_1, mock_result_2]
        mock_exa_client.search.return_value = mock_response

        from deerflow.community.exa.tools import web_search_tool

        result = web_search_tool.func(query="test query", runtime=_P2_RUNTIME)
        parsed = json.loads(result)

        assert len(parsed) == 2
        assert parsed[0]["title"] == "Test Title 1"
        assert parsed[0]["url"] == "https://example.com/1"
        assert parsed[0]["snippet"] == "This is a highlight about the topic."
        assert parsed[1]["snippet"] == "First highlight.\nSecond highlight."

        mock_exa_client.search.assert_called_once_with(
            "test query",
            type="auto",
            num_results=5,
            contents={"highlights": {"max_characters": 1000}},
        )

    def test_search_with_custom_config(self, mock_exa_client):
        """Test search respects custom configuration values."""
        tool_config = MagicMock()
        tool_config.model_extra = {
            "max_results": 10,
            "search_type": "neural",
            "contents_max_characters": 2000,
            "api_key": "test-key",
        }
        fake_config = MagicMock()
        fake_config.get_tool_config.return_value = tool_config

        mock_response = MagicMock()
        mock_response.results = []
        mock_exa_client.search.return_value = mock_response

        from deerflow.community.exa.tools import web_search_tool

        web_search_tool.func(query="neural search", runtime=_runtime_with_config(fake_config))

        mock_exa_client.search.assert_called_once_with(
            "neural search",
            type="neural",
            num_results=10,
            contents={"highlights": {"max_characters": 2000}},
        )

    def test_search_with_no_highlights(self, mock_app_config, mock_exa_client):
        """Test search handles results with no highlights."""
        mock_result = MagicMock()
        mock_result.title = "No Highlights"
        mock_result.url = "https://example.com/empty"
        mock_result.highlights = None

        mock_response = MagicMock()
        mock_response.results = [mock_result]
        mock_exa_client.search.return_value = mock_response

        from deerflow.community.exa.tools import web_search_tool

        result = web_search_tool.func(query="test", runtime=_P2_RUNTIME)
        parsed = json.loads(result)

        assert parsed[0]["snippet"] == ""

    def test_search_empty_results(self, mock_app_config, mock_exa_client):
        """Test search with no results returns empty list."""
        mock_response = MagicMock()
        mock_response.results = []
        mock_exa_client.search.return_value = mock_response

        from deerflow.community.exa.tools import web_search_tool

        result = web_search_tool.func(query="nothing", runtime=_P2_RUNTIME)
        parsed = json.loads(result)

        assert parsed == []

    def test_search_error_handling(self, mock_app_config, mock_exa_client):
        """Test search returns error string on exception."""
        mock_exa_client.search.side_effect = Exception("API rate limit exceeded")

        from deerflow.community.exa.tools import web_search_tool

        result = web_search_tool.func(query="error", runtime=_P2_RUNTIME)

        assert result == "Error: API rate limit exceeded"


class TestWebFetchTool:
    def test_basic_fetch(self, mock_app_config, mock_exa_client):
        """Test basic web fetch returns formatted content."""
        mock_result = MagicMock()
        mock_result.title = "Fetched Page"
        mock_result.text = "This is the page content."

        mock_response = MagicMock()
        mock_response.results = [mock_result]
        mock_exa_client.get_contents.return_value = mock_response

        from deerflow.community.exa.tools import web_fetch_tool

        result = web_fetch_tool.func(url="https://example.com", runtime=_P2_RUNTIME)

        assert result == "# Fetched Page\n\nThis is the page content."
        mock_exa_client.get_contents.assert_called_once_with(
            ["https://example.com"],
            text={"max_characters": 4096},
        )

    def test_fetch_no_title(self, mock_app_config, mock_exa_client):
        """Test fetch with missing title uses 'Untitled'."""
        mock_result = MagicMock()
        mock_result.title = None
        mock_result.text = "Content without title."

        mock_response = MagicMock()
        mock_response.results = [mock_result]
        mock_exa_client.get_contents.return_value = mock_response

        from deerflow.community.exa.tools import web_fetch_tool

        result = web_fetch_tool.func(url="https://example.com", runtime=_P2_RUNTIME)

        assert result.startswith("# Untitled\n\n")

    def test_fetch_no_results(self, mock_app_config, mock_exa_client):
        """Test fetch with no results returns error."""
        mock_response = MagicMock()
        mock_response.results = []
        mock_exa_client.get_contents.return_value = mock_response

        from deerflow.community.exa.tools import web_fetch_tool

        result = web_fetch_tool.func(url="https://example.com/404", runtime=_P2_RUNTIME)

        assert result == "Error: No results found"

    def test_fetch_error_handling(self, mock_app_config, mock_exa_client):
        """Test fetch returns error string on exception."""
        mock_exa_client.get_contents.side_effect = Exception("Connection timeout")

        from deerflow.community.exa.tools import web_fetch_tool

        result = web_fetch_tool.func(url="https://example.com", runtime=_P2_RUNTIME)

        assert result == "Error: Connection timeout"

    def test_fetch_reads_web_fetch_config(self, mock_exa_client):
        """Test that web_fetch_tool reads 'web_fetch' config, not 'web_search'."""
        tool_config = MagicMock()
        tool_config.model_extra = {"api_key": "exa-fetch-key"}
        fake_config = MagicMock()
        fake_config.get_tool_config.return_value = tool_config

        mock_result = MagicMock()
        mock_result.title = "Page"
        mock_result.text = "Content."
        mock_response = MagicMock()
        mock_response.results = [mock_result]
        mock_exa_client.get_contents.return_value = mock_response

        from deerflow.community.exa.tools import web_fetch_tool

        web_fetch_tool.func(url="https://example.com", runtime=_runtime_with_config(fake_config))

        fake_config.get_tool_config.assert_any_call("web_fetch")

    def test_fetch_uses_independent_api_key(self, mock_exa_client):
        """Test mixed-provider config: web_fetch uses its own api_key, not web_search's."""
        with patch("deerflow.community.exa.tools.Exa") as mock_exa_cls:
            mock_exa_cls.return_value = mock_exa_client
            fetch_config = MagicMock()
            fetch_config.model_extra = {"api_key": "exa-fetch-key"}

            def get_tool_config(name):
                if name == "web_fetch":
                    return fetch_config
                return None

            fake_config = MagicMock()
            fake_config.get_tool_config.side_effect = get_tool_config

            mock_result = MagicMock()
            mock_result.title = "Page"
            mock_result.text = "Content."
            mock_response = MagicMock()
            mock_response.results = [mock_result]
            mock_exa_client.get_contents.return_value = mock_response

            from deerflow.community.exa.tools import web_fetch_tool

            web_fetch_tool.func(url="https://example.com", runtime=_runtime_with_config(fake_config))

            mock_exa_cls.assert_called_once_with(api_key="exa-fetch-key")

    def test_fetch_truncates_long_content(self, mock_app_config, mock_exa_client):
        """Test fetch truncates content to 4096 characters."""
        mock_result = MagicMock()
        mock_result.title = "Long Page"
        mock_result.text = "x" * 5000

        mock_response = MagicMock()
        mock_response.results = [mock_result]
        mock_exa_client.get_contents.return_value = mock_response

        from deerflow.community.exa.tools import web_fetch_tool

        result = web_fetch_tool.func(url="https://example.com", runtime=_P2_RUNTIME)

        # "# Long Page\n\n" is 14 chars, content truncated to 4096
        content_after_header = result.split("\n\n", 1)[1]
        assert len(content_after_header) == 4096
