# -*- coding: utf-8 -*-
"""第一层：main() 的离线端到端（网络是假的，文件系统是沙箱）。

为什么要跑整条 main：
  上面那些用例都是按函数拆开的，但真正会让用户看到坏配置的是"函数之间的约定"：
    · fetched 到底是 3 元组还是 4 元组（草稿版就在这里崩了）；
    · config.json 里的 spider 是不是我们真正读 jar 的那一家；
    · 缓存源能不能当 spider 源（不能：缓存里的 jar 可能早就下线了）；
    · 脚本会不会往 filter.json 里写东西（★ 2026-10-03 起绝不允许，见 FilterJsonIsReadOnlyTest）。
  这些只有把 main 从头跑一遍才测得到。网络换成假的是必须的 ——
  真联网的版本在 tests/test_integration.py，默认跳过。
"""

import json
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import _helpers as H  # noqa: E402

build = H.build

JAR_URL_A = "http://cdn.a/jar.png"
JAR_URL_B = "http://cdn.b/jar.png"
SPIDER_A = JAR_URL_A + ";md5;aaa"
SPIDER_B = JAR_URL_B


def doc(sites, spider=SPIDER_A, **extra):
    d = {"spider": spider, "sites": sites}
    d.update(extra)
    return json.dumps(d, ensure_ascii=False)


def py_sites(n, prefix, start=0, api="py_demo"):
    return H.sites(n, prefix=prefix, api=api, start=start)


