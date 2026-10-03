# 集成测试报告（真实池子 vs 上游单仓）

> 由 `python -m unittest tests.test_integration -v` 生成，每次跑覆盖本文件。数字全部动态计算，上游每天更新两次。
>
> 本次真实构建用时 518 秒（测试里把 `time.sleep` 换成了空操作：源间隔 3 秒、重试退避 6 秒，真睡的话这一层要十几分钟；抓取 / 重试 / 校验 / 合并 / 渲染全都是真的，输入用的是仓库里真实的 config/sources.json 与 config/filter.json）。

## 1. 上游源（10 个）

| id | 名字 | 状态 | 站点数 | 用的地址 |
| --- | --- | --- | --- | --- |
| feimao | 肥猫 | 活 | 39 | `http://肥猫.net/tv` |
| fantaiying | 饭太硬 | 挂 | — | `—` |
| wangerxiao | 王二小 | 活 | 96 | `https://9280.kstore.vip/aiwex.json` |
| ouge | 讴歌 | 挂 | — | `—` |
| moyu | 摸鱼 | 挂 | — | `—` |
| ok | OK | 挂 | — | `—` |
| xiaomi | 小米 | 挂 | — | `—` |
| qiaoji | 巧记 | 挂 | — | `—` |
| 4k | 4K小盒子 | 活 | 53 | `http://xhztv.top/4k.json` |
| xiaosa | 潇洒 | 挂 | — | `—` |

合计：3 活 / 0 缓存 / 7 挂

## 2. 我们的产出

- 站点数：**68**
- spider：`https://img2.gelonghui.com/library/46da6-aa33493f-1c1d-4f35-9990-0be4bdbf0c64.png;md5;46da6b6a6a111d7924717db2ae790de3`（512 个类）
- 退出码：0

复核结果：留存的站里，api 的类不在 jar 里的有 **0** 个（应该永远是 0）

## 3. 剔除原因分布

| 原因 | 数量 |
| --- | --- |
| 类不在 jar 里 | 105 |
| 规则：blacklist_keys | 13 |
| 规则：blacklist_name_patterns | 0 |
| 规则：blacklist_api_families | 0 |
| 规则：blacklist_hosts | 0 |
| 同名重复 | 2 |

输入 186 → 保留 68

## 4. 与上游单仓对比

| | 数量 |
| --- | --- |
| 上游站点数 | 167 |
| 我们保留 | 68 |
| 两边都有 | 68 |
| 上游有、我们没有 | 99 |
| 我们有、上游没有 | 0 |

## 5. 【误杀检查】上游有 + 我们也有 + 我们剔掉了

候选总数：**33** 个；其中：类不在包里 20 · 同名重复 2 · 命中规则 11；**解释不出来的：0**

### 类不在 jar 里（客观判死，已现场重读 jar 复核）（20 个）

| key | 名字 | 理由 |
| --- | --- | --- |
| 77 | 👒┃七七┃App | `Kunyu77` 不在这次用的 jar 里 |
| AList | 👁️Alist┃DIY👁️ | `AListGuard` 不在这次用的 jar 里 |
| Douban | 🐮【免费分享】🐮 | `NewDouBanGuard` 不在这次用的 jar 里 |
| PanSou | 🦊┃盘搜┃搜索 | `PanSou` 不在这次用的 jar 里 |
| UpYun | 😻┃Up搜┃搜索 | `UpYun` 不在这次用的 jar 里 |
| Wexconfig | 🐮配置┃中心🐮 | `PanConfigGuard` 不在这次用的 jar 里 |
| biliych |  🅱‍哔哩┃歌曲🅱‍ | `BiliGuard` 不在这次用的 jar 里 |
| csp_Nmys | 🌾┃农民┃直连 | `Nmys` 不在这次用的 jar 里 |
| csp_WoGG | 👽┃玩偶┃4K① | `WoGG` 不在这次用的 jar 里 |
| 一起看 | ❤┃一起┃2K | `YQKan` 不在这次用的 jar 里 |
| 七夜 | 😾┃七夜┃搜索 | `Dovx` 不在这次用的 jar 里 |
| 九六 | 🎀┃九六┃直连 | `Cs1369` 不在这次用的 jar 里 |
| 南坊 | 🎃┃南坊┃App | `AppMao` 不在这次用的 jar 里 |
| 南瓜 | 🎃┃南瓜┃App | `NanGua` 不在这次用的 jar 里 |
| 在线┃直播1 | 📺┃竞技┃直播 | `Yj1211` 不在这次用的 jar 里 |
| 小柚 | 🍊┃小柚┃App | `AppSK` 不在这次用的 jar 里 |
| 玩偶 | 💓‍玩偶┃4K💓‍ | `AiNewWoggGuard` 不在这次用的 jar 里 |
| 繁星 | 💥┃繁星┃App | `AppMao` 不在这次用的 jar 里 |
| 萌米 | 👀┃萌米┃App | `AppMao` 不在这次用的 jar 里 |
| 賤賤 | 💥贱片┃秒播💥 | `WexAiJianPianGuard` 不在这次用的 jar 里 |

