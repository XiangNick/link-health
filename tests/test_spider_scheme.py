# -*- coding: utf-8 -*-
"""spider 的 http→https 安全改写：三重校验的每个分支。

背景（为什么需要它）：
    有些源自己把 spider 写成 http://（王二小就是），而 App 的 JarLoader 会
    【直接拒绝明文 http 的 jar】（JarLoader.java:148 "rejected cleartext http jar"）。
    不改写的话，那份配置在 App 里一个站都跑不起来。
    但改写必须三重校验（http 开头 / https 能下载 / hash 与声明一致），
    否则 https 哪天挂了，反而把"至少还能用的 http jar"换成"根本下不来的 https"。

这些用例全部离线：网络是 FakeNet，文件系统是沙箱。
"""

import hashlib
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import _helpers as H  # noqa: E402

build = H.build


def py_sites(n, prefix):
    return H.sites(n, prefix=prefix, api="py_demo")


def doc(sites, spider):
    return json.dumps({"spider": spider, "sites": sites}, ensure_ascii=False)


HTTP_JAR = "http://cdn.x/jar.png"
HTTPS_JAR = "https://cdn.x/jar.png"
# 真的 jar 字节（一个含 classes.dex 的 zip）—— main 里要拿它解析类清单，
# 用随便一段 bytes 会在读类清单那一步炸掉，测不到想测的东西。
JAR_BYTES = H.jar_of_classes(["AppRJ"])
OTHER_BYTES = H.jar_of_classes(["SomethingElse"])


def md5_of(b):
    return hashlib.md5(b).hexdigest()


class SchemeRewriteUnitTest(unittest.TestCase):
    """直接测 https_equivalent() 这个函数本身。"""

    def setUp(self):
        self._sb_cm = H.sandbox()
        self.sb = self._sb_cm.__enter__()
        self.addCleanup(lambda: self._sb_cm.__exit__(None, None, None))

    def call(self, spider_value, blobs):
        with mock.patch.object(build, "fetch_bytes", H.FakeNet(blobs=blobs).fetch_bytes):
            return build.https_equivalent(spider_value)

    def urlof(self, value):
        return build.spider_url_of(value)

    # ── ① 三重校验全过 → 改写，且 hash 声明必须原样保留 ──
    def test_upgrades_when_https_serves_the_same_bytes(self):
        h = md5_of(JAR_BYTES)
        val = HTTP_JAR + ";md5;" + h
        out, note = self.call(val, {HTTPS_JAR: JAR_BYTES})
        self.assertEqual(self.urlof(out), HTTPS_JAR)
        self.assertIn("改写成功", note)
        # ★ 必须带上 hash：App 对 https 的 jar 要求必须带 ;md5;/;sha256;，
        #   不带就直接不加载（JarLoader.java:117-121）。这条断言是补的 —— 
        #   早先只断言 URL，结果 hash 被丢掉却没测出来。
        self.assertIn(";md5;" + h, out)

    def test_upgrades_with_sha256_too(self):
        h = hashlib.sha256(JAR_BYTES).hexdigest()
        val = HTTP_JAR + ";sha256;" + h
        out, note = self.call(val, {HTTPS_JAR: JAR_BYTES})
        self.assertEqual(self.urlof(out), HTTPS_JAR)
        self.assertIn("sha256", note)
        self.assertIn(";sha256;" + h, out)

    # ── ② https 拿不到 → 保持原样（关键：不能把唯一能用的弄丢）──
    def test_keeps_http_when_https_fails(self):
        val = HTTP_JAR + ";md5;" + md5_of(JAR_BYTES)
        out, note = self.call(val, {HTTPS_JAR: RuntimeError("域名解析不了")})
        self.assertEqual(self.urlof(out), HTTP_JAR)
        self.assertIn("https 探测失败", note)

    # ── ③ hash 不一致 → 保持原样 ──
    def test_keeps_http_when_hash_mismatch(self):
        val = HTTP_JAR + ";md5;" + md5_of(JAR_BYTES)
        out, note = self.call(val, {HTTPS_JAR: b"DIFFERENT CONTENT"})
        self.assertEqual(self.urlof(out), HTTP_JAR)
        self.assertIn("不一致", note)

    def test_keeps_http_when_hash_mismatch_sha256(self):
        val = HTTP_JAR + ";sha256;" + hashlib.sha256(JAR_BYTES).hexdigest()
        out, note = self.call(val, {HTTPS_JAR: b"DIFFERENT CONTENT"})
        self.assertEqual(self.urlof(out), HTTP_JAR)
        self.assertIn("不一致", note)

    # ── 没有声明 hash → 不许改写（没有比对依据时不能动）──
    def test_keeps_http_when_no_hash_declared(self):
        out, note = self.call(HTTP_JAR, {HTTPS_JAR: JAR_BYTES})
        self.assertEqual(self.urlof(out), HTTP_JAR)
        self.assertIn("没有声明", note)

    def test_keeps_http_when_hash_is_empty_placeholder(self):
        """';md5;' 后面是空的（测试夹具里很常见）—— 当成没声明，不许改写。"""
        out, note = self.call(HTTP_JAR + ";md5;", {HTTPS_JAR: JAR_BYTES})
        self.assertEqual(self.urlof(out), HTTP_JAR)
        self.assertIn("没有声明", note)

    # ── 不该动的：已经是 https / 空值 ──
    def test_leaves_https_untouched(self):
        val = HTTPS_JAR + ";md5;" + md5_of(JAR_BYTES)
        out, note = self.call(val, {HTTPS_JAR: JAR_BYTES})
        self.assertEqual(out, val)          # 一个字符都不动
        self.assertEqual(note, "")

    def test_leaves_empty_untouched(self):
        out, note = self.call("", {HTTPS_JAR: JAR_BYTES})
        self.assertEqual(out, "")
        self.assertEqual(note, "")


