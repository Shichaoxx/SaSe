"""Count real HTTP attempts made by Pi at its evaluation budget boundary."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import tempfile
from threading import Thread
import unittest

from sase.evaluation import ARMS, cases, score


ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def provider(task, *, finish_on, finish_reason="stop"):
    requests = []
    valid_text = json.dumps({"ids": task["pages"][0]})

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            number = len(requests) + 1
            requests.append(number)
            delta = {"role": "assistant"}
            if number == finish_on:
                delta["content"] = valid_text
                reason = finish_reason
            else:
                # The tool-requesting message includes a valid-looking answer.
                # On the last available call it must score as exhausted.
                delta["content"] = valid_text
                count = 1 if number == 1 else 2
                delta["tool_calls"] = [
                    {"index": i, "id": f"call-{number}-{i}", "type": "function",
                     "function": {"name": "sase_page", "arguments": '{"page":1}'}}
                    for i in range(count)
                ]
                reason = "tool_calls"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            head = {"id": "test", "object": "chat.completion.chunk", "created": 1, "model": "synthetic"}
            chunks = [
                {**head, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {**head, "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
                 "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18}},
            ]
            for chunk in chunks:
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1", requests
        finally:
            server.shutdown()
            thread.join()


class BudgetTests(unittest.TestCase):
    def trial(self, *, arm, limit, finish_on, finish_reason="stop"):
        task = next(t for t in cases(19) if t["task_type"] == "first-more")
        with tempfile.TemporaryDirectory() as directory, provider(
                task, finish_on=finish_on, finish_reason=finish_reason) as (url, requests):
            config = {"case": task, "arm": arm, "workspace": arm, "db": str(Path(directory, "ledger.sqlite3")),
                      "model": "synthetic", "baseUrl": url, "timeoutMs": 10000}
            if limit is not None:
                config["maxModelCalls"] = limit
            process = subprocess.run(["node", "scripts/eval-runner.mjs"], cwd=ROOT, input=json.dumps(config),
                                     capture_output=True, text=True, check=True, timeout=20)
            row = json.loads(process.stdout)
            self.assertIsNone(row["error"], row)
            self.assertEqual(row["model_calls"], len(requests))
            self.assertEqual(row["total_tokens"], len(requests) * 18)
            return task, row

    def test_natural_answer_at_limit(self):
        task, row = self.trial(arm="gated", limit=2, finish_on=2)
        self.assertEqual(row["termination_reason"], "completed")
        self.assertFalse(row["budget_exhausted"])
        self.assertEqual(row["tool_pages"], [1])
        self.assertEqual(row["model_calls"], 2)
        self.assertTrue(score(task, row)["success"])

    def test_provider_output_limit_is_not_a_natural_stop(self):
        task, row = self.trial(arm="native", limit=2, finish_on=2, finish_reason="length")
        self.assertEqual(row["termination_reason"], "output_limit")
        self.assertEqual(row["answer_diagnostics"]["format"], "strict_json")
        self.assertEqual(score(task, row)["reasons"], ["output_limit"])

    def test_parallel_tool_requests_at_limit(self):
        for arm in ARMS:
            with self.subTest(arm=arm):
                task, row = self.trial(arm=arm, limit=2, finish_on=3)
                self.assertEqual(row["termination_reason"], "model_call_budget")
                self.assertTrue(row["budget_exhausted"])
                self.assertEqual(row["model_calls"], 2)
                self.assertEqual(row["tool_pages"], [1])
                self.assertIsNone(row["final_ids"])
                self.assertIsNone(row["answer_diagnostics"])
                self.assertIs(score(task, row)["success"], False)

    def test_default_budget_counts_replies_not_tools(self):
        _, row = self.trial(arm="native", limit=None, finish_on=7)
        self.assertTrue(row["budget_exhausted"])
        self.assertEqual(row["model_calls"], 6)
        self.assertEqual(len(row["tool_pages"]), 1 + 4 * 2)


if __name__ == "__main__":
    unittest.main()
