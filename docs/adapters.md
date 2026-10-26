# 添加网站模块

来源配置与实现分离：一个适配器可配置多个来源实例，每个实例有自己的队列、增量游标和并发参数。比如多个 GitHub Writeup 仓库分别配置成不同来源名称。

## 复用已有适配器

HTML 网站可先使用 `html` 并配置 `options.start_urls`；RSS / Atom 使用 `rss` 并配置 `options.feed_url` 或 `feed_urls`；站点提供 sitemap 时优先用 `sitemap` 并配置 `options.sitemap_urls`。需要网站专用规则时再实现独立模块。

## 已实现的站点专用适配器

以下适配器都已注册为内置名称，配置里把 `adapter` 写成名字即可，不需要改核心代码。每个适配器都有独立的离线测试和真实格式样本。

| 适配器 | 站点 | 发现方式 | 正文位置 | 备注 |
| --- | --- | --- | --- | --- |
| `ctftime` | ctftime.org | 写题列表 `/writeups`，`?page=N` 翻页，越过末页返回 404 | `#id_description` | robots 要求 `Crawl-delay: 10`，请保持低速率 |
| `anquanke` | www.anquanke.com | sitemap 索引 + 子 sitemap，`<lastmod>` 水位增量 | `div.content#js-article` | 需要登录的文章会被判为无效，不入库 |
| `kanxue` | bbs.kanxue.com | 板块 `forum-<id>.htm`，`forum-<id>-<page>.htm` 翻页 | `div.message.message_rich_type` | 只存首帖；加密链接保留为纯文本代码块 |
| `exploitdb` | www.exploit-db.com | `search?draw&start&length` JSON 分页，从最新记录往回走 | `/raw/<id>` 纯文本 | 保存公开的 exploit 源码；无 raw 时回落到详情页 |
| `hacktricks` | hacktricks.wiki | `/<lang>/sitemap.xml` | `#content main` | mdBook 结构，必须定向取 `#content main` |

通用 `html` 适配器对上面这些站点的正文提取不完整（例如 HackTricks 的 `<main>` 外壳几乎不含正文、CTFtime 会把导航栏算进正文），因此这些站点各自实现了 `parse`，只复用 `FetchClient` 和通用解析辅助函数。

### `sitemap`：通用 sitemap 发现

站点没有可翻页列表、只公开 `sitemap.xml` 时，先用这个通用适配器，不必为此新写模块。它只负责**发现**，正文交给共享解析器：

```yaml
sources:
  some-docs:
    adapter: sitemap
    enabled: true
    options:
      sitemap_urls:
        - https://docs.example.org/sitemap.xml
      # include_regex: ["/posts/"]      # 可选：只保留匹配的 URL
      # exclude_regex: ["/2025/"]       # 可选：排除匹配的 URL
```

行为说明：

- 入口可以是 `sitemapindex` 或 `urlset`，索引里的子 sitemap 会自动跟进；子 sitemap 必须与入口同源，跨源默认跳过（`allow_cross_origin_sitemaps: true` 可放开）。
- 增量游标按子 sitemap 记录 `<lastmod>` 水位：水位以下的条目不再重复交给队列，被编辑后 `lastmod` 变新的条目会重新出现。水位只在子 sitemap 被完整读到末尾后推进。
- 没有可用 `<lastmod>` 的条目每一轮都会重新出现，靠 `Store` 的 identity 幂等去重，因此不会丢内容、也不会产生重复文章。
- `max_items` 截断时记录 `resume_url` / `resume_index`，下一轮从同个子 sitemap 的断点继续，并如实报告 `complete: false`。
- 子 sitemap 返回 404/410 视为已下线并跳过；其余传输错误照常向上抛出，交给 worker 的重试分类处理。
- `<loc>` 会先去除 `#fragment` 再入库，保证同一篇文章的 identity 稳定。
- 正文若需要站点专用选择器（例如 mdBook 的 `#content main`），仍应为该站点单独实现适配器，只把发现逻辑留给本适配器。

### 可直接复制的来源配置

```yaml
sources:
  ctftime:
    adapter: ctftime
    enabled: true
    concurrency: 2
    requests_per_second: 0.1      # robots 声明 Crawl-delay: 10
    max_items: null
    options: {}

  anquanke:
    adapter: anquanke
    enabled: true
    concurrency: 4
    requests_per_second: 1
    max_items: null
    options:
      sitemap_urls:
        - https://www.anquanke.com/sitemap.xml

  kanxue:
    adapter: kanxue
    enabled: true
    concurrency: 3
    requests_per_second: 1
    max_items: null
    options:
      forum_ids: [4, 37, 150, 171]   # 逆向工程 / CTF对抗 / 二进制漏洞 / Pwn

  exploitdb:
    adapter: exploitdb
    enabled: true
    concurrency: 3
    requests_per_second: 1
    max_items: null
    options:
      page_size: 100
      # query: php                  # 可选：只采集匹配的条目

  hacktricks:
    adapter: hacktricks
    enabled: true
    concurrency: 4
    requests_per_second: 1
    max_items: null
    options:
      sitemap_urls:
        - https://hacktricks.wiki/en/sitemap.xml
      include_regex: ["/binary-exploitation/", "/web-security/", "/linux-hardening/"]
```

