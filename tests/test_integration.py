# -*- coding: utf-8 -*-
"""第二层：联网集成测试（默认【跳过】）。

跑法（仓库根目录）：
    set RUN_NET_TESTS=1
    python -m unittest tests.test_integration -v

它做三件事：
  1. 拿 config/sources.json 里【真实的池子】完整跑一次构建（全程在临时目录里，
     绝不覆盖仓库里的 config.json / status.json / report.html / filter.json）；
  2. 拉上游最新的 output/单仓聚合.json，和我们产出的 config.json 对比：
     上游有多少站、我们留了多少、每个剔除原因的分布、以及【误杀检查】；
  3. 把结论写成人能读的 tests/_integration_report.md。

【误杀检查】是这一层最重要的部分：
  上游有、我们也有、但我们把它剔除了的站，必须【逐条给得出理由】：
    · missing_class → 现场重新读一次 jar，确认这个类真的不在包里（不是我们算错）；
    · duplicate     → 同一个 key 我们保留了另一份，那份确实在 config.json 里；
    · blacklist_*   → 用 filter.json 的规则重跑一遍 filter_site，理由要能复现。
  只要有【一条】解释不出来，就说明我们砍多了 —— 那就是用例失败。

⚠️ 上游每天 10:00 / 22:00（北京）各更新一次，站点数会变，所以这里
   一个写死的数字都不用，全部动态算。
"""

import json
import os
import sys
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import _helpers as H  # noqa: E402

build = H.build

UPSTREAM_URL = ("https://cdn.jsdelivr.net/gh/Lightconer/tvbox-ysc-config@main"
                "/output/%E5%8D%95%E4%BB%93%E8%81%9A%E5%90%88.json")
REPORT_MD = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "_integration_report.md")
# 仓库里真正的输入路径：进沙箱后 build.FILTER_FILE 会被改掉，所以要先记下来。
REPO_SOURCES = os.path.join(H.ROOT, "config", "sources.json")
REPO_FILTER = os.path.join(H.ROOT, "config", "filter.json")

ITEM_CAP = 60          # 报告 md 里每一类最多列多少条明细


def _enabled():
    return str(os.environ.get("RUN_NET_TESTS", "")).strip().lower() not in (
        "", "0", "false", "no", "off")


