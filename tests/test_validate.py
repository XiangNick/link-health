# -*- coding: utf-8 -*-
"""第一层：严格校验（valid_sites / validate）。

为什么这一个文件值得单独写：
  上游的校验是 any(key in data)，空数组、只有 lives 的垃圾配置全都能过 ——
  一份"通过了校验但其实没有站点"的配置会让后面所有环节都在空转，
  而且报告上看起来一切正常。我们这边改成"四件事同时成立"，
  所以每一条不通过的分支、以及边界上的那一个（MIN_SITES-1）都必须钉住。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import _helpers as H  # noqa: E402

build = H.build


def _ok_payload(n, spider='"http://cdn.example/jar.png;md5;abc"'):
    sites = ",".join('{"key": "k%d", "name": "n%d", "api": "py_x"}' % (i, i)
                     for i in range(n))
    return '{"sites": [%s], "spider": %s}' % (sites, spider)


class ValidSitesTest(unittest.TestCase):
    def test_non_dict_gives_empty(self):
        """传进来的可能不是 dict（列表 / None / 字符串），不能让 get 抛 AttributeError。"""
        for bad in ([], None, "x", 3):
            self.assertEqual(build.valid_sites(bad), [])

    def test_sites_not_a_list_gives_empty(self):
        self.assertEqual(build.valid_sites({"sites": {"key": "a"}}), [])
        self.assertEqual(build.valid_sites({"sites": "整段字符串"}), [])

    def test_missing_sites_key_gives_empty(self):
        self.assertEqual(build.valid_sites({"lives": []}), [])

    def test_non_dict_entries_are_dropped(self):
        data = {"sites": [{"key": "a"}, "字符串", 42, None, ["x"]]}
        self.assertEqual([s["key"] for s in build.valid_sites(data)], ["a"])

    def test_entries_without_key_are_dropped(self):
        data = {"sites": [{"key": "a"}, {"name": "没有 key"}, {"key": ""}]}
        self.assertEqual([s["key"] for s in build.valid_sites(data)], ["a"])

    def test_blank_key_is_dropped(self):
        """key 是纯空白等于没有 key：留着它，配置里就会出现一个打开即失败的空条目。"""
        data = {"sites": [{"key": "   "}, {"key": "\n\t"}, {"key": " b "}]}
        self.assertEqual([s["key"] for s in build.valid_sites(data)], [" b "])

    def test_falsy_key_is_dropped(self):
        """key 为 None / 0 / "" 这类假值都算"没有 key"：
        str(0 or "") 是空串，于是被丢掉。一个 key 为 0 的条目在 App 里也没法定位。"""
        data = {"sites": [{"key": None}, {"key": 0}, {"key": 12}]}
        self.assertEqual([s["key"] for s in build.valid_sites(data)], [12])

    def test_order_is_preserved(self):
        data = {"sites": [{"key": "c"}, {"key": "a"}, {"key": "b"}]}
        self.assertEqual([s["key"] for s in build.valid_sites(data)], ["c", "a", "b"])


class ValidateTest(unittest.TestCase):
    def test_empty_object_fails(self):
        self.assertFalse(build.validate({})[0])

    def test_empty_sites_array_fails(self):
        """上游最爱放过去的一种：{"sites": []}。空数组也是"有 sites"，但一个站都没有。"""
        ok, why = build.validate({"sites": []})
        self.assertFalse(ok)
        self.assertIn("有效条目", why)

    def test_entry_without_key_fails(self):
        ok, why = build.validate({"sites": [{"nokey": 1}]})
        self.assertFalse(ok)
        self.assertIn("没有带 key", why)

    def test_only_lives_fails(self):
        """只有 lives / parses 这类字段（上游用 any(key in data) 判定时是"合格"的）。"""
        ok, _ = build.validate({"lives": [{"name": "x"}], "spider": "u"})
        self.assertFalse(ok)

    def test_exactly_min_sites_with_spider_passes(self):
        data = {"sites": H.sites(build.MIN_SITES), "spider": "http://a/jar.png;md5;x"}
        ok, why = build.validate(data)
        self.assertTrue(ok, why)
        self.assertIsNone(why)

    def test_one_short_fails(self):
        """差一个就是不通过 —— 这是 MIN_SITES 这条线唯一有意义的边界。"""
        data = {"sites": H.sites(build.MIN_SITES - 1), "spider": "http://a/jar.png"}
        ok, why = build.validate(data)
        self.assertFalse(ok)
        self.assertIn(str(build.MIN_SITES), why)

    def test_more_than_min_passes(self):
        self.assertTrue(build.validate({"sites": H.sites(40), "spider": "u"})[0])

    def test_sites_without_spider_fails(self):
        """没有 spider 的配置就是一堆跑不起来的站：类加载器根本没有落点。"""
        ok, why = build.validate({"sites": H.sites(10)})
        self.assertFalse(ok)
        self.assertIn("spider", why)

    def test_blank_spider_fails(self):
        for blank in ("", "   ", "\n", None):
            self.assertFalse(build.validate({"sites": H.sites(10), "spider": blank})[0])

    def test_non_dict_fails(self):
        for bad in ("字符串", ["sites"], None, 42, True):
            ok, why = build.validate(bad)
            self.assertFalse(ok)
            self.assertIn("不是 JSON 对象", why)

    def test_sites_not_a_list_fails(self):
        ok, why = build.validate({"sites": {"k": 1}, "spider": "u"})
        self.assertFalse(ok)
        self.assertIn("没有 sites 数组", why)

    def test_invalid_entries_do_not_count(self):
        """混着 4 个有效 + 4 个无 key 的：按有效数算，不能凑够 MIN_SITES。"""
        data = {"sites": H.sites(4) + [{"nokey": 1}] * 4, "spider": "u"}
        self.assertFalse(build.validate(data)[0])

    def test_custom_min_sites(self):
        data = {"sites": H.sites(2), "spider": "u"}
        self.assertFalse(build.validate(data, min_sites=3)[0])
        self.assertTrue(build.validate(data, min_sites=2)[0])
        self.assertTrue(build.validate(data, min_sites=1)[0])

    def test_reason_texts_are_specific(self):
        """原因文案要能分辨是哪一条不过：报告/日志全靠它，笼统的 "无效" 等于没写。"""
        cases = [
            ({"sites": []}, "有效条目"),
            ({"sites": [{"nokey": 1}]}, "没有带 key"),
            ({"sites": H.sites(2)}, "要求"),
            ({"sites": H.sites(9)}, "spider"),
        ]
        for data, kw in cases:
            _, why = build.validate(data)
            self.assertIn(kw, why)

    def test_realistic_upstream_payload_passes(self):
        """从真实源抄一段形态（含 BOM/注释后解析出来的对象）也要能过。"""
        raw = ("\ufeff// 免责声明\n" +
               _ok_payload(8).replace("py_x", "csp_Wexdiy"))
        data = build.parse_json_lenient(raw)
        self.assertTrue(build.validate(data)[0])


if __name__ == "__main__":
    unittest.main()
