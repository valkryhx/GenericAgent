"""token_meter 单元测试。

覆盖：真实基准优先、冷启动全量回退、增量边界（最后一条 assistant 之后）、
字符/token 校准比率、图片块固定计数不爆炸、CJK 场景比率合理、异常结构兜底。
纯函数、无外部依赖。
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import token_meter as tm  # noqa: E402


def _user(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _assistant(text):
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


class RealTotalTokensTest(unittest.TestCase):
    def test_prefers_total_tokens(self):
        self.assertEqual(1000, tm.real_total_tokens({"total_tokens": 1000, "input_tokens": 1}))

    def test_derives_from_input_output_when_no_total(self):
        self.assertEqual(900, tm.real_total_tokens({"input_tokens": 500, "output_tokens": 400}))

    def test_none_when_missing_or_empty(self):
        self.assertIsNone(tm.real_total_tokens(None))
        self.assertIsNone(tm.real_total_tokens({}))
        self.assertIsNone(tm.real_total_tokens("not a dict"))
        self.assertIsNone(tm.real_total_tokens({"total_tokens": 0, "input_tokens": 0, "output_tokens": 0}))


class EstimateMsgTokensTest(unittest.TestCase):
    def test_text_scaled_by_cpt(self):
        # 40 字符 / cpt=4 = 10 token
        msg = _user("x" * 40)
        self.assertEqual(10, tm.estimate_msg_tokens(msg, 4.0))

    def test_bad_cpt_falls_back_to_default(self):
        msg = _user("x" * 40)
        # cpt=0 非法 → 用兜底 4 → 10
        self.assertEqual(10, tm.estimate_msg_tokens(msg, 0))

    def test_image_block_fixed_not_exploding(self):
        # base64 巨长的图片块：token 应约等于固定值，而不是几十万
        big = "A" * 500_000
        msg = {"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": big}},
        ]}
        toks = tm.estimate_msg_tokens(msg, 4.0)
        self.assertEqual(tm.IMAGE_BLOCK_TOKENS, toks)

    def test_image_url_block_fixed_not_exploding(self):
        big = "A" * 500_000
        msg = {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{big}"}},
        ]}
        self.assertEqual(tm.IMAGE_BLOCK_TOKENS, tm.estimate_msg_tokens(msg, 4.0))

    def test_image_plus_text(self):
        big = "A" * 500_000
        msg = {"role": "user", "content": [
            {"type": "text", "text": "y" * 40},
            {"type": "image", "source": {"type": "base64", "data": big}},
        ]}
        # 文本 40/4=10 + 图片 1500
        self.assertEqual(10 + tm.IMAGE_BLOCK_TOKENS, tm.estimate_msg_tokens(msg, 4.0))

    def test_tool_use_and_result_counted(self):
        msg = {"role": "assistant", "content": [
            {"type": "tool_use", "name": "read", "input": {"path": "abc"}},
        ]}
        self.assertGreater(tm.estimate_msg_tokens(msg, 4.0), 0)


class CalibratedCptTest(unittest.TestCase):
    def test_returns_fallback_without_real_usage(self):
        hist = [_user("hello world")]
        self.assertEqual(tm.CHARS_PER_TOKEN_FALLBACK, tm.calibrated_cpt(hist, None))

    def test_calibrates_from_real_tokens(self):
        # 400 字符文本、真实 200 token → cpt = 2.0（比默认 4 更贴近中文场景）
        hist = [_user("x" * 400)]
        cpt = tm.calibrated_cpt(hist, {"total_tokens": 200})
        self.assertAlmostEqual(2.0, cpt, places=3)

    def test_cpt_clamped_to_sane_range(self):
        # 极端：字符很少但真实 token 很大 → cpt <1 应被夹到 1.0
        hist = [_user("x" * 10)]
        cpt = tm.calibrated_cpt(hist, {"total_tokens": 100})
        self.assertEqual(1.0, cpt)
        # 极端另一侧：字符巨多真实 token 极小 → 夹到 8.0
        hist2 = [_user("x" * 100_000)]
        cpt2 = tm.calibrated_cpt(hist2, {"total_tokens": 100})
        self.assertEqual(8.0, cpt2)

    def test_image_tokens_excluded_from_calibration(self):
        # 真实 total 含图片 token；校准时应先扣掉，避免把文本 cpt 算歪
        big = "A" * 100_000
        hist = [{"role": "user", "content": [
            {"type": "text", "text": "x" * 400},
            {"type": "image", "source": {"data": big}},
        ]}]
        # 真实 total = 图片1500 + 文本200 = 1700；扣图片后 text_real=200，chars=400 → cpt=2.0
        cpt = tm.calibrated_cpt(hist, {"total_tokens": 1700})
        self.assertAlmostEqual(2.0, cpt, places=3)


class EstimateContextTokensTest(unittest.TestCase):
    def test_cold_start_full_estimate(self):
        # 无 usage：全量字符估算，cpt=4
        hist = [_user("x" * 40), _assistant("y" * 40)]
        # (40+40)/4 = 20
        self.assertEqual(20, tm.estimate_context_tokens(hist, None))

    def test_real_baseline_plus_delta(self):
        # 历史：user, assistant(最后一条模型消息), user(新增增量)
        hist = [
            _user("x" * 400),
            _assistant("y" * 400),
            _user("z" * 400),  # 最后一条 assistant 之后的增量
        ]
        # 真实 total=200（覆盖前两条）。校准 cpt = 全量1200字符/200 = 6 → 夹到 6（<=8 ok）
        # 增量 = 第三条 400字符 / 6 ≈ 66
        # 结果 ≈ 200 + 66 = 266
        got = tm.estimate_context_tokens(hist, {"total_tokens": 200})
        self.assertEqual(200 + int(400 / 6.0), got)

    def test_real_baseline_no_assistant_uses_calibrated_full(self):
        # 有 usage 但无 assistant（异常）→ 全量校准估算，不是 real+delta
        hist = [_user("x" * 400)]
        got = tm.estimate_context_tokens(hist, {"total_tokens": 200})
        # cpt = 400/200 = 2 → 全量 400/2 = 200
        self.assertEqual(200, got)

    def test_delta_after_last_assistant_only(self):
        # 只有真实基准、其后无新增 → 应约等于真实基准（delta=0）
        hist = [_user("x" * 400), _assistant("y" * 400)]
        got = tm.estimate_context_tokens(hist, {"total_tokens": 300})
        self.assertEqual(300, got)


class CJKScenarioTest(unittest.TestCase):
    def test_chinese_ratio_more_accurate_than_fixed_three(self):
        # 纯中文：1 token ≈ 1.5~1.7 字符。给真实 token 让它校准。
        chinese = "你好世界这是一段中文测试文本" * 20  # 280 字
        hist = [_user(chinese)]
        # 假设真实 175 token → cpt = 280/175 = 1.6
        cpt = tm.calibrated_cpt(hist, {"total_tokens": 175})
        self.assertAlmostEqual(1.6, cpt, places=1)
        self.assertLess(cpt, 3.0)  # 明显比旧的固定 3 更小、更贴近中文


if __name__ == "__main__":
    unittest.main()
