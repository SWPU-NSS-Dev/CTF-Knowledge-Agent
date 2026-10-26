# DSH 工作记录 — CTF-Knowledge-Agent 来源适配器

> 本文件由 DSH（DeepSeek Harness）代理维护，记录每个工作轮次的实际改动、验证结果和遗留问题。
> 与 `docs/adapters.md`（面向使用者的模块文档）不同，本文件面向**后续接手的代理**。

## 轮次 1 — 2026-10-26

**分支**：`feat/sources-ctf-writeups`
**角色**：Source Adapter Developer（只新增来源模块，不改调度器/存储/API/CLI 公共接口）

### 交付内容

新增 5 个站点专用适配器（注册名即 `adapter` 值）：

| 适配器 | 文件 | 站点 | 发现方式 | 正文提取 |
| --- | --- | --- | --- | --- |
| `ctftime` | `ctfka/crawler/sources/ctftime.py` | ctftime.org | `/writeups?page=N` 表格分页，404 判定列表末尾 | `#id_description` |
| `anquanke` | `ctfka/crawler/sources/anquanke.py` | www.anquanke.com | sitemap 索引 + 子 sitemap，`<lastmod>` 水位增量 | `div.content#js-article` |
| `kanxue` | `ctfka/crawler/sources/kanxue.py` | bbs.kanxue.com | `forum-<id>.htm` + `forum-<id>-<page>.htm` | `div.message.message_rich_type`（仅首帖） |
| `exploitdb` | `ctfka/crawler/sources/exploitdb.py` | www.exploit-db.com | `search?draw&start&length` JSON，offset 从新到旧 | `/raw/<id>` 纯文本 |
| `hacktricks` | `ctfka/crawler/sources/hacktricks.py` | hacktricks.wiki | `/<lang>/sitemap.xml` | `#content main` |

改动文件（全部在允许范围内）：

```
新增  ctfka/crawler/sources/{ctftime,anquanke,kanxue,exploitdb,hacktricks}.py
新增  tests/test_source_{ctftime,anquanke,kanxue,exploitdb,hacktricks}.py
新增  tests/source_fixtures.py          # 离线 fixture 客户端
新增  tests/fixtures/{ctftime,anquanke,kanxue,exploitdb,hacktricks}/   # 真实格式样本
改动  ctfka/crawler/registry.py         # 注册 5 个内置名
改动  ctfka/crawler/builtin.py          # 兼容再导出
改动  docs/adapters.md                  # 模块说明 + 可复制 YAML
```

未改动：`ctfka/scheduler/`、`ctfka/models/`、`ctfka/api/`、`ctfka/cli/`、`ctfka/ingestion/parser.py`、`ctfka/crawler/http.py`。

### 侦察结论（实测，非假设）

- **robots.txt**：ctftime 要求 `Crawl-delay: 10`；kanxue 禁止 `/admin`、`/user-*`、`/search-*` 等；anquanke/exploit-db/hacktricks 允许抓取。
- **通用 `html` 适配器不足的实证**：
  - HackTricks 的 `<main>` 外壳经通用解析只有 4 字符正文，定向取 `#content main` 得 15,853 字符 + 4 个代码块。
  - CTFtime 通用解析 3,331 字符里大量是导航栏；定向取 `#id_description` 得 2,113 字符正文。
  - anquanke 通用解析 6,661 字符含导航噪声；定向取 `js-article` 得 4,039 字符。
- **看雪翻页格式纠正**：页面里出现的 `forum-171-1-12___.htm` 实际回放第 1 页；真实翻页是 `forum-171-2.htm`。越界页（如第 1000 页）返回 200 并回放末页，因此“整页 id 已全见”是板块结束信号。
- **奇安信攻防社区（forum.butian.net）** 正文需登录才能看见，判定为不可公开采集，未实现适配器（避免存入空壳）。

### 验证结果（本轮实测）

