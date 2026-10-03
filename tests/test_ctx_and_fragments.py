# -*- coding: utf-8 -*-
"""第一层：报告碎片（build_fragments / render_report）与几个纯函数工具。

为什么"键集合必须和模板占位符集合完全一致"这条一定要测：
  这条断言就是"设计稿 = Action 产出"这个保证的机器化版本。
  模板是另一位同事并行维护的，只要他加了一个占位符而我们没给值，
  页面上就会留白（safe_substitute 不会报错）—— 静默留白同样是 bug，
  所以要让它在测试里红，而不是等用户看到一份缺角的报告。

另外两个容易翻车的点也钉在这里：
  · 模板里全是 CSS 花括号，渲染只能用 string.Template（用例里用带花括号的模板证一遍）；
  · 模板文件不存在时必须优雅降级（返回 False 而不是抛异常）——
    模板丢了不该让整条流水线变红，报告本来只是副产品。
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import _helpers as H  # noqa: E402

build = H.build


def template_placeholders():
    """模板里出现过的全部占位符名。

    注意这里刻意【连 $$name 也算进去】：模板顶部的"渲染契约"注释把每个占位符
    都写成 $$name 当文档，渲染后是一个字面 "$name"。把这一份也算上，
    "模板提到过的名字"和"脚本提供的名字"就必须严格相等 —— 多一个少一个都算契约破了。
    """
    with open(build.TEMPLATE_FILE, encoding="utf-8") as f:
        tpl = f.read()
    return set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", tpl))


def visible_leftovers(html_text):
    """正文里还没被替换掉的占位符（剔掉 HTML 注释和 CSS 注释里的说明文字）。"""
    scan = re.sub(r"<!--.*?-->", "", html_text, flags=re.S)
    scan = re.sub(r"/\*.*?\*/", "", scan, flags=re.S)
    return sorted(set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", scan)))


def src(sid, name, ok=True, cached=False, count=10, url="http://a/x.json", errors=None):
    return {"id": sid, "name": name, "state": "live" if ok else "down", "ok": ok,
            "from_cache": cached, "used_url": url, "site_count": count,
            "errors": errors or []}


def ctx(**kw):
    base = {
        "built_at": "2026-10-03 15:20:07",
        "run_label": "第 1 份台账",
        "sources": [src("feimao", "肥猫")],
        "live": 1, "cache": 0, "down": 0,
        "spider_id": "feimao", "spider_name": "肥猫", "spider_classes": 880,
        "kept": 0, "total_input": 0,
        "dropped_all": [], "duplicates": [],
        "delta_down": "—", "delta_new": "—", "delta_gone": "—",
        "foot_scope": "",
    }
    base.update(kw)
    return base


class EscTest(unittest.TestCase):
    def test_angle_brackets(self):
        self.assertEqual(build.esc("<b>hi</b>"), "&lt;b&gt;hi&lt;/b&gt;")

    def test_quotes_are_escaped_too(self):
        """quote=True：碎片要塞进属性（aria-label / 标题）里，只转 <> 不够。"""
        self.assertEqual(build.esc('a"b\'c'), "a&quot;b&#x27;c")

    def test_ampersand(self):
        self.assertEqual(build.esc("a&b"), "a&amp;b")

    def test_none_becomes_empty(self):
        self.assertEqual(build.esc(None), "")

    def test_numbers_and_zero(self):
        self.assertEqual(build.esc(0), "0")
        self.assertEqual(build.esc(False), "False")

    def test_emoji_survive(self):
        self.assertEqual(build.esc("🐮通用类型┃配置中心🐮"), "🐮通用类型┃配置中心🐮")

    def test_escaping_is_not_double_applied(self):
        self.assertEqual(build.esc(build.esc("<")), "&amp;lt;")


class SplitFamiliesTest(unittest.TestCase):
    def test_repeated_families_go_to_main_sorted_desc(self):
        main, tail = build.split_families([("a", 2), ("b", 5), ("a", 1)])
        self.assertEqual(main, [("b", 5), ("a", 3)])
        self.assertEqual(tail, [])

    def test_singletons_go_to_tail(self):
        main, tail = build.split_families([("a", 3), ("b", 1), ("c", 1)])
        self.assertEqual(main, [("a", 3)])
        self.assertEqual(sorted(tail), [("b", 1), ("c", 1)])

    def test_ties_sorted_by_name_for_stable_reports(self):
        """同数量按名字排序：不然报告每天的顺序都可能变，diff 里全是噪音。"""
        main, _ = build.split_families([("b", 2), ("a", 2), ("c", 2)])
        self.assertEqual([f for f, _n in main], ["a", "b", "c"])

    def test_top_n_caps_main(self):
        main, tail = build.split_families([("a", 3), ("b", 2), ("c", 2)], top_n=2)
        self.assertEqual(main, [("a", 3), ("b", 2)])
        self.assertEqual(tail, [("c", 2)])

    def test_empty_input(self):
        self.assertEqual(build.split_families([]), ([], []))

    def test_same_family_accumulates(self):
        main, _ = build.split_families([("x", 1), ("x", 1), ("x", 1)])
        self.assertEqual(main, [("x", 3)])


class CommonPrefixTest(unittest.TestCase):
    def test_common_prefix(self):
        self.assertEqual(build._common_prefix(["Wexdiy", "Wexconfig", "Wexyun"]), "Wex")

    def test_no_common_prefix(self):
        self.assertEqual(build._common_prefix(["abc", "xyz"]), "")

    def test_single_word(self):
        self.assertEqual(build._common_prefix(["abc"]), "abc")

    def test_empty_sequence(self):
        self.assertEqual(build._common_prefix([]), "")


class FamilyLabelTest(unittest.TestCase):
    def test_guard_classes_are_collapsed_to_common_prefix(self):
        """WexdiyGuard / WexconfigGuard / WexyunGuard 收成一条柱子 csp_Wex*Guard，
        报告上才读得出"这一类被砍了 33 个"而不是 33 行一样的 Guard。"""
        label = build.family_label_for(
            "csp_WexdiyGuard", ["WexdiyGuard", "WexconfigGuard", "WexyunGuard"])
        self.assertEqual(label, "csp_Wex*Guard")

    def test_single_class_uses_its_own_name(self):
        self.assertEqual(build.family_label_for("csp_BiliGuard", ["BiliGuard"]),
                         "csp_BiliGuard")

    def test_too_short_prefix_uses_own_name(self):
        self.assertEqual(build.family_label_for("csp_AbGuard", ["AbGuard", "AcGuard"]),
                         "csp_AbGuard")

    def test_non_guard_class_uses_its_own_name(self):
        self.assertEqual(build.family_label_for("csp_AppMao", ["AppMao"]), "csp_AppMao")

    def test_empty_api(self):
        self.assertEqual(build.family_label_for("", []), "（空 api）")

    def test_inner_class_is_truncated(self):
        self.assertEqual(build.family_label_for("csp_XBPQ$1", []), "csp_XBPQ")

    def test_mixed_classes_are_not_collapsed(self):
        label = build.family_label_for("csp_WexGuard", ["WexGuard", "AppMao"])
        self.assertEqual(label, "csp_WexGuard")

    def test_prefix_equal_to_own_name_uses_own_name(self):
        self.assertEqual(build.family_label_for("csp_WexGuard", ["WexGuard", "WexGuard"]),
                         "csp_WexGuard")


class ShortenTest(unittest.TestCase):
    def test_short_text_kept_but_whitespace_collapsed(self):
        self.assertEqual(build._shorten("a   b\n\tc", 40), "a b c")

    def test_long_text_is_cut_with_ellipsis(self):
        out = build._shorten("x" * 100, 10)
        self.assertEqual(len(out), 10)
        self.assertTrue(out.endswith("…"))

    def test_exact_length_is_not_cut(self):
        self.assertEqual(build._shorten("abcd", 4), "abcd")

    def test_none_becomes_empty(self):
        self.assertEqual(build._shorten(None, 5), "")


class BarTierOpacityTest(unittest.TestCase):
    def test_top_bar_is_opaque(self):
        self.assertEqual(build._bar_tier_opacity(0), 1.0)

    def test_decreases_monotonically(self):
        vals = [build._bar_tier_opacity(i) for i in range(8)]
        self.assertEqual(vals, sorted(vals, reverse=True))

    def test_never_below_floor(self):
        """深色底上太淡就看不见了，所以有 0.45 的下限。"""
        self.assertEqual(build._bar_tier_opacity(20), 0.45)


class BuildFragmentsContractTest(unittest.TestCase):
    def test_key_set_matches_template_exactly(self):
        """★ 核心契约：脚本给的键 == 模板用到的占位符，一个不多一个不少。

        少了 → 页面上留白（safe_substitute 不报错，静默出错）；
        多了 → 说明模板删了某块而我们还在算，迟早对不上。
        """
        frag = set(build.build_fragments(build.build_demo_ctx()))
        tpl = template_placeholders()
        self.assertEqual(sorted(frag - tpl), [],
                         "脚本给了模板没用到的变量（模板可能刚删过一块）")
        self.assertEqual(sorted(tpl - frag), [],
                         "模板用了脚本没给的变量（页面上会留白）")

    def test_key_set_is_stable_for_synthetic_ctx(self):
        """换一份完全不同的 ctx（没有剔除、没有缓存源）时键集合也不能变，
        否则某些分支下页面就会缺一块。"""
        frag = set(build.build_fragments(ctx()))
        self.assertEqual(frag, template_placeholders())

    def test_every_value_is_a_string(self):
        """string.Template 只吃字符串：混进 int/None 会在 substitute 时报
        "expected string or bytes-like object"，而且只在数据恰好长成那样时才炸。"""
        for k, v in build.build_fragments(build.build_demo_ctx()).items():
            self.assertIsInstance(v, str, k)


class BuildFragmentsContentTest(unittest.TestCase):
    def test_source_names_are_escaped(self):
        c = ctx(sources=[src("x", "<script>alert(1)</script> 😀")])
        html = build.build_fragments(c)["sources_table"]
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("😀", html)

    def test_all_backup_url_errors_are_listed(self):
        """失败源要把【每一个备用地址】的原因都写出来：
        只显示最后一条会让人以为只挂了一个地址，实际常见的是 7 个全挂 ——
        这直接决定要不要换源。"""
        bad = src("fantaiying", "饭太硬", ok=False, url=None, count=0, errors=[
            {"url": "http://a/tv", "error": "http://a/tv → HTTP 403 Forbidden"},
            {"url": "http://b/tv", "error": "http://b/tv → URLError: 域名解析不了"},
        ])
        html = build.build_fragments(ctx(sources=[bad], live=0, down=1))["sources_table"]
        self.assertIn("HTTP 403", html)
        self.assertIn("域名解析不了", html)
        self.assertIn("is-down", html)

    def test_down_source_without_errors_says_all_failed(self):
        bad = src("x", "某源", ok=False, url=None, count=0, errors=[])
        html = build.build_fragments(ctx(sources=[bad], live=0, down=1))["sources_table"]
        self.assertIn("所有地址均失败", html)

    def test_cache_source_is_marked(self):
        cached = src("ouge", "讴歌", ok=True, cached=True, url="(缓存)",
                     errors=[{"url": "c", "error": "网络未成功，改用本地缓存"}])
        html = build.build_fragments(ctx(sources=[cached], live=0, cache=1))["sources_table"]
        self.assertIn("is-cache", html)
        self.assertIn("使用缓存", html)

    def test_spider_source_is_labelled(self):
        good = src("feimao", "肥猫")
        html = build.build_fragments(ctx(sources=[good], spider_id="feimao"))["sources_table"]
        self.assertIn("本次 spider jar 的来源", html)

    def test_dropped_items_are_escaped_once_only(self):
        """第三列的所有实现内部都自己做 esc，li() 不能再转一次 ——
        转两次页面上就会显示成 &lt; 这种字面量。"""
        d = build.Drop("关键<b>", "名字>", "csp_X", "filter_name_patterns", matched="<i>")
        html = build.build_fragments(ctx(dropped_all=[d]))["dropped_by_rule"]
        self.assertIn("名字命中「&lt;i&gt;」", html)
        self.assertNotIn("&amp;lt;", html)
        self.assertIn("&lt;b&gt;", html)
        self.assertIn("&gt;", html)

    def test_missing_class_reason_text(self):
        d = build.Drop("k", "n", "csp_Nope", "missing_class", matched="Nope",
                       family="csp_Nope")
        frag = build.build_fragments(ctx(dropped_all=[d]))
        self.assertIn("Nope 包里没有", frag["dropped_missing_class"])

    def test_rule_reason_texts_differ_by_reason(self):
        """报告要能一眼看出是哪种规则命中的，所以四种理由的文案必须分开写。"""
        drops = [
            build.Drop("a", "n", "", "filter_keys"),
            build.Drop("b", "n", "", "filter_name_patterns", matched="失效"),
            build.Drop("c", "n", "", "filter_api_prefixes", matched="csp_Wex"),
            build.Drop("d", "n", "", "filter_hosts", matched="nxog.eu.org"),
        ]
        html = build.build_fragments(ctx(dropped_all=drops))["dropped_by_rule"]
        self.assertIn("按 key 过滤", html)
        self.assertIn("名字命中「失效」", html)
        self.assertIn("按 api 前缀过滤：csp_Wex", html)
        self.assertIn("按域名过滤：nxog.eu.org", html)

    def test_duplicate_reason_names_the_winner(self):
        d = build.Drop("k", "推送", "", "duplicate", kept_from="肥猫", dropped_from="王二小")
        html = build.build_fragments(ctx(dropped_all=[d],
                                        duplicates=[d]))["dropped_duplicate"]
        self.assertIn("只留 肥猫 的", html)

    def test_detail_list_is_capped_with_a_visible_note(self):
        """明细超过 DETAIL_CAP 要留一条"另有 N 条"，不能静默截断。"""
        drops = [build.Drop("k%d" % i, "n", "csp_X%d" % i, "missing_class",
                            matched="X%d" % i, family="csp_X") for i in range(build.DETAIL_CAP + 5)]
        html = build.build_fragments(ctx(dropped_all=drops))["dropped_missing_class"]
        self.assertIn("另有 5 条同类记录", html)

    def test_group_counts_and_total(self):
        g1 = [build.Drop("a", "", "csp_X", "missing_class", matched="X", family="csp_X")]
        g2 = [build.Drop("b", "", "", "filter_keys")]
        g3 = [build.Drop("c", "", "", "duplicate", kept_from="A", dropped_from="B")]
        frag = build.build_fragments(ctx(dropped_all=g1 + g2 + g3, duplicates=g3))
        self.assertEqual(frag["drop_g1_count"], "1")
        self.assertEqual(frag["drop_g2_count"], "1")
        self.assertEqual(frag["drop_g3_count"], "1")
        self.assertEqual(frag["dropped_total"], "3")

    def test_chart_bars_use_data_w_not_width(self):
        """柱状图的高亮由模板里的 JS 读 data-w 归一化后自己写：
        脚本这边【不能】自己算 width 百分比，否则 JS 会把结果再算一次。"""
        drops = [build.Drop("a%d" % i, "", "csp_BiliGuard", "missing_class",
                            matched="BiliGuard", family="csp_BiliGuard") for i in range(6)]
        frag = build.build_fragments(ctx(dropped_all=drops))
        self.assertIn('data-w="6"', frag["chart_bars"])
        self.assertNotIn("width", frag["chart_bars"])
        self.assertEqual(frag["chart_max_label"], "6")

    def test_only_missing_class_feeds_the_chart(self):
        """规则命中 / 同名重复没有 api 家族，混进柱状图会出现一条没有名字的柱子。"""
        drops = [build.Drop("b", "", "", "filter_keys"),
                 build.Drop("c", "", "", "duplicate", kept_from="A", dropped_from="B")]
        frag = build.build_fragments(ctx(dropped_all=drops, duplicates=[drops[1]]))
        self.assertEqual(frag["chart_bars"], "")
        self.assertEqual(frag["chart_max_label"], "0")

    def test_tail_label_is_honest_when_all_singletons(self):
        drops = [build.Drop("s%d" % i, "", "csp_C%d" % i, "missing_class",
                            matched="C%d" % i, family="csp_C%d" % i) for i in range(3)]
        frag = build.build_fragments(ctx(dropped_all=drops))
        self.assertIn("零散家族 3 个（各 1 个）", frag["chart_bars"])
        self.assertEqual(frag["bar_tail_text"], "，每个只砍掉 1 个")
        self.assertEqual(frag["bar_tail_sum"], "3×1")

    def test_tail_label_is_honest_when_overflow_has_multiple_sites(self):
        """main 只取 9 条，第 10 个"每族 2 个站"的家族会被挤进长尾。
        这时再说"各 1 个"就是假话：算式写 1×1，实际却砍了 2 个站，整页数字就没人信了。"""
        drops = []
        for f in range(10):
            fam = "csp_F%02d" % f
            drops += [build.Drop("%s_%d" % (fam, j), "", fam, "missing_class",
                                 matched="F%02d" % f, family=fam) for j in range(2)]
        frag = build.build_fragments(ctx(dropped_all=drops))
        self.assertIn("零散家族 1 个（合计 2 个）", frag["chart_bars"])
        self.assertEqual(frag["bar_tail_text"], "，合计 2 个")
        self.assertEqual(frag["bar_tail_sum"], "2 + 2 + 2 + 2 + 2 + 2 + 2 + 2 + 2 + 2")

    def test_tail_sum_formula(self):
        drops = []
        for key, fam in (("a", "csp_A"), ("b", "csp_B"), ("c", "csp_C"), ("d", "csp_D")):
            drops.append(build.Drop(key, "", fam, "missing_class", matched=fam, family=fam))
        drops += [build.Drop("a2", "", "csp_A", "missing_class", matched="A", family="csp_A"),
                  build.Drop("a3", "", "csp_A", "missing_class", matched="A", family="csp_A")]
        frag = build.build_fragments(ctx(dropped_all=drops))
        self.assertEqual(frag["bar_tail_sum"], "3 + 3×1")

    def test_g1_samples_are_capped_at_six(self):
        drops = [build.Drop("k%d" % i, "n%d" % i, "csp_X%d" % i, "missing_class",
                            matched="X%d" % i, family="csp_X") for i in range(10)]
        frag = build.build_fragments(ctx(dropped_all=drops))
        self.assertEqual(frag["drop_g1_samples"].count('class="sample"'), 6)

    def test_g1_samples_empty_without_missing_class(self):
        frag = build.build_fragments(ctx())
        self.assertEqual(frag["drop_g1_samples"], "")
        self.assertEqual(frag["drop_g1_chart"], "")

    def test_g1_chart_lists_all_families(self):
        drops = [build.Drop("k%d" % i, "", "csp_X", "missing_class",
                            matched="X", family="csp_X") for i in range(4)]
        frag = build.build_fragments(ctx(dropped_all=drops))
        self.assertIn("完整分布 · 合计 4 个", frag["drop_g1_chart"])
        self.assertIn("<td class=\"num\">4</td>", frag["drop_g1_chart"])

    def test_kpi_numbers_match_ctx(self):
        c = ctx(kept=80, total_input=100, live=2, cache=1, down=3,
                sources=[src("a", "A"), src("b", "B"), src("c", "C", cached=True),
                         src("d", "D", ok=False), src("e", "E", ok=False), src("f", "F", ok=False)])
        frag = build.build_fragments(c)
        self.assertEqual(frag["site_before"], "100")
        self.assertEqual(frag["site_after"], "80")
        self.assertEqual(frag["site_cut"], "20")
        self.assertEqual(frag["site_cut_pct"], "20")
        self.assertEqual(frag["src_total"], "6")
        self.assertEqual(frag["sources_heading"], "· 6 个来源")
        self.assertEqual(frag["generator"], build.GENERATOR)
        self.assertIn("80 SITES", frag["foot_meta"])
        self.assertIn("6 SOURCES", frag["foot_meta"])

    def test_zero_total_input_does_not_divide_by_zero(self):
        """total_input 为 0 时（所有源都返回空 sites）不能 ZeroDivisionError。"""
        frag = build.build_fragments(ctx(kept=0, total_input=0))
        self.assertEqual(frag["site_cut_pct"], "0")
        self.assertEqual(frag["cmp_a_pct"], "0")
        self.assertEqual(frag["dup_bar_ours_pct"], "0")


class RenderReportTest(unittest.TestCase):
    def test_missing_template_degrades_gracefully(self):
        """模板丢了要返回 False 而不是抛异常：报告只是副产品，
        不能因为模板文件没了就把整条流水线弄红。"""
        with H.sandbox() as sb:
            os.remove(sb.template)
            with H.capture_stdout() as out:
                ok = build.render_report(build.build_demo_ctx())
            self.assertFalse(ok)
            self.assertIn("[WARN]", out.getvalue())
            self.assertFalse(sb.exists(sb.report))

    def test_renders_real_template_and_leaves_no_placeholder(self):
        with H.sandbox() as sb:
            ok = build.render_report(build.build_demo_ctx())
            html_text = sb.read_report()
        self.assertTrue(ok)
        self.assertEqual(visible_leftovers(html_text), [])
        self.assertIn("<!DOCTYPE html>", html_text)

    def test_synthetic_ctx_renders_without_leftovers(self):
        with H.sandbox() as sb:
            build.render_report(ctx(kept=0, total_input=0, sources=[]))
            html_text = sb.read_report()
        self.assertEqual(visible_leftovers(html_text), [])

    def test_css_braces_are_not_treated_as_placeholders(self):
        """模板里全是 CSS 花括号：这条用例就是钉住"只能用 string.Template，
        不能用 str.format / f-string"这个决定。"""
        with H.sandbox(template_text="<style>.a{color:red}.b{width:1%}</style>$built_at") as sb:
            self.assertTrue(build.render_report(ctx()))
            html_text = sb.read_report()
        self.assertIn(".a{color:red}", html_text)
        self.assertIn("2026-10-03 15:20:07", html_text)

    def test_missing_variable_warns_but_still_writes(self):
        """safe_substitute 的行为约定：模板刚加了一个我们还没给的变量时，
        页面留白 + 日志告警，而不是抛异常让 Action 变红。"""
        with H.sandbox(template_text="$built_at / $not_yet_provided") as sb:
            with H.capture_stdout() as out:
                ok = build.render_report(ctx())
            html_text = sb.read_report()
        self.assertTrue(ok)
        self.assertIn("not_yet_provided", out.getvalue())
        self.assertIn("$not_yet_provided", html_text)

    def test_double_dollar_in_comment_is_not_reported(self):
        """模板约定注释里的占位符写成 $$name（渲染成一个字面 $name 当文档），
        那不算"没替换掉的占位符"，不该告警。"""
        with H.sandbox(template_text="<!-- $$chart_bars 是说明文字 -->$built_at") as sb:
            with H.capture_stdout() as out:
                build.render_report(ctx())
            html_text = sb.read_report()
        self.assertNotIn("没被替换", out.getvalue())
        self.assertIn("$chart_bars 是说明文字", html_text)


