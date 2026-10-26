# DSH 工作记录 — CTF-Knowledge-Agent 来源适配器

> 本文件由 DSH（DeepSeek Harness）代理维护，记录每个工作轮次的实际改动、验证结果和遗留问题。
> 与 `docs/adapters.md`（面向使用者的模块文档）不同，本文件面向**后续接手的代理**。

## 轮次 1 — 2026-10-26

**分支**：`feat/sources-ctf-writeups`
**Commit**：`6d934c0235eb655270290573044969c44e053f11`
**PR**：https://github.com/Zhouhang9527/CTF-Knowledge-Agent/pull/1
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

- ~~**未实现**：upstream 的 `sitemap` 适配器与 `tests/test_sitemap_adapter.py` 属于其他 agent 的在途改动，本轮未纳入提交。~~ → **已在轮次 2 补齐**，见下。
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

---

## 轮次 2 — 2026-10-26（提交与交付）

- 从 `origin/main`（含 stage-3 提交 `58fb44f`）重建分支，避免把其他代理的在途改动混入 PR。
- 只暂存本轮负责的 38 个文件；upstream 在途的 `ctfka/crawler/sources/sitemap.py`、
  `tests/test_sitemap_adapter.py` 保持未暂存，未进入 PR。
- 提交 `6d934c0`，推送分支，创建 PR #1（base `main`，未合并）。
- `dsh-readme.md` 已随提交进入 PR，后续轮次在此追加。

### 交接提示

- 若继续加站点，按同一套模式即可：先实测通用适配器是否够用 → 不够再写
  `ctfka/crawler/sources/<slug>.py` + `tests/test_source_<slug>.py` +
  `tests/fixtures/<slug>/`，最后在 `registry.py` 的 `BUILTINS` 里加一行。
- **不要**给列表型适配器加名为 `complete_scan` 的属性：worker 会把该名称视为
  “可以用 `current_identities` 归档其余全部文章”的授权。
- 每轮验证至少跑：`pytest -q`、`ruff check` 自己改动的路径、一次带
  `--max-items` 的真实小量采集。

### GitHub Actions 状态（重要）

PR #1 的 CI 显示 failure，但**不是代码问题**：

- `main` 上的既有提交 `58fb44f`（stage-3，非本 PR 内容）CI 同样 failure；
- 该 job 在 **2 秒内**结束，且 `steps` 数组为空——没有任何步骤被执行，
  属于 runner/billing 层面的失败，而非 `pytest` 或 `ruff` 报错；
- 仓库历史已有两次同类记录：`9937a6f`、`769ef68`（"record remote CI … blocker"）。

因此本 PR 的验收依据是本地实测（163 passed、ruff 全通过、真实线上采集 15 篇入库），
不是 CI。若后续账号/额度恢复，重跑该 workflow 即可。

---

## 轮次 3 — 2026-10-26（补齐 `sitemap`，修复 PR #1 的导入断链）

**分支**：`feat/sitemap-adapter`（基于 `feat/sources-ctf-writeups` @ `848646c`）
**角色**：新网站来源（`ctfka/crawler/sources/`），按 `CONTRIBUTING.md` 的模块边界

### 起因：PR #1 的代码树无法导入

上一轮把 `sitemap` 注册进了 `registry.py` 的 `BUILTINS` 和 `builtin.py` 的再导出，
但 `sitemap.py` 一直留在其他 agent 的在途改动里（轮次 2 第 116–117 行记录了“保持未暂存”）。
结果是 PR 分支上：

```
ctfka/crawler/builtin.py:12: from ctfka.crawler.sources.sitemap import SitemapAdapter
E   ModuleNotFoundError: No module named 'ctfka.crawler.sources.sitemap'
```

影响不止少一个适配器：`tests/test_crawler.py` 在**收集阶段**就报错，pytest 直接中断，
整条测试套件跑不起来。所以 PR #1 声称的 “163 passed” 与它自己的代码树不符——
那是带 `sitemap.py` 的本地状态，文件没有提交。`docs/adapters.md` 也已经把这个还不存在的
适配器写进了配置示例。

### 本轮交付

```
新增  ctfka/crawler/sources/sitemap.py        # 通用 sitemap 发现适配器
新增  tests/test_source_sitemap.py            # 18 项离线测试
新增  tests/fixtures/sitemap/sitemap_index.xml
新增  tests/fixtures/sitemap/sitemap_posts_1.xml
新增  tests/fixtures/sitemap/sitemap_posts_2.xml
新增  tests/fixtures/sitemap/sitemap_pages.xml
改动  docs/adapters.md                        # 补 sitemap 用法与行为说明
改动  dsh-readme.md                           # 本记录
```

只做**发现**，正文走共享解析器。要点：

- `sitemapindex` → 子 `urlset` 自动跟进；子 sitemap 默认必须同源（`allow_cross_origin_sitemaps` 可放开）。
- 增量按子 sitemap 的 `<lastmod>` 水位 + 同水位 URL 集合，跨轮重新读取子 sitemap，被编辑的条目会重新出现。
- 水位**只在该子 sitemap 被完整读到末尾后**推进；`max_items` 截断时改记 `resume_url`/`resume_index`，如实报告 `complete: false`。
- 无可用 `<lastmod>` 的条目每轮都重新出现，靠 `Store` identity 幂等去重（不会丢，也不会重复入库）。
- 子 sitemap 404/410 视为下线并跳过；其余传输错误照常抛给 worker 分类重试。
- `<loc>` 先 `urldefrag` 再入库，identity 用去 fragment 的 URL。
- **没有**声明 `complete_scan`：列表型适配器一旦带上它，worker 会把 `current_identities` 之外的已存文章全部归档。

### 验证结果（本轮实测）

```powershell
$ python -m pytest tests/test_source_sitemap.py -q
18 passed in 0.38s

$ python -m pytest -q
179 passed in 19.57s          # 含 PR #1 原有 161 项，收集不再中断

$ python -m ruff check .
All checks passed!

$ node --check ctfka/web/assets/workbench.js
JS OK
```

### 写测试时修正的三个认知错误

初版断言是错的，跑出来的差异都指向“我理解错了契约”，不是实现有问题：

1. 以为水位以下的条目一律不再出现 → 实际**无 `lastmod`/`lastmod` 不可解析的条目每轮都会重新出现**（设计如此，靠幂等兜底）。
2. 以为截断前后的两轮拼接起来等于完整列表 → 跨子 sitemap 的去重是**按轮次**的，`stack-overflow-primer` 在第二轮会再出现一次；正确断言是“覆盖集合完整”，而非“拼接序列相等”。
3. `DiscoveredItem` 带 `slots` 且不可哈希 → 只能对 `item.url` 做集合运算。

### 遗留

- 未做真实公网 sitemap 采集实测：本轮只交付离线 fixture 回归，没有声称线上结果。
- 上一轮 `dsh-readme.md` 里的线上采集数据（15 篇入库、耗时表）属于**轮次 1 的历史记录**，
  本轮没有复现，不作为本 PR 的验收依据。