`max_items: null` 表示不因人为数量上限停止发现；上面各站点的实际内容范围和站点访问规则仍然生效。`requests_per_second` 是可选的运维控制，默认 `0` 表示不加人为延时。

### 各适配器的增量与恢复语义

- **ctftime**：游标记录 `next_page` 与 `last_page`。列表按页递增，遇到 404 才认定列表结束；`max_items` 截断时不标记完成。
- **anquanke**：每个子 sitemap 记录 `lastmod` 水位和该水位上的文章 id。只有完整读完一个子 sitemap 才推进水位；被 `max_items` 截断时保留 `resume_url` / `resume_index`，下次从该位置继续。文章被编辑后 `lastmod` 变新，会被重新入队，由 Store 生成新版本。
- **kanxue**：游标记录 `next_board` / `next_page` / `next_index`。截断发生在页内时记录页内偏移，避免重复入队；越界页会回放末页内容，因此“整页 id 已全部见过”即为板块结束信号。
- **exploitdb**：接口按最旧在前返回，适配器从最高 offset 往回走，让最新 exploit 先入库。游标 `next_offset` 始终落在实际生效的页大小网格上（服务器可能把 `length` 截得更小）。
- **hacktricks**：游标记录已完成的 sitemap 与已见页面 URL；`max_items` 截断时不会把当前 sitemap 记为已完成。

### 反爬与访问边界

这些适配器遵守仓库既有的访问边界：请求走 `FetchClient`（超时、响应大小、并发、`requests_per_second`、`Retry-After`、重定向逐跳校验、robots 检查）。它们不伪造 UA 以外的身份、不绕过登录或付费墙、不破解验证码：

- 需要登录才能看到正文的文章（如部分奇安信攻防社区页面）会被 `validate` 判为无效并记录为失败，不会存入空壳。
- 看雪的 `mark.encrypted` 链接是站方用于跳转保护的混淆串，适配器原样保留在 `text` 代码块里，不在采集端执行站方的解码脚本。
- `ctftime` 的 robots 声明 `Crawl-delay: 10`，示例配置使用 `requests_per_second: 0.1`。

## 编写插件

继承 `ctfka.crawler.adapters.SourceAdapter`。构造参数为 `SourceConfig` 和 `FetchClient`；后者提供连接复用、网络请求和错误分类。三个数据对象在 `ctfka.contracts` 中定义。

| 方法 | 返回值 | 职责 |
| --- | --- | --- |
| `discover(cursor)` | 异步迭代 `DiscoveredItem` | 遍历列表或目录，提供稳定 identity 与 URL |
| `fetch(item)` | `FetchedDocument` | 抓取原始字节、内容类型及来源元数据 |
| `parse(document)` | `ParsedDocument` | 提取正文；默认复用 Markdown/HTML 解析 |
| `normalize(parsed)` | `ParsedDocument` | 规范化元数据与内容格式 |
| `validate(parsed)` | 无；失败抛异常 | 判断标题、正文等是否有效 |

`identity` 必须在文章更新时保持稳定。例如 GitHub 采用仓库和文件路径，commit SHA 则保存为版本元数据。改变 URL 或提交版本不能让同一篇文章失去历史关联。

状态通过 `state` 暴露。GitHub 完整扫描会先持久化任务，再等待全部变更文件入库后推进 commit 游标；部分扫描不能触发删除归档。插件必须清楚区分完整发现与主动截断。

## 注册

本地模块可直接写入 YAML：

```yaml
sources:
  my-site:
    adapter: my_package.my_site:MySiteAdapter
    concurrency: 8
    requests_per_second: 0
    max_items: null
    options:
      start_urls: [https://example.org/ctf/]
```

分发为独立安装包时，在该包的 `pyproject.toml` 添加：

```toml
[project.entry-points."ctfka.sources"]
my-site = "my_package.my_site:MySiteAdapter"
```

安装后即可将配置中的 adapter 改为 `my-site`。注册表通过安装包元数据发现插件，无需编辑核心文件。

## 验收

使用真实格式的离线页面测试分页、稳定身份、正文和代码保留，再用显式 `--max-items` 做小量线上验证。插件异常应进入任务失败分类，不返回伪造成功。保存原始字节，并让后续解析改进能够重放。

站点专用适配器的测试位于 `tests/test_source_<slug>.py`，离线样本位于 `tests/fixtures/<slug>/`。样本来自真实响应，只删掉了与解析无关的内联脚本、样式和图标，以保证离线测试覆盖真实标记结构：

```sh
pytest tests/test_source_ctftime.py tests/test_source_anquanke.py \
       tests/test_source_kanxue.py tests/test_source_exploitdb.py \
       tests/test_source_hacktricks.py
```

小量线上验证示例（会真实访问公网站点）：

```sh
ctfka sources list
ctfka crawl --source hacktricks --max-items 3 --max-jobs 3
ctfka crawl --source ctftime    --max-items 3 --max-jobs 3
ctfka jobs list
ctfka search "IDA"
```