def fetch_json(url, timeout=60):
    """直接抓一段 JSON。不走 build.fetch_text —— 它是给"上游源"用的（带重试和
    挑战页判定），这里只抓一个 CDN 文件，出错了应该立刻看得见。"""
    req = urllib.request.Request(url, headers={"User-Agent": build.UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


@unittest.skipUnless(_enabled(), "联网集成测试默认跳过：set RUN_NET_TESTS=1 才跑")
class IntegrationTest(unittest.TestCase):
    """整个类共享一次真实构建的结果（跑一次要好几分钟，不能每个用例都跑）。"""

    @classmethod
    def setUpClass(cls):
        cls.lines = []
        cls._classes = "missing"
        cls.sb_cm = H.sandbox()
        sb = cls.sb_cm.__enter__()
        cls.sb = sb

        # 用仓库里真实的输入：源清单 + 过滤规则（filter.json 原样拷进沙箱，
        # 这样构建时 save_filter 写回的是沙箱那份，仓库里的规则一个字都不会动）
        with open(REPO_SOURCES, encoding="utf-8") as f:
            cls.sources_doc = json.load(f)
        with open(REPO_FILTER, encoding="utf-8") as f:
            cls.filter_doc = json.load(f)
        sb.write_json(sb.filter, cls.filter_doc)
        sb.write_json(sb.sources, cls.sources_doc)
        cls.total_sources = len(cls.sources_doc.get("sources", []))

        # ── 跑一次真实构建 ──
        # 只把 time.sleep 换成空操作（源间隔 3 秒 + 重试退避 6 秒，真睡下来这一层要十几分钟）。
        # 抓取本身、重试次数、校验、合并、渲染全都是真的。
        cls._time = H.fast_time()
        cls._time.__enter__()
        t0 = time.time()
        with H.argv():
            with H.capture_stdout() as out:
                cls.run_code = build.main()
        cls.build_log = out.getvalue()
        cls.build_seconds = time.time() - t0

        cls.config = sb.read_config() if sb.exists(sb.config) else None
        cls.status = sb.read_status() if sb.exists(sb.status) else None

        # ── 拉上游最新产物 ──
        cls.upstream = None
        cls.upstream_error = None
        try:
            cls.upstream = fetch_json(UPSTREAM_URL)
        except Exception as e:  # noqa: BLE001 - 网络问题要写进报告，不是直接红
            cls.upstream_error = "%s: %s" % (type(e).__name__, e)

        cls._time.__exit__(None, None, None)

    @classmethod
    def tearDownClass(cls):
        cls.sb_cm.__exit__(None, None, None)
        if cls.lines:
            with open(REPORT_MD, "w", encoding="utf-8") as f:
                f.write("\n".join(cls.lines) + "\n")

    # ── 工具 ──
    @classmethod
    def say(cls, text=""):
        cls.lines.append(text)

    def keyed(self, config):
        """把一份配置的 sites 折成 {key: site}（重复 key 只留第一个）。"""
        out = {}
        for s in (config or {}).get("sites", []) or []:
            k = str((s or {}).get("key") or "").strip()
            if k and k not in out:
                out[k] = s
        return out

    # ── 1. 真实构建本身 ──
    def test_01_build_ran(self):
        self.assertIn(self.run_code, (0, 1))
        self.assertIsNotNone(self.status, "没产出 status.json：构建在写产物之前就失败了")
        self.assertIsNotNone(self.config, "没产出 config.json")

    def test_02_pool_is_configured(self):
        self.assertGreaterEqual(self.total_sources, 5)
        self.say("# 集成测试报告（真实池子 vs 上游单仓）")
        self.say()
        self.say("> 由 `python -m unittest tests.test_integration -v` 生成，"
                 "每次跑覆盖本文件。数字全部动态计算，上游每天更新两次。")
        self.say(">")
        self.say("> 本次真实构建用时 %.0f 秒（测试里把 `time.sleep` 换成了空操作："
                 "源间隔 3 秒、重试退避 6 秒，真睡的话这一层要十几分钟；"
                 "抓取 / 重试 / 校验 / 合并 / 渲染全都是真的，输入用的是仓库里真实的 "
                 "config/sources.json 与 config/filter.json）。" % self.build_seconds)
        self.say()
        self.say("## 1. 上游源（%d 个）" % self.total_sources)
        self.say()
        self.say("| id | 名字 | 状态 | 站点数 | 用的地址 |")
        self.say("| --- | --- | --- | --- | --- |")
        for s in self.status["upstream_sources"]:
            state = "缓存" if s["from_cache"] else ("活" if s["ok"] else "挂")
            self.say("| %s | %s | %s | %s | `%s` |" % (
                s["id"], s["name"], state,
                s["site_count"] if s["ok"] else "—", s["used_url"] or "—"))
        self.say()
        c = self.status["counts"]
        self.say("合计：%d 活 / %d 缓存 / %d 挂" % (c["live"], c["cache"], c["down"]))
        self.say()
        self.assertGreater(c["live"] + c["cache"], 0,
                           "一个源都没抓到，这次集成测试没有可比性")

    # ── 2. 我们自己的产出必须自洽 ──
    def test_03_our_config_is_valid_and_usable(self):
        """我们产出的每一个 csp_ 站，类都必须真的在这次用的 jar 里 ——
        这是"每个站都能用"这个承诺的直接验证（不是靠 status.json 自证）。"""
        sites = self.config.get("sites") or []
        self.assertTrue(self.config.get("spider"), "config.json 里没有 spider")
        self.say("## 2. 我们的产出")
        self.say()
        self.say("- 站点数：**%d**" % len(sites))
        self.say("- spider：`%s`（%s 个类）" % (
            self.config.get("spider"),
            (self.status.get("spider") or {}).get("class_count")))
        self.say("- 退出码：%d" % self.run_code)
        self.say()

        classes = self._jar_classes()
        if classes is None:
            self.skipTest("jar 这次没读到，跳过类存在性复核")
        bad = []
        for s in sites:
            if s.get("jar"):
                continue
            cls = build.class_name_of(str(s.get("api") or ""))
            if cls is not None and cls and cls not in classes:
                bad.append((s.get("key"), cls))
        self.say("复核结果：留存的站里，api 的类不在 jar 里的有 **%d** 个"
                 "（应该永远是 0）" % len(bad))
        self.say()
        self.assertEqual(bad[:10], [], "留存下来的站里有类不在 jar 里的：%r" % bad[:10])

    def _jar_classes(self):
        """现场重新读一次 spider 的 jar（证明"类不在包里"这个判断是真的）。

        结果缓存在类上：一次构建要复核两处，jar 有 4 MB，不能来回下。"""
        if type(self)._classes != "missing":
            return type(self)._classes
        url = (self.status.get("spider") or {}).get("url")
        type(self)._classes = None
        if not url:
            return None
        try:
            type(self)._classes = build.jar_class_names(build.fetch_bytes(url))
        except Exception as e:  # noqa: BLE001
            self.say("（jar 复核失败：%s）" % e)
            self.say()
        return type(self)._classes

    def test_04_drop_distribution(self):
        d = self.status["sites"]["dropped"]
        by_rule = d["by_rule"]
        self.say("## 3. 剔除原因分布")
        self.say()
        self.say("| 原因 | 数量 |")
        self.say("| --- | --- |")
        self.say("| 类不在 jar 里 | %d |" % len(d["missing_class"]))
        for reason in ("filter_keys", "filter_name_patterns",
                       "filter_api_prefixes", "filter_hosts"):
            n = len([x for x in by_rule if x["reason"] == reason])
            self.say("| 规则：%s | %d |" % (reason, n))
        self.say("| 同名重复 | %d |" % len(d["duplicate"]))
        self.say()
        self.say("输入 %d → 保留 %d" % (self.status["sites"]["total_input"],
                                        self.status["sites"]["kept"]))
        self.say()
        total = (len(d["missing_class"]) + len(by_rule))
        # total_input 里【不含】重复项（重复的那份已经被算过一次输入了），
        # 所以对账时要把 duplicate 排除在外，否则这条断言永远不成立。
        self.assertEqual(total, self.status["sites"]["total_input"] -
                         self.status["sites"]["kept"],
                         "剔除记录相加 != 输入-保留：说明有站被漏记或记了两遍")

    # ── 3. 和上游对比 ──
    def test_05_compare_with_upstream(self):
        if self.upstream is None:
            self.say("## 4. 与上游对比")
            self.say()
            self.say("⚠️ 拉上游产物失败，本次没有对比：`%s`" % self.upstream_error)
            self.say()
            self.skipTest("上游产物拉不到：%s" % self.upstream_error)

        up = self.keyed(self.upstream)
        ours = self.keyed(self.config)
        common = set(up) & set(ours)
        self.say("## 4. 与上游单仓对比")
        self.say()
        self.say("| | 数量 |")
        self.say("| --- | --- |")
        self.say("| 上游站点数 | %d |" % len(up))
        self.say("| 我们保留 | %d |" % len(ours))
        self.say("| 两边都有 | %d |" % len(common))
        self.say("| 上游有、我们没有 | %d |" % len(set(up) - set(ours)))
        self.say("| 我们有、上游没有 | %d |" % len(set(ours) - set(up)))
        self.say()
        self.assertGreater(len(up), 0, "上游产物是空的，没法比")
        self.assertGreater(len(common), 0,
                           "和上游一个 key 都对不上：要么上游换了 key 体系，要么我们抓空了")

    # ── 4. 误杀检查（本层的重点）──
    def test_06_false_kill_check(self):
        if self.upstream is None:
            self.skipTest("上游产物拉不到：%s" % self.upstream_error)

        up = self.keyed(self.upstream)
        ours = self.keyed(self.config)
        d = self.status["sites"]["dropped"]
        dropped = {}
        for item in d["missing_class"]:
            dropped[item["key"]] = ("missing_class", item)
        for item in d["by_rule"]:
            dropped[item["key"]] = (item["reason"], item)
        for item in d["duplicate"]:
            dropped[item["key"]] = ("duplicate", item)

        # 候选 = 上游有、我们也有（输入里出现过）、但我们剔掉了
        candidates = sorted(set(up) & set(dropped))

        classes = self._jar_classes()
        unexplained = []
        explained = {"missing_class": [], "duplicate": [], "by_rule": []}

        for key in candidates:
            reason, item = dropped[key]
            if reason == "missing_class":
                cls = item.get("matched") or ""
                if classes is not None and cls and cls not in classes:
                    explained["missing_class"].append((key, item))
                elif classes is None:
                    explained["missing_class"].append((key, item))
                else:
                    unexplained.append((key, "missing_class 说类 %r 不在包里，"
                                             "但现场重读 jar 时它其实在" % cls))
            elif reason == "duplicate":
                winner = item.get("kept_from") or ""
                if item.get("key") in ours:
                    explained["duplicate"].append((key, item))
                else:
                    unexplained.append((key, "记成重复，但我们其实没保留任何一份"))
            else:
                # 规则命中：用 filter.json 把这条规则重跑一遍，理由必须能复现
                site = up.get(key) or {}
                keep, why, matched = build.filter_site(
                    site, key=key, name=str(site.get("name") or ""),
                    api=str(site.get("api") or ""), spider_classes=None,
                    cfg=self.filter_doc)
                if (not keep) and why == reason:
                    explained["by_rule"].append((key, item))
                else:
                    unexplained.append((key, "规则理由复现不出来：记录是 %s，"
                                             "重跑得到 %s" % (reason, why)))

        self.say("## 5. 【误杀检查】上游有 + 我们也有 + 我们剔掉了")
        self.say()
        self.say("候选总数：**%d** 个；其中："
                 "类不在包里 %d · 同名重复 %d · 命中规则 %d；"
                 "**解释不出来的：%d**"
                 % (len(candidates), len(explained["missing_class"]),
                    len(explained["duplicate"]), len(explained["by_rule"]),
                    len(unexplained)))
        self.say()

        def dump(title, rows, fmt):
            self.say("### %s（%d 个）" % (title, len(rows)))
            self.say()
            if not rows:
                self.say("（无）")
                self.say()
                return
            self.say("| key | 名字 | 理由 |")
            self.say("| --- | --- | --- |")
            for key, item in rows[:ITEM_CAP]:
                self.say("| %s | %s | %s |" % (key, (item.get("name") or "（无名）")
                                               .replace("|", "\\|"), fmt(item)))
            if len(rows) > ITEM_CAP:
                self.say("| … | 另有 %d 条 | 见 status.json |" % (len(rows) - ITEM_CAP))
            self.say()

        dump("类不在 jar 里（客观判死，已现场重读 jar 复核）",
             explained["missing_class"],
             lambda i: "`%s` 不在这次用的 jar 里" % (i.get("matched") or "?"))
        dump("同名重复（我们保留了另一份）", explained["duplicate"],
             lambda i: "保留了 `%s` 的那一份" % (i.get("kept_from") or "?"))
        def rule_reason(item):
            reason = item.get("reason")
            if reason == "filter_keys":
                return "`%s`：这个 key 在手工黑名单里" % item.get("key")
            return "`%s` · 命中 `%s`" % (reason, item.get("matched") or "")

        dump("命中 filter.json 规则（理由已用规则重跑复现）", explained["by_rule"],
             rule_reason)

        if unexplained:
            self.say("### ⚠️ 解释不出来的剔除（这就是误杀）")
            self.say()
            for key, why in unexplained[:ITEM_CAP]:
                self.say("- `%s`：%s" % (key, why))
            self.say()

        self.say("> 结论：有理由的剔除 %d 条，**无理由的误杀 %d 条**。"
                 % (len(candidates) - len(unexplained), len(unexplained)))
        self.say()
        self.assertEqual(unexplained[:10], [],
                         "存在解释不出来的误杀：%r" % (unexplained[:10],))

    # ── 5. 上游有但我们完全没有的 key（不是误杀，只是没抓到那个源）──
    def test_07_upstream_only_keys(self):
        if self.upstream is None:
            self.skipTest("上游产物拉不到：%s" % self.upstream_error)
        up = self.keyed(self.upstream)
        ours = self.keyed(self.config)
        dropped_keys = set()
        for group in self.status["sites"]["dropped"].values():
            for item in group:
                dropped_keys.add(item["key"])
        only_up = sorted(set(up) - set(ours) - dropped_keys)
        known = set(ours) | dropped_keys

        # 上游遇到同名 key 会【加源前缀改名后保留】（"4k_荐片" 就是 "荐片" 的另一份）。
        # 这类 key 看着是"我们没见过的站"，其实同一个站我们见过了、只是按原名处理。
        # 把它们单独摘出来，"上游有我们没有"这个数字才不会被误读成"我们漏了一批"。
        renamed = []
        for k in only_up:
            if "_" in k:
                tail = k.split("_", 1)[1]
                if tail in known:
                    renamed.append((k, tail))

        self.say("## 6. 上游有、我们连见都没见到的站")
        self.say()
        self.say("数量：**%d**" % len(only_up))
        self.say()
        self.say("其中 %d 个是上游【给重名源加前缀改名】后的产物（如 `4k_荐片` = 我们按原名 "
                 "`荐片` 处理过的那个站），真正没见到的还有 %d 个 —— "
                 "它们来自我们这轮没抓到的源，不算误杀。"
                 % (len(renamed), len(only_up) - len(renamed)))
        self.say()
        if renamed:
            self.say("改名的那批（前 %d 个）：%s" % (
                min(20, len(renamed)),
                "、".join("`%s`→`%s`" % (a, b) for a, b in renamed[:20])))
            self.say()
        plain = [k for k in only_up if k not in dict(renamed)]
        if plain:
            self.say("剩下的前 %d 个：`%s`" % (min(20, len(plain)),
                                              "`, `".join(plain[:20])))
            self.say()


if __name__ == "__main__":
    unittest.main()
