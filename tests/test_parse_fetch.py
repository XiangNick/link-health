# -*- coding: utf-8 -*-
"""第一层：解析 / IDN / 抓取（全部离线，假 urlopen）。

为什么这几件事值得单独立一个文件：
  · 上游源发回来的东西【不是规范 JSON】是常态（BOM、行注释、多段拼接、控制字符），
    这里每一条都是真的踩过的坑 —— BOM 那一条今天就踩了三次；
  · IDN 那两个函数是"能不能发出这个请求"的唯一关口：
    host 没转 punycode、或者 path/query 里的中文没做百分号编码，
    http.client 会直接抛 UnicodeEncodeError，整个源就废了；
  · fetch_text 的重试边界（4xx 不重试、空响应/挑战页重试）决定了报告里
    能不能看出"到底是对方挂了还是被反爬了"，所以每条都单独钉住。
"""

import json
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import _helpers as H  # noqa: E402

build = H.build


class StripJsCommentsTest(unittest.TestCase):
    def test_removes_whole_line_comments(self):
        """整行 // 注释必须剔掉：上游接口会在 JSON 后面追加免责声明。"""
        text = '// 免责声明：仅供学习\n{"a": 1}'
        self.assertEqual(build._strip_js_comments(text), '{"a": 1}')

    def test_removes_indented_comment_lines(self):
        """只判 lstrip 后的前缀：注释前面有缩进也必须认出来。"""
        self.assertEqual(build._strip_js_comments("   \t// x\n{}"), "{}")

    def test_keeps_double_slash_inside_a_line(self):
        """行中间出现的 // 不能当注释：URL 里的 http:// 就是两个斜杠。"""
        text = '{"a": "http://x.com/y"}'
        self.assertEqual(build._strip_js_comments(text), text)

    def test_keeps_every_non_comment_line_in_order(self):
        """非注释行一条都不能少、顺序不能变 —— 少一行就是把 JSON 改坏了。"""
        text = "{\n// c\n  \"a\": 1,\n// d\n  \"b\": 2\n}"
        self.assertEqual(build._strip_js_comments(text),
                         '{\n  "a": 1,\n  "b": 2\n}')


class ParseJsonLenientTest(unittest.TestCase):
    def test_parses_bom_prefixed_json(self):
        """带 BOM 的 JSON 必须能解析 —— 今天踩了三次：BOM 会让 json.loads 直接报
        "Expecting value: line 1 column 1"，而文件肉眼看上去完全正常。"""
        self.assertEqual(build.parse_json_lenient('\ufeff{"a": 1}'), {"a": 1})

    def test_parses_bom_then_whitespace(self):
        """BOM 后面还跟了换行/空格也要能解析（顺序是先去 BOM 再 strip）。"""
        self.assertEqual(build.parse_json_lenient('\ufeff\n  {"a": 1}  '), {"a": 1})

    def test_parses_json_with_line_comments(self):
        self.assertEqual(build.parse_json_lenient('// c\n{"a": 1}'), {"a": 1})

    def test_takes_first_segment_of_concatenated_json(self):
        """多段 JSON 拼接（上游把两份配置直接粘在一起）取第一段。"""
        self.assertEqual(build.parse_json_lenient('{"a": 1}{"b": 2}'), {"a": 1})

    def test_takes_first_segment_across_lines(self):
        self.assertEqual(build.parse_json_lenient('{"a": 1}\n{"b": 2}\n'), {"a": 1})

    def test_allows_unescaped_control_chars(self):
        """带未转义控制字符的正文只有 strict=False 才收得下；真实接口里出现过制表符。"""
        self.assertEqual(build.parse_json_lenient('{"a": "x\ty"}'), {"a": "x\ty"})

    def test_parses_plain_json(self):
        self.assertEqual(build.parse_json_lenient('{"sites": [], "spider": "u"}'),
                         {"sites": [], "spider": "u"})

    def test_parses_top_level_array(self):
        self.assertEqual(build.parse_json_lenient("[1, 2]"), [1, 2])

    def test_raises_on_empty_text(self):
        with self.assertRaises(json.JSONDecodeError):
            build.parse_json_lenient("")

    def test_raises_on_garbage(self):
        with self.assertRaises(json.JSONDecodeError):
            build.parse_json_lenient("<html>反爬挑战页</html>")

    def test_raises_on_truncated_json(self):
        """解析失败的异常类型必须是 JSONDecodeError：fetch_text 靠它给错误补正文开头。"""
        with self.assertRaises(json.JSONDecodeError):
            build.parse_json_lenient('{"a": }')