class DevDocsStripTest(unittest.TestCase):
    """模板顶部的维护者说明必须在【产物】里被剥掉，但必须留在【模板文件】里。

    为什么两件事都要测：
      · 不剥 → 产物第一眼是一大坨开发文档（实测占 287 行 / 13.5% 字节），
        而且它自己写着"本文件是模板、请勿当报告看"；
      · 从模板里删掉 → template_placeholders() 扫不到那 28 个 $$name 文档名，
        BuildFragmentsContractTest 的"严格相等"当场破 —— 文档必须留在模板里。
    这两条是矛盾的，只有"留模板、剥产物"能同时满足。
    """

    def read_template(self):
        with open(H.TEMPLATE_SRC, encoding="utf-8") as f:
            return f.read()

    def test_real_template_has_both_markers(self):
        tpl = self.read_template()
        self.assertIn("@@DEV-DOCS-START@@", tpl)
        self.assertIn("@@DEV-DOCS-END@@", tpl)

    def test_strip_removes_the_block_from_the_real_template(self):
        tpl = self.read_template()
        out = build.strip_dev_docs(tpl)
        self.assertNotIn("@@DEV-DOCS-START@@", out)
        self.assertNotIn("@@DEV-DOCS-END@@", out)
        # 这段文字只存在于维护者说明里，产物里绝对不能有
        for gone in ("本文件是【模板】，不是成品", "【渲染契约】", "占位符总清单"):
            self.assertNotIn(gone, out, f"产物里残留了维护者说明：{gone}")
        # 真正的报告结构必须还在
        self.assertTrue(out.startswith("<!DOCTYPE html>"), out[:60])
        self.assertIn("<html lang=\"zh-CN\">", out)
        self.assertIn("</html>", out)

    def test_strip_keeps_doctype_first_and_adjacent_to_html(self):
        """剥完不能让 <html> 和 <!DOCTYPE> 之间空出几行 —— 那会让产物第一屏是空白。

        这条同时防止一个更隐蔽的错：万一将来有人把 START 标记挪到 DOCTYPE 前面，
        DOCTYPE 就不是文件第一个东西了，浏览器会降级到 quirks mode。
        """
        out = build.strip_dev_docs(self.read_template())
        self.assertEqual(out.count("<!DOCTYPE html>"), 1,
                         "DOCTYPE 只能有一个，多了说明模板被拼坏过")
        self.assertRegex(out, r"\A<!DOCTYPE html>\s*<html lang=\"zh-CN\">")

    def test_strip_only_drops_docs_only_names(self):
        """剥离只能丢掉"【只】出现在说明里的名字"，正文真正用到的占位符一个都不许少。

        ⚠️ 这条是【有意的例外清单】，不是放宽校验：
           site_cut_pct / dropped_total 由 build_fragments 算出来（build.py:1013 / 965），
           但模板正文从头到尾没用过 —— 它们是"算而不用的死变量"，只靠说明里的那两行
           才被 template_placeholders() 看见（因此 BuildFragmentsContractTest 才没红）。
           剥掉说明后它们必然消失，这是【正确的暴露】而不是 bug。

        另外注意：这里比的是 abs，不能拿 template_placeholders() 直接比 —— 后者扫的是
        模板【文件原文】、故意含说明里的 $$name，它的口径必须保持不动（见
        test_key_set_matches_template_exactly 的说明）。
        """
        tpl = self.read_template()
        stripped = build.strip_dev_docs(tpl)
        before = set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", tpl))
        after = set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", stripped))
        self.assertEqual(after - before, set(), "剥离不该凭空多出占位符")
        self.assertEqual(before - after, {"site_cut_pct", "dropped_total"},
                         "剥离丢掉的必须只有这两个死变量；多丢了就是正文被误删")

    def test_every_body_placeholder_survives_the_strip(self):
        """正文（说明块之外）用到的占位符，剥离后一个都不能少 —— 少一个就是页面留白。

        读段落的方式与 strip_dev_docs 无关：按标记手动切，避免"用被测函数去验证自己"。
        """
        tpl = self.read_template()
        body = tpl.split("@@DEV-DOCS-END@@ -->", 1)[1]
        body_ph = set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", body))
        stripped_ph = set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)",
                                     build.strip_dev_docs(tpl)))
        self.assertTrue(body_ph, "正文一个占位符都没有？标记八成插错了位置")
        self.assertEqual(body_ph - stripped_ph, set(),
                         "正文里的占位符被剥离误删了")

    def test_markers_themselves_carry_no_dollar(self):
        """标记会被正则扫 $$name，标记里混进 $ 就会污染契约清单。"""
        for m in ("@@DEV-DOCS-START@@", "@@DEV-DOCS-END@@"):
            self.assertNotIn("$", m)

    def test_missing_markers_returns_template_unchanged(self):
        """标记丢了时不能返回空串：宁可产物多一段注释（丑但能看），
        也绝不能把整份报告弄成空白。"""
        plain = "<!DOCTYPE html>\n<html>$built_at</html>"
        self.assertEqual(build.strip_dev_docs(plain), plain)

    def test_only_the_first_block_is_stripped(self):
        """只剥第一块（count=1）：产物里出现第二块只可能有人手写，不是我们的文档。

        ⚠️ 标记内容不能用单字母（A/B 之类）：断言失败时 unittest 会把 "unexpectedly
        found" 这段【报错文本】回显出来，里面本身就有大写字母，assertNotIn("A", out)
        会被自己的报错消息喂饱、变成一个永远失败的假用例。用带 @@ 的长标记。
        """
        tpl = ("<!DOCTYPE html>\n"
               "<!-- @@DEV-DOCS-START@@FIRSTBLOCK@@DEV-DOCS-END@@ -->\n"
               "<html><!-- @@DEV-DOCS-START@@SECONDBLOCK@@DEV-DOCS-END@@ --></html>")
        out = build.strip_dev_docs(tpl)
        self.assertNotIn("FIRSTBLOCK", out)
        self.assertIn("SECONDBLOCK", out)

    def test_real_render_output_has_no_dev_docs(self):
        """端到端：拿真模板真渲染一遍，产物里不许有维护者说明，也不许有残留占位符。"""
        with H.sandbox() as sb:          # copy_template=True → 用的就是仓库里那份真模板
            with H.capture_stdout() as out:
                self.assertTrue(build.render_report(ctx()))
            html_text = sb.read_report()
        for gone in ("本文件是【模板】，不是成品", "【渲染契约】", "占位符总清单"):
            self.assertNotIn(gone, html_text)
        self.assertNotIn("@@DEV-DOCS", html_text)
        self.assertEqual(visible_leftovers(html_text), [])
        self.assertNotIn("没被替换", out.getvalue())


if __name__ == "__main__":
    unittest.main()
