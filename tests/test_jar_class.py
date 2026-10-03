# -*- coding: utf-8 -*-
"""第一层：读 jar 的类清单，以及 api -> 类名的映射。

为什么这是整套测试里最要紧的一块：
  App 加载 site 的代码是纯字符串拼接
      loader.loadClass("com.github.catvod.spider." + api.split("csp_")[1])
  没有任何降级或回退 —— "类不在 jar 里"就等于"这个源必然失败"。
  所以这个函数判错的方向只有两种，两种都很贵：
    · 判宽了（该剔的没剔）→ 用户点开就是白等一次超时；
    · 判严了（一个好站被当成没有类）→ 我们白白砍掉能用的源，还解释不出理由。
  下面连"读不出来（空集合）"这种最容易静默犯错的情况都单独钉了一条。
"""

import os
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import _helpers as H  # noqa: E402

build = H.build


class JarClassNamesTest(unittest.TestCase):
    def test_reads_all_class_names(self):
        jar = H.jar_of_classes(["WexdiyGuard", "BiliGuard", "PanWebShare"])
        self.assertEqual(build.jar_class_names(jar),
                         {"WexdiyGuard", "BiliGuard", "PanWebShare"})

    def test_inner_class_keeps_dollar(self):
        """dex 里内部类的描述符是 Lcom/x/Foo$1;，这里【不做】截断（截断是 class_name_of 的事），
        否则就拿不到"jar 里到底有没有这个内部类"这个事实了。"""
        jar = H.jar_of_classes(["XBPQ", "XBPQ$1"])
        self.assertEqual(build.jar_class_names(jar), {"XBPQ", "XBPQ$1"})

    def test_long_name_exercises_uleb_continuation(self):
        """长度 >= 128 的字符串，长度字段是两字节 ULEB128：
        少读一个续字节，后面所有偏移全错，读出来的类名会变成一片乱码。"""
        long_cls = "A" * 200
        jar = H.jar_of_classes(["短", long_cls])
        names = build.jar_class_names(jar)
        self.assertIn("短", names)
        self.assertIn(long_cls, names)

    def test_non_descriptor_strings_are_ignored(self):
        """dex 字符串表里绝大部分是普通字符串（方法名、常量），不能当成类名。"""
        dex = H.make_dex([
            "Lcom/a/Foo;",             # 类
            "hello world",             # 普通字符串
            "LNoSlash;",               # 没有 / 的描述符：不是包里的类
            "Lcom/a/Foo",              # 结尾没有 ;：不是类型描述符
            "V",                       # 基本类型
            "Lcom/b/Bar;",             # 又一个类
        ])
        self.assertEqual(build.jar_class_names(H.zip_jar(dex)), {"Foo", "Bar"})

    def test_raises_when_no_class_name_can_be_read(self):
        """【安全底线】空集合必须当失败抛出去。

        调用方 main 是用 None 判"jar 读不出来 → 跳过类存在性校验"的，
        如果这里安静地返回空集合，所有 csp_ 站都会因为"类不在空集合里"被判死 ——
        一次 dex 格式变化就能产出一份空配置，而且日志上完全看不出异常。"""
        dex = H.make_dex(["hello", "world"])
        with self.assertRaises(ValueError):
            build.jar_class_names(H.zip_jar(dex))

    def test_raises_on_truncated_dex(self):
        """截断的 dex（只有 8 字节）读不出 string_ids 表，同样是"读失败"而不是"没有类"。"""
        with self.assertRaises(ValueError):
            build.jar_class_names(H.zip_jar(b"\x00" * 8))

    def test_raises_when_classes_dex_missing(self):
        """jar 在但里面没有 classes.dex -> KeyError，由 main 兜住并跳过静态筛。"""
        jar = H.zip_jar(None, extra={"AndroidManifest.xml": b"<xml/>"})
        with self.assertRaises(KeyError):
            build.jar_class_names(jar)

    def test_raises_on_non_zip(self):
        """真实的 .png 伪装成 jar 时其实是 zip；真拿到一张 png 也要抛出 BadZipFile。"""
        with self.assertRaises(zipfile.BadZipFile):
            build.jar_class_names(b"\x89PNG\r\n\x1a\n this is not a zip")

    def test_multi_string_dex_with_many_entries(self):
        """几百个类的真实规模（示例里 880 个）：偏移表读错一格就会串行。"""
        names = ["C%03dGuard" % i for i in range(300)]
        self.assertEqual(build.jar_class_names(H.jar_of_classes(names)), set(names))


class SpiderUrlOfTest(unittest.TestCase):
    def test_strips_md5_suffix(self):
        """配置里 spider 常常写成 "url;md5;摘要"，拿它去请求会 404。"""
        self.assertEqual(build.spider_url_of("https://a/b.png;md5;abc123"),
                         "https://a/b.png")

    def test_returns_value_as_is_without_md5(self):
        self.assertEqual(build.spider_url_of("https://a/b.png"), "https://a/b.png")

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(build.spider_url_of("  https://a/b.png;md5;x  "),
                         "https://a/b.png")

    def test_empty_and_none_give_empty_string(self):
        for v in ("", None):
            self.assertEqual(build.spider_url_of(v), "")

    def test_only_first_md5_segment_is_used(self):
        self.assertEqual(build.spider_url_of("https://a/b.png;md5;a;md5;b"),
                         "https://a/b.png")


class ClassNameOfTest(unittest.TestCase):
    def test_csp_prefix_is_stripped(self):
        self.assertEqual(build.class_name_of("csp_AppRJ"), "AppRJ")

    def test_inner_class_is_truncated_at_dollar(self):
        """csp_XBPQ$1 加载的是内部类，但身份按外层类算：dex 里通常两者都在。"""
        self.assertEqual(build.class_name_of("csp_XBPQ$1"), "XBPQ")

    def test_other_loaders_return_none(self):
        """py_ / js: / assets: 走的是别的加载器，判不了 —— 返回 None 表示"放过"，不是"没有类"。"""
        for api in ("py_cctv_full", "js:xxx", "assets://a", "", "Csp_X"):
            self.assertIsNone(build.class_name_of(api))

    def test_none_returns_none(self):
        self.assertIsNone(build.class_name_of(None))

    def test_whitespace_is_stripped_first(self):
        self.assertEqual(build.class_name_of("  csp_AppRJ  "), "AppRJ")

    def test_bare_csp_prefix_gives_empty_string(self):
        """api 就是 "csp_"（上游偶尔有这种残条目）：约定返回空串而不是 None，
        空串会被当作"类不存在"剔除掉 —— 一个没有类名的 csp_ 站本来就是坏的。"""
        self.assertEqual(build.class_name_of("csp_"), "")

    def test_roundtrip_with_jar_class_names(self):
        """把 dex 读出来的类名和这个映射对上：验证过的那条链路要能自洽。"""
        jar = H.jar_of_classes(["AppRJ", "XBPQ"])
        classes = build.jar_class_names(jar)
        self.assertIn(build.class_name_of("csp_AppRJ"), classes)
        self.assertIn(build.class_name_of("csp_XBPQ$1"), classes)


if __name__ == "__main__":
    unittest.main()