```
$ pytest -q
163 passed, 1 warning in 18.21s

$ pytest tests/test_source_*.py -q
71 passed          # 5 个新适配器测试

$ ruff check ctfka/crawler/ tests/test_source_*.py tests/source_fixtures.py
All checks passed!
```

真实小量线上采集（`outputs/live_acceptance.py`，经真实 `CrawlService` 端到端）：

| 来源 | 发现 | 入库 | 耗时 | 正文长度 | 来源状态 |
| --- | --- | --- | --- | --- | --- |
| ctftime | 3 | 3 | 43.4s | 1050–1260 | 限速 0.1 rps（robots Crawl-delay:10） |
| anquanke | 3 | 3 | 29.3s | 2990–4036 | `resume_index: 223`（sitemap1 内） |
| kanxue | 3 | 3 | 4.8s | 2118–3182 | `next_index: 3`（页内偏移） |
| exploitdb | 3 | 3 | 12.6s | 5126–43194 | `next_offset: 49445 / total 49449` |
| hacktricks | 3 | 3 | 8.7s | 完整文章 | `pages` 记录 3 条 |

数据库统计：`sources: 5, articles: 15, versions: 15, raw_bytes: 1,191,120`。
检索命中：`IDA`、`exploit`、`CVE`、`逆向` 均返回上述已入库文章。

真实样本示例：
- ctftime `Relay Artifact TX-7`（RSA 共享素数，含 6 个代码块）
- anquanke `Node.js、Python、Bun全中招：一个压缩库的内存漏洞`
- kanxue `[分享]pwn网站收集`
- exploitdb `PHP2Shell (CVE-2026-109618) - PHP core versions after v5.4 - RCE`（43,194 字符源码）
- hacktricks `CVE-2021-30807: IOMobileFrameBuffer OOB`

### 本轮修掉的真实缺陷（都是测试逼出来的）

1. `FetchedDocument.url` 用 raw 端点会让入库 URL 变成 `/raw/<id>`，与发现身份 `/exploits/<id>` 不一致 → 改为始终报告记录页。
2. anquanke sitemap 指纹跳过会导致“文章更新无法发现” → 改为按 `<lastmod>` 水位增量（水位+同水位 id 集合），跨轮重新读取子 sitemap。
3. anquanke 截断时误推进水位会跳过未读文章 → 只有完整读完一个子 sitemap 才推进。
4. kanxue 截断记 `next_page` 会重复入队整页 → 改记 `next_index` 页内偏移。
5. exploitdb 假定服务端 honour 了 `length` → 按实际返回行数校准 offset 网格。
6. 各适配器只读 `cursor` 参数而忽略 `set_state()` → 两者都支持。

### 遗留问题 / 后续注意

- **未实现**：upstream 的 `sitemap` 适配器与 `tests/test_sitemap_adapter.py` 属于其他 agent 的在途改动，本轮未纳入提交。
- **未实现适配器的站点**：奇安信攻防社区（需登录）、先知社区 xz.aliyun.com（文章页有阿里云 WAF 的 JS 挑战，未做绕过）、安全客的 RSS 未发布。
- **看雪加密链接**：`mark.encrypted` 是站方跳转保护混淆串，适配器原样保留在 `text` 代码块中，**不执行**站方解码脚本。
- **exploitdb** 的 JSON 接口对部分排序参数返回 500，只用默认顺序（最旧在前）+ 从高位 offset 回溯。
- 空壳保护：需要登录的文章由 `validate` 抛错进入失败分类，不会存入空正文。
- 若后续要放宽 `ctftime` 速率，请先看 robots 的 `Crawl-delay: 10`。

### 复现命令

```powershell
# 离线测试
.venv\Scripts\python.exe -m pytest tests/test_source_ctftime.py tests/test_source_anquanke.py `
  tests/test_source_kanxue.py tests/test_source_exploitdb.py tests/test_source_hacktricks.py -q

# 真实小量采集（会访问公网）
.venv\Scripts\python.exe outputs/live_acceptance.py
```