class IdnaHostTest(unittest.TestCase):
    def test_ascii_passes_through(self):
        self.assertEqual(build._idna_host("a.example.com"), "a.example.com")

    def test_chinese_becomes_punycode(self):
        out = build._idna_host("肥猫.com")
        self.assertTrue(out.startswith("xn--"))
        self.assertTrue(out.endswith(".com"))
        self.assertTrue(out.isascii())

    def test_already_punycode_is_not_encoded_twice(self):
        """已经是 punycode 的域名不能被二次编码成 xn--xn--…（那是个不存在的域名）。"""
        once = build._idna_host("肥猫.com")
        self.assertEqual(build._idna_host(once), once)

    def test_invalid_label_falls_back_to_original(self):
        """非法标签（空标签）时原样返回而不是抛异常：抓取失败该由 fetch 报，不该在这里炸。"""
        self.assertEqual(build._idna_host("a..b"), "a..b")

    def test_trailing_dot_falls_back_to_original(self):
        self.assertEqual(build._idna_host("a.example.com."), "a.example.com.")


class IdnEncodeTest(unittest.TestCase):
    def test_chinese_host_becomes_punycode(self):
        out = build.idn_encode("http://肥猫.com/tv")
        self.assertEqual(out, "http://%s/tv" % build._idna_host("肥猫.com"))

    def test_keeps_userinfo(self):
        """user:pass@ 必须原样保留 —— 草稿版本的重写把 userinfo 整段丢了，
        结果是一个"看起来对、其实少了认证"的 URL。"""
        out = build.idn_encode("http://user:pass@肥猫.com/x")
        self.assertTrue(out.startswith("http://user:pass@"), out)
        self.assertTrue(out.endswith("/x"), out)

    def test_quotes_non_ascii_userinfo(self):
        out = build.idn_encode("http://用户:密码@肥猫.com/x")
        self.assertTrue(out.startswith(
            "http://%E7%94%A8%E6%88%B7:%E5%AF%86%E7%A0%81@"), out)

    def test_keeps_port(self):
        out = build.idn_encode("http://肥猫.com:8080/tv")
        self.assertTrue(out.endswith(":8080/tv"), out)

    def test_keeps_port_with_userinfo(self):
        out = build.idn_encode("http://u:p@肥猫.com:8080/tv")
        self.assertEqual(out, "http://u:p@%s:8080/tv" % build._idna_host("肥猫.com"))

    def test_punycode_host_is_untouched(self):
        url = "http://xn--zp8-mf3g9f.v.nxog.top/m/"
        self.assertEqual(build.idn_encode(url), url)

    def test_ascii_url_is_untouched(self):
        url = "http://a.example.com/p?q=1#f"
        self.assertEqual(build.idn_encode(url), url)

    def test_path_and_query_are_left_alone(self):
        """idn_encode 只负责 host：path/query 的中文留给 idn_encode_best 做百分号编码，
        两个函数分工不能互相越界。"""
        self.assertEqual(build.idn_encode("http://a.com/中文?q=中文#片段"),
                         "http://a.com/中文?q=中文#片段")

    def test_url_without_host_is_returned_as_is(self):
        """csp_XXX / 相对路径这类没有 host 的值不能抛异常，原样返回让上层去报错。"""
        for value in ("csp_Foo", "/m/111.php", "http://", ""):
            self.assertEqual(build.idn_encode(value), value)


class IdnEncodeBestTest(unittest.TestCase):
    def test_chinese_host_and_chinese_query_both_handled(self):
        out = build.idn_encode_best("http://肥猫.com/111.php?ou=公众号")
        self.assertTrue("xn--" in out)
        self.assertTrue(out.isascii(), out)

    def test_ascii_url_passes_through(self):
        url = "http://xhztv.top/4k.json"
        self.assertEqual(build.idn_encode_best(url), url)

    def test_existing_percent_escapes_are_not_double_encoded(self):
        """已经百分号编码过的查询串不能再编一次（%E5 变成 %25E5 就是一个 404 的地址）。"""
        url = "http://a.com/111.php?ou=%E5%85%AC%E4%BC%97%E5%8F%B7"
        self.assertEqual(build.idn_encode_best(url), url)

    def test_result_is_always_ascii_encodable(self):
        """这条函数存在的唯一理由：http.client 拿到非 ascii 就抛 UnicodeEncodeError。
        所以对一批真实形态的 URL，返回值必须能 encode("ascii")。"""
        urls = [
            "http://www.mpanso.com/小米/DEMO.json",
            "http://我不是摸鱼儿.top",
            "https://欧歌.v.nxog.top/m/",
            "https://xn--zp8-mf3g9f.v.nxog.top/m/111.php?ou=公众号欧歌app&mz=index"
            "&jar=index&123&b=欧歌zp8",
            "http://a.com/#片段",
            "http://a.com/a b",
        ]
        for u in urls:
            out = build.idn_encode_best(u)
            self.assertTrue(out.isascii(), "%r -> %r" % (u, out))
            out.encode("ascii")

    def test_keeps_percent_in_path(self):
        url = "http://a.com/%E5%B0%8F%E7%B1%B3/DEMO.json"
        self.assertEqual(build.idn_encode_best(url), url)