class SanitizedSpiderTest(unittest.TestCase):
    """sanitized_spider：有效 hash 必须保留，无效占位 hash 才清掉。"""

    def test_keeps_valid_md5(self):
        h = md5_of(JAR_BYTES)
        val = HTTPS_JAR + ";md5;" + h
        self.assertEqual(build.sanitized_spider(val), val)

    def test_keeps_valid_sha256(self):
        h = hashlib.sha256(JAR_BYTES).hexdigest()
        val = HTTPS_JAR + ";sha256;" + h
        self.assertEqual(build.sanitized_spider(val), val)

    def test_strips_invalid_placeholder_hash(self):
        """';md5;aaa' 不是 32 位十六进制 —— 留着只会让人误以为"有校验"。"""
        self.assertEqual(build.sanitized_spider(HTTPS_JAR + ";md5;aaa"), HTTPS_JAR)

    def test_strips_truncated_hash(self):
        self.assertEqual(build.sanitized_spider(HTTPS_JAR + ";md5;c3e8c08b"), HTTPS_JAR)

    def test_converts_idn_host(self):
        out = build.sanitized_spider("https://肥猫.com/jar.png;md5;" + md5_of(JAR_BYTES))
        self.assertTrue(out.startswith("https://xn--"), out)


class SchemeRewriteInMainTest(unittest.TestCase):
    """整条 main 跑一遍：改写后的 https 必须【同时】用于读 jar 和写 config.json。"""

    def setUp(self):
        self._sb_cm = H.sandbox()
        self.sb = self._sb_cm.__enter__()
        self.addCleanup(lambda: self._sb_cm.__exit__(None, None, None))
        self._time = H.fast_time()
        self._time.__enter__()
        self.addCleanup(lambda: self._time.__exit__(None, None, None))

    def run_main(self, sources_doc, url_map, blobs=None, blob=None):
        from unittest import mock
        self.sb.write_sources(sources_doc)
        self.sb.write_filter({})
        net = H.FakeNet(url_map, blob=blob, blobs=blobs)
        with mock.patch.object(build, "fetch_text", net.fetch_text), \
                mock.patch.object(build, "fetch_bytes", net.fetch_bytes):
            with H.argv():
                with H.capture_stdout() as out:
                    code = build.main()
        self.net = net
        return code, out.getvalue()

    def test_main_writes_the_https_spider_and_uses_it_for_the_class_list(self):
        """源里写的是 http，三重校验过 → config.json 里必须是 https，
        而且读类清单用的也必须是同一个 https 地址（否则静态筛和 App 不一致）。"""
        spider = HTTP_JAR + ";md5;" + md5_of(JAR_BYTES)
        sources = {"spider_source": "A",
                   "sources": [{"id": "A", "name": "甲源", "urls": ["http://a/1.json"]}]}
        code, log = self.run_main(
            sources,
            {"http://a/1.json": doc(py_sites(6, "a"), spider)},
            blobs={HTTPS_JAR: JAR_BYTES})
        self.assertEqual(code, 0, log)

        cfg = self.sb.read_config()
        self.assertTrue(cfg["spider"].startswith("https://"), cfg["spider"])
        self.assertEqual(build.spider_url_of(cfg["spider"]), HTTPS_JAR)

        st = self.sb.read_status()
        self.assertEqual(st["spider"]["url"], HTTPS_JAR)
        self.assertIn("改写成功", st["spider"]["note"])

        # https 地址出现过（探测 + 读类清单），源里的 http 不该被拿去读 jar
        self.assertIn(HTTPS_JAR, self.net.byte_calls)
        self.assertEqual(self.net.byte_calls.count(HTTPS_JAR), 2,
                         "https 应该被调用两次：一次探测 hash、一次读类清单")

    def test_main_keeps_http_spider_when_https_hash_differs(self):
        """https 给的内容和声明不一致 → 保持源里的 http，并说明原因。

        注意 http 那份必须是【能解析的好 jar】：否则会先在"读类清单"那一步失败，
        spider_note 被覆盖成"jar 读取失败"，就测不到 scheme 改写的结论了。
        """
        spider = HTTP_JAR + ";md5;" + md5_of(JAR_BYTES)
        sources = {"spider_source": "A",
                   "sources": [{"id": "A", "name": "甲源", "urls": ["http://a/1.json"]}]}
        code, log = self.run_main(
            sources,
            {"http://a/1.json": doc(py_sites(6, "a"), spider)},
            blobs={HTTPS_JAR: OTHER_BYTES, HTTP_JAR: JAR_BYTES})
        self.assertEqual(code, 0, log)

        cfg = self.sb.read_config()
        self.assertTrue(cfg["spider"].startswith("http://"), cfg["spider"])
        self.assertEqual(build.spider_url_of(cfg["spider"]), HTTP_JAR)
        self.assertIn("不一致", self.sb.read_status()["spider"]["note"])
        # 声明了但校验不过的 hash 不该留在配置里（留着只会让人以为"有校验"）
        self.assertNotIn(";md5;", cfg["spider"])


if __name__ == "__main__":
    unittest.main()
