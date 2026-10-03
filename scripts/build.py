#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build.py —— 外部来源快照构建器

抓取多个公开源 → 校验 → 清洗 → 合并成一个单仓配置，并渲染一份人看的构建报告。

【为什么只用标准库】
脚本在 GitHub Actions 里每天跑。多一个第三方依赖，就多一份"某天 pip 装不上、
整条流水线变红"的风险。这里要用的能力（HTTP / JSON / 正则 / zip / HTML 渲染）
标准库全都有。

用法：
    python scripts/build.py --demo            用内置样例数据渲染报告（不联网、不抓取）
    python scripts/build.py                   正常联网构建
    python scripts/build.py --cache-dir DIR   网络失败时从 DIR/<sid>.json 兜底，
                                              并把本次成功的结果写回该目录（滚动缓存）

输入（仓库里手工维护的文件）：
    config/sources.json    上游源清单 + 【显式指定谁提供 spider】
    config/filter.json     每轮实时生效的过滤规则（脚本只读【不写】）

输出（脚本生成，不要手改）：
    config.json     给 App 读的聚合单仓（spider + home + sites + warningText/合并字段）
    status.json     构建状态（机器可读；下游源一层、站点一层都写清楚）
    report.html     构建报告（人看的，图文并茂）
"""

import argparse
import datetime
import hashlib
import html
import json
import os
import re
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from io import BytesIO

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_FILE = os.path.join(ROOT, "config", "sources.json")
FILTER_FILE = os.path.join(ROOT, "config", "filter.json")
TEMPLATE_FILE = os.path.join(ROOT, "scripts", "report_template.html")
OUT_CONFIG = os.path.join(ROOT, "config.json")
OUT_STATUS = os.path.join(ROOT, "status.json")
OUT_REPORT = os.path.join(ROOT, "report.html")

TZ = datetime.timezone(datetime.timedelta(hours=8))
GENERATOR = "yingshi-build/1.0"

# 复刻上游调好的那几个参数（这几条它写得是对的，值得沿用）
UA = "Mozilla/5.0"          # 完整 Chrome UA 反而会被下发反爬挑战页
TIMEOUT = 20
MAX_RETRIES = 2
RETRY_BACKOFF = 3
URL_INTERVAL = 1.5
SOURCE_INTERVAL = 3

# 一个"够用"的配置至少要有这么多个站点。上游只判 any(...)，空数组也照收。
MIN_SITES = 5

# 合并时这些字段不进 base，之后统一按列表合并（含 ads，不能漏）。
# ⚠️ lives（直播）【故意不在列表里】：这个配置是给家里老人看影视剧用的，
#    直播那套用不上、也不想要，所以最终产物里不要出现 lives。
#    SKIP_FROM_BASE 里带着它 → 连 base 那一份的 lives 也不会被带进结果。
MERGE_FIELDS = ("parses", "doh", "rules", "flags", "exts", "ads")
SKIP_FROM_BASE = {"sites", "lives", *MERGE_FIELDS}

# 明确从产物里剔掉的字段。写在代码里（不是 filter.json）是因为这是
# "我们要不要这类内容"的产品决定，不是可以每天改的过滤规则。
DROP_FIELDS = ("lives",)

# 报告里明细列表最多列多少条。站点可能上百个，全塞进 HTML 会让文件膨胀到几 MB；
# 超出的部分靠"完整分布表"交代（数量是完整的，只是不逐条铺开）。
DETAIL_CAP = 200


# ════════════════════════════════════════════════════════════
# 一、抓取（带重试；每条错误信息都带上 URL 和正文片段）
# ════════════════════════════════════════════════════════════

def _idna_host(host: str) -> str:
    """把主机名转成 punycode；失败就原样返回（说明它本来就是 ascii）。"""
    try:
        return host.encode("idna").decode("ascii")
    except Exception:
        return host


def idn_encode(url: str) -> str:
    """中文域名转 punycode（http://肥猫.com/ → http://xn--.../）。

    只替换 host 那一段：userinfo / host / port 分别转好再拼回去，
    既不会丢 user:pass@，也不会出现"域名里恰好含 host 子串被误替换"。
    """
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if not host:
        return url

    netloc = _idna_host(host)
    if parsed.username:
        # userinfo 里的用户名/密码也可能有非 ascii 字符，各自转码
        userinfo = urllib.parse.quote(parsed.username, safe="")
        if parsed.password:
            userinfo += ":" + urllib.parse.quote(parsed.password, safe="")
        netloc = userinfo + "@" + netloc
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def idn_encode_best(url: str) -> str:
    """尽量把 URL 编码成能去请求的形式。

    URL 里嵌中文分两种情况：
      · 整条都是明文中文（http://肥猫.com/tv）→ 转 punycode；
      · 只有 path/query 带中文（.../111.php?ou=公众号）→ netloc 本来就是 ascii，
        转 punycode 没意义，但非 ascii 字节会让 http.client 抛 UnicodeEncodeError，
        所以路径与查询串按非 ascii 做百分号编码。
    """
    target = idn_encode(url)
    try:
        target.encode("ascii")
        return target
    except UnicodeEncodeError:
        pass
    parsed = urllib.parse.urlsplit(target)
    # safe 里保留 % 以免把已有的合法转义写成 %25
    path = urllib.parse.quote(parsed.path, safe="/%:@&=+$,;~()!'*")
    query = urllib.parse.quote(parsed.query, safe="/%:@&=+$,;~()!'*?")
    fragment = urllib.parse.quote(parsed.fragment, safe="/%:@&=+$,;~()!'*?")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path, query, fragment))


def _strip_js_comments(text: str) -> str:
    """剔除 JSON 里内嵌的 // 行注释（部分接口会追加免责注释）。"""
    return "\n".join(ln for ln in text.split("\n") if not ln.lstrip().startswith("//"))


def parse_json_lenient(text: str):
    """宽容解析：去 BOM、剔注释；若返回多段 JSON 拼接，取第一段。"""
    text = text.lstrip("\ufeff").strip()
    text = _strip_js_comments(text)
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        # raw_decode 失败会抛 JSONDecodeError，由调用方归类为"解析失败"
        obj, _ = json.JSONDecoder(strict=False).raw_decode(text)
        return obj


