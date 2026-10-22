"""能力返回值兼容层的回归测试。

SDK 会解包单字段能力的结果，Host 也可能返回 RPC 包装字典；
两种形态都必须能取到业务值，否则命令侧与工具侧的取图会静默失败。
"""

from __future__ import annotations

import unittest

from core.maibot_compat import unwrap_capability


class UnwrapCapabilityTest(unittest.TestCase):
    def test_sdk_unwrapped_message(self) -> None:
        message = {"message_id": "1", "message_segments": [{"type": "image"}]}
        self.assertIs(unwrap_capability(message, "message"), message)

    def test_sdk_unwrapped_list(self) -> None:
        messages = [{"message_id": "1"}, {"message_id": "2"}]
        self.assertEqual(unwrap_capability(messages, "messages"), messages)

    def test_wrapped_success(self) -> None:
        self.assertEqual(
            unwrap_capability({"success": True, "message": {"message_id": "1"}}, "message"),
            {"message_id": "1"},
        )

    def test_wrapped_failure(self) -> None:
        self.assertIsNone(unwrap_capability({"success": False, "error": "nope"}, "message"))

    def test_wrapped_missing_key(self) -> None:
        self.assertIsNone(unwrap_capability({"success": True}, "message"))

    def test_none(self) -> None:
        self.assertIsNone(unwrap_capability(None, "message"))


if __name__ == "__main__":
    unittest.main()
