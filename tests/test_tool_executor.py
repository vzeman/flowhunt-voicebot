from __future__ import annotations

import unittest

from voicebot.tool_executor import AgentToolExecutor


class AgentToolExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_executor_runs_sync_handler(self) -> None:
        executor = AgentToolExecutor()
        executor.register("sync_tool", lambda args: {"value": args["value"]})

        result = await executor.execute("sync_tool", {"value": 3})

        self.assertEqual(result, {"value": 3})

    async def test_tool_executor_runs_async_handler(self) -> None:
        executor = AgentToolExecutor()

        async def handler(args):
            return {"value": args["value"] + 1}

        executor.register("async_tool", handler)

        result = await executor.execute("async_tool", {"value": 3})

        self.assertEqual(result, {"value": 4})

    async def test_tool_executor_reports_unknown_tool(self) -> None:
        executor = AgentToolExecutor()

        with self.assertRaises(KeyError):
            await executor.execute("missing", {})

    def test_tool_executor_lists_registered_names(self) -> None:
        executor = AgentToolExecutor()
        executor.register("b", lambda args: {})
        executor.register("a", lambda args: {})

        self.assertEqual(executor.registered_names(), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