def fetch_text(url: str) -> str:
    """抓文本，带重试。重试边界：

        超时 / 连接错误 / 空响应 / JSON 解析失败 → 重试（可能是反爬冷却）
        4xx / 5xx                              → 不重试（确定性失败）

    返回【文本】而不是解析好的对象：解析放到调用方做，出错时才能把
    实际拿到的那段正文（反爬挑战页 / 停机公告）写进错误信息里。

    【注意】错误信息必须带 URL，否则备用地址全 403 时，
    报告里会出现一堆一模一样的 "HTTP Error 403: Forbidden"，等于没有信息。
    """
    target = idn_encode_best(url)
    last_err = None
    last_body = ""
    for attempt in range(1, MAX_RETRIES + 1):
        body = ""
        try:
            req = urllib.request.Request(target, headers={
                "User-Agent": UA,
                "Accept": "application/json,text/plain,*/*",
            })
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                body = resp.read().decode("utf-8", "replace")
            if not body.strip():
                raise ValueError("空响应（0 字节）")
            # 先在这里试解析一次：解析不过就当"疑似反爬挑战页"重试。
            # 这样一来，返回挑战页 HTML 的接口还有第二次机会，而不是一把就判死。
            parse_json_lenient(body)
            return body
        except urllib.error.HTTPError as e:
            last_err = RuntimeError(f"{url} → HTTP {e.code} {e.reason}")
            break                                    # 4xx/5xx 不重试
        except Exception as e:                       # 超时/连接/空响应/解析失败
            last_body = body
            last_err = RuntimeError(f"{url} → {type(e).__name__}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt * 2)
    # 解析类失败时补一段正文开头，反爬挑战页 / 停机公告页一眼就能认出来
    if last_body and "JSONDecodeError" in str(last_err):
        head = re.sub(r"\s+", " ", last_body[:120]).strip()
        last_err = RuntimeError(f"{last_err}；正文开头：{head}")
    raise last_err if last_err else RuntimeError(f"{url} → 未知错误")


def fetch_bytes(url: str) -> bytes:
    """抓二进制（spider jar 其实是伪装成 .png 的 zip）。jar 最大，超时给足。"""
    req = urllib.request.Request(idn_encode_best(url), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=max(TIMEOUT, 60)) as resp:
        return resp.read()


# ════════════════════════════════════════════════════════════
# 二、严格校验（不合格的源一律不采用）
# ════════════════════════════════════════════════════════════

def valid_sites(data) -> list:
    """挑出 sites 里真正能用的条目（是 dict 且有 key）。"""
    sites = data.get("sites") if isinstance(data, dict) else None
    if not isinstance(sites, list):
        return []
    return [s for s in sites if isinstance(s, dict) and str(s.get("key") or "").strip()]


def validate(data, min_sites=MIN_SITES):
    """返回 (是否合格, 原因)。

    四件事必须同时成立：是 dict、sites 是列表、有效条目 >= min_sites、有 spider。
    最后那条很关键 —— 没有 spider 的配置就是一堆跑不起来的站。
    """
    if not isinstance(data, dict):
        return False, "不是 JSON 对象"
    raw = data.get("sites")
    if not isinstance(raw, list):
        return False, "没有 sites 数组"
    valid = valid_sites(data)
    if not valid:
        return False, "sites 里没有带 key 的有效条目"
    if len(valid) < min_sites:
        return False, f"sites 有效条目只有 {len(valid)} 个（要求 >= {min_sites}）"
    if not str(data.get("spider") or "").strip():
        return False, "没有 spider 字段"
    return True, None


# ════════════════════════════════════════════════════════════
# 三、读 jar 的类清单（客观判断"这个站必然跑不了"）
# ════════════════════════════════════════════════════════════

def jar_class_names(jar_bytes: bytes) -> set:
    """从一个（伪装成 .png 的）jar 里读出全部类名。

    这个 jar 里没有 .class，只有 classes.dex —— 所以要解析 dex 的字符串表。
    dex 头里：string_ids_size 在偏移 0x38，string_ids_off 在 0x3C。

    为什么必须按"类名"判：App 加载 site 的代码是纯字符串拼接
        loader.loadClass("com.github.catvod.spider." + api.split("csp_")[1])
    没有任何降级或回退 —— 所以"类不在"就等于"这个源必然失败"。
    """
    with zipfile.ZipFile(BytesIO(jar_bytes)) as z:
        dex = z.read("classes.dex")

    def uleb(b, i):
        """读一个 dex 的 ULEB128（字符串长度用的变长编码）。"""
        r = s = 0
        while True:
            x = b[i]
            i += 1
            r |= (x & 0x7F) << s
            if not (x & 0x80):
                break
            s += 7
        return r, i

    n = int.from_bytes(dex[0x38:0x3C], "little")
    off = int.from_bytes(dex[0x3C:0x40], "little")
    names = set()
    for k in range(n):
        o = int.from_bytes(dex[off + k * 4: off + k * 4 + 4], "little")
        try:
            _, p = uleb(dex, o)
            end = dex.index(b"\x00", p)
            sv = dex[p:end].decode("utf-8")
        except Exception:
            continue
        # 只收 Lcom/xxx/Yyy; 这种类型描述符，取最后一段作为类名
        if sv.startswith("L") and sv.endswith(";") and "/" in sv:
            names.add(sv[1:-1].rsplit("/", 1)[-1])
    # 一个真 jar 不可能一个类名都读不出来。空集合必须当【失败】抛出去，绝不能安静地返回：
    # 调用方（main）是拿 None 判"读不出来 → 跳过类存在性校验"的，空集合会让它以为读成功，
    # 于是所有 csp_ 站都因为"类不在空集合里"被判死 —— 一次 jar 格式变化就会产出空配置。
    if not names:
        raise ValueError("从 dex 的字符串表里没读出任何类名（dex 损坏，或格式不是预期的 classes.dex）")
    return names


def spider_url_of(value: str) -> str:
    """配置里 spider 可能是 "url;md5;xxx" 或 "url;sha256;xxx"，取真正的 url。

    ⚠️ 两种后缀都要切。早先这里只切 ";md5;" —— 遇到 sha256 就会把
    "https://….png;sha256;<64位>" 整串当成 URL 拿去请求，必然失败。
    App 端是两种都支持的（JarLoader.splitHash 先试 sha256 再试 md5），
    所以我们这边也必须两种都认。
    """
    s = str(value or "")
    for sep in (";sha256;", ";md5;"):
        if sep in s:
            return s.split(sep, 1)[0].strip()
    return s.strip()


# ⚠️ 这两条正则需要"认出"一个 hash 声明，长度必须和 App 端一致：
#   App 的 JarLoader.splitHash 是 `jar.split(";md5;", 2)` —— 只要分隔符在就认，
#   不检查长度。所以如果这里只匹配精确长度，会出现"我们以为没声明、
#   App 却当成有声明去校验"的错位，最后 jar 因校验失败而根本不加载。
#   反过来，长度精确的正则用来判断"这个声明的值是否可信"。
_MD5_ANY = re.compile(r";md5;([^;\s]*)")
_SHA256_ANY = re.compile(r";sha256;([^;\s]*)")
_MD5_DECL = re.compile(r";md5;([0-9a-fA-F]{32})(?![0-9a-fA-F])")
_SHA256_DECL = re.compile(r";sha256;([0-9a-fA-F]{64})(?![0-9a-fA-F])")


def split_hash_decl(value: str):
    """按 App 的规则把 spider 拆成 (url, decl)。decl 形如 ";md5;xxx"；没有就是 ""。

    顺序必须和 App 一致：先找 sha256，再找 md5（JarLoader.splitHash）。
    """
    s = str(value or "")
    for sep, _pat in ((";sha256;", _SHA256_ANY), (";md5;", _MD5_ANY)):
        i = s.find(sep)
        if i >= 0:
            return s[:i].strip(), s[i:].strip()
    return s.strip(), ""


def declared_hash_of(spider_value: str):
    """取出 (hash_type, hash)，但【只认精确长度的值】；占位/截断的值返回 (None, None)。"""
    _url, decl = split_hash_decl(spider_value)
    m = _MD5_DECL.search(decl)
    if m:
        return "md5", m.group(1).lower()
    m = _SHA256_DECL.search(decl)
    if m:
        return "sha256", m.group(1).lower()
    return None, None


def _digest_of(data, hash_type: str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    if hash_type == "sha256":
        return hashlib.sha256(data).hexdigest()
    return hashlib.md5(data).hexdigest()


def https_equivalent(spider_value: str):
    """把 http:// 的 jar 换成 https://，【仅当三重校验全过】。

    三重校验：
      ① 原值必须是 http:// 开头（https 的不用动，别的 scheme 不动）
      ② 改写成 https 后必须真的能下载下来
      ③ 下载到的字节算出的 hash 必须和源里【声明的】hash 完全一致
         （声明了 hash 才能校验；没声明就拒绝改写 —— 没有比对依据时不许动）

    为什么非要校验：一旦 https 那天不可用（或对端换了内容），改写就会把
    "至少还能用的 http jar" 换成"根本下不来的 https"，等于把唯一能用的东西弄丢。
    宁可不改，也不许猜。

    :return: (value, note)
             value 是【完整的 spider 值】—— 改写成 https 时会把源里声明的
             hash 尾巴原样带上（`…jpg;md5;fc8f99…`）。绝不能只返回光秃秃的 URL：
             App 对 https 的 jar 要求"必须带 ;sha256;/;md5;"，不带就
             【直接不加载】（JarLoader.java:117-121），比 hash 对不上还糟。
             note 是空串表示没改写；非空表示改写了或为何不改写。
    """
    s = str(spider_value or "").strip()
    url, decl = split_hash_decl(s)

    # 已经是 https（或别的 scheme）就原样返回，一个字都不动
    if not url.lower().startswith("http://"):
        return s, ""

    # 没有声明就不能校验 —— 没有比对依据时不许动（占位/截断的值也不算声明）
    hash_type, want = declared_hash_of(s)
    if hash_type is None:
        return s, "源里没有声明可用的 md5/sha256，无法校验，因此未改写为 https"

    cand_url = "https://" + url[len("http://"):]
    try:
        data = fetch_bytes(cand_url)
        if data is None:
            raise ValueError("下载返回空")
    except Exception as e:
        return s, f"https 探测失败（{str(e)[:80]}），保持源里原本的 http"

    got = _digest_of(data, hash_type)
    if got != want:
        # 我们既然已经判定这个声明的值是错的，就不能把它留在配置里：
        # 留着会让 App 的 verify 必然失败 → jar 直接不加载，比不带 hash 还糟。
        return url, (f"https 取到的内容 hash 与源里声明的不一致"
                     f"（声明 {want[:12]}… 实得 {got[:12]}…），保持 http、并去掉失效的 hash")

    # 关键：把 hash 声明一起带上，不能只写 URL
    return cand_url + decl, f"http→https 改写成功（{hash_type} 校验一致）"


def sanitized_spider(value: str) -> str:
    """写进 config.json 前把 spider 收拾干净。

    干两件事：
      ① 去掉【不可信的 hash 声明】—— 有些源写的是 ";md5;aaa"、";md5;c3e8c08b"
         这种占位或截断的值。留着它 App 的 verify 必然失败，比不带 hash 还糟
         （不带 hash 至少还能加载；带了错的直接 return 不加载）。
      ② 非 ascii（中文域名）的主机转 punycode —— App 拿到明文中文域名没法发请求。
    """
    s = str(value or "").strip()
    if not s:
        return s
    url, decl = split_hash_decl(s)
    if decl and declared_hash_of(s)[0] is None:
        s = url                       # 声明不可信 → 去掉
    return idn_encode(s)


def class_name_of(api: str):
    """把 site.api 映射成 jar 里的类名。返回 None 表示"走别的加载器，判不了"。"""
    a = (api or "").strip()
    if a.startswith("csp_"):
        return a[len("csp_"):].split("$")[0]
    return None


# ════════════════════════════════════════════════════════════
# 四、过滤规则（config/filter.json）
# ════════════════════════════════════════════════════════════

# filter.json 的字段 + 默认值。
#
# ★ 2026-10-03 重构：这个文件从"记录被剔除过谁"改成"每轮实时跑的规则"。
#   · 脚本【只读不写】—— 上一版会写回 failure_streak / blacklist_keys（连续失败自动拉黑），
#     结果是名单只增不减：一次用错 jar 判定的 missing_class 连续 3 次就永久进黑名单，
#     而黑名单优先级高于类校验 → 换回对的 jar 也永远出不来（实测 38 个好站被这样误杀）。
#   · 类存在性是【当轮 jar 的函数】，每轮现下现读，不需要也不应该被记录下来。
#
# 字段名从 blacklist_* 改成 filter_*（语义：规则，不是名单）。
# 旧的 blacklist_* 名字仍然兼容读取（见 _FIELD_ALIASES），这样推送瞬间不会读到空规则。
FILTER_DEFAULTS = {
    "filter_keys": [],
    "filter_name_patterns": [],
    "filter_api_prefixes": [],
    "filter_hosts": [],
    "whitelist_keys": [],
    "only_sources": [],
}

# 旧字段名 → 新字段名。只为兼容：同事手里的旧 filter.json 还能读。
_FIELD_ALIASES = {
    "blacklist_keys": "filter_keys",
    "blacklist_name_patterns": "filter_name_patterns",
    "blacklist_api_families": "filter_api_prefixes",
    "blacklist_hosts": "filter_hosts",
}


HOST_RE = re.compile(r"https?://[^\s\"'<>()\[\]{},;|\\]+", re.I)


def load_filter(path=None):
    """读 filter.json，缺字段用默认值补齐。读不到/读坏了也不让构建失败。

    path 用 None 而不是直接把 FILTER_FILE 写成默认值：默认值是在 def 时求值的，
    写成默认值以后，测试（或任何想换目录跑的调用方）改 build.FILTER_FILE 就没用了，
    会一边"以为在跑沙箱"、一边把仓库里真正的 filter.json 写掉。

    ★ 同时接受旧字段名（blacklist_keys 等），见 _FIELD_ALIASES。
    ★ 只读，不写。这个文件完全由人维护（见模块顶部 FILTER_DEFAULTS 的说明）。
    """
    path = path or FILTER_FILE
    cfg = dict((k, (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v))
               for k, v in FILTER_DEFAULTS.items())
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                # 新名字优先；只有在新名字缺席时才吃旧名字
                for old, new in _FIELD_ALIASES.items():
                    if new not in raw and old in raw and raw[old] is not None:
                        raw[new] = raw[old]
                for k in FILTER_DEFAULTS:
                    if k in raw and raw[k] is not None:
                        cfg[k] = raw[k]
                # 下划线开头的是给人看的说明字段，原样带回（现在脚本不写盘，带回来只是为了报告/调试）
                for k, v in raw.items():
                    if k.startswith("_") and k not in cfg:
                        cfg[k] = v
        except Exception as e:
            print(f"[WARN] filter.json 解析失败（{e}），按默认规则继续")
    return cfg


def extract_hosts(site) -> set:
    """把一个 site 里所有出现过的 host 抠出来。

    api 本身可能是一个完整 URL，ext 可能是字符串 / 对象 / 数组，里面也可能塞着 URL。
    做法：把它们拍平成一大段文本，用正则捞出所有 http(s):// 片段，再逐个取 hostname。
    只认 http(s) 前缀是刻意的 —— 配置里还有 csp_Xxx / js: / assets: 这类值，
    盲目按 ":" 切会切出一堆垃圾。
    """
    parts = [str(site.get("api") or "")]
    ext = site.get("ext")
    if ext is not None:
        if isinstance(ext, str):
            parts.append(ext)
        else:
            try:
                parts.append(json.dumps(ext, ensure_ascii=False))
            except Exception:
                parts.append(str(ext))
    hosts = set()
    for chunk in parts:
        for m in HOST_RE.findall(chunk):
            try:
                h = urllib.parse.urlsplit(m).hostname
            except Exception:
                h = None
            if h:
                hosts.add(h.lower())
    return hosts


def host_blacklisted(host: str, black_hosts) -> bool:
    """后缀匹配：nxog.eu.org 能匹配 woog.nxog.eu.org。

    要做成"标签边界"上的后缀匹配，不能直接 endswith ——
    否则 notnxog.eu.org 也会被 nxog.eu.org 匹配上。所以要求
    host == rule 或 host 以 "." + rule 结尾。
    """
    h = (host or "").lower().strip(".")
    for rule in black_hosts:
        r = str(rule or "").lower().strip().strip(".")
        if not r:
            continue
        if h == r or h.endswith("." + r):
            return True
    return False


class Drop:
    """被剔除的一个 site，以及【为什么】被剔除。

    报告要按三类分开呈现，所以 reason 必须细分、matched 要写清命中了哪个词：
      · missing_class  → 类不在 jar 里（必然跑不了）
      · by_rule        → 命中 filter.json 里手工维护的规则
      · duplicate      → 同 key 已保留过（我们只留优先级最高的那份）
    """

    __slots__ = ("key", "name", "api", "reason", "matched", "family",
                 "kept_from", "dropped_from")

    def __init__(self, key, name, api, reason, matched="", family="",
                 kept_from="", dropped_from=""):
        self.key = key
        self.name = name
        self.api = api
        self.reason = reason
        self.matched = matched
        self.family = family
        self.kept_from = kept_from
        self.dropped_from = dropped_from

    def as_dict(self):
        d = {"key": self.key, "name": self.name, "api": self.api, "reason": self.reason}
        if self.matched:
            d["matched"] = self.matched
        if self.kept_from:
            d["kept_from"] = self.kept_from
            d["dropped_from"] = self.dropped_from
        return d


def filter_site(site, *, key, name, api, spider_classes, cfg):
    """按固定优先级判断一个 site 该不该留。返回 (是否保留, reason, matched)。

    优先级（顺序不能改，否则"白名单无条件保留"会被别的规则推翻）：
      1 白名单 key                        → 无条件保留
      2 key 在 filter_keys                → filter_keys
      3 名字命中任何过滤正则（记下哪一个）  → filter_name_patterns
      4 api 以任何过滤前缀开头             → filter_api_prefixes
      5 api / ext 里的 host 命中过滤域名   → filter_hosts
      6 没有自带 jar 且 api 以 csp_ 开头，类不在 spider 的 jar 里 → missing_class
      7 其余                              → 保留

    ★ 前 5 条是【人写的规则】（filter.json），第 6 条是【当轮 jar 的函数】。
      第 6 条永远实时算、绝不被记录成规则 —— 踩过的坑：把一次判定的 missing_class
      写进 filter_keys，换回对的 jar 之后那些站也再不会出现。

    第 6 条的两个前提都不能省：
      · site 自带了 jar：那它加载的是自己那个 jar 里的类，不能拿 spider 的类清单去判；
      · 只有 csp_ 前缀才会走 jar 加载器，py_ / js: / assets: 之类判不了，放过。
    """
    if key in set(cfg.get("whitelist_keys") or []):
        return True, None, ""

    if key in set(cfg.get("filter_keys") or []):
        return False, "filter_keys", ""

    if name:
        for pat in cfg.get("filter_name_patterns") or []:
            if not pat:
                continue
            try:
                if re.search(pat, name):
                    return False, "filter_name_patterns", str(pat)
            except re.error:
                # 正则写错了不能连累整个构建：跳过这一条，并在日志里留痕
                print(f"[WARN] filter.json 里的名字正则无效，已跳过：{pat}")

    for fam in cfg.get("filter_api_prefixes") or []:
        if fam and api.startswith(str(fam)):
            return False, "filter_api_prefixes", str(fam)

    black_hosts = cfg.get("filter_hosts") or []
    if black_hosts:
        for h in extract_hosts(site):
            if host_blacklisted(h, black_hosts):
                return False, "filter_hosts", h

    if not site.get("jar") and spider_classes is not None:
        cls = class_name_of(api)
        if cls is not None and cls not in spider_classes:
            return False, "missing_class", cls

    return True, None, ""


# ════════════════════════════════════════════════════════════
# 五、合并（同 key 只留一份；spider 只认本次真抓到的源）
# ════════════════════════════════════════════════════════════

def merge(fetched, spider_classes, cfg, cohort: dict):
    """合并多个单仓配置。fetched 已按 sources.json 的顺序排好（=优先级）。

    和上游的区别：
      ② sites 同 key【只保留先出现的那个】，不是"加源前缀改名后塞进来"
      ③ spider 显式来自 sources.json 里标了 spider_source 的源（见 main），
         并且逐个校验 api 的类在 jar 里，不在就不写进配置
      ⑩ ads 也参与列表合并（上游 skip 掉却不再合并，等于只留 base 那家）
      ★ lives（直播）不合并、也不从 base 带出（见 DROP_FIELDS）：这个配置只给看影视剧用
    """
    if not fetched:
        return None

    base = fetched[0][2]
    merged = {k: v for k, v in base.items() if k not in SKIP_FROM_BASE}

    kept, seen = [], set()
    seen_owner = {}          # key -> 保住它的上游源名（写进 status 的 duplicate 组）
    drops = []

    # fetched 的元素是【4 元组】(sid, name, data, from_cache) —— 见 main 里的 append。
    # 这里少解一个就是 ValueError: too many values to unpack，只要有任意一个源抓成功，
    # 整个构建就会崩在合并这一步（config.json / status.json / report.html 一个都产不出来）。
    for sid, src_name, data, _from_cache in fetched:
        for s in data.get("sites", []) or []:
            if not isinstance(s, dict):
                continue
            key = str(s.get("key") or "").strip()
            if not key:
                continue
            name = str(s.get("name") or "")
            api = str(s.get("api") or "")

            # 去重只认"已经保留下来"的 key。先判重再判规则会出两个问题：
            #    · 被规则剔掉的 key 会占住名额，后面那份同样 key 的站也一并不能留；
            #    · 去重（优先级问题）和过滤（规则问题）混成一团，报告里既说不清
            #      是"重复"还是"命中规则"。
            if key in seen:
                drops.append(Drop(key, name, api, "duplicate",
                                  kept_from=seen_owner.get(key, ""), dropped_from=src_name))
                continue

            keep, reason, matched = filter_site(
                s, key=key, name=name, api=api,
                spider_classes=spider_classes, cfg=cfg)
            if keep:
                seen.add(key)
                seen_owner[key] = src_name
                kept.append(s)
            else:
                drops.append(Drop(key, name, api, reason, matched))

    merged["sites"] = kept

    # 列表字段全部参与合并去重（含 ads）。按 name/url 认身份，
    # 认不出来的退化成整段 JSON 比较 —— 保证同一份配置不会重复出现两次。
    for field in MERGE_FIELDS:
        seen_item, out = set(), []
        # 同样是 4 元组（这个循环漏了跟上面那个一样的改动，测试一跑就露出来了）
        for _sid, _n, data, _from_cache in fetched:
            for item in data.get(field, []) or []:
                if isinstance(item, dict):
                    ident = item.get("name") or item.get("url") or json.dumps(
                        item, ensure_ascii=False, sort_keys=True)
                else:
                    ident = str(item)
                if ident in seen_item:
                    continue
                seen_item.add(ident)
                out.append(item)
        if out:
            merged[field] = out

    # 明确剔掉的字段（比如直播）：base 里带了的也要抹掉。
    # 这一步放在所有合并之后，做最后一道"产物里绝对不该有"的保证 ——
    # 将来谁往 MERGE_FIELDS 或 base 里加了这类字段，也漏不过去。
    for field in DROP_FIELDS:
        merged.pop(field, None)

    cohort["kept"] = len(kept)
    # 另外存一份【保留下来的站点列表】：cohort["kept"] 是数量（status.json 里
    # sites.kept 就是它），但调用方经常需要那一串 site 本身（算首页候选、
    # 报告里列举保留下来的站），所以这里额外给一份列表，别让调用方去猜。
    cohort["kept_sites"] = kept
    cohort["dropped"] = drops
    cohort["total_input"] = len(kept) + sum(1 for d in drops if d.reason != "duplicate")
    return merged


# ════════════════════════════════════════════════════════════
# 六、把 status.json 的碎片攒成报告要的上下文
# ════════════════════════════════════════════════════════════

def esc(v) -> str:
    """HTML 转义。源名字里有 emoji、全角符号，甚至 <>，不转义会把页面结构搞坏。"""
    return html.escape(str(v if v is not None else ""), quote=True)


def split_families(items, top_n=9):
    """按族统计 (计数降序) 并切成"主体 + 长尾"。

    items 是 (展示名, 计数) 的可迭代。长尾阈值取"只出现 1 次的族"：
    它们单看毫无意义，堆成一句"19 个 api 家族各 1 个站"反而最有信息量。
    """
    counter = Counter()
    for fam, n in items:
        counter[fam] += n
    ordered = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    main = [(f, n) for f, n in ordered if n > 1][:top_n]
    tail = [(f, n) for f, n in ordered if (f, n) not in set(main)]
    return main, tail


def _common_prefix(words) -> str:
    """一组字符串的最长公共前缀。"""
    words = list(words)
    if not words:
        return ""
    head = words[0]
    for w in words[1:]:
        while not w.startswith(head):
            head = head[:-1]
            if not head:
                return ""
    return head


def family_label_for(api: str, class_names) -> str:
    """给一个 api 定"显示族名"，也就是柱状图上的那一行标签。

    同一批被砍的站放在一起看：类名全都是 XxxGuard 时，取它们去掉 "Guard"
    之后的最长公共前缀当族名 —— Wexdiy / Wexconfig / Wexyun 的公共前缀是
    "Wex"，报告上就是 csp_Wex*Guard（54 个站收成一条柱子）。
    只有一个类名、或公共前缀短到读不出来时，直接用类名本身
    （csp_BiliGuard / csp_DoubanGuard）；不是 Guard 结尾的类照旧用自己的名字。
    """
    classes = [c for c in class_names if c]
    cls = (api or "").strip()
    if cls.startswith("csp_"):
        cls = cls[len("csp_"):].split("$")[0]
    if not (cls.endswith("Guard") and classes
            and all(c.endswith("Guard") for c in classes)):
        return f"csp_{cls}" if cls else "（空 api）"
    head = _common_prefix([c[:-len("Guard")] for c in classes])
    if len(classes) == 1 or head == cls[:-len("Guard")] or len(head) < 2:
        return f"csp_{cls}"
    return f"csp_{head}*Guard"


def build_fragments(ctx: dict) -> dict:
    """把结构化数据渲染成模板要的那十几个 HTML 片段 + 文字变量。"""
    v = {}

    # ── 上游源表（$sources_table）──
    rows = []
    for s in ctx["sources"]:
        errs = [e for e in (s.get("errors") or []) if e.get("error")]
        if s["from_cache"]:
            state, row_cls, note = "cache", " is-cache", "使用缓存（本次没抓到）"
        elif s["ok"]:
            state, row_cls, note = "live", "", "本次抓取成功"
        else:
            state, row_cls = "down", " is-down"
            # 失败时把【每一个备用地址】的原因都写出来（status.json 里本来就是一条一条记的）。
            # 只显示最后一条会让人以为"只挂了一个地址"，实际常见的是 7 个地址全挂 ——
            # 这直接决定要不要换源，所以不能省。
            note = " · ".join(_shorten(e["error"], 70) for e in errs) or "所有地址均失败"
        if s["id"] == ctx["spider_id"] and state == "live":
            note = "本次 spider jar 的来源"
        rows.append(
            f'            <tr class="src-row{row_cls}">\n'
            f'              <th scope="row" class="src-name">{esc(s["name"])} '
            f'<span class="src-id">{esc(s["id"])}</span></th>\n'
            f'              <td><span class="src-status {state}">'
            f'<span class="pip" aria-hidden="true"></span>{state}</span></td>\n'
            f'              <td class="num">{s["site_count"] if s["ok"] else "—"}</td>\n'
            f'              <td class="url">{esc(s["used_url"] or "—")}</td>\n'
            f'              <td class="note">{esc(_shorten(note, 620))}</td>\n'
            f'            </tr>')
    v["sources_table"] = "\n".join(rows)

    # ── 柱状图（$chart_bars）──
    # DOM 必须是 <div class="bar">，bar-fill 上带 data-w（数量）：
    # 模板里的 JS 会读 data-w 做归一化（最长的一根 = 轨道宽 93.4%），
    # 脚本这边【不能】自己写 width 百分比，否则 JS 会把它再算一次。
    # 只统计"类不在包里"这一组：另外两组（规则命中 / 同名重复）是按名字和
    # 优先级判的，没有 api 家族可归类，混进来会出现一条没有名字的柱子。
    missing = [d for d in ctx["dropped_all"] if d.reason == "missing_class"]
    main, tail = split_families([(d.family, 1) for d in missing])
    bars = []
    for i, (fam, n) in enumerate(main):
        if i == 0:
            fill_cls, fill_style = "", ""
        else:
            fill_cls = " hatch"
            fill_style = f' style="opacity:{_bar_tier_opacity(i)}"'
        bars.append(
            f'            <div class="bar" style="--i:{i}">\n'
            f'              <span class="bar-label">{esc(_shorten(fam, 46))}</span>\n'
            f'              <span class="bar-track"><span class="bar-fill{fill_cls}"{fill_style} '
            f'data-w="{n}"></span></span>\n'
            f'              <span class="bar-val">{n}</span>\n'
            f'            </div>')
    tail_sum = sum(n for _f, n in tail)
    # 长尾的标准形态是"一堆只出现 1 次的家族"，但也可能是"出现 >= 2 次、只是排在 top_n 之外"
    # 的家族被挤进来（上面的 main 只取前 9 条）。后者再写"各 1 个"就是一句假话：
    # 报告里那句算式会说 9×1，可实际这 9 个家族共砍掉 15 个站 —— 数字对不上，人就信不过整页。
    tail_all_one = all(n == 1 for _f, n in tail)
    if tail_sum:
        tail_label = (f"零散家族 {len(tail)} 个" +
                      ("（各 1 个）" if tail_all_one else f"（合计 {tail_sum} 个）"))
        bars.append(
            f'            <div class="bar" style="--i:{len(main)}">\n'
            f'              <span class="bar-label">{esc(tail_label)}</span>\n'
            f'              <span class="bar-track"><span class="bar-fill hatch" '
            f'data-w="{tail_sum}"></span></span>\n'
            f'              <span class="bar-val">{tail_sum}</span>\n'
            f'            </div>')
    v["chart_bars"] = "\n".join(bars)

    nums = [n for _f, n in main] + ([tail_sum] if tail_sum else [])
    v["chart_max_label"] = str(max(nums)) if nums else "0"
    v["bar_tail_lead"] = f"零散家族 {len(tail)} 个"
    v["bar_tail_text"] = ("，每个只砍掉 1 个" if tail_all_one
                          else f"，合计 {tail_sum} 个")
    if tail_all_one:
        tail_note = f"{len(tail)}×1" if len(tail) > 1 else "1"
    else:
        tail_note = str(tail_sum)
    v["bar_tail_sum"] = " + ".join([str(n) for _f, n in main] +
                                   ([tail_note] if tail_sum else [])) or "0"

    # ── 三组被剔除的站 ──
    def li(items, third):
        """把 Drop 列表渲染成 <li class="drop-item"> 序列。

        third 负责生成第三列。它的返回值一律当成【已经转义好的 HTML】，
        这里不再二次转义 —— 否则「保留 肥猫 的」里嵌的 esc() 会被转成 &amp; 之类，
        页面上直接显示成 &lt; 这种字面量。
        三个 third 实现（reason_g1 / reason_g2 / dup_reason）内部自己做 esc。
        """
        out = []
        for d in items[:DETAIL_CAP]:
            out.append(
                f'                <li class="drop-item">\n'
                f'                  <span class="drop-key">{esc(d.key)}</span>\n'
                f'                  <span class="drop-name">{esc(d.name or "（无名）")}</span>\n'
                f'                  <span class="drop-reason">{third(d)}</span>\n'
                f'                </li>')
        if len(items) > DETAIL_CAP:
            out.append(
                f'                <li class="drop-item">\n'
                f'                  <span class="drop-key">…</span>\n'
                f'                  <span class="drop-name">另有 {len(items) - DETAIL_CAP} 条同类记录</span>\n'
                f'                  <span class="drop-reason">完整数量见上方分布表</span>\n'
                f'                </li>')
        return "\n".join(out)

    g1 = [d for d in ctx["dropped_all"] if d.reason == "missing_class"]
    g2 = [d for d in ctx["dropped_all"] if d.reason in
          ("filter_keys", "filter_name_patterns", "filter_api_prefixes", "filter_hosts")]

    def reason_g1(d):
        return f"{esc(d.matched)} 包里没有"

    def reason_g2(d):
        if d.reason == "filter_keys":
            return "按 key 过滤"
        if d.reason == "filter_name_patterns":
            return f"名字命中「{esc(d.matched)}」"
        if d.reason == "filter_api_prefixes":
            return f"按 api 前缀过滤：{esc(d.matched)}"
        return f"按域名过滤：{esc(d.matched)}"

    def dup_reason(d):
        return f"只留 {esc(d.kept_from)} 的"

    v["dropped_missing_class"] = li(sorted(g1, key=lambda d: (d.family, d.key)), reason_g1)
    v["dropped_by_rule"] = li(sorted(g2, key=lambda d: (d.reason, d.key)), reason_g2)
    v["dropped_duplicate"] = li(sorted(ctx["duplicates"], key=lambda d: d.key), dup_reason)

    # 组 1 的"典型样本"六宫格：前 6 个被砍的站，给人一个具体的手感
    samples = []
    for d in sorted(g1, key=lambda d: d.key)[:6]:
        samples.append(
            f'                  <div class="sample">\n'
            f'                    <span class="s-key">{esc(d.matched)}</span>\n'
            f'                    <span class="s-name">{esc(d.name)}</span>\n'
            f'                    <span class="s-api">{esc(d.api)}</span>\n'
            f'                  </div>')
    v["drop_g1_samples"] = (
        '              <p class="pi-hint">随便挑几个看一眼。</p>\n'
        '              <div class="samples">\n' + "\n".join(samples) +
        "\n              </div>") if samples else ""

    # 组 1 的完整分布表：数量是完整的（明细列表可能被 DETAIL_CAP 截断）
    main_all, tail_all = split_families([(d.family, 1) for d in g1], top_n=99)
    trows = []
    for fam, n in main_all:
        trows.append(f'                              <tr><td class="a">{esc(fam)}</td>'
                     f'<td class="num">{n}</td><td class="r">包里没有</td></tr>')
    if tail_all:
        trows.append(f'                              <tr><td class="a">其余 {len(tail_all)} 个 api 家族'
                     f'</td><td class="num">{sum(n for _f, n in tail_all)}</td>'
                     f'<td class="r">包里没有</td></tr>')
    v["drop_g1_chart"] = (
        '              <div class="foldmore">\n'
        f'                <h4>完整分布 · 合计 {len(g1)} 个</h4>\n'
        '                <div class="inner-scroll" style="margin-top:10px">\n'
        '                  <table>\n'
        '                    <caption class="sr">被砍站点的 api 家族完整计数</caption>\n'
        '                    <thead><tr><th scope="col">api 家族</th>'
        '<th scope="col" class="num">数量</th><th scope="col">判定</th></tr></thead>\n'
        '                    <tbody>\n' + "\n".join(trows) + "\n"
        f'                      <tr class="total-row"><td class="k">合计</td>'
        f'<td class="num">{len(g1)}</td><td class="r">全部砍掉</td></tr>\n'
        '                    </tbody>\n'
        '                  </table>\n'
        '                </div>\n'
        '              </div>') if g1 else ""

    v["drop_g1_count"] = str(len(g1))
    v["drop_g2_count"] = str(len(g2))
    v["drop_g3_count"] = str(len(ctx["duplicates"]))
    v["dropped_total"] = str(len(g1) + len(g2) + len(ctx["duplicates"]))
    # ⚠️ 这段文案的措辞很重要，别退回成"代码不在这一个包里"就完事：
    #    组 1 的规模【由我们选哪个 jar 决定】，不是"这些源本身差"。
    #    真实数据：肥猫当 jar → 组 1 有 105 个；王二小当 jar → 只有 53 个。
    #    说得含糊，人就会把"砍掉 105 个"当成成绩，而它其实是选错包的代价。
    v["drop_g1_note"] = ("这一份只加载了一个程序包；这些站要用的类不在里面 —— "
                        "不剔掉，App 加载时就抛 ClassNotFoundException，每次搜索还白等超时")
    v["drop_g2_note"] = "教学课 / 广告 / 停更的源，搜剧只会返回垃圾结果"
    v["drop_g3_note"] = "上游遇到重名源会改名保留，同一个源出现了好几份"

    v["drop_g1_col1"], v["drop_g1_col2"], v["drop_g1_col3"] = "key", "名字", "包里没有这个类"
    v["drop_g2_col1"], v["drop_g2_col2"], v["drop_g2_col3"] = "key", "名字", "为什么砍"
    v["drop_g3_col1"], v["drop_g3_col2"], v["drop_g3_col3"] = "key", "名字", "去重结果"

    # ── 组 3 的对比条（上游含重复 vs 我们去重后）──
    before, after = ctx["total_input"], ctx["kept"]
    dup_n = len(ctx["duplicates"])
    v["dup_lead"] = (f"上游遇到重名源会改名保留，同一个源会出现好几份（「推送」就有 4 份）。我们只留一份。")
    v["dup_bar_aria"] = (f"上游单仓 {before} 个站（含 {dup_n} 个改名重复），"
                         f"我们的 {after} 个站，没有重复。")
    v["dup_bar_up_pct"] = "100"
    v["dup_bar_up_value"] = f"<b>{before}</b> 个（含 {dup_n} 个改名重复）"
    v["dup_bar_ours_pct"] = f"{round(after / before * 100, 1) if before else 0}"
    v["dup_bar_ours_value"] = f"<b>{after}</b> 个（去重后）"

    # ── 两份配置对比 ──
    bad = max(before - after - dup_n, 0)
    ok_a = before - bad
    v["cmp_a_pct"] = f"{round(bad / before * 100, 1) if before else 0}"
    v["cmp_a_ok_pct"] = f"{round(ok_a / before * 100, 1) if before else 0}"
    v["cmp_a_bad"] = str(bad)
    v["cmp_a_ok"] = str(ok_a)
    v["cmp_a_desc"] = (f"其中 <b>{bad}</b> 个站的代码不在包里，打开就失败，"
                       f"每个还要白等一次超时。")
    v["cmp_b_desc"] = "每个源都查过一遍代码在不在包里，不在的一个都不写进来。"
    v["cmp_a_aria"] = f"上游单仓 {before} 个站：用不了 {bad} 个，能用 {ok_a} 个。"
    v["cmp_b_aria"] = f"我们的配置 {after} 个站，每个都能用。"
    v["cmp_b_seg_label"] = f"{after} 个全部可用"

    # ── KPI / 页脚 ──
    v["src_live"] = str(ctx["live"])
    v["src_cache"] = str(ctx["cache"])
    v["src_down"] = str(ctx["down"])
    v["src_total"] = str(len(ctx["sources"]))
    v["src_live_badge"] = str(ctx["live"])
    v["site_before"] = str(before)
    v["site_after"] = str(after)
    v["site_cut"] = str(before - after)
    v["site_cut_pct"] = f"{round((before - after) / before * 100) if before else 0}"
    v["cut_headline"] = f"这 {before - after} 个"
    v["spider_name"] = esc(ctx["spider_name"] or "（未选定）")
    v["spider_id"] = esc(ctx["spider_id"] or "—")
    v["spider_classes"] = str(ctx["spider_classes"] or "—")
    v["sources_heading"] = f"· {len(ctx['sources'])} 个来源"
    v["run_label"] = ctx["run_label"]
    v["delta_down"] = ctx["delta_down"]
    v["delta_new"] = ctx["delta_new"]
    v["delta_gone"] = ctx["delta_gone"]
    v["foot_meta"] = f"{ctx['built_at']} · {after} SITES · {len(ctx['sources'])} SOURCES"
    v["foot_scope"] = ctx["foot_scope"]

    # ── 页头 ──
    v["built_at"] = ctx["built_at"]
    v["generator"] = GENERATOR
    return v


def _shorten(text, n):
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def _bar_tier_opacity(i):
    """柱状图第 i 条（0 是最高那条）的填充强度。

    最高的一条给纯白实心（主体），其余用冷蓝斜纹、强度递减。
    必须显式写 opacity —— 深色底上光靠"蓝色"区分不出主次。
    """
    return round(max(0.45, 1.0 - 0.12 * i), 2)


# ════════════════════════════════════════════════════════════
# 七、渲染报告（string.Template，绝不能用 str.format / f-string）
# ════════════════════════════════════════════════════════════

# ── 模板里"只给维护者看"的区块（渲染时整块剥掉）──
#
# 背景：report_template.html 顶部有 287 行【写给维护者】的说明（渲染契约、占位符
# 清单、示例期望值）。它最初只是一段 HTML 注释，但 string.Template 不会删注释，
# 于是每天原样灌进产物：实测产物 1990 行里 287 行是它，占字节 13.5%。
# 报告打开第一眼看到的就是这坨开发文档，而且它自己还写着"本文件是模板、请勿当报告看"。
#
# ⚠️ 为什么用标记剥离、而不是直接把这 287 行从模板里删掉：
#   tests/test_ctx_and_fragments.py 的 template_placeholders() 会扫【整个模板文件】，
#   并【刻意】把注释里连写的 $$name 也算成"模板提到过的占位符"，然后断言
#   "模板提到的名字 == 脚本提供的名字"（严格相等，多一个少一个都算契约破了）。
#   那段文档正是这份清单的栖身处 —— 删掉它，契约测试会当场红。
#   → 所以文档留在模板里（测试照扫），只在【渲染产物】时剥掉。
#
# ⚠️ 标记里不许出现 `$`：本文件是被正则扫 $$name 的，标记本身若带 $ 会污染契约。
DEV_DOCS_RE = re.compile(r"[ \t]*<!--\s*@@DEV-DOCS-START@@.*?@@DEV-DOCS-END@@\s*-->\s*",
                         re.S)


def strip_dev_docs(tpl: str) -> str:
    """剥掉模板里 @@DEV-DOCS-START@@ / @@DEV-DOCS-END@@ 之间的维护者说明。

    只被 render_report 调用 —— 契约测试扫的是【模板文件原文】，不受影响。
    标记缺失时原样返回：宁可产物里多一段注释（丑但能看），也不能把报告弄没。
    """
    out = DEV_DOCS_RE.sub("", tpl, count=1)
    return out if out != tpl else tpl


def render_report(ctx: dict) -> bool:
    """渲染 report.html。返回是否真的写出了文件。

    ⚠️ 这里绝不能用 str.format() 或 f-string 去套模板：
       模板里的 CSS 全是花括号，format 会在第一个 { 上直接炸。
       string.Template 认的是 $name，与 CSS 完全不冲突。

    ⚠️ 用 safe_substitute 而不是 substitute：
       模板由另一位同事并行维护，变量表随时可能增删。用 safe_substitute 时
       少给一个变量最多是那一块留白，不会把整条 Action 流水线弄红。
       但我们仍会扫一遍正文里的残留 $name 并告警 —— 静默留白同样是 bug。

    ⚠️ 模板顶部的「渲染契约」是 HTML 注释，里面写满了 $chart_bars 这类
       占位符名当说明文字，所以模板约定它们要连写成两个美元符号。
       那块说明由 strip_dev_docs() 在渲染前整块剥掉（它不进产物，页面上也看不见），
       但正文里一个 $name 都不许剩。
    """
    if not os.path.exists(TEMPLATE_FILE):
        print(f"[WARN] 报告模板不存在，跳过报告生成：{TEMPLATE_FILE}")
        return False

    with open(TEMPLATE_FILE, encoding="utf-8") as f:
        tpl = f.read()

    # 先剥维护者说明，再替换占位符：顺序反了会把文档里的 $$name 也当成真占位符处理。
    tpl = strip_dev_docs(tpl)
    out = string.Template(tpl).safe_substitute(build_fragments(ctx))

    # 残留占位符检测：只看"页面上看得见"的部分，把 HTML 注释和 CSS 注释都剔掉。
    # 模板约定注释里的占位符名要连写两个美元符号（$$name），渲染后是一个字面
    # "$name"，那是给人看的文档，不是没替换掉的占位符 —— 不该报警。
    scan = re.sub(r"<!--.*?-->", "", out, flags=re.S)
    scan = re.sub(r"/\*.*?\*/", "", scan, flags=re.S)
    # 万一 strip_dev_docs 因为标记缺失没剥掉，也别让那 28 个文档名刷屏成"残留告警"。
    scan = DEV_DOCS_RE.sub("", scan, count=1)
    leftovers = sorted(set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", scan)))
    if leftovers:
        print(f"[WARN] 正文里有 {len(leftovers)} 个占位符没被替换（模板可能刚改过）："
              f"{', '.join(leftovers[:12])}")

    with open(OUT_REPORT, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"[OK]   报告已输出：{OUT_REPORT}（{len(out)} 字符）")
    return True


# ════════════════════════════════════════════════════════════
# 八、--demo 样例数据（固定数字：9 个上游源 / 167 个站 / 砍掉 80 个）
# ════════════════════════════════════════════════════════════

# 上游源：(id, 名字, 状态, 站点数, 用的地址, 失败原因)
# 这张表就是报告上"上游源"那张卡的唯一数据来源：3 活 + 1 缓存 + 6 挂 = 9 个。\n# 数量级参照真实抓取：肥猫 39（实测）、4K 53（实测）、王二小 63。
DEMO_SOURCES = [
    ("feimao", "肥猫", "live", 39, "http://肥猫.net/tv", ""),
    ("wangerxiao", "王二小", "live", 63,
     "https://d.kstore.dev/download/9280/wex.json", ""),
    ("4k", "4K小盒子", "live", 53, "http://xhztv.top/4k.json", ""),
    ("ouge", "讴歌", "cache", 12, "(缓存)", "SSL 握手失败，用上次的缓存"),
    ("fantaiying", "饭太硬", "down", 0, None, "返回的不是 JSON"),
    ("moyu", "摸鱼", "down", 0, None, "域名解析不了"),
    ("ok", "OK", "down", 0, None, "域名解析不了"),
    ("xiaomi", "小米", "down", 0, None, "连不上"),
    ("xiaosa", "潇洒", "down", 0, None, "404"),
]

# 类不在包里（80 个）：(展示族名, 归族用哪个类名, 一个包里没有的类名, 数量)
# Wex 系 57 + 6 + 3 + 3 + 2 + 9 个长尾 = 80。
# 类不在包里（56 个）：(类名列表, 数量)
# 算法数：Wex 33 + BiliGuard 6 + DoubanGuard 3 + AppMao 3 + PanWebShare 2
# + 长尾 9 个各 1 = 56。下面 DEMO_MISSING_EXAMPLES 会覆盖前 6 条，其中 3 条
# 换成 Wex 的例子、3 条换成别的族 —— Wex 的条数前后不变，仍是 33。
#
# ⚠️ 56 这个数不是随便挑的：每个站只会有一个剔除原因，所以
#    组1 + 组2 + 组3 必须【正好等于】被砍掉的站数 =
#    56（类不在包里）+ 16（规则命中）+ 8（同名重复）= 80。
#    而 167（输入）= 87（留下）+ 80（砍掉）。三处数字互相咬死，改一个就得改其余两个。
DEMO_MISSING_GROUPS = [
    (["WexdiyGuard", "WexconfigGuard", "WexhanxiaoquanGuard", "WexzhuboGuard",
      "WexgzhGuard", "WexzhipianGuard", "WexyunGuard"], 33),
    (["BiliGuard"], 6),
    (["DoubanGuard"], 3),
    (["AppMao"], 3),
    (["PanWebShare"], 2),
]
DEMO_MISSING_SINGLES = ["YQKan", "Yj1211", "WoggGuard", "AListGuard", "KanqiuGuard",
                        "PushGuard", "DouDou", "AppSK", "NanGua"]

# 具体例子：(key, 名字, 类名) —— 直接进组 1 的明细列表。
# 类名必须来自上面那份清单，否则它会归到另一个族，柱状图就散了。
DEMO_MISSING_EXAMPLES = [
    ("WexdiyGuard", "💥韩剧┃秒播💥", "WexdiyGuard"),
    ("Wexconfig", "🐮通用类型┃配置中心🐮", "WexconfigGuard"),
    ("Wexhanxiaoquan", "💥韩剧┃秒播💥", "WexhanxiaoquanGuard"),
    ("Douban", "🐮【免费分享】🐮", "DoubanGuard"),
    ("Doubanaaa", "⬇️【网盘类先扫码】⬇️", "DoubanGuard"),
    ("玩偶", "💓玩偶┃4K💓", "WoggGuard"),
]

# 规则命中（16 个）：(key, 名字, reason, 命中的词)。前 12 个按 key 过滤，后 4 个按名字命中。
# 注意这是 --demo 的固定样例数据，用来渲染报告；reason 必须与 filter_site 现在返回的名字一致。
DEMO_RULE_HITS = [
    ("csp_少儿", "📚┃少儿┃教育", "filter_keys", ""),
    ("csp_小学", "📚┃小学┃课堂", "filter_keys", ""),
    ("csp_初中", "📚┃初中┃课堂", "filter_keys", ""),
    ("csp_高中", "📚┃高中┃课堂", "filter_keys", ""),
    ("少儿教育", "📚少儿┃教育📚", "filter_keys", ""),
    ("小学课堂", "📚小学┃课堂📚", "filter_keys", ""),
    ("初中课堂", "📚初中┃课堂📚", "filter_keys", ""),
    ("高中教育", "📚高中┃教育📚", "filter_keys", ""),
    ("央视经典", "📺┃央视┃经典", "filter_keys", ""),
    ("py_cctv_少儿", "📺┃央视┃少儿", "filter_keys", ""),
    ("豆瓣1", "📢公告停更", "filter_keys", ""),
    ("push_agent", "关注公众号：熊猫是只肥猫", "filter_keys", ""),
    ("csp_wogg1", "🐲接口失效", "filter_name_patterns", "失效"),
    ("csp_woog2", "🐲关注公众号", "filter_name_patterns", "关注公众号"),
    ("夸快3", "❤装歌APP", "filter_name_patterns", "装歌"),
    ("夸快2", "❤重新领取", "filter_name_patterns", "重新领取"),
]

# 同名重复（8 个 = 4 个 key × 2 份，其中 key 为 push_agent 的共 4 份）
DEMO_DUPLICATES = [
    ("push_agent", "推送", "feimao", "肥猫", "wangerxiao", "王二小"),
    ("push_agent", "推送", "feimao", "肥猫", "4k", "4K小盒子"),
    ("push_agent", "推送", "feimao", "肥猫", "ouge", "讴歌"),
    ("push_agent", "推送", "feimao", "肥猫", "ok", "OK"),
    ("config", "配置", "ouge", "讴歌", "4k", "4K小盒子"),
    ("看球", "看球", "wangerxiao", "王二小", "4k", "4K小盒子"),
    ("豆瓣", "豆瓣", "4k", "4K小盒子", "ouge", "讴歌"),
    ("荐片", "荐片", "4k", "4K小盒子", "wangerxiao", "王二小"),
]

# 留下来的站：按族扎堆生成（不是真数据，只为了让报告上的 87 个站看起来合理）
DEMO_KEEP = [
    ("csp_Wex*Guard", ["Wexdiy", "Wexconfig", "Wexplayer", "Wexhls", "Wexlive",
                       "Wexm3u8", "Wexvod", "Wexpan", "Wexpush", "Wexweb"], 40),
    ("csp_DoubanGuard", ["Douban"], 8),
    ("csp_PanWebShare", ["PanWeb", "PanShare"], 8),
    ("csp_BiliGuard", ["Bili"], 6),
    ("csp_QuarkGuard", ["Quark"], 5),
    ("csp_AliGuard", ["Ali"], 5),
    ("csp_UcGuard", ["Uc"], 4),
    ("csp_TyysGuard", ["Tyys"], 4),
    ("csp_MiguGuard", ["Migu"], 4),
    ("csp_YsdqGuard", ["Ysdq"], 3),
]
DEMO_KEEP_NAMES = ["🐮通用类型┃配置中心🐮", "💥韩剧┃秒播💥", "🎬电影仓库", "📺电视直播合集",
                   "⚽体育直播", "🧸动漫小屋", "🎭短剧专区", "📚纪录片库", "🍿院线新片",
                   "🎵DJ音乐"]


# --demo 用的固定构建时间：样例报告要能一次渲染出同样的结果，不跟着机器时钟变
DEMO_BUILT_AT = "2026-10-03 15:20:07"


def build_demo_ctx(built_at: str = "", run_label: str = "第 1 份台账") -> dict:
    """构造固定的样例上下文：不联网、不读仓库里的输入文件。

    主口径（报告上最大的那三个数字全部对得上）：
        167 → 87，砍掉 80
    分类明细（三组有重叠，所以相加 80 + 16 + 8 = 104 > 80）：
        类不在包里 80 · 规则命中 16 · 同名重复 8
    """
    built_at = built_at or DEMO_BUILT_AT
    sources = []
    for sid, name, state, count, url, why in DEMO_SOURCES:
        # 三个状态各自的布尔值直接写死成表，不再用字符串比较推出来 ——
        # 少一层推导就少一处能出错、也少一处要读的地方。
        is_cache = (state == "cache")
        is_down = (state == "down")
        is_ok = not is_down
        errors = [] if not is_down else [
            {"url": url or f"http://{sid}.example/tv", "error": why}]
        sources.append({"id": sid, "name": name, "state": state, "ok": is_ok,
                        "from_cache": is_cache, "used_url": url,
                        "site_count": count, "errors": errors})

    # ── 组 1：类不在包里（80 个）──
    # 族名走和真实构建同一套算法（family_label_for）：同一组里的类名放在一起算
    # 公共前缀，Wexdiy/Wexconfig/Wexyun… 合起来就是 csp_Wex*Guard 一条。
    class_label = {}
    for classes, _n in DEMO_MISSING_GROUPS:
        label = family_label_for(f"csp_{classes[0]}", classes)
        for cls in classes:
            class_label[cls] = label
    for cls in DEMO_MISSING_SINGLES:
        class_label[cls] = f"csp_{cls}"

    def label_of(cls):
        return class_label.get(cls) or f"csp_{cls}"

    dropped = []
    for classes, n in DEMO_MISSING_GROUPS:
        for j in range(n):
            cls = classes[j % len(classes)]
            dropped.append(Drop(cls, "", f"csp_{cls}", "missing_class",
                                matched=cls, family=label_of(cls)))
    for cls in DEMO_MISSING_SINGLES:
        dropped.append(Drop(cls, "", f"csp_{cls}", "missing_class",
                            matched=cls, family=label_of(cls)))
    assert len(dropped) == 56, f"样例数据算错了：类不在包里 {len(dropped)} != 56"
    # 明细列表用具体例子覆盖掉前 6 条（它们只有类名，没有名字）
    dropped[:len(DEMO_MISSING_EXAMPLES)] = [
        Drop(key, name, f"csp_{cls}", "missing_class", matched=cls, family=label_of(cls))
        for key, name, cls in DEMO_MISSING_EXAMPLES]

    # ── 组 2：规则命中（16 个）──
    by_rule = [Drop(key, name, "", reason, matched=matched)
               for key, name, reason, matched in DEMO_RULE_HITS]
    assert len(by_rule) == 16, f"样例数据算错了：规则命中 {len(by_rule)} != 16"

    # ── 组 3：同名重复（8 个）──
    duplicates = [Drop(key, name, "", "duplicate",
                       kept_from=keep_src, dropped_from=drop_src)
                  for key, name, _ks, keep_src, _ds, drop_src in DEMO_DUPLICATES]
    assert len(duplicates) == 8, f"样例数据算错了：同名重复 {len(duplicates)} != 8"

    # ── 留下来的 87 个（按族铺开）──
    kept = []
    for fam, classes, n in DEMO_KEEP:
        for j in range(n):
            cls = classes[j % len(classes)]
            kept.append({"key": f"{cls}{j}", "name": DEMO_KEEP_NAMES[j % len(DEMO_KEEP_NAMES)],
                         "api": f"csp_{cls}", "family": fam})
    assert len(kept) == 87, f"样例数据算错了：留下 {len(kept)} != 87"

    # ── 源状态计数：写成显式循环，不用推导式里的布尔链 ──
    # （推导式里那种 `if a and not b` 的数法在本环境实测会少算一个，
    #   所以这里改成最笨、最好读、也最好验的写法。）
    n_live = n_cache = n_down = 0
    for s in sources:
        if s["from_cache"]:
            n_cache = n_cache + 1
        elif s["ok"]:
            n_live = n_live + 1
        else:
            n_down = n_down + 1
    assert n_live + n_cache + n_down == len(sources), "样例数据算错了：源状态计数对不上"

    ctx = {
        "built_at": built_at,
        "run_label": run_label,
        "sources": sources,
        "live": n_live,
        "cache": n_cache,
        "down": n_down,
        "spider_id": "feimao",
        "spider_name": "肥猫",
        "spider_classes": 880,
        "kept": len(kept),
        # 三个组的站【互不重叠】（每个站只命中一个剔除原因），所以相加就是全部输入
        "total_input": len(kept) + len(dropped) + len(by_rule) + len(duplicates),
        "dropped_all": dropped + by_rule + duplicates,
        "duplicates": duplicates,
        "delta_down": "+5",
        "delta_new": "3 个",
        "delta_gone": "2 个",
        "foot_scope": "本次为 --demo：固定的样例数据，不是当天真实抓取结果。",
    }

    # 报告上最大的那几个数字，最后再对一遍账：对不上就直接崩，
    # 别让一份"看起来对"的样例报告流出去。
    # 核心不变式：留下的 + 砍掉的 = 全部输入，且【三个组的数量之和 == 被砍掉的站数】。
    # 每个站只会命中一个剔除原因（filter_site 是 if/elif 语义，命中就 continue），
    # 所以三组之间【不该有重叠】—— 早先那句"三组相加大于总数是有重叠"是错的。
    cut = ctx["total_input"] - ctx["kept"]
    assert cut == len(ctx["dropped_all"]), \
        f"样例数据算错了：砍掉 {cut} != 剔除记录 {len(ctx['dropped_all'])}"
    assert len(dropped) + len(by_rule) + len(duplicates) == cut, \
        f"样例数据算错了：三组 {len(dropped)}+{len(by_rule)}+{len(duplicates)} != 砍掉 {cut}"
    assert ctx["total_input"] == 167 and ctx["kept"] == 87, \
        f"样例数据算错了：{ctx['total_input']} → {ctx['kept']} 应该是 167 → 87"
    assert ctx["live"] == 3 and ctx["cache"] == 1 and ctx["down"] == 5, \
        f"样例数据算错了：源状态 {ctx['live']}/{ctx['cache']}/{ctx['down']} != 3/1/5"
    return ctx


# ════════════════════════════════════════════════════════════
# 九、上次构建状态（读 status.json 做差分；不再有任何跨轮累积的规则）
# ════════════════════════════════════════════════════════════

def load_prev_status(path=None):
    """读上一次的 status.json（没有就返回 None）。path 为 None 时用 OUT_STATUS。"""
    path = path or OUT_STATUS
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARN] 上一次的 status.json 读不出来（{e}），本次按首次构建处理")
        return None


def update_failure_streak(cfg, cohort):
    """【已停用】原来是"站点连续失败 N 次就自动进黑名单"。

    ★ 2026-10-03 删除该机制，原因（真机踩出来的）：

      它把"类不存在"当成本站失败并跨轮累加，连续 3 次就把 key 追加进
      blacklist_keys（并写回 filter.json）。而 filter_site 的优先级里
      黑名单(第2) 高于 类校验(第6) —— 一旦写进去，那个站【再也不会被重新评估】。

      踩坑场景：spider_source 选成了 wangxiao 的 jar（290 类），而池子里
      肥猫那一族的站（豆瓣/潮流/肥猫/干荐片/看球…）叫的类只在肥猫的 jar（512 类）里。
      于是它们连续 3 轮被判 missing_class → 38 个好站被永久拉黑。
      后来换回肥猫的 jar，这 38 个站明明能跑了，却因为黑名单优先级更高而永远出不来。

      更根本的问题：jar 读失败时 spider_classes 是 None → 类校验【整个跳过】，
      所以"网络抖动导致误判"这条路径根本不存在 —— 那个 ×3 的等待期防的是
      一个不存在的风险，却引入了一个真风险（陈旧累积、名单只增不减）。

      现在的做法：类存在性【每轮实时判、当场剔、不记录】。判据是当轮 jar 的函数，
      存下来必然过期。

    保留这个函数名是为了让旧调用点/旧测试有明确的失败信息，而不是静默什么都不做。
    """
    raise RuntimeError(
        "update_failure_streak 已停用（2026-10-03）：类存在性改为每轮实时判定，"
        "不再累积任何黑名单。filter.json 只由人维护，脚本不写它。")


# ════════════════════════════════════════════════════════════
# 十、主流程
# ════════════════════════════════════════════════════════════

def log(*a):
    print(*a, flush=True)


def build_ctx_from_status(status, prev_status, dropped_all, duplicates):
    """把 status.json 的内容折成报告上下文（--demo 之外都走这条路）。"""
    # 三状态一个站只算一次，算完加总必须等于源总数 —— 写成显式循环，
    # 既不依赖推导式里布尔链的写法，也顺手给「加起来等于总数」留了道保险。
    live = cache = down = 0
    for s in status["upstream_sources"]:
        if s["from_cache"]:
            cache = cache + 1
        elif s["ok"]:
            live = live + 1
        else:
            down = down + 1
    assert live + cache + down == len(status["upstream_sources"]), \
        "源状态计数对不上（live+cache+down != 源总数）"

    sites = status["sites"]
    spider = status["spider"]

    # 与上次的差分。首次构建时三个数字没有意义，统一写成 "—"，
    # 由 $run_label 去说"首次构建"这件事 —— 把 "首次构建" 塞进"死源"那一格是错的。
    if prev_status is None:
        run_label, d_down, d_new, d_gone = "首次构建", "—", "—", "—"
    else:
        prev_ids = {s["id"] for s in prev_status.get("upstream_sources", [])}
        cur_ids = {s["id"] for s in status["upstream_sources"]}
        new_ids = sorted(cur_ids - prev_ids)
        gone_ids = sorted(prev_ids - cur_ids)
        prev_down = {s["id"] for s in prev_status.get("upstream_sources", []) if not s["ok"]}
        cur_down = {s["id"] for s in status["upstream_sources"] if not s["ok"]}
        run_label = f"第 {int(prev_status.get('build_no') or 1) + 1} 份台账"
        diff = len(cur_down) - len(prev_down)
        d_down = f"+{diff}" if diff >= 0 else str(diff)
        d_new = f"{len(new_ids)} 个"
        d_gone = f"{len(gone_ids)} 个"

    return {
        "built_at": status["built_at"],
        "run_label": run_label,
        "sources": status["upstream_sources"],
        "live": live,
        "cache": cache,
        "down": down,
        "spider_id": spider.get("source_id"),
        "spider_name": spider.get("source_name"),
        "spider_classes": spider.get("class_count"),
        "kept": sites["kept"],
        "total_input": sites["total_input"],
        "dropped_all": dropped_all,
        "duplicates": duplicates,
        "delta_down": d_down,
        "delta_new": d_new,
        "delta_gone": d_gone,
        "foot_scope": status.get("foot_scope", ""),
    }


def choose_home(kept_sites, candidates):
    """【已停用】原来是挑一个 key 写进产物的 `home` 字段当默认首页。

    ★ 2026-10-03 停用，因为那个字段对本站 App 是【死的】：
      App 决定首页的是 VodConfig.java:288 的三级回退 ——
        ① custom.home()：用户在 App 里手动选过的（存本地数据库）
        ② 云端 config.json 的 home 字段（用 key 去 filter 站点）
        ③ 都没有 → getSites().get(0)
      而 VodConfig 里读的 JSON 键是 logo/notice/danmaku/wallpaper/spider/sites/parses，
      【没有 home】（全仓库唯一读它的是 ConfigImport.java:57，那里把它当"配置名字"用）。
      所以写了也不生效，只会让人以为首页被指定了。

      想改默认首页只能改【合并顺序】（sources.json 的源顺序 → sites[0]）。

    保留函数是为了让旧调用点/旧测试拿到明确的失败信息，而不是静默返回空。
    """
    raise RuntimeError(
        "choose_home 已停用（2026-10-03）：产物不再写 home 字段（App 不读它）。"
        "默认首页 = sites[0]，由 sources.json 的源顺序决定。")


def main() -> int:
    ap = argparse.ArgumentParser(description="外部来源快照构建器")
    ap.add_argument("--demo", action="store_true",
                    help="用固定样例数据渲染报告：不联网、不抓取，退出码 0")
    ap.add_argument("--cache-dir", default=None,
                    help="网络失败时从该目录读 <sid>.json 兜底，并把本次成功结果写回")
    args = ap.parse_args()

    now = datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

    # ── demo：只渲染一份样例报告，绝不联网、绝不覆盖 config.json / status.json ──
    if args.demo:
        # 时间用固定的那个常数，报告上的时间戳就不会跟着机器时钟跑
        log(f"[DEMO] 用样例数据渲染报告（{DEMO_BUILT_AT}）")
        ctx = build_demo_ctx(DEMO_BUILT_AT, "第 1 份台账")
        ok = render_report(ctx)
        log("=" * 60)
        log(f"[DEMO] 上游源 {ctx['live']} 活 / {ctx['cache']} 缓存 / {ctx['down']} 挂")
        # 主口径：167 → 87，砍掉 80。三个组各记一个原因、互不重叠，
        # 所以 56 + 16 + 8 正好等于 80（不是"有重叠所以大于总数"）。
        cut = ctx["total_input"] - ctx["kept"]
        n_missing = sum(1 for d in ctx["dropped_all"] if d.reason == "missing_class")
        n_rule = sum(1 for d in ctx["dropped_all"]
                     if d.reason not in ("missing_class", "duplicate"))
        log(f"[DEMO] 站点 {ctx['total_input']} → {ctx['kept']}，砍掉 {cut} 个")
        log(f"[DEMO] 剔除原因：类不在包里 {n_missing} · 规则命中 {n_rule} · "
            f"同名重复 {len(ctx['duplicates'])}")
        log("[DEMO] 报告" + ("已生成。" if ok else "跳过（模板不存在，这是预期内的优雅降级）。"))
        return 0

    cfg = load_filter()
    with open(SOURCES_FILE, encoding="utf-8") as f:
        src_cfg = json.load(f)

    only = set(cfg.get("only_sources") or [])
    sources = [s for s in src_cfg.get("sources", []) if not only or s.get("id") in only]
    wanted_spider = src_cfg.get("spider_source")
    prev_status = load_prev_status()

    log(f"源清单：{len(sources)} 个    spider 指定来自：{wanted_spider or '(未指定)'}")

    fetched = []          # [(sid, name, data, from_cache)]
    upstream_sources = []

    for src in sources:
        sid = src.get("id") or src.get("name")
        name = src.get("name", "未知")
        entry = {"id": sid, "name": name, "ok": False, "from_cache": False,
                 "used_url": None, "site_count": 0, "errors": []}
        saved = False

        for url in src.get("urls", []):
            try:
                data = parse_json_lenient(fetch_text(url))
                ok, why = validate(data)
                if not ok:
                    # ⑫ 校验不达标 → 继续试下一个备用地址，而不是直接放弃这一家
                    entry["errors"].append({"url": url, "error": f"校验不通过：{why}"})
                    log(f"[SKIP] {name}  {url}  ->  {why}")
                    time.sleep(URL_INTERVAL)
                    continue
                fetched.append((sid, name, data, False))
                entry.update(ok=True, used_url=url, site_count=len(valid_sites(data)))
                log(f"[OK]   {name}  <-  {url}  ({entry['site_count']} 站点)")
                saved = True
                break
            except Exception as e:
                # ⑨ 每个地址的失败原因都单独记一条，不再只留最后一个
                entry["errors"].append({"url": url, "error": str(e)[:300]})
                log(f"[FAIL] {name}  {url}  ->  {e}")
            time.sleep(URL_INTERVAL)

        # 兜底缓存。同时把成功的结果写回缓存目录，这样缓存目录会自己滚动更新，
        # 不需要额外挂一个"上传上一天产物"的 Action 步骤。
        if args.cache_dir:
            cache = os.path.join(args.cache_dir, f"{sid}.json")
            if not saved and os.path.exists(cache):
                try:
                    with open(cache, encoding="utf-8") as f:
                        cached = json.load(f)
                    ok, why = validate(cached)
                    if ok:
                        fetched.append((sid, name, cached, True))
                        entry.update(ok=True, from_cache=True, used_url="(缓存)",
                                     site_count=len(valid_sites(cached)))
                        entry["errors"].append({"url": cache, "error": "网络未成功，改用本地缓存"})
                        saved = True
                        log(f"[CACHE] {name}  <-  本地缓存（{entry['site_count']} 站点）")
                    else:
                        entry["errors"].append({"url": cache, "error": f"缓存也不合格：{why}"})
                except Exception as e:
                    entry["errors"].append({"url": cache, "error": str(e)[:300]})
            elif saved and not entry["from_cache"]:
                try:
                    os.makedirs(args.cache_dir, exist_ok=True)
                    with open(cache, "w", encoding="utf-8") as f:
                        json.dump(next(d for i, _n, d, _c in fetched if i == sid), f,
                                  ensure_ascii=False, indent=2)
                except Exception as e:
                    log(f"[WARN] 写缓存失败（不影响结果）：{e}")

        if not saved:
            entry["note"] = "所有地址均失败"
            log(f"[FAIL] {name}  全部地址失败")
        upstream_sources.append(entry)
        time.sleep(SOURCE_INTERVAL)

    # ⑧ 只有【本次真的抓到】的源才有资格提供 spider。
    #    缓存源不能用：它的 spider 可能指向一个早就下线的 jar，
    #    而静态筛的结果完全依赖这个 jar 的类清单，选错会把好站全判死。
    live_ids = [f[0] for f in fetched if not f[3]]
    spider_id = wanted_spider if wanted_spider in live_ids else (live_ids[0] if live_ids else None)
    spider_note = ""
    if wanted_spider and spider_id != wanted_spider:
        spider_note = (f"指定的 {wanted_spider} 本次没抓到，"
                       f"回退到 {spider_id or '(无)'}；静态筛结果可能与预期不同")

    spider_classes, spider_url, spider_name = None, None, None
    spider_value = None          # 最终要写进 config.json 的那一份（可能已改成 https）
    scheme_note = ""             # scheme 改写的说明（优先级低于回退/读 jar 失败）
    if spider_id:
        rec = next((f for f in fetched if f[0] == spider_id), None)
        if rec:
            spider_name = rec[1]
            # ⚠️ 只在【这里】做一次 scheme 改写，并把结果既用于读类清单、又用于写 config.json。
            # 两处必须同源：否则 App 加载的 jar 和我们做静态筛的 jar 不是同一份，
            # 校验过的类可能不在 App 的包里（这是以前踩过的同一个坑）。
            spider_value, scheme_note = https_equivalent(str(rec[2].get("spider") or ""))
            if spider_note:
                # 回退这类原因更要紧，别被 scheme 的说明盖掉
                scheme_note = ""
            elif scheme_note:
                log(f"  spider 地址处理：{scheme_note}")
                spider_note = scheme_note
            spider_url = spider_url_of(spider_value)
            if spider_url:
                try:
                    log(f"读取 spider 的类清单：{spider_url[:80]}…")
                    spider_classes = jar_class_names(fetch_bytes(spider_url))
                    log(f"  -> jar 里有 {len(spider_classes)} 个类")
                except Exception as e:
                    # jar 读不出来时【不能】让静态筛按空集跑：那会把所有 csp_ 站全判死。
                    # 跳过静态筛是更保守的选择（少砍几个站），并在报告里写清楚。
                    log(f"  -> 读 jar 失败：{e}（本次跳过类存在性校验）")
                    spider_note = f"jar 读取失败，本次跳过类存在性校验：{e}"[:200]

    cohort = {}
    merged = merge(fetched, spider_classes, cfg, cohort)
    if merged is None:
        log("[FATAL] 没有任何源可用（config.json / status.json / report.html 均未改动）")
        return 1

    kept = cohort["kept"]
    dropped_all = cohort["dropped"]
    duplicates = [d for d in dropped_all if d.reason == "duplicate"]

    # ⚠️ config.json 里的 spider 必须来自【真正被我们拿去读 jar 的那个源】(spider_id)。
    # merge() 只会把 fetched[0]（也就是优先级最高、可能是缓存的那一家）的 spider 带进 base，
    # 所以当 spider_id 发生回退（指定的源没抓到、或它只是个缓存源）时，
    # App 加载的 jar 和我们做类校验用的 jar 就不是同一个 —— 校验过的类不在 App 的包里，
    # 结果就是"配置看着没问题，装上去一半的站打不开"。这里显式改写成选定的那一份。
    #
    # 注意用的是上面已经过 scheme 校验的 spider_value（不是源里的原文）：
    # App 只接受 https 的 jar，明文 http 会被 JarLoader 直接拒绝。
    # 如果 url 本身带非 ascii（中文域名），写进配置前先转 punycode，否则那个地址没法用。
    if spider_value:
        merged["spider"] = sanitized_spider(spider_value)

    # ★ 这里原来会调 update_failure_streak(cfg, cohort) 并把结果 save_filter(cfg) 写回
    #   filter.json（自动拉黑）。2026-10-03 整段删除：
    #   filter.json 现在【只由人维护】，脚本只读不写；类存在性每轮实时判定。
    #   详见 update_failure_streak 的说明（那个函数现在会直接抛异常，防止有人再调）。
    if not (kept or dropped_all):
        log("[WARN] 本次一个站点都没拿到（config.json 仍会照常产出，见下面的拒空闸）")

    merged["warningText"] = (
        f"本配置由 {len([s for s in upstream_sources if s['ok']])} 个公开源聚合，"
        f"已做类存在性校验与规则清洗；仅供学习交流，请勿商用"
    )

    # ★ 不再往产物 config.json 里写 home 字段，也不再算它。
    #   理由（2026-10-03 在真机 + 源码上核实过）：
    #   App 决定首页用的是 VodConfig.java:288 的三级回退 ——
    #     ① custom.home()：用户在 App 里手动选过的（存本地数据库）
    #     ② 云端 config.json 的 home 字段（用 key 去 filter 站点）
    #     ③ 都没有 → getSites().get(0)
    #   而实测【云端这个 home 字段对本站 App 是死的】：VodConfig 里读的 JSON 键是
    #   logo/notice/danmaku/wallpaper/spider/sites/parses，没有 home
    #   （全仓库唯一读它的是 ConfigImport.java:57，那里把它当"配置名字"用）。
    #   旧配置（用户一直在用的那份）本来也没有这个字段。
    #
    #   后果：默认首页 = sites[0] = 【合并顺序里第一个被保留的站】，
    #   而合并顺序由 sources.json 的源顺序决定。想改默认首页就改源顺序。
    log("首页：不写 home 字段（App 取 sites[0] 或用户在电视上手动选的那个）")

    with open(OUT_CONFIG, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    def group(reason):
        return [d.as_dict() for d in dropped_all if d.reason == reason]

    status = {
        "built_at": now,
        "generator": GENERATOR,
        "build_no": int((prev_status or {}).get("build_no") or 0) + 1,
        "spider": {"source_id": spider_id, "source_name": spider_name,
                   "url": spider_url,
                   "class_count": len(spider_classes) if spider_classes else None,
                   "note": spider_note},
        "upstream_sources": [
            {"id": s["id"], "name": s["name"], "ok": s["ok"], "from_cache": s["from_cache"],
             "used_url": s["used_url"], "site_count": s["site_count"], "errors": s["errors"]}
            for s in upstream_sources
        ],
        "sites": {
            "total_input": cohort["total_input"],
            "kept": kept,
            "dropped": {
                "missing_class": group("missing_class"),
                "by_rule": [d.as_dict() for d in dropped_all if d.reason not in
                            ("missing_class", "duplicate")],
                "duplicate": [d.as_dict() for d in duplicates],
            },
        },
        "counts": {
            "live": sum(1 for s in upstream_sources if s["ok"] and not s["from_cache"]),
            "cache": sum(1 for s in upstream_sources if s["from_cache"]),
            "down": sum(1 for s in upstream_sources if not s["ok"]),
        },
        # 已停用：脚本不再自动拉黑任何站（见 update_failure_streak 的说明）。
        # 保留这个空字段是为了不让下游读 status.json 的地方 KeyError。
        "auto_blacklisted": [],
    }
    with open(OUT_STATUS, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)

    ctx = build_ctx_from_status(status, prev_status, dropped_all, duplicates)
    render_report(ctx)

    log("")
    log("=" * 60)
    log(f"源：{status['counts']['live']} live / {status['counts']['cache']} cache / "
        f"{status['counts']['down']} down")
    log(f"spider 来自：{spider_id}（{spider_name}，"
        f"{len(spider_classes) if spider_classes else '未知'} 个类）")
    log(f"站点：{cohort['total_input']} → {kept}")
    log(f"  因【类不在 jar 里】剔除：{len(group('missing_class'))}")
    log(f"  因【规则】剔除：{len(status['sites']['dropped']['by_rule'])}")
    log(f"  因【重复】丢弃：{len(duplicates)}")
    log(f"输出：{OUT_CONFIG} · {OUT_STATUS} · {OUT_REPORT}")
    return 0 if (status["counts"]["live"] or status["counts"]["cache"]) else 1


if __name__ == "__main__":
    sys.exit(main())
