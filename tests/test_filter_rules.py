# -*- coding: utf-8 -*-
"""第一层：规则文件的读写 + host 抠取/黑名单 + filter_site 的七级优先级。

为什么优先级要一条一条钉：
  filter_site 是"删站"的唯一出口，顺序错了就是灾难性的行为差异 ——
  比如白名单如果排在黑名单后面，"手工加白的源"会被规则推翻；
  再比如 missing_class 如果排在名字规则前面，报告里"为什么砍"就再也说不清了。
另外 host 黑名单有个著名的坑：直接 endswith 会让 notnxog.eu.org 被 nxog.eu.org 命中，
误伤一整片无关的域名，所以标签边界那条负面用例必须留着。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import _helpers as H  # noqa: E402

build = H.build

# run_filter 的"没传 classes"哨兵：None 在这里是有意义的取值（jar 读不出来），
# 不能拿 None 当"用默认值"，否则 test_none_spider_classes_never_drops 会假绿。
_UNSET = object()


def cfg(**kw):
    """造一份过滤规则：不传的字段用"空规则"。"""
    out = dict(build.FILTER_DEFAULTS)
    out.update(kw)
    return out


class LoadFilterTest(unittest.TestCase):
    def test_missing_file_falls_back_to_defaults(self):
        """filter.json 不存在也要能跑（第一次 clone 下来、或同事误删）：
        Action 变红比少一条规则糟糕得多。"""
        with H.sandbox() as sb:
            got = build.load_filter(sb.filter)
        self.assertEqual(set(got), set(build.FILTER_DEFAULTS))
        self.assertEqual(got["filter_keys"], [])

    def test_broken_json_is_tolerated(self):
        with H.sandbox() as sb:
            sb.write(sb.filter, "{ 这不是 json")
            with H.capture_stdout() as out:
                got = build.load_filter(sb.filter)
        self.assertIn("[WARN]", out.getvalue())
        self.assertEqual(got["filter_keys"], [])

    def test_missing_fields_are_filled_with_defaults(self):
        """同事手工编辑时删掉一个键，脚本要按默认值继续，不能 KeyError。"""
        with H.sandbox() as sb:
            sb.write_filter({"filter_keys": ["a"]}, sb.filter)
            got = build.load_filter(sb.filter)
        self.assertEqual(got["filter_keys"], ["a"])
        for k in build.FILTER_DEFAULTS:
            self.assertIn(k, got)

    def test_none_value_falls_back_to_default(self):
        with H.sandbox() as sb:
            sb.write_json(sb.filter, {"filter_keys": None})
            got = build.load_filter(sb.filter)
        self.assertEqual(got["filter_keys"], [])

    def test_comment_fields_are_carried_back(self):
        """下划线开头的是给人看的注释，load/save 往返必须原样保留，否则写回一次就丢一批说明。"""
        with H.sandbox() as sb:
            sb.write_json(sb.filter, {"_说明": "别手改 failure_streak", "filter_keys": []})
            got = build.load_filter(sb.filter)
        self.assertIn("_说明", got)
        self.assertEqual(got["_说明"], "别手改 failure_streak")

    def test_unknown_plain_field_is_ignored(self):
        """不认识的普通字段不进配置：白名单式的字段表才能保证"写回什么"是可预期的。"""
        with H.sandbox() as sb:
            sb.write_json(sb.filter, {"不认识的字段": 1})
            got = build.load_filter(sb.filter)
        self.assertNotIn("不认识的字段", got)

    def test_defaults_are_not_shared_mutably(self):
        """默认值表里有 list：两次加载之间不能共用同一个列表对象，
        否则 A 次运行往 filter_keys 里塞的东西会漏到 B 次运行里。"""
        with H.sandbox() as sb:
            a = build.load_filter(sb.filter)
            a["filter_keys"].append("x")
            b = build.load_filter(sb.filter)
        self.assertEqual(b["filter_keys"], [])
        self.assertEqual(build.FILTER_DEFAULTS["filter_keys"], [])


class ExtractHostsTest(unittest.TestCase):
    def test_ext_as_plain_url_string(self):
        site = {"api": "csp_X", "ext": "http://v.rbotv.cn/xxx"}
        self.assertEqual(build.extract_hosts(site), {"v.rbotv.cn"})

    def test_ext_as_object_with_json_field(self):
        site = {"api": "csp_X", "ext": {"json": "https://a.example.com/cfg.json"}}
        self.assertEqual(build.extract_hosts(site), {"a.example.com"})

    def test_ext_as_object_with_site_urls_list(self):
        site = {"api": "csp_X", "ext": {"site_urls": ["https://a.com", "http://b.com/x"]}}
        self.assertEqual(build.extract_hosts(site), {"a.com", "b.com"})

    def test_ext_as_bare_array(self):
        site = {"api": "csp_X", "ext": ["http://a.com", "https://b.com/p"]}
        self.assertEqual(build.extract_hosts(site), {"a.com", "b.com"})

    def test_api_itself_counted_when_it_is_a_url(self):
        """api 本身可能就是一条 URL（走 js:/assets: 之外的加载路径）。"""
        self.assertEqual(build.extract_hosts({"api": "http://api.example.com/x"}),
                         {"api.example.com"})

    def test_csp_api_yields_nothing(self):
        """csp_Xxx / py_xxx 这类没有 http 前缀的值不能被当成 host：
        盲目按 ":" 切会切出一堆垃圾（"csp_少儿" 会变成一个叫 csp_少儿 的"域名"）。"""
        self.assertEqual(build.extract_hosts({"api": "csp_少儿", "ext": "assets://abc"}), set())
        self.assertEqual(build.extract_hosts({"api": "js:http"}), set())

    def test_unparseable_input_gives_empty_set(self):
        """ext 是不可序列化的对象也不能抛异常：清洗阶段崩一次，整条流水线就红了。"""
        self.assertEqual(build.extract_hosts({"api": None, "ext": object()}), set())
        self.assertEqual(build.extract_hosts({}), set())
        self.assertEqual(build.extract_hosts({"ext": 123}), set())

    def test_hosts_are_lowercased(self):
        self.assertEqual(build.extract_hosts({"ext": "http://V.RBOTV.CN/x"}),
                         {"v.rbotv.cn"})

    def test_host_excludes_port(self):
        self.assertEqual(build.extract_hosts({"ext": "http://a.com:8080/x"}), {"a.com"})

    def test_no_duplicates(self):
        site = {"ext": "http://a.com/1 http://a.com/2"}
        self.assertEqual(build.extract_hosts(site), {"a.com"})

    def test_ext_none_is_fine(self):
        self.assertEqual(build.extract_hosts({"api": "csp_X", "ext": None}), set())

    def test_nested_json_in_ext_dict(self):
        site = {"ext": {"a": {"b": ["http://deep.example.com/x"]}}}
        self.assertEqual(build.extract_hosts(site), {"deep.example.com"})


class HostBlacklistedTest(unittest.TestCase):
    RULES = ["nxog.eu.org"]

    def test_exact_match(self):
        self.assertTrue(build.host_blacklisted("nxog.eu.org", self.RULES))

    def test_subdomain_match(self):
        self.assertTrue(build.host_blacklisted("woog.nxog.eu.org", self.RULES))

    def test_tag_boundary_not_endswith(self):
        """【关键】notnxog.eu.org 不能被 nxog.eu.org 命中。

        直接写 h.endswith(r) 就会命中 —— 于是所有以 "nxog.eu.org" 结尾的字符串
        都被拉黑，误伤一整片无关域名。所以必须是 h == r 或 h.endswith("." + r)。"""
        self.assertFalse(build.host_blacklisted("notnxog.eu.org", self.RULES))
        self.assertFalse(build.host_blacklisted("evilnxog.eu.org", self.RULES))

    def test_suffix_attack_not_matched(self):
        self.assertFalse(build.host_blacklisted("nxog.eu.org.evil.com", self.RULES))

    def test_case_insensitive(self):
        self.assertTrue(build.host_blacklisted("WOOG.NXOG.EU.ORG", self.RULES))

    def test_rule_with_dots_and_spaces_is_normalized(self):
        self.assertTrue(build.host_blacklisted("a.b.com", [" .B.com. "]))

    def test_host_with_trailing_dot(self):
        """DNS 里 "a.com." 和 "a.com" 是同一个东西，黑名单不能因为多一个点就放过。"""
        self.assertTrue(build.host_blacklisted("a.nxog.eu.org.", self.RULES))

    def test_empty_rule_is_skipped(self):
        """空规则如果参与匹配，h.endswith("." + "") 会命中一切 —— 等于全站拉黑。"""
        self.assertFalse(build.host_blacklisted("anything.com", ["", "  ", None]))
        self.assertFalse(build.host_blacklisted("anything.com", ["."]))

    def test_empty_host_is_not_matched(self):
        self.assertFalse(build.host_blacklisted("", self.RULES))
        self.assertFalse(build.host_blacklisted(None, self.RULES))

    def test_any_rule_can_hit(self):
        self.assertTrue(build.host_blacklisted("x.bad.com", ["a.com", "bad.com"]))

    def test_no_rules_means_no_hit(self):
        self.assertFalse(build.host_blacklisted("a.com", []))


class DropTest(unittest.TestCase):
    def test_minimal_dict(self):
        d = build.Drop("k", "名字", "csp_X", "missing_class")
        self.assertEqual(d.as_dict(),
                         {"key": "k", "name": "名字", "api": "csp_X",
                          "reason": "missing_class"})

    def test_matched_is_included_when_present(self):
        d = build.Drop("k", "n", "a", "filter_name_patterns", matched="失效")
        self.assertEqual(d.as_dict()["matched"], "失效")

    def test_kept_from_and_dropped_from_go_together(self):
        """重复项要能说清"留了谁、丢了谁"，否则报告上那句"只留 肥猫 的"没法写。"""
        d = build.Drop("k", "n", "a", "duplicate", kept_from="肥猫", dropped_from="王二小")
        self.assertEqual(d.as_dict()["kept_from"], "肥猫")
        self.assertEqual(d.as_dict()["dropped_from"], "王二小")

    def test_empty_matched_is_omitted(self):
        d = build.Drop("k", "n", "a", "filter_keys", matched="")
        self.assertNotIn("matched", d.as_dict())

    def test_slots_are_declared(self):
        """__slots__ 写错一个字就会在赋值时 AttributeError；顺手确认它能被正常构造。"""
        d = build.Drop("k", "n", "a", "missing_class", matched="C", family="csp_C")
        self.assertEqual((d.key, d.name, d.api, d.family), ("k", "n", "a", "csp_C"))


class FilterSiteTest(unittest.TestCase):
    CLASSES = {"AppRJ", "WexdiyGuard"}

    def site(self, **kw):
        s = {"key": "k", "name": "名字", "api": "csp_AppRJ"}
        s.update(kw)
        return s

    def run_filter(self, site, cfg_obj, classes=_UNSET):
        """classes 不传就用默认的"jar 类清单"；显式传 None 表示"jar 读不出来"。
        所以这里必须用一个哨兵值，不能用 None 当"没传"。"""
        if classes is _UNSET:
            classes = self.CLASSES
        return build.filter_site(site, key=site.get("key"), name=site.get("name"),
                                 api=site.get("api") or "",
                                 spider_classes=classes, cfg=cfg_obj)

    # ── 白名单：无条件保留 ──
    def test_whitelist_wins_over_everything(self):
        """白名单必须【无条件】保留：它同时命中 key 黑名单、名字正则、api 家族、
        host 黑名单和缺失类，一条都不许翻盘 —— 顺序写错这里就会红。"""
        site = self.site(key="白名单站", name="关注公众号",
                         api="csp_不存在的类", ext="http://nxog.eu.org/x")
        c = cfg(whitelist_keys=["白名单站"],
                filter_keys=["白名单站"],
                filter_name_patterns=["关注公众号"],
                filter_api_prefixes=["csp_不存在"],
                filter_hosts=["nxog.eu.org"])
        keep, reason, matched = self.run_filter(site, c)
        self.assertTrue(keep)
        self.assertIsNone(reason)
        self.assertEqual(matched, "")

    # ── 黑名单 key ──
    def test_blacklist_key(self):
        keep, reason, _ = self.run_filter(self.site(), cfg(filter_keys=["k"]))
        self.assertFalse(keep)
        self.assertEqual(reason, "filter_keys")

    def test_blacklist_key_beats_name_pattern(self):
        """优先级：key 黑名单在名字正则之前，报告里的原因才不会写成"名字命中"这种半截解释。"""
        c = cfg(filter_keys=["k"], filter_name_patterns=["名字"])
        _, reason, _ = self.run_filter(self.site(), c)
        self.assertEqual(reason, "filter_keys")

    # ── 名字正则 ──
    def test_name_pattern_records_which_word_matched(self):
        """matched 字段必须记下命中哪个词：报告上要写"名字命中「失效」"，
        只给个 True 就没法解释这条站为什么被砍。"""
        c = cfg(filter_name_patterns=["失效", "停更"])
        _, reason, matched = self.run_filter(self.site(name="🐲接口失效"), c)
        self.assertEqual(reason, "filter_name_patterns")
        self.assertEqual(matched, "失效")

    def test_first_matching_pattern_wins(self):
        c = cfg(filter_name_patterns=["停更", "失效"])
        _, _, matched = self.run_filter(self.site(name="失效又停更"), c)
        self.assertEqual(matched, "停更")

    def test_pattern_uses_search_not_fullmatch(self):
        c = cfg(filter_name_patterns=["关注公众号"])
        _, reason, _ = self.run_filter(
            self.site(name="📢最新地址请关注公众号【熊猫】获取"), c)
        self.assertEqual(reason, "filter_name_patterns")

    def test_broken_regex_is_skipped_without_crash(self):
        """同事把正则写坏了不能连累整个构建：跳过这一条 + 日志留痕，其它的照常生效。"""
        c = cfg(filter_name_patterns=["([unclosed", "失效"])
        with H.capture_stdout() as out:
            _, reason, matched = self.run_filter(self.site(name="接口失效"), c)
        self.assertEqual(reason, "filter_name_patterns")
        self.assertEqual(matched, "失效")
        self.assertIn("[WARN]", out.getvalue())

    def test_empty_pattern_is_skipped(self):
        """空正则 re.search("", name) 会命中一切 —— 那是一条能砍掉所有站的规则，必须跳过。"""
        _, reason, _ = self.run_filter(self.site(name="正常名字"),
                                       cfg(filter_name_patterns=[""]))
        self.assertIsNone(reason)

    def test_no_name_skips_name_patterns(self):
        _, reason, _ = self.run_filter(self.site(name=""), cfg(filter_name_patterns=["."]))
        self.assertIsNone(reason)

    # ── api 家族 ──
    def test_api_family_prefix(self):
        _, reason, matched = self.run_filter(self.site(api="csp_Wexdiy"),
                                             cfg(filter_api_prefixes=["csp_Wex"]))
        self.assertEqual(reason, "filter_api_prefixes")
        self.assertEqual(matched, "csp_Wex")

    def test_api_family_must_be_prefix(self):
        self.assertIsNone(self.run_filter(self.site(api="xxx_csp_Wex"),
                                          cfg(filter_api_prefixes=["csp_Wex"]))[1])

    def test_empty_family_is_skipped(self):
        """空家族串是 startswith 的万能前缀，会把所有站都拉黑。"""
        self.assertIsNone(self.run_filter(self.site(), cfg(filter_api_prefixes=[""]))[1])

    def test_api_family_beats_host_rule(self):
        site = self.site(api="csp_Wexdiy", ext="http://nxog.eu.org/x")
        c = cfg(filter_api_prefixes=["csp_Wex"], filter_hosts=["nxog.eu.org"])
        self.assertEqual(self.run_filter(site, c)[1], "filter_api_prefixes")

    # ── host 黑名单 ──
    def test_host_blacklist_from_api(self):
        site = self.site(key="h", api="http://nxog.eu.org/cfg.json")
        _, reason, matched = self.run_filter(site, cfg(filter_hosts=["nxog.eu.org"]))
        self.assertEqual(reason, "filter_hosts")
        self.assertEqual(matched, "nxog.eu.org")

    def test_host_blacklist_from_ext(self):
        site = self.site(ext={"json": "https://woog.nxog.eu.org/a.json"})
        _, reason, _ = self.run_filter(site, cfg(filter_hosts=["nxog.eu.org"]))
        self.assertEqual(reason, "filter_hosts")

    def test_host_blacklist_tag_boundary_no_false_kill(self):
        site = self.site(ext="http://notnxog.eu.org/a.json")
        self.assertIsNone(self.run_filter(site, cfg(filter_hosts=["nxog.eu.org"]))[1])

    # ── missing_class ──
    def test_missing_class_is_dropped(self):
        site = self.site(key="x", api="csp_没有这个类")
        keep, reason, matched = self.run_filter(site, cfg())
        self.assertFalse(keep)
        self.assertEqual(reason, "missing_class")
        self.assertEqual(matched, "没有这个类")

    def test_site_with_own_jar_skips_class_check(self):
        """自带 jar 的站加载的是它自己的 jar 里的类，不能拿 spider 的类清单去判它。"""
        site = self.site(key="x", api="csp_没有这个类", jar="http://a/own.jar")
        self.assertTrue(self.run_filter(site, cfg())[0])

    def test_none_spider_classes_never_drops(self):
        """【最容易写错、后果最严重的一条】
        jar 读不出来时 spider_classes 是 None；如果这里按"空集合"处理，
        所有 csp_ 站会被一口气全判死 —— 产出空配置，而且日志上看不出任何异常。"""
        site = self.site(key="x", api="csp_任何类")
        keep, reason, _ = self.run_filter(site, cfg(), classes=None)
        self.assertTrue(keep)
        self.assertIsNone(reason)

    def test_empty_spider_classes_drops_everything(self):
        """反过来，空集合（不是 None）确实会剔干净 —— 这正是"读 jar 失败必须抛异常
        而不是返回空集合"的原因，两条用例互为例证。"""
        site = self.site(key="x", api="csp_任何类")
        keep, reason, _ = self.run_filter(site, cfg(), classes=set())
        self.assertFalse(keep)
        self.assertEqual(reason, "missing_class")

    def test_non_csp_api_skips_class_check(self):
        """py_ / js: 之类走别的加载器，判不了必须放过（判死了就是误杀）。"""
        for api in ("py_cctv_full", "js:xxx", "assets://a", ""):
            self.assertTrue(self.run_filter(self.site(key="x", api=api), cfg())[0])

    def test_missing_class_comes_last(self):
        """规则命中要排在类检查之前：报告按"手工规则"和"客观判死"分三组，
        顺序反了就会出现"规则里明明写着要砍，报告却说它类不在包里"。"""
        site = self.site(key="k", api="csp_没有这个类")
        c = cfg(filter_keys=["k"])
        self.assertEqual(self.run_filter(site, c)[1], "filter_keys")

    # ── 正常保留 ──
    def test_clean_site_is_kept(self):
        keep, reason, matched = self.run_filter(self.site(), cfg())
        self.assertTrue(keep)
        self.assertIsNone(reason)
        self.assertEqual(matched, "")

    def test_all_rules_empty_keeps_everything_valid(self):
        for api in ("csp_AppRJ", "csp_WexdiyGuard", "py_x"):
            self.assertTrue(self.run_filter(self.site(key="k", api=api), cfg())[0])

    def test_missing_cfg_keys_do_not_crash(self):
        """cfg 是残缺的（旧版 filter.json）也不能崩 —— 用 .get 兜底。"""
        keep, _, _ = build.filter_site({"key": "k", "name": "n", "api": "csp_AppRJ"},
                                       key="k", name="n", api="csp_AppRJ",
                                       spider_classes=self.CLASSES, cfg={})
        self.assertTrue(keep)


if __name__ == "__main__":
    unittest.main()
