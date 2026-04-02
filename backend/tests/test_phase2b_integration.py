"""Phase 2-B integration tests.

End-to-end test: simulate a run's complete lifecycle, verify data
is correctly written to both RunStore and RunEventStore.
"""

import asyncio
from uuid import uuid4

import pytest

from deerflow.runtime.events.store.memory import MemoryRunEventStore
from deerflow.runtime.journal import RunJournal
from deerflow.runtime.runs.store.memory import MemoryRunStore


class _FakeMessage:
    def __init__(self, content, usage):
        self.content = content
        self.tool_calls = []
        self.response_metadata = {"model_name": "test-model"}
        self.usage_metadata = usage
        self.id = "test-msg-id"

    def model_dump(self):
        return {"type": "ai", "content": self.content, "id": self.id, "tool_calls": [], "usage_metadata": self.usage_metadata, "response_metadata": self.response_metadata}


class _FakeGeneration:
    def __init__(self, message):
        self.message = message


class _FakeLLMResult:
    def __init__(self, content, usage):
        self.generations = [[_FakeGeneration(_FakeMessage(content, usage))]]


def _make_llm_response(content="Hello", usage=None):
    return _FakeLLMResult(content, usage)


class TestRunLifecycle:
    @pytest.mark.anyio
    async def test_full_run_lifecycle(self):
        """Simulate a complete run lifecycle with RunStore + RunEventStore."""
        run_store = MemoryRunStore()
        event_store = MemoryRunEventStore()

        # 1. Create run
        await run_store.put("r1", thread_id="t1", status="pending")

        # 2. Write human_message
        await event_store.put(
            thread_id="t1",
            run_id="r1",
            event_type="human_message",
            category="message",
            content="What is AI?",
        )

        # 3. Simulate RunJournal callback sequence
        on_complete_data = {}

        def on_complete(**data):
            on_complete_data.update(data)

        journal = RunJournal("r1", "t1", event_store, on_complete=on_complete, flush_threshold=100)
        journal.set_first_human_message("What is AI?")

        # chain_start (top-level)
        journal.on_chain_start({}, {"messages": ["What is AI?"]}, run_id=uuid4(), parent_run_id=None)

        # llm_start + llm_end
        llm_run_id = uuid4()
        journal.on_llm_start({"name": "gpt-4"}, ["prompt"], run_id=llm_run_id, tags=["lead_agent"])
        usage = {"input_tokens": 50, "output_tokens": 100, "total_tokens": 150}
        journal.on_llm_end(_make_llm_response("AI is artificial intelligence.", usage=usage), run_id=llm_run_id, tags=["lead_agent"])

        # chain_end (triggers on_complete + flush_sync which creates a task)
        journal.on_chain_end({}, run_id=uuid4(), parent_run_id=None)
        await journal.flush()
        # Let event loop process any pending flush tasks from _flush_sync
        await asyncio.sleep(0.05)

        # 4. Verify messages
        messages = await event_store.list_messages("t1")
        assert len(messages) == 2  # human + ai
        assert messages[0]["event_type"] == "human_message"
        assert messages[1]["event_type"] == "ai_message"
        assert messages[1]["content"] == "AI is artificial intelligence."

        # 5. Verify events
        events = await event_store.list_events("t1", "r1")
        event_types = {e["event_type"] for e in events}
        assert "run_start" in event_types
        assert "llm_start" in event_types
        assert "llm_end" in event_types
        assert "run_end" in event_types

        # 6. Verify on_complete data
        assert on_complete_data["total_tokens"] == 150
        assert on_complete_data["llm_call_count"] == 1
        assert on_complete_data["lead_agent_tokens"] == 150
        assert on_complete_data["message_count"] == 1
        assert on_complete_data["last_ai_message"] == "AI is artificial intelligence."
        assert on_complete_data["first_human_message"] == "What is AI?"

    @pytest.mark.anyio
    async def test_run_with_tool_calls(self):
        """Simulate a run that uses tools."""
        event_store = MemoryRunEventStore()
        journal = RunJournal("r1", "t1", event_store, flush_threshold=100)

        # tool_start + tool_end
        journal.on_tool_start({"name": "web_search"}, '{"query": "AI"}', run_id=uuid4())
        journal.on_tool_end("Search results...", run_id=uuid4(), name="web_search")
        await journal.flush()

        events = await event_store.list_events("t1", "r1")
        assert len(events) == 2
        assert events[0]["event_type"] == "tool_start"
        assert events[1]["event_type"] == "tool_end"

    @pytest.mark.anyio
    async def test_multi_run_thread(self):
        """Multiple runs on the same thread maintain unified seq ordering."""
        event_store = MemoryRunEventStore()

        # Run 1
        await event_store.put(thread_id="t1", run_id="r1", event_type="human_message", category="message", content="Q1")
        await event_store.put(thread_id="t1", run_id="r1", event_type="ai_message", category="message", content="A1")

        # Run 2
        await event_store.put(thread_id="t1", run_id="r2", event_type="human_message", category="message", content="Q2")
        await event_store.put(thread_id="t1", run_id="r2", event_type="ai_message", category="message", content="A2")

        messages = await event_store.list_messages("t1")
        assert len(messages) == 4
        assert [m["seq"] for m in messages] == [1, 2, 3, 4]
        assert messages[0]["run_id"] == "r1"
        assert messages[2]["run_id"] == "r2"

    @pytest.mark.anyio
    async def test_runmanager_with_store_backing(self):
        """RunManager persists to RunStore when one is provided."""
        from deerflow.runtime.runs.manager import RunManager

        run_store = MemoryRunStore()
        mgr = RunManager(store=run_store)

        record = await mgr.create("t1", assistant_id="lead_agent")
        # Verify persisted to store
        row = await run_store.get(record.run_id)
        assert row is not None
        assert row["thread_id"] == "t1"
        assert row["status"] == "pending"

        # Status update
        from deerflow.runtime.runs.schemas import RunStatus

        await mgr.set_status(record.run_id, RunStatus.running)
        row = await run_store.get(record.run_id)
        assert row["status"] == "running"

    @pytest.mark.anyio
    async def test_runmanager_create_or_reject_persists(self):
        """create_or_reject also persists to store."""
        from deerflow.runtime.runs.manager import RunManager

        run_store = MemoryRunStore()
        mgr = RunManager(store=run_store)

        record = await mgr.create_or_reject("t1", "lead_agent", metadata={"key": "val"})
        row = await run_store.get(record.run_id)
        assert row is not None
        assert row["status"] == "pending"
        assert row["metadata"] == {"key": "val"}

    @pytest.mark.anyio
    async def test_follow_up_metadata_in_messages(self):
        """human_message metadata carries follow_up_to_run_id."""
        event_store = MemoryRunEventStore()

        # Run 1
        await event_store.put(thread_id="t1", run_id="r1", event_type="human_message", category="message", content="Q1")
        await event_store.put(thread_id="t1", run_id="r1", event_type="ai_message", category="message", content="A1")

        # Run 2 (follow-up)
        await event_store.put(
            thread_id="t1",
            run_id="r2",
            event_type="human_message",
            category="message",
            content="Tell me more",
            metadata={"follow_up_to_run_id": "r1"},
        )

        messages = await event_store.list_messages("t1")
        assert len(messages) == 3
        assert messages[2]["metadata"]["follow_up_to_run_id"] == "r1"

    @pytest.mark.anyio
    async def test_summarization_in_history(self):
        """summary message appears correctly in message history."""
        event_store = MemoryRunEventStore()

        await event_store.put(thread_id="t1", run_id="r1", event_type="human_message", category="message", content="Q1")
        await event_store.put(thread_id="t1", run_id="r1", event_type="ai_message", category="message", content="A1")
        await event_store.put(thread_id="t1", run_id="r2", event_type="summary", category="message", content="Previous conversation summarized.", metadata={"replaced_count": 2})
        await event_store.put(thread_id="t1", run_id="r2", event_type="human_message", category="message", content="Q2")
        await event_store.put(thread_id="t1", run_id="r2", event_type="ai_message", category="message", content="A2")

        messages = await event_store.list_messages("t1")
        assert len(messages) == 5
        assert messages[2]["event_type"] == "summary"
        assert messages[2]["metadata"]["replaced_count"] == 2

    @pytest.mark.anyio
    async def test_db_backed_run_lifecycle(self, tmp_path):
        """Full lifecycle with SQLite-backed RunRepository + DbRunEventStore."""
        from deerflow.persistence.engine import close_engine, get_session_factory, init_engine
        from deerflow.persistence.repositories.run_repo import RunRepository
        from deerflow.runtime.events.store.db import DbRunEventStore
        from deerflow.runtime.runs.manager import RunManager

        url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
        await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
        sf = get_session_factory()

        run_store = RunRepository(sf)
        event_store = DbRunEventStore(sf)
        mgr = RunManager(store=run_store)

        # Create run
        record = await mgr.create("t1", "lead_agent")
        run_id = record.run_id

        # Write human_message
        await event_store.put(thread_id="t1", run_id=run_id, event_type="human_message", category="message", content="Hello DB")

        # Simulate journal
        on_complete_data = {}
        journal = RunJournal(run_id, "t1", event_store, on_complete=lambda **d: on_complete_data.update(d), flush_threshold=100)
        journal.set_first_human_message("Hello DB")

        journal.on_chain_start({}, {}, run_id=uuid4(), parent_run_id=None)
        llm_rid = uuid4()
        journal.on_llm_start({"name": "test"}, [], run_id=llm_rid, tags=["lead_agent"])
        journal.on_llm_end(_make_llm_response("DB response", usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}), run_id=llm_rid, tags=["lead_agent"])
        journal.on_chain_end({}, run_id=uuid4(), parent_run_id=None)
        await journal.flush()
        await asyncio.sleep(0.05)

        # Verify run persisted
        row = await run_store.get(run_id)
        assert row is not None
        assert row["status"] == "pending"  # RunManager set it, journal doesn't update status

        # Update completion
        await run_store.update_run_completion(run_id, status="success", **on_complete_data)
        row = await run_store.get(run_id)
        assert row["status"] == "success"
        assert row["total_tokens"] == 15

        # Verify messages from DB
        messages = await event_store.list_messages("t1")
        assert len(messages) == 2
        assert messages[0]["event_type"] == "human_message"
        assert messages[1]["event_type"] == "ai_message"

        # Verify events from DB
        events = await event_store.list_events("t1", run_id)
        event_types = {e["event_type"] for e in events}
        assert "run_start" in event_types
        assert "llm_end" in event_types
        assert "run_end" in event_types

        await close_engine()
