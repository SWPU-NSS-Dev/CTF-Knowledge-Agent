# CTF Knowledge Agent

协作仓库：[SWPU-NSS-Dev/CTF-Knowledge-Agent](https://github.com/SWPU-NSS-Dev/CTF-Knowledge-Agent)。多人分工、目录责任、共享接口与 PR 验收流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。

CTF Knowledge Agent 是一个本地优先的 CTF 资料采集、混合检索与本地 AI 助手。Stage 2 在 Stage 1 的采集、不可变原始快照和 SQLite 版本存储上，增加 Ollama 本地模型、知识抽取、关键词与语义混合检索，以及 OpenAI 兼容聊天 API。模型输出属于 AI 推断，引用可追溯到原文，但不会自动标记为人工验证。

第三阶段增加带证据的 AWDP 图谱、只读多步 Agent、人工审核和中文本地工作台。关系引用不可变文章版本，审核前保持候选状态。没有可用模型时，Agent 返回明确标记的确定性证据报告。管理写入与模型生成使用配置的 bearer token。

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

## 本地模型与离线使用

模型由本机 Ollama 提供；应用不会自动下载模型，也不会回退到云端。先从 [Ollama 官方安装页](https://ollama.com/download) 安装并启动 Ollama，然后在联网时拉取聊天和嵌入模型：

```powershell
ollama pull qwen3:4b
ollama pull qwen3-embedding:0.6b
ollama list
```

Windows 也可以用仓库父目录中的便携版 Ollama，不需要安装系统服务或改全局环境变量。默认启动脚本查找 `../.runtimes/ollama-v0.40.11/bin/ollama.exe`，并把模型目录设为 `../.runtimes/ollama-models`：

```powershell
.\scripts\start-ollama.ps1
```

脚本只给新 Ollama 进程设置 `OLLAMA_HOST=127.0.0.1:11434`、`OLLAMA_MODELS` 和 `OLLAMA_NO_CLOUD=1`；若本机服务已能响应，会直接打印版本和已安装模型并退出。运行日志位于被 Git 忽略的 `outputs/ollama-stdout.log` 与 `outputs/ollama-stderr.log`。也可显式指定放在其他位置的运行时和模型目录：

```powershell
.\scripts\start-ollama.ps1 `
  -RuntimePath 'D:\tools\ollama\ollama.exe' `
  -ModelsDir 'D:\models\ollama'
```

模型拉取仍由用户显式执行，例如在 Ollama 运行后执行 `ollama pull qwen3:4b` 和 `ollama pull qwen3-embedding:0.6b`。应用配置默认连接 `http://127.0.0.1:11434`，便携脚本默认地址无需改配置；若使用别的监听地址，可在 `config.local.yaml` 的 `llm.base_url` 设置，或通过 `CTFKA_OLLAMA_BASE_URL` 环境变量覆盖。`llm.chat_model` 和 `llm.embedding_model` 也可在本地配置中设成已拉取的模型名。默认便携运行时和模型目录位于项目目录之外，不会被脚本下载或加入仓库。

默认名称可在 `config.local.yaml` 的 `llm.chat_model` 和 `llm.embedding_model` 修改，也可分别用 `CTFKA_CHAT_MODEL`、`CTFKA_EMBEDDING_MODEL` 覆盖；服务地址可用 `CTFKA_OLLAMA_BASE_URL` 修改。拉取完成后，日常模型推理与向量化只连接本机 Ollama，可断开互联网使用；采集公网来源仍然需要网络。模型文件由 Ollama 管理，不会写入本项目或 Git。

默认 `llm.think: true`，回答使用模型的 `message.content`，不展示独立的 `message.thinking`。本次实测的 qwen3:4b 仅声明支持开启思考；设置 false 不能保证关闭。切换模型时可查看 Ollama `/api/show` 的 `thinking.values` 后配置；思考会占用生成 Token，较小的 `max_tokens` 可能在最终回答前耗尽，接口会如实返回 `finish_reason: length`。

JSON Schema 提取单独使用 `llm.structured_think: false`。本次模型在结构化输出时使用此设置可直接生成有效 JSON；开启思考可能先耗尽生成预算而没有 JSON。两项均可按所选模型能力调整。

```sh
ctfka models status
ctfka index build --no-semantic   # 仅生成可用的确定性分块/关键词索引
ctfka index build --semantic      # 对缺失内容生成嵌入，可中断后重跑
ctfka search --mode hybrid "格式化字符串"
ctfka chat "如何定位这个漏洞的根因？"
ctfka extract ARTICLE_ID
ctfka knowledge list
ctfka knowledge show CARD_ID
```

导入或采集不会因模型不可用而丢弃原文。先构建关键词索引；模型可用后再运行语义索引。嵌入按内容哈希和模型身份缓存，重跑可复用未变化内容。切换嵌入模型后重新构建语义索引；不同模型和维度不会混用。`config.yaml` 仅提供可覆盖的默认设置，超时、上下文、分块大小和检索数量均可配置。

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

运行 `ctfka serve` 会在默认地址 `127.0.0.1:8000` 提供 FastAPI 服务。只有在确实需要对局域网开放时才设置 `--host 0.0.0.0`。根页面提供本地管理工作台，`/docs` 提供 API 文档。

Stage 3 管理界面位于服务根页面 `/`。点击“管理令牌”后，可在当前标签页内存中输入 `config.local.yaml` 中的 token；页面关闭即清除。AWDP 练习夹具只在显式运行 `ctfka demo seed-awdp` 时写入本地数据，可用返回的实体 ID 浏览关系和比较修复方案：

```sh
ctfka demo seed-awdp
ctfka graph entities --kind challenge
ctfka graph attack-fix --entity-id ENTITY_ID
ctfka review pending
ctfka agent "比较这个挑战的两种修复方案" --entity-id ENTITY_ID
```

图谱证据始终指向已保存的文章版本和原文区间。新关系等待人工审核；编辑已审核实体会使其回到未验证状态。Agent 只读取本地资料，不会运行文章、脚本或利用代码。

搜索、文章读取、来源列表、统计、任务状态和健康检查为读取接口。创建来源、开始采集、重试、重建索引和备份需要 `Authorization: Bearer <token>`。未配置 token 时，管理接口会返回 503 并保持关闭。

本地模型状态：`GET /api/v1/models/status`；OpenAI 模型列表：`GET /v1/models`。聊天支持 `POST /v1/chat/completions` 和兼容别名 `/api/v1/chat/completions`，请求使用 `model`、文本 `messages`、`stream`、`temperature`、`top_p`、`max_tokens`。聊天和提取、语义索引构建属于管理操作，需要 bearer token。流式响应使用 SSE，包含 assistant role、content 增量、结束块及 `data: [DONE]`；客户端断开会取消上游生成。未安装模型在开始流式响应前返回 HTTP 错误，流开始后的服务错误使用 SSE `error` 帧。

```sh
curl http://127.0.0.1:8000/api/v1/models/status
curl -N -H 'Authorization: Bearer YOUR_TOKEN' -H 'Content-Type: application/json' \
  -d '{"model":"qwen3:4b","stream":true,"messages":[{"role":"user","content":"介绍栈溢出"}]}' \
  http://127.0.0.1:8000/v1/chat/completions
```

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

运行 `pytest` 执行离线测试，运行 `ruff check .` 检查代码风格。[第一阶段报告](docs/stage1.md)记录公开来源采集验收；[第二阶段报告](docs/stage2.md)记录本地模型、混合检索、RAG、流式接口及备份恢复实测；[第三阶段报告](docs/stage3.md)记录 AWDP 图谱、Agent、审核与工作台验收。
