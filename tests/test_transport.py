import io
import json
import unittest

from sase.cli import MAX_FRAME, serve
from sase.core import Core


class TransportTest(unittest.TestCase):
    def test_invalid_frames_do_not_interrupt_following_request(self):
        core = Core(":memory:")
        self.addCleanup(core.close)
        valid = b'{"id":"next","op":"inspect","workspace":"w","v":1}\n'
        data = b"bad json\n" + b"[" * 2048 + b"]" * 2048 + b"\n" + b"x" * (MAX_FRAME + 1) + b"\n" + valid
        sink = io.StringIO()
        serve(core, io.BytesIO(data), sink)
        *errors, result = [json.loads(line) for line in sink.getvalue().splitlines()]
        self.assertEqual([r["error"] for r in errors], ["invalid_request"] * 3)
        self.assertEqual(result, {"id": "next", "ok": True, "result": {"decisions": []}})
