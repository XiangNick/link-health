# -*- coding: utf-8 -*-
"""测试用例的公共夹具。

【为什么要单独一个模块】
  · 被测脚本 build.py 放在 scripts/ 下（不是包），每个用例文件都要先把路径接上；
  · 假 HTTP 响应 / 假 jar / 临时沙箱目录这三样夹具被好几个用例文件复用，
    复制多份就会出现"改了夹具只改一处"的隐患 —— 测试自己先腐烂掉。
本文件以 _ 开头、不以 test 开头，unittest discover 不会把它当用例收集。
"""

import contextlib
import io
import json
import os
import shutil
import struct
import sys
import types
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
TEMPLATE_SRC = os.path.join(SCRIPTS, "report_template.html")
for _p in (SCRIPTS, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import build  # noqa: E402


# ────────────────────────────────────────────────────────────
# 假 HTTP
# ────────────────────────────────────────────────────────────

class FakeResp:
    """urlopen 返回值的替身：只实现 build.py 用到的那几个方法。"""

    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """替换 urllib.request.urlopen。

    值可以是 bytes/str（当正文）、Exception 实例（当抛出）、或 list（按顺序逐个消费，
    用来模拟"第一次空响应、第二次成功"这种序列）。
    另外把每次请求的 URL / 头 / 超时都记下来 —— 重试次数、UA、超时这类约定只能靠这个验。
    """

    def __init__(self, mapping):
        self.mapping = dict(mapping)
        self.calls = []

    def __call__(self, req, timeout=None, **kw):
        url = getattr(req, "full_url", None) or req
        headers = dict(getattr(req, "headers", None) or {})
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        if url not in self.mapping:
            raise AssertionError("用例没给这个地址准备响应：%r" % (url,))
        val = self.mapping[url]
        if isinstance(val, list):
            val = val.pop(0) if val else RuntimeError("响应队列被取空了：%s" % url)
        if isinstance(val, Exception):
            raise val
        if isinstance(val, str):
            val = val.encode("utf-8")
        return FakeResp(val)

    @property
    def urls(self):
        return [c["url"] for c in self.calls]


class FakeNet:
    """给 main() 用的假网络。

    和 FakeOpener 的分工：FakeOpener 是给 fetch_text / fetch_bytes 自己写用例用的
    （要验重试和 header）；这里是给"整条 main 流程"用的，按 URL 直接给结果，
    省得为每个用例编造 403/挑战页。两次运行共用一个 FakeNet 也没问题（是无状态的查表）。
    """

    def __init__(self, texts=None, blob=None, blobs=None):
        self.texts = dict(texts or {})
        self.blob = blob
        # blobs：按 URL 分别给不同的 jar 字节。给 scheme 改写那类用例用 ——
        # 同一个 jar 的 http 和 https 地址必须能分别返回（可以一致、也可以故意不一致）。
        self.blobs = dict(blobs or {})
        self.text_calls = []
        self.byte_calls = []

    def fetch_text(self, url):
        self.text_calls.append(url)
        val = self.texts.get(url)
        if isinstance(val, Exception):
            raise val
        if val is None:
            raise RuntimeError("用例没给这个地址准备响应：%s" % url)
        return val

    def fetch_bytes(self, url):
        self.byte_calls.append(url)
        if url in self.blobs:
            val = self.blobs[url]
        else:
            val = self.blob
        if isinstance(val, Exception):
            raise val
        return val

    def install(self):
        """返回 (patcher 列表)，调用方负责 start/stop。"""
        from unittest import mock
        return [mock.patch.object(build, "fetch_text", self.fetch_text),
                mock.patch.object(build, "fetch_bytes", self.fetch_bytes)]


def fast_time():
    """把 build 里的 time 换成一个 sleep 是空操作的替身。

    重试退避实测 3~6 秒、源间隔 3 秒，真睡的话"第一层测试 < 2 秒"这条硬要求就没了，
    也会让整套用例慢到没人愿意跑。sleep 只影响节奏，不影响被测逻辑。
    """
    return _Patch(build, "time", types.SimpleNamespace(sleep=lambda *a, **k: None))


class _Patch:
    """极简的 patch：__enter__ 换掉属性，__exit__ 换回来。"""

    def __init__(self, obj, name, value):
        self.obj, self.name, self.value, self.old = obj, name, value, None

    def __enter__(self):
        self.old = getattr(self.obj, self.name)
        setattr(self.obj, self.name, self.value)
        return self.value

    def __exit__(self, *exc):
        setattr(self.obj, self.name, self.old)
        return False


@contextlib.contextmanager
def capture_stdout():
    """接住 print：build.py 的降级路径全靠日志说话，用例要能断言它确实说了。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


# ────────────────────────────────────────────────────────────
# 假 jar（真的去下一个 4 MB 的 jar 就不可能离线、也不可能 2 秒跑完）
# ────────────────────────────────────────────────────────────

def _uleb(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def make_dex(strings):
    """拼一个最小可用的 classes.dex。

    只做实 build.jar_class_names 真正读的那部分：头里偏移 0x38 的 string_ids_size、
    0x3C 的 string_ids_off，然后一张 (偏移 -> ULEB128 长度 + utf8 + 0x00) 的字符串表。
    这样"读类名"这条路径（含 ULEB128 续字节、非描述符串、内嵌 $ 的内部类）能离线钉死。
    """
    ids = bytearray()
    body = bytearray()
    off = 0x70
    cur = off + 4 * len(strings)
    for s in strings:
        raw = s.encode("utf-8")
        blob = _uleb(len(raw)) + raw + b"\x00"
        ids += struct.pack("<I", cur)
        body += blob
        cur += len(blob)
    head = bytearray(0x70)
    head[0x38:0x3C] = struct.pack("<I", len(strings))
    head[0x3C:0x40] = struct.pack("<I", off)
    return bytes(head) + bytes(ids) + bytes(body)


def zip_jar(dex=None, extra=None):
    """把 dex 装成一个 zip（真实世界里它伪装成 .png）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if dex is not None:
            z.writestr("classes.dex", dex)
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def jar_of_classes(names, package="com/github/catvod/spider"):
    """按"类名清单"造一个 jar，用于测类存在性校验。"""
    descs = ["L%s/%s;" % (package, n) for n in names]
    return zip_jar(make_dex(descs))


# ────────────────────────────────────────────────────────────
# 沙箱：把 build.py 的输入输出指到临时目录
# ────────────────────────────────────────────────────────────

_PATH_ATTRS = ("SOURCES_FILE", "FILTER_FILE", "TEMPLATE_FILE",
               "OUT_CONFIG", "OUT_STATUS", "OUT_REPORT")


class Sandbox:
    def __init__(self, root, copy_template=True, template_text=None):
        self.root = root
        self.sources = os.path.join(root, "sources.json")
        self.filter = os.path.join(root, "filter.json")
        self.template = os.path.join(root, "report_template.html")
        self.config = os.path.join(root, "config.json")
        self.status = os.path.join(root, "status.json")
        self.report = os.path.join(root, "report.html")
        if template_text is not None:
            self.write(self.template, template_text)
        elif copy_template:
            shutil.copyfile(TEMPLATE_SRC, self.template)

    # 小工具：读写 JSON / 文本
    def write(self, path, text):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def write_json(self, path, obj):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)

    def read_json(self, path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def exists(self, path):
        return os.path.exists(path)

    def write_sources(self, obj, path=None):
        self.write_json(path or self.sources, obj)

    def write_filter(self, obj, path=None):
        self.write_json(path or self.filter, obj)

    def read_config(self):
        return self.read_json(self.config)

    def read_status(self):
        return self.read_json(self.status)

    def read_filter(self):
        return self.read_json(self.filter)

    def read_report(self):
        with open(self.report, encoding="utf-8") as f:
            return f.read()


@contextlib.contextmanager
def sandbox(**kw):
    """把 build 的 6 个路径常量指到一个临时目录，退出时还原。

    为什么每个碰文件的用例都必须钻进来：build.py 的输入输出全是模块级常量，
    直接调 main() 会把仓库里真正要提交的 config/filter.json、config.json、
    status.json、report.html 覆盖掉 —— 跑一次测试就把工作区改脏，比不测还糟。
    """
    import tempfile
    saved = {a: getattr(build, a) for a in _PATH_ATTRS}
    root = tempfile.mkdtemp(prefix="lh-test-")
    sb = Sandbox(root, **kw)
    build.SOURCES_FILE = sb.sources
    build.FILTER_FILE = sb.filter
    build.TEMPLATE_FILE = sb.template
    build.OUT_CONFIG = sb.config
    build.OUT_STATUS = sb.status
    build.OUT_REPORT = sb.report
    try:
        yield sb
    finally:
        for a, v in saved.items():
            setattr(build, a, v)
        shutil.rmtree(root, ignore_errors=True)


# ────────────────────────────────────────────────────────────
# 造数据的小工具
# ────────────────────────────────────────────────────────────

def sites(n, prefix="k", api="py_demo", name=None, start=0):
    """造 n 个合法 site（有 key）。默认用 py_ 前缀的 api —— 那不会走类存在性校验，
    专门测"类不在包里"的用例请自己传 api="csp_Xxx"。"""
    return [{"key": "%s%d" % (prefix, i), "name": name or ("站%s%d" % (prefix, i)),
             "api": api} for i in range(start, start + n)]


def payload(n, prefix="k", api="py_demo", spider="http://cdn.example/jar.png;md5;abc"):
    return {"spider": spider, "sites": sites(n, prefix=prefix, api=api)}


@contextlib.contextmanager
def argv(*args):
    """替换 sys.argv，让 main() 以为自己是被命令行调起来的。"""
    old = sys.argv
    sys.argv = ["build.py"] + [str(a) for a in args]
    try:
        yield
    finally:
        sys.argv = old