### 同名重复（我们保留了另一份）（2 个）

| key | 名字 | 理由 |
| --- | --- | --- |
| 荐片 | 🧲┃荐片┃磁力 | 保留了 `肥猫` 的那一份 |
| 豆瓣 | 🔥公众号：聚玩盒 | 保留了 `肥猫` 的那一份 |

### 命中 filter.json 规则（理由已用规则重跑复现）（11 个）

| key | 名字 | 理由 |
| --- | --- | --- |
| csp_初中 | 📚┃初中┃课堂 | `csp_初中`：这个 key 在手工黑名单里 |
| csp_小学 | 📚┃小学┃课堂 | `csp_小学`：这个 key 在手工黑名单里 |
| csp_少儿 | 📚┃少儿┃教育 | `csp_少儿`：这个 key 在手工黑名单里 |
| csp_高中 | 📚┃高中┃课堂 | `csp_高中`：这个 key 在手工黑名单里 |
| push_agent | 📽推送 | `push_agent`：这个 key 在手工黑名单里 |
| py_cctv_少儿 | 📺┃央视┃少儿 | `py_cctv_少儿`：这个 key 在手工黑名单里 |
| 初中课堂 | 📚初中┃课堂📚 | `初中课堂`：这个 key 在手工黑名单里 |
| 央视经典 | 📺┃央视┃经典 | `央视经典`：这个 key 在手工黑名单里 |
| 小学课堂 | 📚小学┃课堂📚 | `小学课堂`：这个 key 在手工黑名单里 |
| 少儿教育 | 📚少儿┃教育📚 | `少儿教育`：这个 key 在手工黑名单里 |
| 高中教育 | 📚高中┃课堂📚 | `高中教育`：这个 key 在手工黑名单里 |

> 结论：有理由的剔除 33 条，**无理由的误杀 0 条**。

## 6. 上游有、我们连见都没见到的站

数量：**68**

其中 8 个是上游【给重名源加前缀改名】后的产物（如 `4k_荐片` = 我们按原名 `荐片` 处理过的那个站），真正没见到的还有 60 个 —— 它们来自我们这轮没抓到的源，不算误杀。

改名的那批（前 8 个）：`4k_csp_Nmys`→`csp_Nmys`、`4k_push_agent`→`push_agent`、`4k_荐片`→`荐片`、`4k_豆瓣`→`豆瓣`、`ouge_config`→`config`、`ouge_push_agent`→`push_agent`、`wangerxiao_push_agent`→`push_agent`、`wangerxiao_看球`→`看球`

剩下的前 20 个：`115`, `Doubana`, `Doubanaaa`, `Iktv`, `Wex275tingshu`, `Wex97pansoGuard`, `WexHaiYinsoGuard`, `WexLunhuiDJ`, `WexNewBiLiLive`, `WexNewDouYu`, `WexNewHuYa`, `WexWo123panGuard`, `WexbaidusoGuard`, `Wexbaobaobashi`, `Wexbeiwa`, `Wexbiliys`, `WexbttwoGuard`, `Wexduanju001`, `Wexduanjuhema`, `Wexduanjusuipian`