class MainFlowTest(unittest.TestCase):
    def setUp(self):
        self._sb_cm = H.sandbox()
        self.sb = self._sb_cm.__enter__()
        self.addCleanup(lambda: self._sb_cm.__exit__(None, None, None))
        self._time = H.fast_time()
        self._time.__enter__()
        self.addCleanup(lambda: self._time.__exit__(None, None, None))

    # ── 跑一次构建 ──
    def run_main(self, sources_doc, url_map, jar=None, args=(), filter_doc=None):
        if filter_doc is not None:
            self.sb.write_json(self.sb.filter, filter_doc)
        self.sb.write_sources(sources_doc)
        net = H.FakeNet(url_map, jar)
        with mock.patch.object(build, "fetch_text", net.fetch_text), \
                mock.patch.object(build, "fetch_bytes", net.fetch_bytes):
            with H.argv(*args):
                with H.capture_stdout() as out:
                    code = build.main()
        self.net = net
        return code, out.getvalue()

    def two_sources(self, spider_source="A"):
        return {
            "spider_source": spider_source,
            "sources": [
                {"id": "A", "name": "甲源", "urls": ["http://a/1.json"]},
                {"id": "B", "name": "乙源", "urls": ["http://b/1.json"]},
            ],
        }

    def texts(self, a, b, **kw):
        out = {"http://a/1.json": a, "http://b/1.json": b}
        out.update(kw)
        return out

    # ── 1. 完整跑通 ──
    def test_full_build_writes_all_artifacts(self):
        code, log = self.run_main(
            self.two_sources(),
            self.texts(doc(py_sites(6, "a")), doc(py_sites(5, "b"), spider=SPIDER_B)),
            jar=H.jar_of_classes(["AppRJ"]))
        self.assertEqual(code, 0, log)
        cfg = self.sb.read_config()
        st = self.sb.read_status()
        self.assertEqual(len(cfg["sites"]), 11)
        self.assertEqual(st["counts"], {"live": 2, "cache": 0, "down": 0})
        self.assertEqual(st["sites"]["kept"], 11)
        self.assertEqual(st["sites"]["total_input"], 11)
        self.assertEqual(st["build_no"], 1)
        self.assertTrue(self.sb.exists(self.sb.report))
        self.assertIn("2 个公开源", cfg["warningText"])
        self.assertEqual(st["upstream_sources"][0]["site_count"], 6)

    def test_report_has_no_leftover_placeholder(self):
        self.run_main(self.two_sources(),
                      self.texts(doc(py_sites(6, "a")), doc(py_sites(5, "b"), spider=SPIDER_B)),
                      jar=H.jar_of_classes(["AppRJ"]))
        html = self.sb.read_report()
        scan = re.sub(r"<!--.*?-->", "", html, flags=re.S)
        self.assertEqual(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", scan), [])

    # ── 2. spider 必须来自被指定的那个源（回归：merge 只带 base 的那一份）──
    def test_config_spider_comes_from_the_designated_source(self):
        """sources.json 指定的 spider_source 是 A，但 A 排在 B 后面（base 是 B）。
        config.json 里的 spider 必须是 A 的 —— 否则 App 加载的 jar 和
        我们做类校验用的 jar 不是同一个，一批站装上去就是打不开。

        注意断言的是"清洗后"的值：夹具里写的是 ";md5;aaa" 这种【无效占位 hash】
        （不是 32 位十六进制），写进配置前会被 sanitized_spider 去掉 ——
        留着它只会让人误以为这个 jar 是有校验的。真实的 32 位 md5 会被保留。"""
        code, log = self.run_main(
            self.two_sources(spider_source="A"),
            self.texts(doc(py_sites(6, "a"), spider=SPIDER_A),
                       doc(py_sites(5, "b"), spider=SPIDER_B)),
            jar=H.jar_of_classes(["AppRJ"]))
        self.assertEqual(code, 0, log)
        self.assertEqual(self.sb.read_config()["spider"], JAR_URL_A)
        self.assertEqual(self.sb.read_status()["spider"]["source_id"], "A")
        self.assertEqual(self.net.byte_calls, [JAR_URL_A])
        self.assertEqual(self.net.text_calls[0], "http://a/1.json")

    def test_falls_back_when_designated_source_is_down(self):
        code, log = self.run_main(
            self.two_sources(spider_source="A"),
            self.texts(RuntimeError("http://a/1.json → URLError: 域名解析不了"),
                       doc(py_sites(5, "b"), spider=SPIDER_B)),
            jar=H.jar_of_classes(["AppRJ"]))
        self.assertEqual(code, 0, log)
        st = self.sb.read_status()
        self.assertEqual(st["spider"]["source_id"], "B")
        self.assertIn("本次没抓到", st["spider"]["note"])
        self.assertIn("回退到 B", st["spider"]["note"])
        self.assertEqual(self.sb.read_config()["spider"], JAR_URL_B)

    def test_cache_source_cannot_provide_spider(self):
        """缓存源不能当 spider 源：缓存里的 spider 可能指向一个早就下线的 jar，
        而静态筛完全依赖这个 jar 的类清单 —— 选错会把好站全判死。"""
        cache_dir = os.path.join(self.sb.root, "cache")
        os.makedirs(cache_dir)
        with open(os.path.join(cache_dir, "A.json"), "w", encoding="utf-8") as f:
            f.write(doc(py_sites(6, "ac"), spider=SPIDER_A))
        code, log = self.run_main(
            self.two_sources(spider_source="A"),
            self.texts(RuntimeError("网络挂了"), doc(py_sites(5, "b"), spider=SPIDER_B)),
            jar=H.jar_of_classes(["AppRJ"]),
            args=("--cache-dir", cache_dir))
        self.assertEqual(code, 0, log)
        st = self.sb.read_status()
        a_entry = [s for s in st["upstream_sources"] if s["id"] == "A"][0]
        self.assertTrue(a_entry["from_cache"])
        self.assertEqual(a_entry["used_url"], "(缓存)")
        self.assertEqual(st["spider"]["source_id"], "B")
        self.assertEqual(self.sb.read_config()["spider"], SPIDER_B)
        self.assertEqual(st["counts"], {"live": 1, "cache": 1, "down": 0})

    def test_cache_only_run_still_exits_zero(self):
        cache_dir = os.path.join(self.sb.root, "cache2")
        os.makedirs(cache_dir)
        with open(os.path.join(cache_dir, "A.json"), "w", encoding="utf-8") as f:
            f.write(doc(py_sites(6, "ac")))
        code, log = self.run_main(
            self.two_sources(spider_source="A"),
            self.texts(RuntimeError("挂"), RuntimeError("挂")),
            jar=H.jar_of_classes(["AppRJ"]),
            args=("--cache-dir", cache_dir))
        self.assertEqual(code, 0, log)
        st = self.sb.read_status()
        self.assertEqual(st["counts"], {"live": 0, "cache": 1, "down": 1})
        # 没有一个活着的源 → 不选 spider，也不做类存在性校验（保守：少砍几个站）
        self.assertIsNone(st["spider"]["source_id"])
        self.assertIsNone(st["spider"]["class_count"])
        self.assertEqual(self.net.byte_calls, [])

    def test_successful_result_is_written_back_to_cache_dir(self):
        """缓存目录要自己滚动更新，不需要额外挂一个"上传上一天产物"的 Action 步骤。"""
        cache_dir = os.path.join(self.sb.root, "cache3")
        self.run_main(self.two_sources(),
                      self.texts(doc(py_sites(6, "a")), doc(py_sites(5, "b"), spider=SPIDER_B)),
                      jar=H.jar_of_classes(["AppRJ"]),
                      args=("--cache-dir", cache_dir))
        with open(os.path.join(cache_dir, "A.json"), encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(len(saved["sites"]), 6)

    # ── 3. 校验 / 备用地址 ──
    def test_invalid_payload_moves_to_the_next_url(self):
        """校验不达标 → 继续试下一个备用地址，而不是直接放弃这一家。"""
        sources = {"spider_source": "A", "sources": [
            {"id": "A", "name": "甲源", "urls": ["http://a/1.json", "http://a/2.json"]}]}
        code, log = self.run_main(
            sources,
            {"http://a/1.json": '{"sites": [], "spider": "u"}',
             "http://a/2.json": doc(py_sites(6, "a"))},
            jar=H.jar_of_classes(["AppRJ"]))
        self.assertEqual(code, 0, log)
        st = self.sb.read_status()
        self.assertEqual(st["upstream_sources"][0]["used_url"], "http://a/2.json")
        self.assertIn("校验不通过", st["upstream_sources"][0]["errors"][0]["error"])
        self.assertIn("有效条目", log)

    def test_every_failed_url_gets_its_own_error_record(self):
        """每个地址的失败原因都要单独记一条：只留最后一条会让人以为只挂了一个地址，
        报告上也就看不出"到底该不该换源"。"""
        sources = {"spider_source": "B", "sources": [
            {"id": "A", "name": "甲源",
             "urls": ["http://a/1.json", "http://a/2.json", "http://a/3.json"]},
            {"id": "B", "name": "乙源", "urls": ["http://b/1.json"]}]}
        code, log = self.run_main(
            sources,
            {"http://a/1.json": RuntimeError("一号地址超时"),
             "http://a/2.json": RuntimeError("二号地址 404"),
             "http://a/3.json": '{"sites": [], "spider": "u"}',
             "http://b/1.json": doc(py_sites(5, "b"), spider=SPIDER_B)},
            jar=H.jar_of_classes(["AppRJ"]))
        self.assertEqual(code, 0, log)
        st = self.sb.read_status()
        a_entry = [s for s in st["upstream_sources"] if s["id"] == "A"][0]
        errs = [e["error"] for e in a_entry["errors"]]
        self.assertEqual(len(errs), 3)
        self.assertTrue(any("一号地址超时" in e for e in errs))
        self.assertTrue(any("二号地址 404" in e for e in errs))
        self.assertTrue(any("校验不通过" in e for e in errs))
        self.assertFalse(a_entry["ok"])
        self.assertEqual(a_entry["site_count"], 0)

    def test_no_source_available_writes_nothing(self):
        """一个源都不可用：直接退出码 1，且【不动】任何产物 ——
        半份配置比没有配置危险得多。"""
        self.sb.write_filter({"filter_keys": ["手工规则"]})
        before = self.sb.read_filter()
        code, log = self.run_main(
            self.two_sources(),
            self.texts(RuntimeError("挂"), RuntimeError("挂")))
        self.assertEqual(code, 1)
        self.assertFalse(self.sb.exists(self.sb.config))
        self.assertFalse(self.sb.exists(self.sb.status))
        self.assertFalse(self.sb.exists(self.sb.report))
        self.assertEqual(self.sb.read_filter(), before)
        self.assertIn("[FATAL]", log)

    # ── 4. 连续失败跨天累加 ──
    def _streak_run(self, extra_sites, jar):
        a_sites = py_sites(6, "a") + extra_sites
        return self.run_main(
            {"spider_source": "B", "sources": [
                {"id": "A", "name": "甲源", "urls": ["http://a/1.json"]},
                {"id": "B", "name": "乙源", "urls": ["http://b/1.json"]}]},
            self.texts(doc(a_sites), doc(py_sites(5, "b"), spider=SPIDER_B)),
            jar=jar)

    # ── 5. 规则、去重、列表字段 ──
    def test_blacklist_rule_is_applied_and_reported(self):
        filt = dict(build.FILTER_DEFAULTS, filter_keys=["a1"],
                    filter_name_patterns=["失效"])
        code, log = self.run_main(
            self.two_sources(),
            self.texts(doc(py_sites(6, "a") + [{"key": "名人", "name": "接口失效",
                                                "api": "py_x"}]),
                       doc(py_sites(5, "b"), spider=SPIDER_B)),
            jar=H.jar_of_classes(["AppRJ"]), filter_doc=filt)
        self.assertEqual(code, 0, log)
        dropped = self.sb.read_status()["sites"]["dropped"]["by_rule"]
        reasons = sorted((d["reason"], d["key"]) for d in dropped)
        self.assertEqual(reasons, [("filter_keys", "a1"),
                                   ("filter_name_patterns", "名人")])
        names = [s["key"] for s in self.sb.read_config()["sites"]]
        self.assertNotIn("a1", names)
        self.assertNotIn("名人", names)

    def test_duplicate_key_keeps_the_first_and_is_reported(self):
        code, log = self.run_main(
            self.two_sources(),
            self.texts(doc([{"key": "push_agent", "name": "推送(甲)", "api": "py_a"}] +
                           py_sites(6, "a")),
                       doc([{"key": "push_agent", "name": "推送(乙)", "api": "py_b"}] +
                           py_sites(5, "b"), spider=SPIDER_B)),
            jar=H.jar_of_classes(["AppRJ"]))
        self.assertEqual(code, 0, log)
        kept = [s for s in self.sb.read_config()["sites"] if s["key"] == "push_agent"]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["name"], "推送(甲)")
        dups = self.sb.read_status()["sites"]["dropped"]["duplicate"]
        self.assertEqual(len(dups), 1)
        self.assertEqual((dups[0]["kept_from"], dups[0]["dropped_from"]), ("甲源", "乙源"))

    def test_list_fields_are_merged(self):
        code, log = self.run_main(
            self.two_sources(),
            self.texts(doc(py_sites(6, "a"), ads=[{"name": "甲广告", "url": "http://a/1"}]),
                       doc(py_sites(5, "b"), spider=SPIDER_B,
                           ads=[{"name": "乙广告", "url": "http://b/1"}])),
            jar=H.jar_of_classes(["AppRJ"]))
        self.assertEqual(code, 0, log)
        self.assertEqual([a["name"] for a in self.sb.read_config()["ads"]],
                         ["甲广告", "乙广告"])

    def test_only_sources_limits_the_run(self):
        code, log = self.run_main(
            self.two_sources(),
            self.texts(doc(py_sites(6, "a")), doc(py_sites(5, "b"), spider=SPIDER_B)),
            jar=H.jar_of_classes(["AppRJ"]),
            filter_doc=dict(build.FILTER_DEFAULTS, only_sources=["B"]))
        self.assertEqual(code, 0, log)
        st = self.sb.read_status()
        self.assertEqual([s["id"] for s in st["upstream_sources"]], ["B"])
        self.assertEqual(self.net.text_calls, ["http://b/1.json"])

    def test_class_not_in_jar_is_dropped(self):
        """正常的静态筛：类不在包里 → 剔掉，并写进 missing_class 那一组。"""
        code, log = self.run_main(
            self.two_sources(),
            self.texts(doc(py_sites(6, "a") + [{"key": "坏站", "api": "csp_没有的类"}]),
                       doc(py_sites(5, "b"), spider=SPIDER_B)),
            jar=H.jar_of_classes(["AppRJ"]))
        self.assertEqual(code, 0, log)
        st = self.sb.read_status()
        dropped = st["sites"]["dropped"]["missing_class"]
        self.assertEqual([d["key"] for d in dropped], ["坏站"])
        self.assertEqual(dropped[0]["matched"], "没有的类")
        self.assertEqual(st["spider"]["class_count"], 1)
        self.assertEqual(st["spider"]["url"], JAR_URL_A)

    def test_class_in_jar_is_kept(self):
        code, log = self.run_main(
            self.two_sources(),
            self.texts(doc(py_sites(6, "a") + [{"key": "好站", "api": "csp_AppRJ$1"}]),
                       doc(py_sites(5, "b"), spider=SPIDER_B)),
            jar=H.jar_of_classes(["AppRJ"]))
        self.assertEqual(code, 0, log)
        self.assertIn("好站", [s["key"] for s in self.sb.read_config()["sites"]])

    # ── 6. jar 读不出来时的降级 ──
    def test_unreadable_jar_skips_class_filtering(self):
        """jar 读不出来时【不能】按空集跑静态筛：那会把所有 csp_ 站全判死。
        跳过校验是更保守的选择（少砍几个站），并在报告里写清楚。"""
        code, log = self.run_main(
            self.two_sources(),
            self.texts(doc(py_sites(6, "a") + [{"key": "csp站", "api": "csp_随便什么类"}]),
                       doc(py_sites(5, "b"), spider=SPIDER_B)),
            jar=b"\x89PNG\r\n\x1a\n this is not a zip")
        self.assertEqual(code, 0, log)
        st = self.sb.read_status()
        self.assertIsNone(st["spider"]["class_count"])
        self.assertIn("跳过类存在性校验", st["spider"]["note"])
        self.assertEqual(st["sites"]["dropped"]["missing_class"], [])
        self.assertIn("csp站", [s["key"] for s in self.sb.read_config()["sites"]])
        self.assertIn("读 jar 失败", log)

    def test_jar_without_any_class_name_also_skips_class_filtering(self):
        """回归：dex 里一个类名都读不出来时，jar_class_names 现在会抛异常，
        于是这里同样走"跳过校验"。如果它安静地返回空集合，
        这个 csp_ 站就会被判死 —— 一份坏 dex 能把整份配置清空。"""
        code, log = self.run_main(
            self.two_sources(),
            self.texts(doc(py_sites(6, "a") + [{"key": "csp站", "api": "csp_随便什么类"}]),
                       doc(py_sites(5, "b"), spider=SPIDER_B)),
            jar=H.zip_jar(b"\x00" * 8))
        self.assertEqual(code, 0, log)
        st = self.sb.read_status()
        self.assertIsNone(st["spider"]["class_count"])
        self.assertIn("csp站", [s["key"] for s in self.sb.read_config()["sites"]])

    # ── 7. 台账编号 / --demo ──
    def test_build_no_increments_across_runs(self):
        args = (self.two_sources(),
                self.texts(doc(py_sites(6, "a")), doc(py_sites(5, "b"), spider=SPIDER_B)))
        jar = H.jar_of_classes(["AppRJ"])
        self.run_main(*args, jar=jar)
        first = self.sb.read_status()["build_no"]
        self.run_main(*args, jar=jar)
        second = self.sb.read_status()["build_no"]
        self.assertEqual((first, second), (1, 2))
        self.assertIn("第 2 份台账", self.sb.read_report())

    def test_demo_does_not_touch_config_or_status(self):
        code, log = self.run_main({}, {}, args=("--demo",))
        self.assertEqual(code, 0, log)
        self.assertFalse(self.sb.exists(self.sb.config))
        self.assertFalse(self.sb.exists(self.sb.status))
        self.assertTrue(self.sb.exists(self.sb.report))
        self.assertIn("167", self.sb.read_report())

    def test_demo_output_is_byte_identical_across_runs(self):
        self.run_main({}, {}, args=("--demo",))
        first = self.sb.read_report()
        self.run_main({}, {}, args=("--demo",))
        self.assertEqual(first, self.sb.read_report())

    # ── 8. 产物里【不该】有 home 字段 ──
    #
    # ★ 2026-10-03 起不再写 home。原因（在 App 源码 + 真机上核实过）：
    #   App 决定首页是 VodConfig.java:288 的三级回退 —— ①用户在 App 里手动选过的
    #   （存本地数据库）② 云端 config.json 的 home 字段 ③ getSites().get(0)。
    #   而 VodConfig 读的 JSON 键是 logo/notice/danmaku/wallpaper/spider/sites/parses，
    #   【没有 home】—— 写了也不生效。默认首页 = sites[0]，由 sources.json 源顺序决定。
    def test_config_never_contains_home_field(self):
        filt = dict(build.FILTER_DEFAULTS, home_candidates=["a3"])
        code, log = self.run_main(
            dict(self.two_sources(), home_candidates=["a3"]),
            self.texts(doc(py_sites(6, "a")), doc(py_sites(5, "b"), spider=SPIDER_B)),
            jar=H.jar_of_classes(["AppRJ"]))
        self.assertEqual(code, 0, log)
        cfg = self.sb.read_config()
        self.assertNotIn("home", cfg, "产物里不该再有 home 字段（App 不读它）")
        # 默认首页是 sites[0]，由合并顺序决定 —— 这里第一条来自优先级最高的 A 源
        self.assertEqual(cfg["sites"][0]["key"], "a0")
        self.assertIn("不写 home", log)


class FilterJsonIsReadOnlyTest(unittest.TestCase):
    """★ 本文件最要紧的一条不变式：脚本【绝不】写 filter.json。

    历史教训（2026-10-03 真机踩出来的）：上一版会把"类不存在"连续 3 次的站
    自动追加进 filter.json 的 blacklist_keys。而黑名单优先级(第2)高于类校验(第6)，
    一旦写进去，那个站再也不会被重新评估 —— 换回对的 jar 也永远出不来。
    实测被这样永久误杀 38 个好站。

    所以这里钉死：跑完构建，filter.json 的内容必须与构建前【逐字节相同】。
    """

    def setUp(self):
        self._sb_cm = H.sandbox()
        self.sb = self._sb_cm.__enter__()
        self.addCleanup(lambda: self._sb_cm.__exit__(None, None, None))
        self._time = H.fast_time()
        self._time.__enter__()
        self.addCleanup(lambda: self._time.__exit__(None, None, None))

    def test_default_filter_keys_all_take_effect(self):
        """★ 仓库里那份 filter.json 的 filter_keys，每一条都必须真的把对应站点挡掉。

        为什么单独测这个：2026-10-03 重构时我差点把其中 5 条（央视经典/py_cctv_少儿/
        csp_wogg1/csp_woog2/豆瓣1）漏掉 —— 它们混在 67 条历史黑名单里，
        不逐条核验根本看不出来。漏了就等于那几个站会出现在电视上。
        """
        real = build.load_filter(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config", "filter.json"))
        keys = real.get("filter_keys") or []
        self.assertTrue(keys, "config/filter.json 的 filter_keys 不该是空的")

        for key in keys:
            site = {"key": key, "name": "某个名字", "api": "py_x"}
            keep, reason, _m = build.filter_site(
                site, key=key, name=site["name"], api=site["api"],
                spider_classes={"AppRJ"}, cfg=real)
            self.assertFalse(keep, "filter_keys 里的 %r 没生效" % key)
            self.assertEqual(reason, "filter_keys")

        # 教育类那 4 条必须是"类存在、只有靠 filter_keys 才挡得住"的
        for edu in ("csp_少儿", "csp_小学", "csp_初中", "csp_高中"):
            self.assertIn(edu, keys, "教育类 %s 必须留在 filter_keys 里" % edu)

    def test_whitelist_beats_filter_keys(self):
        """白名单优先级最高：同时在两个列表里时必须保留（否则"后悔药"是假的）。"""
        c = dict(build.FILTER_DEFAULTS,
                 filter_keys=["k"], whitelist_keys=["k"])
        keep, reason, _ = build.filter_site(
            {"key": "k", "name": "n", "api": "py_x"},
            key="k", name="n", api="py_x", spider_classes=set(), cfg=c)
        self.assertTrue(keep)
        self.assertIsNone(reason)

    def test_filter_json_is_byte_identical_after_a_build(self):
        filt = dict(build.FILTER_DEFAULTS,
                    filter_keys=["手工规则"],
                    filter_name_patterns=["失效"])
        self.sb.write_filter(filt)
        self.sb.write_sources({
            "spider_source": "A",
            "sources": [
                {"id": "A", "name": "甲源", "urls": ["http://a/1.json"]},
                {"id": "B", "name": "乙源", "urls": ["http://b/1.json"]},
            ],
        })
        before = open(self.sb.filter, "rb").read()

        net = H.FakeNet(
            {"http://a/1.json": doc(py_sites(6, "a") + [{"key": "坏站", "api": "csp_没有的类"}]),
             "http://b/1.json": doc(py_sites(5, "b"), spider=SPIDER_B)},
            H.jar_of_classes(["AppRJ"]))
        with mock.patch.object(build, "fetch_text", net.fetch_text), \
                mock.patch.object(build, "fetch_bytes", net.fetch_bytes), \
                H.argv(), H.capture_stdout():
            code = build.main()

        self.assertEqual(code, 0)
        after = open(self.sb.filter, "rb").read()
        self.assertEqual(before, after, "脚本不能写 filter.json")
        # 被类校验剔掉的站，绝不能因此进 filter_keys
        self.assertNotIn("坏站", json.loads(after.decode("utf-8"))["filter_keys"])

    def test_filter_json_untouched_even_when_repeated(self):
        """连跑三次（旧机制下第 3 次会触发自动拉黑），filter.json 仍必须原样。"""
        self.sb.write_filter(dict(build.FILTER_DEFAULTS, filter_keys=["手工规则"]))
        self.sb.write_sources({
            "spider_source": "A",
            "sources": [
                {"id": "A", "name": "甲源", "urls": ["http://a/1.json"]},
                {"id": "B", "name": "乙源", "urls": ["http://b/1.json"]},
            ],
        })
        before = open(self.sb.filter, "rb").read()
        dead = [{"key": "死站", "name": "死站", "api": "csp_已经删掉的类"}]
        for _ in range(3):
            net = H.FakeNet(
                {"http://a/1.json": doc(py_sites(6, "a") + dead),
                 "http://b/1.json": doc(py_sites(5, "b"), spider=SPIDER_B)},
                H.jar_of_classes(["AppRJ"]))
            with mock.patch.object(build, "fetch_text", net.fetch_text), \
                    mock.patch.object(build, "fetch_bytes", net.fetch_bytes), \
                    H.argv(), H.capture_stdout():
                build.main()
        self.assertEqual(before, open(self.sb.filter, "rb").read())
        self.assertNotIn("死站", json.loads(
            open(self.sb.filter, encoding="utf-8").read())["filter_keys"])


if __name__ == "__main__":
    unittest.main()
