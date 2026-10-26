# CTF Knowledge Agent

CTF Knowledge Agent 是一个本地优先的 CTF 资料采集与离线搜索工具。Stage 1 提供公开来源插件、Markdown/HTML 解析、不可变原始快照、带版本的 SQLite 存储、持久化爬取任务与重试、命令行和 FastAPI 服务。AI、RAG、AWDP 自动化、PDF 解析和完整管理界面属于后续阶段，目前没有对应的伪实现接口。

## 安装

需要 Python 3.11 或更新版本。Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Linux：

```sh
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

## 初始化与使用

运行 `ctfka init` 会创建 `config.local.yaml`，并生成本地管理 API 的 bearer token。请保管好该文件和 token。命令默认优先读取 `config.local.yaml`，不存在时才读取仓库中的无密钥模板 `config.yaml`；也可以用全局参数 `--config PATH` 显式指定配置。`--data-dir PATH` 可覆盖数据目录。

默认配置启用 CTF Wiki，GitHub 示例源默认关闭。没有设置 `max_items` 时，发现会一直进行到来源耗尽；需要短时试跑时，同时设置发现数和本次处理数：

```sh
ctfka crawl --source ctfwiki --max-items 3 --max-jobs 3
ctfka search "buffer overflow"
ctfka sources list
ctfka jobs list
ctfka index build
ctfka backup create ./backup.zip
ctfka backup restore ./backup.zip
```

`--max-items` 限制本次发现并入队的条目数；`--max-jobs` 限制这次 CLI 调用处理的任务数。它们是临时运行参数，不会改写已保存的来源配置。API 请求也可提供这两个参数。API 服务的后台 worker 会持续处理持久化队列和到期重试，所以 `max_jobs` 只限制请求触发的这一轮处理，不会限制整个队列；若要限制该次发现，请设置 `max_items`。

`ctfka import ./references` 可导入本地 `.md`、`.markdown`、`.html` 和 `.htm` 文件，解析与存储路径和网页采集相同。`ctfka worker` 会持续处理已有任务及重试；按 Ctrl+C 可停止。API 服务启动时也会恢复过期租约并运行后台 worker。

## 来源配置和插件

内置 CTF Wiki 在 `options` 为空时从 `https://ctf-wiki.org/` 开始。同一个 `ctfka.sources` Python entry point 插件可通过安装包注册，不需要修改核心代码。HTTP API 的来源配置端点只接受已安装的注册名称，不会导入请求内容提供的 Python 模块。只有本地可信 YAML 配置可以使用 `module:Class` 指向自行安装的插件。

不同适配器使用各自的 `options`。例如，GitHub 适配器需要仓库 owner 和 repo；HTML 适配器需要 `start_urls`；RSS 适配器需要 `feed_url` 或 `feed_urls`。下面的 GitHub 来源可以直接复制到 `config.local.yaml`，用于公开仓库：

```yaml
sources:
  writeups-2014:
    adapter: github
    enabled: true
    max_items: null
    options:
      owner: ctfs
      repo: write-ups-2014
```

然后运行 `ctfka crawl --source writeups-2014 --max-items 3 --max-jobs 3`。网站和公开仓库会变化，也可能限流或暂时不可用。采集器会遵守响应大小、重试信息和公开访问边界，无法保证固定的远程吞吐量。来源数、文章数、知识库寿命都没有硬编码总量上限；`max_items` 是可选的运维控制参数。

采集器默认直连，刻意不读取 `HTTP_PROXY`、`HTTPS_PROXY` 等环境代理变量。若网络需要代理，可在来源 `options.proxy_url` 中显式设置 `socks5://` 或 `socks5h://` 代理；HTTP CONNECT 代理目前不受支持，配置后会明确报错。不要把含有代理凭据的配置提交到仓库。

## API

运行 `ctfka serve` 会在默认地址 `127.0.0.1:8000` 提供 FastAPI 服务。只有在确实需要对局域网开放时才设置 `--host 0.0.0.0`。根页面提供本地项目说明，`/docs` 提供 API 文档。

搜索、文章读取、来源列表、统计、任务状态和健康检查为读取接口。创建来源、开始采集、重试、重建索引和备份需要 `Authorization: Bearer <token>`。未配置 token 时，管理接口会返回 503 并保持关闭。

```sh
curl 'http://127.0.0.1:8000/api/v1/search?q=buffer%20overflow'
curl -H 'Authorization: Bearer YOUR_TOKEN' -H 'Content-Type: application/json' \
  -d '{"source":"ctfwiki","max_items":3,"max_jobs":3}' \
  http://127.0.0.1:8000/api/v1/crawl/jobs
```

## 公开来源复现实测

在可访问公网的环境里，先初始化配置，再运行以下小规模试采集。命令输出中的发现、入队、完成数、耗时和搜索结果才是本次运行记录；远程站点状态不同，结果也会不同。

```sh
ctfka init
ctfka crawl --source ctfwiki --max-items 3 --max-jobs 3
ctfka search "CTF"
ctfka jobs list
```

GitHub 公开仓库可用 README 中的 `writeups-2014` 示例源按相同方式运行。不要把实际计数或耗时从文档示例推断为采集成功。

## 数据与开发

SQLite 数据库、原始快照、来源状态和持久任务都保存在 `data_dir` 下。备份带有文件哈希清单，恢复前会校验清单并阻止路径穿越。`.gitignore` 已忽略数据库、快照、日志、生成配置、token 和采集输出。

运行 `pytest` 执行离线测试，运行 `ruff check .` 检查代码风格。`docs/stage1.md` 记录 Stage 1 的范围和公开来源试跑方法。
