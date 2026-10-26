# 添加网站模块

来源配置与实现分离：一个适配器可配置多个来源实例，每个实例有自己的队列、增量游标和并发参数。比如多个 GitHub Writeup 仓库分别配置成不同来源名称。

## 复用已有适配器

HTML 网站可先使用 `html` 并配置 `options.start_urls`；RSS / Atom 使用 `rss` 并配置 `options.feed_url` 或 `feed_urls`。需要网站专用规则时再实现独立模块。

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
