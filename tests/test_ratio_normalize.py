"""
视频比例规范化的回归测试。
覆盖：_normalize_ratio / _fix_ratios_in_text / _auto_fix_ratios_product。
运行：python3 -m unittest tests.test_ratio_normalize  -v
"""
import unittest
from tests._loader import load_app_server

m = load_app_server()


class TestNormalizeRatio(unittest.TestCase):
    """核心：化简 + 横竖屏方向 + 家族阈值判定"""

    def test_5_16_竖屏大家族_归到_9_16(self):
        self.assertEqual(m._normalize_ratio(5, 16), (9, 16))

    def test_3_5_竖屏小家族_归到_3_4(self):
        self.assertEqual(m._normalize_ratio(3, 5), (3, 4))

    def test_2_3_竖屏小家族_归到_3_4(self):
        self.assertEqual(m._normalize_ratio(2, 3), (3, 4))

    def test_1_1_强制归到_3_4(self):
        self.assertEqual(m._normalize_ratio(1, 1), (3, 4))

    def test_4_5_竖屏小家族_归到_3_4(self):
        self.assertEqual(m._normalize_ratio(4, 5), (3, 4))

    def test_5_3_横屏小家族_归到_4_3(self):
        self.assertEqual(m._normalize_ratio(5, 3), (4, 3))

    def test_4_11_化简后大家族_归到_9_16(self):
        self.assertEqual(m._normalize_ratio(4, 11), (9, 16))

    def test_11_4_横屏大家族_归到_16_9(self):
        self.assertEqual(m._normalize_ratio(11, 4), (16, 9))

    def test_4_10_化简成2_5_小家族(self):
        # gcd 化简成 2:5，max=5 ≤ 10 → 小家族
        self.assertEqual(m._normalize_ratio(4, 10), (3, 4))

    def test_1080_1920_化简后已是9_16(self):
        # 化简后是标准比例，仍走规范化得到 9:16
        self.assertEqual(m._normalize_ratio(1080, 1920), (9, 16))

    def test_家族阈值临界_10是小家族(self):
        # max=10 走小家族（≤ 阈值 10）
        self.assertEqual(m._normalize_ratio(4, 10), (3, 4))


class TestFixRatiosInText(unittest.TestCase):
    """上下文白名单 + 数字边界 + 已标准放行"""

    def test_已标准比例不被修改(self):
        text = "视频比例竖屏9:16一半，横屏16:9一半"
        new, fixes = m._fix_ratios_in_text(text)
        self.assertEqual(new, text)
        self.assertEqual(fixes, [])

    def test_有上下文_5_16_被改成_9_16(self):
        new, fixes = m._fix_ratios_in_text("视频比例5:16一半")
        self.assertEqual(new, "视频比例9:16一半")
        self.assertEqual(len(fixes), 1)
        self.assertEqual(fixes[0][0], "5:16")
        self.assertEqual(fixes[0][1], "9:16")

    def test_竖屏_3_5_被改成_3_4(self):
        new, _ = m._fix_ratios_in_text("竖屏3:5")
        self.assertEqual(new, "竖屏3:4")

    def test_无关键词_不修改_比分_3_5(self):
        new, fixes = m._fix_ratios_in_text("比分 3:5 平")
        self.assertEqual(new, "比分 3:5 平")
        self.assertEqual(fixes, [])

    def test_时间戳_不修改_2_30(self):
        # 2:30 比值 0.067 低于 RATIO_VALUE_MIN，被过滤
        new, fixes = m._fix_ratios_in_text("下午 2:30 开拍")
        self.assertEqual(new, "下午 2:30 开拍")
        self.assertEqual(fixes, [])

    def test_点号日期_不匹配_5_16(self):
        new, fixes = m._fix_ratios_in_text("5.16_组A_产品B")
        self.assertEqual(new, "5.16_组A_产品B")
        self.assertEqual(fixes, [])

    def test_数字边界保护_1080_1920不被切成_080_192(self):
        # 已标准（化简后 9:16）应直接放行；即便没放行也不能切碎
        new, fixes = m._fix_ratios_in_text("视频尺寸 1080:1920")
        self.assertEqual(new, "视频尺寸 1080:1920")
        self.assertEqual(fixes, [])

    def test_1_1_强制改成_3_4(self):
        new, fixes = m._fix_ratios_in_text("比例1:1")
        self.assertEqual(new, "比例3:4")
        self.assertEqual(len(fixes), 1)

    def test_化简后非标准_540_1080_改成_3_4(self):
        # 540:1080 化简为 1:2，max=2 → 小家族竖屏 = 3:4
        new, _ = m._fix_ratios_in_text("视频比例 540:1080")
        self.assertEqual(new, "视频比例 3:4")

    def test_空串_None_类型安全(self):
        self.assertEqual(m._fix_ratios_in_text("")[0], "")
        self.assertEqual(m._fix_ratios_in_text(None)[0], None)
        self.assertEqual(m._fix_ratios_in_text(123)[0], 123)


class TestFixRatiosProduct(unittest.TestCase):
    """产品级别：title + 所有单元格"""

    def test_title保留_单元格被改(self):
        prod = {
            "title": "产品（视频比例竖屏9:16一半，横屏16:9一半）",
            "rows": [
                ["5.16", "医生A", "患者A", "视频比例5:16", "道具", "/", "/", "/", "/"],
                ["5.16", "医生B", "患者B", "无关", "比分 3:5", "/", "/", "/", "/"],
            ],
        }
        fixed, details = m._auto_fix_ratios_product(prod)
        self.assertEqual(fixed, 1)
        self.assertEqual(prod["title"], "产品（视频比例竖屏9:16一半，横屏16:9一半）")
        self.assertEqual(prod["rows"][0][3], "视频比例9:16")
        self.assertEqual(prod["rows"][1][4], "比分 3:5")  # 误伤防御
        self.assertEqual(details[0]["where"], "row1 col4")
        self.assertEqual(details[0]["old"], "5:16")
        self.assertEqual(details[0]["new"], "9:16")

    def test_无可修复_返回0(self):
        prod = {"title": "纯标题", "rows": [["a", "b", "c"]]}
        fixed, details = m._auto_fix_ratios_product(prod)
        self.assertEqual(fixed, 0)
        self.assertEqual(details, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
