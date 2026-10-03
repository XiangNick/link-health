# -*- coding: utf-8 -*-
"""第一层：merge() —— 合并、去重、列表字段归并。

这个函数是"上游 N 份配置 -> 我们 1 份配置"的唯一通道，三条语义都和上游【故意不同】：
  · 同名 key 只保留先出现的那份（上游是改名后全部塞进来，用户看到一堆重复源）；
  · ads 这类列表字段必须一起合并（上游 skip 掉就不再合并，等于只留 base 那家）；
  · fetched 是 4 元组 —— 这一条是回归测试：草稿版按 3 元组解包，
    只要有任意一个源抓成功，整个构建就会崩在合并这一步。
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import _helpers as H  # noqa: E402

build = H.build


def fetch(sid, data, name=None, cached=False):
    """造一个 fetched 元素。刻意保留 4 元组形态（sid, name, data, from_cache）。"""
    return (sid, name or sid, data, cached)


def payload(sites, spider="http://a/jar.png", **extra):
    d = {"spider": spider, "sites": sites}
    d.update(extra)
    return d


class MergeTupleContractTest(unittest.TestCase):
    def test_accepts_four_tuple_entries(self):
        """回归：fetched 的元素是 (sid, name, data, from_cache) 四元组。

        草稿版 merge 里写的是 `for sid, src_name, data in fetched`，
        只要有任意一个源抓成功就会 ValueError: too many values to unpack ——
        整个构建死在这一步，config.json / status.json / report.html 一个都产不出来。
        这条用例就是钉住这个契约，防止有人改回去。"""
        cohort = {}
        out = build.merge([fetch("a", payload(H.sites(3)))], None, {}, cohort)
        self.assertEqual(len(out["sites"]), 3)
        self.assertEqual(cohort["kept"], 3)

    def test_three_tuple_entries_raise(self):
        """反过来钉：3 元组会被明确地拒绝（宁可在测试里炸，也不要在 Action 里炸）。"""
        with self.assertRaises(ValueError):
            build.merge([("a", "A", payload(H.sites(3)))], None, {}, {})

    def test_cached_flag_does_not_affect_site_merge(self):
        """缓存源和实时源在合并阶段一视同仁（谁能提供 spider 才是 main 的事）。"""
        cohort = {}
        out = build.merge([fetch("a", payload([{"key": "k1", "api": "py_x"}]), cached=True)],
                          None, {}, cohort)
        self.assertEqual([s["key"] for s in out["sites"]], ["k1"])


class MergeSitesTest(unittest.TestCase):
    def test_empty_input_gives_none(self):
        """一个源都没抓到时返回 None：调用方据此判"没有任何源可用"，不能产出半份配置。"""
        self.assertIsNone(build.merge([], None, {}, {}))

    def test_same_key_keeps_the_first_occurrence(self):
        """同名 key 只留先出现的那个（不是改名堆进来）：
        上游会把重复源改个名字全塞进配置，用户在搜索页看到同一个源四遍。"""
        cohort = {}
        out = build.merge([
            fetch("feimao", payload([{"key": "push_agent", "name": "推送(肥猫)",
                                      "api": "py_a"}]), "肥猫"),
            fetch("wangerxiao", payload([{"key": "push_agent", "name": "推送(王二小)",
                                          "api": "py_b"}]), "王二小"),
        ], None, {}, cohort)
        self.assertEqual(len(out["sites"]), 1)
        self.assertEqual(out["sites"][0]["name"], "推送(肥猫)")

    def test_duplicate_record_carries_kept_and_dropped_from(self):
        cohort = {}
        build.merge([
            fetch("feimao", payload([{"key": "k", "api": "py_a"}]), "肥猫"),
            fetch("4k", payload([{"key": "k", "api": "py_b"}]), "4K小盒子"),
        ], None, {}, cohort)
        dups = [d for d in cohort["dropped"] if d.reason == "duplicate"]
        self.assertEqual(len(dups), 1)
        self.assertEqual(dups[0].kept_from, "肥猫")
        self.assertEqual(dups[0].dropped_from, "4K小盒子")

    def test_filtered_key_does_not_occupy_the_slot(self):
        """被规则剔掉的 key 不能占住名额：否则后面同 key 的站也一并留不下来，
        而报告里既说不清是"重复"还是"命中规则"。

        用【名字规则】构造这种局面：甲源那份因为名字失效被砍，乙源那份同名同 key
        但名字干净 —— 正确的行为是留乙源那一份（seen 只记"真的留下了"的 key）。"""
        cohort = {}
        c = dict(build.FILTER_DEFAULTS, blacklist_name_patterns=["失效"])
        out = build.merge([
            fetch("a", payload([{"key": "k", "name": "接口失效", "api": "py_a"}])),
            fetch("b", payload([{"key": "k", "name": "好站", "api": "py_b"}])),
        ], None, c, cohort)
        self.assertEqual([s["name"] for s in out["sites"]], ["好站"])
        self.assertEqual([d.reason for d in cohort["dropped"]], ["blacklist_name_patterns"])

    def test_sites_without_key_are_skipped(self):
        cohort = {}
        out = build.merge([fetch("a", payload([{"key": ""}, {"name": "无 key"},
                                               {"key": "  "}, {"key": "ok"}]))],
                          None, {}, cohort)
        self.assertEqual([s["key"] for s in out["sites"]], ["ok"])

    def test_non_dict_site_entries_are_skipped(self):
        cohort = {}
        out = build.merge([fetch("a", payload(["字符串", 42, None, {"key": "ok"}]))],
                          None, {}, cohort)
        self.assertEqual([s["key"] for s in out["sites"]], ["ok"])

    def test_each_site_gets_exactly_one_reason(self):
        """每个站只能命中一个剔除原因：三组的数量相加必须正好等于被砍掉的站数，
        报告上"167 -> 87、砍掉 80"这几个数字才是自洽的。"""
        cohort = {}
        c = dict(build.FILTER_DEFAULTS, blacklist_keys=["k1"])
        build.merge([
            fetch("a", payload([{"key": "k1", "api": "py_a"},
                                {"key": "k2", "api": "csp_没有这个类"},
                                {"key": "k3", "api": "py_c"}])),
            fetch("b", payload([{"key": "k3", "api": "py_d"}])),
        ], {"AppRJ"}, c, cohort)
        reasons = sorted(d.reason for d in cohort["dropped"])
        self.assertEqual(reasons, ["blacklist_keys", "duplicate", "missing_class"])

    def test_base_scalar_fields_are_copied(self):
        """base（优先级最高的那份）里的普通字段要跟着走：
        sites/lives 之外的东西（比如 sites 之外的元数据）不能丢。"""
        cohort = {}
        base = payload([{"key": "k", "api": "py_a"}], spider="http://a/jar.png",
                       wallPaper="http://a/w.png", 自定义="值")
        out = build.merge([fetch("a", base)], None, {}, cohort)
        self.assertEqual(out["wallPaper"], "http://a/w.png")
        self.assertEqual(out["自定义"], "值")
        self.assertEqual(out["spider"], "http://a/jar.png")

    def test_spider_is_copied_from_base_verbatim(self):
        """merge 只负责把 base 的 spider 带出来；"spider 必须来自被指定的那个源"
        是 main 的职责（当 spider_id 回退时 main 会改写它），所以这里断言的是原样带出。"""
        cohort = {}
        out = build.merge([
            fetch("a", payload([{"key": "k", "api": "py_a"}], spider="http://base/jar.png;md5;1")),
            fetch("b", payload([{"key": "j", "api": "py_b"}], spider="http://other/jar.png")),
        ], None, {}, cohort)
        self.assertEqual(out["spider"], "http://base/jar.png;md5;1")

    def test_empty_sites_source_still_yields_config(self):
        cohort = {}
        out = build.merge([fetch("a", payload([]))], None, {}, cohort)
        self.assertEqual(out["sites"], [])
        self.assertEqual(cohort["kept"], 0)


class MergeListFieldsTest(unittest.TestCase):
    def test_ads_is_merged_too(self):
        """【和上游的关键差异】ads 必须参与列表合并。
        上游从 base 里 skip 掉广告却不再合并，等于只为用户保留 base 那一家的广告源。"""
        cohort = {}
        out = build.merge([
            fetch("a", payload([], ads=[{"name": "base广告", "url": "http://a/1"}]),
                  "A"),
            fetch("b", payload([], ads=[{"name": "b广告", "url": "http://b/1"},
                                        {"name": "base广告", "url": "http://a/1"}]), "B"),
        ], None, {}, cohort)
        names = [x["name"] for x in out["ads"]]
        self.assertEqual(names, ["base广告", "b广告"])

    def test_all_merge_fields_are_merged(self):
        cohort = {}
        a = payload([], lives=[{"name": "L1", "url": "u1"}], parses=[{"name": "P1"}],
                    doh=[{"name": "D1", "url": "d1"}], rules=[{"name": "R1", "host": "h"}],
                    flags=["f1"], exts=["e1"], ads=[{"name": "AD1"}])
        b = payload([], lives=[{"name": "L2", "url": "u2"}], parses=[{"name": "P2"}],
                    doh=[{"name": "D2", "url": "d2"}], rules=[{"name": "R2", "host": "h2"}],
                    flags=["f2"], exts=["e2"], ads=[{"name": "AD2"}])
        out = build.merge([fetch("a", a), fetch("b", b)], None, {}, cohort)
        for field in build.MERGE_FIELDS:
            self.assertEqual(len(out[field]), 2, field)

    def test_lives_is_dropped_entirely(self):
        """lives（直播）是【故意不要】的：这个配置只给看影视剧用。

        base 里带的要抹掉、各源里的也不合并 —— 两条路都得堵死，
        否则换个源的顺序就会漏出来。"""
        cohort = {}
        base = payload([{"key": "k", "api": "py_a"}],
                       lives=[{"name": "base直播", "url": "u0"}],
                       parses=[{"name": "P1"}], ads=[{"name": "AD1"}])
        b = payload([{"key": "k2", "api": "py_b"}], lives=[{"name": "b直播"}])
        out = build.merge([fetch("a", base), fetch("b", b)], None, {}, cohort)
        self.assertNotIn("lives", out)
        self.assertNotIn("L1", json.dumps(out, ensure_ascii=False))
        # 别的列表字段不受影响
        self.assertEqual(len(out["parses"]), 1)
        self.assertEqual(len(out["ads"]), 1)
        self.assertIn("lives", build.DROP_FIELDS)

    def test_lives_not_in_merge_fields(self):
        """防回归：谁把 lives 加回 MERGE_FIELDS，这个用例立刻红。"""
        self.assertNotIn("lives", build.MERGE_FIELDS)
        self.assertIn("lives", build.SKIP_FROM_BASE)
        self.assertIn("lives", build.DROP_FIELDS)

    def test_dedupe_by_name(self):
        cohort = {}
        a = payload([], ads=[{"name": "同名", "url": "http://a"}])
        b = payload([], ads=[{"name": "同名", "url": "http://b"}])
        out = build.merge([fetch("a", a), fetch("b", b)], None, {}, cohort)
        self.assertEqual([x["url"] for x in out["ads"]], ["http://a"])

    def test_dedupe_by_url_when_no_name(self):
        cohort = {}
        a = payload([], ads=[{"url": "http://same"}])
        b = payload([], ads=[{"url": "http://same"}, {"url": "http://other"}])
        out = build.merge([fetch("a", a), fetch("b", b)], None, {}, cohort)
        self.assertEqual([x["url"] for x in out["ads"]], ["http://same", "http://other"])

    def test_dedupe_by_whole_json_when_anonymous(self):
        """既没有 name 也没有 url 的字典：退化成整段 JSON 比较，
        保证同一份配置里的同一个对象不会在结果里出现两次。"""
        cohort = {}
        a = payload([], rules=[{"host": "h", "rule": ["a"]}])
        b = payload([], rules=[{"host": "h", "rule": ["a"]}])
        out = build.merge([fetch("a", a), fetch("b", b)], None, {}, cohort)
        self.assertEqual(len(out["rules"]), 1)

    def test_non_dict_items_dedupe_by_string(self):
        cohort = {}
        a = payload([], flags=["x", "y"])
        b = payload([], flags=["y", "z"])
        out = build.merge([fetch("a", a), fetch("b", b)], None, {}, cohort)
        self.assertEqual(out["flags"], ["x", "y", "z"])

    def test_empty_field_is_not_written(self):
        """空字段不写进结果：配置干净一点，App 那边也不会因为空数组走奇怪的兜底分支。"""
        cohort = {}
        out = build.merge([fetch("a", payload([{"key": "k", "api": "py_a"}]))],
                          None, {}, cohort)
        self.assertNotIn("ads", out)
        self.assertNotIn("lives", out)

    def test_order_follows_source_priority(self):
        """合并顺序按源优先级：base 的条目在前，报告/配置里的顺序才稳定可复现。

        （原来这里用的是 lives，但 lives 现在被明确剔掉了，改用 ads 验同一件事。）"""
        cohort = {}
        a = payload([], ads=[{"name": "A1"}])
        b = payload([], ads=[{"name": "B1"}])
        out = build.merge([fetch("a", a), fetch("b", b)], None, {}, cohort)
        self.assertEqual([x["name"] for x in out["ads"]], ["A1", "B1"])


class MergeCohortTest(unittest.TestCase):
    def test_cohort_counts(self):
        cohort = {}
        c = dict(build.FILTER_DEFAULTS, blacklist_keys=["k2"])
        build.merge([
            fetch("a", payload([{"key": "k1", "api": "py_a"},
                                {"key": "k2", "api": "py_b"}])),
            fetch("b", payload([{"key": "k1", "api": "py_c"}])),
        ], None, c, cohort)
        self.assertEqual(cohort["kept"], 1)
        self.assertEqual(len(cohort["dropped"]), 2)

    def test_total_input_does_not_double_count_duplicates(self):
        """total_input 是"上游给了我们多少个站"，重复项已经被算过一次输入了，
        不能再把它加一遍（否则报告上"砍掉 = 输入 - 保留"这个等式就不成立）。"""
        cohort = {}
        build.merge([
            fetch("a", payload([{"key": "k", "api": "py_a"}])),
            fetch("b", payload([{"key": "k", "api": "py_b"}])),
        ], None, {}, cohort)
        self.assertEqual(cohort["total_input"], 1)
        self.assertEqual(cohort["kept"], 1)

    def test_total_input_counts_filtered_sites(self):
        cohort = {}
        c = dict(build.FILTER_DEFAULTS, blacklist_keys=["k1"])
        build.merge([fetch("a", payload([{"key": "k1", "api": "py_a"},
                                         {"key": "k2", "api": "py_b"}]))],
                    None, c, cohort)
        self.assertEqual(cohort["total_input"], 2)
        self.assertEqual(cohort["kept"], 1)
        self.assertEqual(len(cohort["dropped"]), 1)


if __name__ == "__main__":
    unittest.main()
