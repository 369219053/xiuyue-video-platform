"""
_random_combine 单元测试：
  1) 视觉组（人物a/b/场景/道具/动作/镜头）整组绑定不串行
  2) 台词组（台词/字幕）整组绑定不串行
  3) 颜色洗牌：人物列里的服装颜色被替换为颜色池里的色
  4) 黑名单保护：'白大褂' 不会被换色
  5) 颜色池为空安全降级（不报错）
  6) 颜色池只剩 1 种时安全降级
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.excel_parser import (  # noqa: E402
    _random_combine,
    _extract_color_pool,
    _swap_colors_in_text,
    _classify_columns,
)


HEADER = "人物a // 人物b // 场景 // 道具 // 动作 // 镜头参考图 // 台词 // 最终台词"


def _row(*vals):
    return " // ".join(vals)


class TestClassifyColumns(unittest.TestCase):
    def test_视觉组与台词组分类(self):
        headers = HEADER.split(" // ")
        text_idx, visual_idx, char_idx = _classify_columns(headers)
        self.assertEqual(text_idx, [6, 7])
        self.assertEqual(visual_idx, [0, 1, 2, 3, 4, 5])
        self.assertEqual(char_idx, [0, 1])


class TestColorPool(unittest.TestCase):
    def test_提取池子_去重保序(self):
        texts = ["中年男,黑色衬衫,戴眼镜", "女,姜黄色上衣", "男,黑色T恤", "男,深红色外套"]
        pool = _extract_color_pool(texts)
        self.assertEqual(pool, ["黑", "姜黄", "深红"])

    def test_白纸黑笔不会被提取(self):
        # 这些后面不跟服装词，所以颜色不应被收入池
        pool = _extract_color_pool(["桌上有白纸", "手里拿黑笔"])
        self.assertEqual(pool, [])

    def test_白大褂不进入颜色池(self):
        # 白大褂 是固定搭配，'大褂' 不在服装白名单 → 白色不会被作为可洗颜色来源
        pool = _extract_color_pool(["医生穿白大褂"])
        self.assertEqual(pool, [])


class TestSwapColors(unittest.TestCase):
    def test_衬衫颜色被替换(self):
        rng = __import__("random").Random(42)
        out = _swap_colors_in_text("中年男,黑色衬衫", ["黑", "蓝", "灰"], rng)
        self.assertNotEqual(out, "中年男,黑色衬衫")
        self.assertTrue(out.startswith("中年男,"))
        self.assertTrue(out.endswith("色衬衫"))

    def test_白大褂被保护不换色(self):
        rng = __import__("random").Random(0)
        out = _swap_colors_in_text("医生,白大褂", ["白", "蓝", "红"], rng)
        self.assertEqual(out, "医生,白大褂")

    def test_中医服可被换色(self):
        # 中医服 不在黑名单（只有白大褂），允许换
        rng = __import__("random").Random(0)
        out = _swap_colors_in_text("老中医,深红色中医服", ["深红", "藏青", "墨绿"], rng)
        self.assertNotEqual(out, "老中医,深红色中医服")
        self.assertIn("中医服", out)

    def test_颜色池为空_原样返回(self):
        rng = __import__("random").Random(0)
        out = _swap_colors_in_text("中年男,黑色衬衫", [], rng)
        self.assertEqual(out, "中年男,黑色衬衫")

    def test_颜色池只剩1种_原样返回(self):
        rng = __import__("random").Random(0)
        out = _swap_colors_in_text("中年男,黑色衬衫", ["黑"], rng)
        self.assertEqual(out, "中年男,黑色衬衫")


class TestRandomCombineBinding(unittest.TestCase):
    def setUp(self):
        # 4 行模版：每行视觉组与台词组都有明显标识
        self.templates = [
            _row("男A1黑色衬衫", "女B1蓝色上衣", "客厅S1", "道具D1", "动作M1", "镜头C1", "台词T1", "最终F1"),
            _row("男A2灰色T恤", "女B2姜黄色外套", "卧室S2", "道具D2", "动作M2", "镜头C2", "台词T2", "最终F2"),
            _row("男A3深蓝色衬衫", "女B3深红色上衣", "餐厅S3", "道具D3", "动作M3", "镜头C3", "台词T3", "最终F3"),
            _row("男A4绿色衬衫", "女B4白色T恤", "厨房S4", "道具D4", "动作M4", "镜头C4", "台词T4", "最终F4"),
        ]

    def _row_tag(self, text: str) -> str:
        """提取列里的行号数字（每列只有一个数字）。"""
        import re
        m = re.search(r"\d", text)
        return m.group(0) if m else ""

    def test_视觉组同一行抽取_不串行(self):
        rows = _random_combine(self.templates, HEADER, count=50, seed=1)
        for r in rows:
            cols = r.split(" // ")
            # 视觉组：人物a/人物b/场景/道具/动作/镜头 6 列的行号应同号
            tags = [self._row_tag(cols[i]) for i in (0, 1, 2, 3, 4, 5)]
            self.assertEqual(len(set(tags)), 1, f"视觉组串行：{cols}")

    def test_台词组同一行抽取_不串行(self):
        rows = _random_combine(self.templates, HEADER, count=50, seed=2)
        for r in rows:
            cols = r.split(" // ")
            self.assertEqual(self._row_tag(cols[6]), self._row_tag(cols[7]), f"台词组串行：{cols}")

    def test_视觉组与台词组互相独立(self):
        rows = _random_combine(self.templates, HEADER, count=50, seed=3)
        mixed = sum(
            1 for r in rows
            if self._row_tag(r.split(" // ")[2]) != self._row_tag(r.split(" // ")[6])
        )
        self.assertGreater(mixed, 0, "视觉/台词应能来自不同源行")

    def test_颜色洗牌产生多样性(self):
        # 同一组视觉行被多次抽中时，人物a 的颜色应有变化
        rows = _random_combine(self.templates, HEADER, count=100, seed=4)
        colors_seen = set()
        for r in rows:
            colors_seen.add(r.split(" // ")[0])
        # 4 行模版 × 颜色洗牌，期望大于 4 种"人物a"文本
        self.assertGreater(len(colors_seen), 4, f"颜色洗牌未产生多样性，只见 {len(colors_seen)} 种")

    def test_空模版_返回空列表(self):
        self.assertEqual(_random_combine([], HEADER, 10), [])

    def test_count_为0_返回空列表(self):
        self.assertEqual(_random_combine(self.templates, HEADER, 0), [])


class TestDurationColumn(unittest.TestCase):
    """时长列：归视觉组绑定 + 空/非数字 → '10' 兜底"""
    HEADER_D = "人物a // 场景 // 台词 // 时长"

    def test_时长归视觉组(self):
        headers = self.HEADER_D.split(" // ")
        text_idx, visual_idx, char_idx = _classify_columns(headers)
        self.assertEqual(text_idx, [2])
        self.assertIn(3, visual_idx)                  # 时长 → 视觉组
        self.assertIn(0, visual_idx)                  # 人物a → 视觉组
        self.assertIn(1, visual_idx)                  # 场景  → 视觉组

    def test_时长留空_默认补10(self):
        tpl = [
            "人物a-v1 // 场景v1 // 台词t1 // /",       # 时长空（占位 /）
            "人物a-v2 // 场景v2 // 台词t2 // ",         # 时长空字符串
        ]
        rows = _random_combine(tpl, self.HEADER_D, count=10, seed=7)
        for r in rows:
            cols = r.split(" // ")
            self.assertEqual(cols[3], "10", f"空时长未兜底：{cols}")

    def test_时长非数字_兜底为10(self):
        tpl = ["人物a-v1 // 场景v1 // 台词t1 // 五秒"]
        rows = _random_combine(tpl, self.HEADER_D, count=5, seed=8)
        for r in rows:
            self.assertEqual(r.split(" // ")[3], "10")

    def test_时长合法数字_原样保留(self):
        tpl = [
            "人物a-v1 // 场景v1 // 台词t1 // 8",
            "人物a-v2 // 场景v2 // 台词t2 // 12.5",
        ]
        rows = _random_combine(tpl, self.HEADER_D, count=20, seed=9)
        for r in rows:
            d = r.split(" // ")[3]
            self.assertIn(d, ("8", "12.5"))

    def test_无时长列_老格式仍能跑(self):
        # 兼容性：旧 Excel 没有时长列，不应报错
        old_header = "人物a // 场景 // 台词"
        tpl = ["A // 房间 // 你好", "B // 户外 // 早上"]
        rows = _random_combine(tpl, old_header, count=5, seed=10)
        self.assertEqual(len(rows), 5)
        for r in rows:
            self.assertEqual(len(r.split(" // ")), 3)


if __name__ == "__main__":
    unittest.main()
