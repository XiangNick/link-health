# -*- coding: utf-8 -*-
"""第一层：--demo 样例数据、status.json 的读取、failure_streak 累加、报告上下文。

为什么 failure_streak 要一条条钉：
  它是唯一会【写回仓库文件】的逻辑。写错了不是"报告不好看"，而是：
    · 把好站写进黑名单 → 那天开始每天少一批源，而且没人会注意到；
    · 把历史计数抹掉 → "连续失败 N 次"这个判断永远攒不满，等于功能失效。
  两个方向都很贵，所以"清零""+1""达到阈值拉黑""没出现就忘掉"各一条。
"""

import datetime
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import _helpers as H  # noqa: E402

build = H.build


class BuildDemoCtxTest(unittest.TestCase):
    def test_headline_numbers_agree(self):
        """主口径：167 -> 87，砍掉 80。这三个数字是报告上最大的字，
        也是 build_demo_ctx 里那几条 assert 盯的东西，用例再钉一遍防止有人手改样例。"""
        c = build.build_demo_ctx()
        self.assertEqual(c["total_input"], 167)
        self.assertEqual(c["kept"], 87)
        self.assertEqual(c["total_input"] - c["kept"], 80)

    def test_three_groups_sum_to_the_cut(self):
        """每个站只有一个剔除原因，所以三组相加必须【正好等于】砍掉的站数。"""
        c = build.build_demo_ctx()
        g1 = [d for d in c["dropped_all"] if d.reason == "missing_class"]
        g2 = [d for d in c["dropped_all"] if d.reason not in ("missing_class", "duplicate")]
        self.assertEqual((len(g1), len(g2), len(c["duplicates"])), (56, 16, 8))
        self.assertEqual(len(g1) + len(g2) + len(c["duplicates"]), 80)

    def test_source_states(self):
        c = build.build_demo_ctx()
        self.assertEqual((c["live"], c["cache"], c["down"]), (3, 1, 5))
        self.assertEqual(len(c["sources"]), 9)
        self.assertEqual(c["live"] + c["cache"] + c["down"], len(c["sources"]))

    def test_built_at_is_fixed_by_default(self):
        """样例报告要能一次渲染出同样的结果，不跟着机器时钟变 ——
        否则每次跑 --demo 都会产生一个"内容没变的巨大 diff"。"""
        self.assertEqual(build.build_demo_ctx()["built_at"], build.DEMO_BUILT_AT)
        self.assertNotEqual(build.DEMO_BUILT_AT,
                            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    def test_built_at_and_run_label_can_be_overridden(self):
        c = build.build_demo_ctx("2020-01-01 00:00:00", "第 9 份台账")
        self.assertEqual(c["built_at"], "2020-01-01 00:00:00")
        self.assertEqual(c["run_label"], "第 9 份台账")

    def test_is_deterministic(self):
        a, b = build.build_demo_ctx(), build.build_demo_ctx()
        self.assertEqual([d.as_dict() for d in a["dropped_all"]],
                         [d.as_dict() for d in b["dropped_all"]])
        self.assertEqual(a["sources"], b["sources"])

    def test_every_drop_has_a_known_reason(self):
        for d in build.build_demo_ctx()["dropped_all"]:
            self.assertIn(d.reason, ("missing_class", "duplicate", "filter_keys",
                                     "filter_name_patterns", "filter_api_prefixes",
                                     "filter_hosts"))

    def test_spider_is_the_designated_source(self):
        c = build.build_demo_ctx()
        self.assertEqual(c["spider_id"], "feimao")
        self.assertEqual(c["spider_name"], "肥猫")
        self.assertGreater(c["spider_classes"], 0)

    def test_demo_report_is_self_consistent_with_fragments(self):
        """样例数据 + 报告碎片走一遍：柱状图/明细/合计都要能渲染出来。"""
        frag = build.build_fragments(build.build_demo_ctx())
        self.assertEqual(frag["site_before"], "167")
        self.assertEqual(frag["site_after"], "87")
        self.assertEqual(frag["site_cut"], "80")
        self.assertEqual(frag["drop_g1_count"], "56")
        self.assertEqual(frag["drop_g2_count"], "16")
        self.assertEqual(frag["drop_g3_count"], "8")
        self.assertIn("csp_Wex*Guard", frag["chart_bars"])


class LoadPrevStatusTest(unittest.TestCase):
    def test_missing_file_gives_none(self):
        with H.sandbox() as sb:
            self.assertIsNone(build.load_prev_status(sb.status))

    def test_broken_json_gives_none_with_warning(self):
        """上次的 status.json 坏了要按"首次构建"处理，不能把构建弄崩 ——
        它只是我们的产物，不是输入。"""
        with H.sandbox() as sb:
            sb.write(sb.status, "{坏掉的 json")
            with H.capture_stdout() as out:
                self.assertIsNone(build.load_prev_status(sb.status))
        self.assertIn("[WARN]", out.getvalue())

    def test_reads_object(self):
        with H.sandbox() as sb:
            sb.write_json(sb.status, {"build_no": 7, "upstream_sources": []})
            self.assertEqual(build.load_prev_status(sb.status)["build_no"], 7)


def cohort(kept=(), failed=(), others=()):
    """造一个 merge() 风格的 cohort。"""
    drops = [build.Drop(k, "", "", "missing_class", matched="C") for k in failed]
    drops += [build.Drop(k, "", "", "filter_keys") for k in others]
    return {"kept": [{"key": k} for k in kept], "dropped": drops}


def status_doc(**kw):
    """造一份 status.json 的样例（build_ctx_from_status 的输入）。"""
    doc = {
        "built_at": "2026-10-03 15:20:07",
        "build_no": 3,
        "spider": {"source_id": "feimao", "source_name": "肥猫", "url": "http://a/jar.png",
                   "class_count": 880, "note": ""},
        "upstream_sources": [
            {"id": "a", "name": "A", "ok": True, "from_cache": False, "used_url": "u",
             "site_count": 10, "errors": []},
        ],
        "sites": {"total_input": 100, "kept": 80, "dropped": {}},
        "counts": {"live": 1, "cache": 0, "down": 0},
        "foot_scope": "口径",
    }
    doc.update(kw)
    return doc


class BuildCtxFromStatusTest(unittest.TestCase):
    def test_counts_and_passthrough(self):
        c = build.build_ctx_from_status(status_doc(), None, [], [])
        self.assertEqual((c["live"], c["cache"], c["down"]), (1, 0, 0))
        self.assertEqual(c["kept"], 80)
        self.assertEqual(c["total_input"], 100)
        self.assertEqual(c["spider_id"], "feimao")
        self.assertEqual(c["spider_classes"], 880)
        self.assertEqual(c["foot_scope"], "口径")
        self.assertEqual(c["built_at"], "2026-10-03 15:20:07")

    def test_state_counting_with_all_three_kinds(self):
        doc = status_doc(upstream_sources=[
            {"id": "a", "name": "A", "ok": True, "from_cache": False},
            {"id": "b", "name": "B", "ok": True, "from_cache": True},
            {"id": "c", "name": "C", "ok": False, "from_cache": False},
            {"id": "d", "name": "D", "ok": False, "from_cache": False},
        ])
        c = build.build_ctx_from_status(doc, None, [], [])
        self.assertEqual((c["live"], c["cache"], c["down"]), (1, 1, 2))

    def test_first_build_labels_dashes(self):
        """首次构建时三个差分数字没有意义，统一写成 "—"，
        把"首次构建"塞进"死源差值"那一格是错的（那是两件事）。"""
        c = build.build_ctx_from_status(status_doc(), None, [], [])
        self.assertEqual(c["run_label"], "首次构建")
        self.assertEqual((c["delta_down"], c["delta_new"], c["delta_gone"]),
                         ("—", "—", "—"))

    def test_second_build_computes_deltas(self):
        prev = status_doc(build_no=3, upstream_sources=[
            {"id": "a", "name": "A", "ok": True, "from_cache": False},
            {"id": "gone", "name": "G", "ok": True, "from_cache": False},
        ])
        cur = status_doc(upstream_sources=[
            {"id": "a", "name": "A", "ok": False, "from_cache": False},
            {"id": "new", "name": "N", "ok": True, "from_cache": False},
        ])
        c = build.build_ctx_from_status(cur, prev, [], [])
        self.assertEqual(c["run_label"], "第 4 份台账")
        self.assertEqual(c["delta_down"], "+1")
        self.assertEqual(c["delta_new"], "1 个")
        self.assertEqual(c["delta_gone"], "1 个")

    def test_delta_down_is_negative_when_fewer_sources_are_down(self):
        prev = status_doc(upstream_sources=[
            {"id": "a", "name": "A", "ok": False, "from_cache": False},
            {"id": "b", "name": "B", "ok": False, "from_cache": False}])
        cur = status_doc(upstream_sources=[
            {"id": "a", "name": "A", "ok": True, "from_cache": False},
            {"id": "b", "name": "B", "ok": False, "from_cache": False}])
        c = build.build_ctx_from_status(cur, prev, [], [])
        self.assertEqual(c["delta_down"], "-1")

    def test_missing_build_no_defaults_to_first(self):
        prev = status_doc()
        prev.pop("build_no")
        c = build.build_ctx_from_status(status_doc(), prev, [], [])
        self.assertEqual(c["run_label"], "第 2 份台账")

    def test_dropped_lists_are_passed_through(self):
        d = build.Drop("k", "n", "a", "missing_class", matched="C", family="csp_C")
        c = build.build_ctx_from_status(status_doc(), None, [d], [])
        self.assertEqual(c["dropped_all"], [d])
        self.assertEqual(c["duplicates"], [])

    def test_missing_foot_scope_defaults_empty(self):
        doc = status_doc()
        doc.pop("foot_scope")
        self.assertEqual(build.build_ctx_from_status(doc, None, [], [])["foot_scope"], "")

    def test_ctx_can_render_the_real_template(self):
        """把真实 status 折出来的 ctx 直接渲染一遍：这条把"status 结构变了但
        报告还按老结构取值"这种漂移挡在测试里。"""
        c = build.build_ctx_from_status(status_doc(), None, [], [])
        frag = build.build_fragments(c)
        self.assertEqual(frag["site_after"], "80")


if __name__ == "__main__":
    unittest.main()
