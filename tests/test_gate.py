"""最小测试集：只用标准库 unittest。

跑法：python3 -m unittest discover -s tests
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import proactive_gate.__main__ as gate  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")


class QuietHours(unittest.TestCase):
    def test_default_night_is_quiet(self):
        gate.CFG["quiet_start"], gate.CFG["quiet_end"] = 23, 9
        for hour in (23, 0, 3, 8):
            now = datetime(2026, 11, 2, hour, 0, tzinfo=TZ)
            expect = now.hour >= 23 or now.hour < 9
            self.assertTrue(expect, f"{hour} 点应在静默内")

    def test_daytime_is_not_quiet(self):
        for hour in (9, 12, 15, 22):
            self.assertFalse(hour >= 23 or hour < 9)


class Dedupe(unittest.TestCase):
    def setUp(self):
        gate.CFG["threshold"] = 0.55

    def test_same_hook_blocked(self):
        hook = "他之前说过的：那套方案还悬着"
        history = [{"time": datetime(2026, 11, 1, 12, tzinfo=TZ).isoformat(),
                    "hook_id": gate.hook_fingerprint(hook), "mode": "old_topic"}]
        cand = gate.Candidate(hook, "share", "x", "facts", "body", None)
        self.assertIsNotNone(gate.duplicate_reason(cand, history))

    def test_different_hook_allowed(self):
        history = [{"time": datetime(2026, 11, 1, 12, tzinfo=TZ).isoformat(),
                    "hook_id": gate.hook_fingerprint("完全不相干的一件旧事"),
                    "mode": "old_topic"}]
        cand = gate.Candidate("另一件新的事", "share", "x", "facts", "body", None)
        self.assertIsNone(gate.duplicate_reason(cand, history))

    def test_fingerprint_is_stable_and_not_reversible(self):
        a = gate.hook_fingerprint("同一句话")
        b = gate.hook_fingerprint("同一句话")
        self.assertEqual(a, b)
        self.assertNotIn("同一句话", a)
        self.assertNotEqual(a, gate.hook_fingerprint("另一句话"))


class Scoring(unittest.TestCase):
    def test_specific_beats_vague(self):
        now = datetime(2026, 11, 2, 15, tzinfo=TZ)
        recent = [gate.tokenize("上周那个方案你觉得怎么样")]
        vague = gate.Candidate("随便说点什么", "share", "x", "facts", "随便说点什么", None)
        sharp = gate.Candidate(
            "他说周三下午三点前要把那两章改完", "share", "x", "facts",
            "他说周三下午三点前要把那两章改完", None,
        )
        a = gate.score_candidate(vague, recent, [], now, 0.6)["score"]
        b = gate.score_candidate(sharp, recent, [], now, 0.6)["score"]
        self.assertGreater(b, a)

    def test_rephrase_candidate_discounts_relevance(self):
        now = datetime(2026, 11, 2, 15, tzinfo=TZ)
        text = "这个方案我们下周再定吧"
        recent = [gate.tokenize(text)]
        c = gate.Candidate(text, "old_topic", "x", "conversations", text, now,
                           needs_rephrase=True)
        plain = gate.Candidate(text, "old_topic", "x", "conversations", text, now)
        discounted = gate.score_candidate(c, recent, [], now, 0.6)["relevance"]
        full = gate.score_candidate(plain, recent, [], now, 0.6)["relevance"]
        self.assertLess(discounted, full)


class Budget(unittest.TestCase):
    def test_daily_cap_blocks(self):
        gate.CFG["max_per_day"] = 1
        now = datetime(2026, 11, 2, 15, tzinfo=TZ)
        history = [{"time": now.isoformat(), "hook": "x", "mode": "share"}]
        today = sum(1 for h in history
                    if gate.timestamp(h["time"]).date() == now.date())
        self.assertGreaterEqual(today, gate.CFG["max_per_day"])

    def test_seven_day_cap(self):
        gate.CFG["max_per_7d"] = 5
        now = datetime(2026, 11, 2, 15, tzinfo=TZ)
        history = [{"time": (now - timedelta(days=d)).isoformat(),
                    "hook": f"h{d}", "mode": "share"} for d in range(5)]
        recent = sum(1 for h in history
                     if gate.timestamp(h["time"]) > now - timedelta(days=7))
        self.assertGreaterEqual(recent, gate.CFG["max_per_7d"])


class ConfigDefaults(unittest.TestCase):
    """2026-11-03：默认值从"宽松"改成"保守但能开得了口"。

    注意：前面 Budget 的用例会就地改 gate.CFG，所以这里每条都要先重置回默认。
    """

    def setUp(self):
        gate.CFG.update({
            "max_per_day": 1,
            "max_per_7d": 3,
            "cooldown_after_user_min": 13,
            "cooldown_after_unanswered_min": 72 * 60,
            "max_consecutive_unanswered": 2,
        })

    def test_default_caps(self):
        self.assertEqual(gate.CFG["max_per_day"], 1)
        self.assertEqual(gate.CFG["max_per_7d"], 3)

    def test_reply_gap_is_minutes_not_ninety(self):
        # 旧版 90 分钟：白天一直在聊天就整天让位，一次都发不出去。
        self.assertEqual(gate.CFG["cooldown_after_user_min"], 13)
        self.assertLess(gate.CFG["cooldown_after_user_min"], 30)

    def test_unanswered_cooldown_is_72h(self):
        self.assertEqual(gate.CFG["cooldown_after_unanswered_min"], 72 * 60)

    def test_consecutive_unanswered_limit_exists(self):
        self.assertEqual(gate.CFG.get("max_consecutive_unanswered"), 2)


if __name__ == "__main__":
    unittest.main()


class SendHistorySchema(unittest.TestCase):
    """发送历史的写入格式和读取格式必须一致。

    这里盯的是一个真实 bug：写入端存 `hook_id`，读取端却找 `hook`，
    于是第一次成功发送之后，下一轮把所有历史判成无效 → 强制 SILENT →
    在没有任何报错的情况下永久不再说话。
    """

    def setUp(self):
        self.now = datetime(2026, 11, 3, 15, 0, tzinfo=gate.TZ)

    def _state_with_send(self, **overrides):
        send = {
            "time": "2026-11-03T14:00:00+08:00",
            "hook_id": "a" * 12,
            "mode": "share",
            "has_question": False,
        }
        send.update(overrides)
        return {"sends": [send]}

    def test_written_records_are_read_back(self):
        errors: list[str] = []
        history = gate.send_history(self._state_with_send(), [], self.now, errors)
        self.assertEqual(errors, [])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["hook_id"], "a" * 12)

    def test_recorded_fields_survive_normalization(self):
        # hook_id 和 has_question 都得留住，否则去重和问号统计一起失效。
        errors: list[str] = []
        history = gate.send_history(
            self._state_with_send(has_question=True), [], self.now, errors
        )
        self.assertTrue(history[0]["has_question"])

    def test_legacy_hook_field_is_migrated(self):
        # 更早的版本存的是 hook 原文，读的时候要能转成指纹，且不把原文留下。
        state = {"sends": [{
            "time": "2026-11-03T14:00:00+08:00",
            "hook": "这是一段很长的原文，不该被写回 state",
            "mode": "share",
        }]}
        errors: list[str] = []
        history = gate.send_history(state, [], self.now, errors)
        self.assertEqual(errors, [])
        self.assertNotIn("hook", history[0])
        self.assertRegex(history[0]["hook_id"], r"^[0-9a-f]{12}$")

    def test_missing_has_question_tightens_rather_than_loosens(self):
        state = {"sends": [{
            "time": "2026-11-03T14:00:00+08:00",
            "hook_id": "b" * 12, "mode": "share",
        }]}
        history = gate.send_history(state, [], self.now, [])
        self.assertTrue(history[0]["has_question"])

    def test_duplicate_detection_actually_sees_the_history(self):
        # 归一化丢了 hook_id 的话，这条会返回 None（去重完全失效）。
        hook = "同一段由头，发过一次就不该再发"
        fp = gate.hook_fingerprint(hook)
        state = self._state_with_send(hook_id=fp)
        errors: list[str] = []
        history = gate.send_history(state, [], self.now, errors)
        self.assertEqual(errors, [])

        candidate = gate.Candidate(
            hook=hook, mode="share", source="diary/2026-11-03.md",
            kind="diary", content="正文正文正文正文正文正文",
            occurred_at=self.now,
        )
        self.assertEqual(gate.duplicate_reason(candidate, history), "hook 前 40 字重复")

    def test_different_hook_is_not_flagged_as_duplicate(self):
        errors: list[str] = []
        history = gate.send_history(self._state_with_send(), [], self.now, errors)
        # mode 用跟历史不同的那个，否则会先被"mode 不得连续两次相同"拦下，
        # 测不到 hook 去重本身。
        candidate = gate.Candidate(
            hook="另一段完全不同的由头，没发过", mode="reply",
            source="diary/2026-11-03.md", kind="diary",
            content="正文正文正文正文正文正文", occurred_at=self.now,
        )
        self.assertIsNone(gate.duplicate_reason(candidate, history))