class FetchTextTest(unittest.TestCase):
    def _call(self, mapping, url, **kw):
        opener = H.FakeOpener(mapping)
        import unittest.mock as mock
        with mock.patch.object(build.urllib.request, "urlopen", opener), H.fast_time():
            try:
                return build.fetch_text(url), opener
            except Exception as e:  # noqa: BLE001 - 用例要同时看异常和调用记录
                e.opener = opener
                raise

    def test_returns_body_and_sends_ua(self):
        url = "http://a.com/x.json"
        body = '{"sites": []}'
        out, opener = self._call({url: body}, url)
        self.assertEqual(out, body)
        self.assertEqual(opener.calls[0]["headers"].get("User-agent"), build.UA)
        self.assertEqual(opener.calls[0]["timeout"], build.TIMEOUT)

    def test_encodes_url_before_requesting(self):
        url = "http://肥猫.com/中文?q=公众号"
        target = build.idn_encode_best(url)
        out, opener = self._call({target: "{}"}, url)
        self.assertEqual(out, "{}")
        self.assertEqual(opener.urls, [target])
        self.assertTrue(target.isascii())

    def test_empty_body_is_retried_then_succeeds(self):
        """空响应（0 字节）当成"可能被反爬冷却"，重试一次；这是重试存在的第二个理由。"""
        url = "http://a.com/x.json"
        out, opener = self._call({url: [b"", '{"a": 1}']}, url)
        self.assertEqual(out, '{"a": 1}')
        self.assertEqual(len(opener.calls), 2)

    def test_challenge_page_is_retried(self):
        """返回挑战页 HTML 的接口还有第二次机会，不是一把判死。"""
        url = "http://a.com/x.json"
        out, opener = self._call({url: ["<html>challenge</html>", '{"a": 1}']}, url)
        self.assertEqual(out, '{"a": 1}')
        self.assertEqual(len(opener.calls), 2)

    def test_http_error_is_not_retried(self):
        """4xx/5xx 是确定性失败，重试只是浪费时间（也给对方留点余地）。"""
        url = "http://a.com/x.json"
        err = urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        with self.assertRaises(RuntimeError) as cm:
            self._call({url: err}, url)
        opened = cm.exception.opener
        self.assertEqual(len(opened.calls), 1)
        self.assertEqual(len(opened.calls), 1)

    def test_http_error_message_carries_url_and_status(self):
        """错误信息必须带 URL：备用地址全 403 时，报告里不能出现一堆一样的
        "HTTP Error 403: Forbidden"（等于没有信息）。"""
        url = "http://a.com/x.json"
        err = urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        with self.assertRaises(RuntimeError) as cm:
            self._call({url: err}, url)
        msg = str(cm.exception)
        self.assertIn(url, msg)
        self.assertIn("403", msg)
        self.assertIn("Forbidden", msg)

    def test_parse_failure_is_retried_up_to_max_retries(self):
        url = "http://a.com/x.json"
        with self.assertRaises(RuntimeError) as cm:
            self._call({url: ["nope", "nope"]}, url)
        self.assertEqual(len(cm.exception.opener.calls), build.MAX_RETRIES)

    def test_parse_failure_message_includes_body_head(self):
        """解析类失败要补一段正文开头：反爬挑战页 / 停机公告页一眼就能认出来。"""
        url = "http://a.com/x.json"
        with self.assertRaises(RuntimeError) as cm:
            self._call({url: "<html>\n  请稍候  </html>"}, url)
        msg = str(cm.exception)
        self.assertIn(url, msg)
        self.assertIn("正文开头", msg)
        # 连续空白被压成一个空格，日志才不会因为正文里的换行散架
        self.assertIn("<html> 请稍候 </html>", msg)

    def test_connection_error_is_reported_with_type(self):
        url = "http://a.com/x.json"
        with self.assertRaises(RuntimeError) as cm:
            self._call({url: [OSError("连接被重置"), OSError("连接被重置")]}, url)
        self.assertIn("OSError", str(cm.exception))
        self.assertIn(url, str(cm.exception))


class FetchBytesTest(unittest.TestCase):
    def test_returns_bytes_with_generous_timeout(self):
        """jar 是最大的那个响应，超时给足（至少 60 秒），否则大 jar 永远抓不完。"""
        url = "http://cdn.example/jar.png"
        opener = H.FakeOpener({url: b"\x89PNGzip"})
        import unittest.mock as mock
        with mock.patch.object(build.urllib.request, "urlopen", opener):
            out = build.fetch_bytes(url)
        self.assertEqual(out, b"\x89PNGzip")
        self.assertGreaterEqual(opener.calls[0]["timeout"], 60)
        self.assertEqual(opener.calls[0]["headers"].get("User-agent"), build.UA)

    def test_encodes_url_before_requesting(self):
        url = "http://肥猫.com/jar.png"
        target = build.idn_encode_best(url)
        opener = H.FakeOpener({target: b"x"})
        import unittest.mock as mock
        with mock.patch.object(build.urllib.request, "urlopen", opener):
            build.fetch_bytes(url)
        self.assertEqual(opener.urls, [target])


if __name__ == "__main__":
    unittest.main()
