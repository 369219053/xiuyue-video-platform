"""
台词/最终台词差异自动覆盖的回归测试。
覆盖：_find_col_idx / _char_diff_ratio / _auto_fix_script_rows / _should_skip_table。
运行：python3 -m unittest tests.test_script_fix  -v
"""
import unittest
from tests._loader import load_app_server

m = load_app_server()


class TestShouldSkipTable(unittest.TestCase):
    def test_命中_ai修改_关键词(self):
        self.assertTrue(m._should_skip_table("ai修改的视频汇总"))
        self.assertTrue(m._should_skip_table("AI修改的视频汇总"))  # 大小写不敏感

    def test_其他副表正常通过(self):
        self.assertFalse(m._should_skip_table("5.16_刘原原组_产品A"))
        self.assertFalse(m._should_skip_table("某视频汇总"))

    def test_空串安全返回False(self):
        self.assertFalse(m._should_skip_table(""))
        self.assertFalse(m._should_skip_table(None))


class TestFindColIdx(unittest.TestCase):
    HEADERS = ["日期", "人物a", "人物b", "场景", "道具",
               "台词", "最终台词", "人物动作", "镜头参考图"]

    def test_台词列定位到第5列(self):
        self.assertEqual(
            m._find_col_idx(self.HEADERS, m.SCRIPT_COL_KEYWORDS), 5
        )

    def test_最终台词列定位到第6列(self):
        self.assertEqual(
            m._find_col_idx(self.HEADERS, m.SUBTITLE_COL_KEYWORDS), 6
        )

    def test_找不到返回负1(self):
        self.assertEqual(
            m._find_col_idx(["日期", "人物a"], m.SCRIPT_COL_KEYWORDS), -1
        )


class TestCharDiffRatio(unittest.TestCase):
    def test_完全相同_差异0(self):
        self.assertEqual(m._char_diff_ratio("同样的句子", "同样的句子"), 0.0)

    def test_两边都空_差异0(self):
        self.assertEqual(m._char_diff_ratio("", ""), 0.0)
        self.assertEqual(m._char_diff_ratio("  ", "  "), 0.0)

    def test_一边空一边非空_差异满格(self):
        self.assertEqual(m._char_diff_ratio("", "非空"), 1.0)
        self.assertEqual(m._char_diff_ratio("非空", ""), 1.0)

    def test_小幅修改_差异小于阈值(self):
        # 单字符差异，相似度高 → 差异低
        d = m._char_diff_ratio("不管多严重的肌留 记住这两样",
                               "不管多严重的肌瘤 记住这两样")
        self.assertLess(d, 0.2)

    def test_完全不同_差异大于阈值(self):
        d = m._char_diff_ratio("短句", "完全不一样的另一句话很长很长的内容")
        self.assertGreater(d, m.SUBTITLE_DIFF_THRESHOLD)


class TestAutoFixScriptRows(unittest.TestCase):
    HEADERS = ["日期", "人物a", "人物b", "场景", "道具",
               "台词", "最终台词", "人物动作", "镜头参考图"]

    def _row(self, script, subtitle):
        return ["5.16", "医生", "患者", "办公室", "道具",
                script, subtitle, "动作", "/"]

    def test_差异大的行被覆盖(self):
        rows = [
            self._row("原始台词AAA", "原始台词AAA"),                    # 一致
            self._row("原始台词BBB", "字幕完全不一样的内容CCC"),         # 差异大
            self._row("原始台词CCC", "原始台词CCD"),                    # 差异小
            self._row("",         "正确字幕DDD"),                       # 旧值空
        ]
        fixed, details = m._auto_fix_script_rows(self.HEADERS, rows)
        self.assertGreaterEqual(fixed, 2)
        # 行 2 被覆盖
        self.assertEqual(rows[1][5], "字幕完全不一样的内容CCC")
        # 行 4 被覆盖（旧值空 → 差异满格）
        self.assertEqual(rows[3][5], "正确字幕DDD")
        # 行 1/3 保持
        self.assertEqual(rows[0][5], "原始台词AAA")
        self.assertEqual(rows[2][5], "原始台词CCC")
        # 详情结构正确
        self.assertTrue(all("row" in d and "diff" in d for d in details))

    def test_最终台词空_不覆盖(self):
        rows = [self._row("旧台词", "")]
        fixed, _ = m._auto_fix_script_rows(self.HEADERS, rows)
        self.assertEqual(fixed, 0)
        self.assertEqual(rows[0][5], "旧台词")

    def test_找不到列_返回0(self):
        fixed, details = m._auto_fix_script_rows(["日期", "人物"], [["x", "y"]])
        self.assertEqual(fixed, 0)
        self.assertEqual(details, [])


class TestParseTableName3(unittest.TestCase):
    """副表名三段解析：日期 + 组名 + 产品名"""

    def test_标准三段_5_15_刘原原组_娇茵舒凝胶(self):
        self.assertEqual(
            m.parse_table_name3("5.15_刘原原组_娇茵舒凝胶"),
            ("5.15", "刘原原组", "娇茵舒凝胶"),
        )

    def test_产品名含下划线(self):
        # 后续 _ 都归到 product
        self.assertEqual(
            m.parse_table_name3("5.16_冯会宁组_teenlab_维生素d"),
            ("5.16", "冯会宁组", "teenlab_维生素d"),
        )

    def test_无日期前缀_两段(self):
        self.assertEqual(
            m.parse_table_name3("刘原原组_娇茵舒凝胶"),
            ("", "刘原原组", "娇茵舒凝胶"),
        )

    def test_单段_退化为原名(self):
        self.assertEqual(m.parse_table_name3("某副表"), ("", "某副表", "某副表"))

    def test_向后兼容_parse_table_name二段返回不变(self):
        self.assertEqual(
            m.parse_table_name("5.15_刘原原组_娇茵舒凝胶"),
            ("刘原原组", "娇茵舒凝胶"),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
