# 第二阶段：本地 AI RAG（0.2.0）

本阶段在第一阶段独立仓库上增加本地模型、分块、向量索引、混合检索、知识卡片、带引用问答和 SSE。主代理负责架构、接口契约、审查和实际验收，应用实现及修复由 GPT-6 Luna 子代理完成。第三阶段 AWDP Agent 和完整管理 Web UI 尚未实施。

## 实现与主要文件

- `ctfka/llm/`：可替换 Provider 契约与 Ollama 实现，模型状态、精确标签及 digest、嵌入、普通生成、真实 NDJSON 流、超时和取消。HTTP 200 中的模型错误也作为失败处理；应用不下载模型或回退到云端。
- `ctfka/processing/chunking.py`：按 Markdown 结构分块，保留完整代码块、标题层级、前后关联及精确字符偏移；拼接可恢复完整标准化正文。长代码块可以超过普通文本目标长度。
- `ctfka/indexing/storage.py`、`service.py`：独立第二阶段迁移，Chunk FTS、模型隔离的向量缓存、增量构建、提取审计及知识卡片。缓存身份包含模型名、不可变 digest、嵌入选项和维度，内容按 SHA-256 复用。
- `ctfka/retrieval/`：BM25、余弦相似度、RRF 和精确技术符号/短中文词组加权，带来源过滤、证据组装和引用校验；缺少模型或匹配向量索引时明确降级到关键词检索。
- `ctfka/processing/extraction.py`：Pydantic JSON Schema 提取，校验逐字证据、版本和卡片证据关联；模型不提供偏移时，仅对原文中唯一出现的精确 quote 计算偏移。失败保留审计和原文，不覆盖已有成功记录。
- `ctfka/api/app.py`、`ctfka/cli/main.py`：模型状态、索引、混合搜索、知识卡片、普通及流式对话。SSE 发送真实增量、结束原因、引用元数据和 `[DONE]`，客户端断连取消上游请求。
- `scripts/evaluate_hybrid.py`：固定合成语料的真实本地嵌入评估；`scripts/start-ollama.ps1`：Windows 便携版本地服务启动器。
- `tests/test_stage2_storage.py`、`test_stage2_rag_protocol.py` 和扩展 API 测试覆盖分块、缓存、模型/维度隔离、引用、提取、错误及取消协议。

## 复现命令

```powershell
python -m pip install -e '.[dev]'
ctfka init
# 联网时预装模型；应用本身不下载模型
ollama pull qwen3:4b
ollama pull qwen3-embedding:0.6b
ctfka models status
ctfka import ./notes
ctfka index build --semantic
ctfka search --mode hybrid "__free_hook"
ctfka chat "栈溢出的基本前提是什么？请引用来源。"
ctfka extract ARTICLE_ID
ctfka knowledge list
ctfka serve
ctfka backup create ./backup.zip
```

Windows 便携运行时可通过 `./scripts/start-ollama.ps1` 启动，路径参数见 README。索引构建与采集解耦，模型不可用仍能保存和检索原文；模型恢复后重跑 `index build --semantic`。`--no-semantic` 完全不请求模型。`retrieval.exact_match_boost` 默认 0.05，设为 0 可关闭额外精确匹配加权。检索结果数量、上下文、分块和批次设置均可配置。

## 可用 API

保留第一阶段的来源、队列、文章、搜索、统计、备份等接口，新增：

```text
GET  /v1/models
GET  /api/v1/models/status
POST /v1/chat/completions
POST /api/v1/chat/completions
GET  /api/v1/knowledge
GET  /api/v1/knowledge/{id}
POST /api/v1/articles/{id}/extract
POST /api/v1/index/build?semantic=true
POST /api/v1/index/rebuild?semantic=true
GET  /api/v1/search?q=...&mode=hybrid
```

对话、提取和索引写入需要 bearer token。未配置 token 时保持关闭。聊天接受文本 `messages/model/stream/temperature/top_p/max_tokens`；不支持的 tools、图片、额外参数或 n>1 明确拒绝。未安装模型返回 404，不可达返回 503，超时返回 504；流中失败发送 error 事件，不能伪装为正常完成。服务 `/docs` 提供实际请求结构。

## 实测环境与数据

日期 2026-10-26，Windows、Python 3.12.12、内存约 14.8 GiB、RTX 5060 Laptop 8 GiB。使用 Ollama 0.40.11 官方便携 Windows 发行版，压缩包 SHA-256 为 `bda4a493ba8a7c24d57263cd843cd68cee84f44aed4299eeed532acad7eda12a`。服务绑定 127.0.0.1，设置 `OLLAMA_NO_CLOUD=1`，运行时和模型均存放在仓库外的 `../.runtimes/`。

| 模型 | 大小（bytes） | digest |
| --- | ---: | --- |
| qwen3:4b | 2497293931 | `359d7dd4bcdab3d86b87d73ac27966f4dbb9f5efdfcc75d34a8764a09474fae7` |
| qwen3:0.6b | 522653767 | `7df6b6e09427a769808717c0a93cadc4ae99ed4eb8bf5ca557c90846becea435` |
| qwen3-embedding:0.6b | 639150858 | `ac6da0dfba84a81fdbfbaf330198c33cd77c4cdfc53e8bc50eb581914a15621d` |

嵌入维度 1024。验收使用第一阶段实际采集的 4 篇公开文章恢复副本，生成 24 个 Chunk；另加入明确标注的本地提取 fixture 后共 5 篇、25 个 Chunk。fixture 不计为实时采集成果。本阶段不重新爬取站点。

## 已完成的真实链路验证

- 4 篇文章首次本地嵌入 24 个 Chunk，耗时 43.167 秒，包含冷模型加载；重建 0.036 秒，新增 0、复用 24，没有失败批次。
- 实际混合检索返回 5 条，耗时 0.063 秒。qwen3:0.6b 对 CTF Wiki 栈溢出文章回答两个基本前提，引用 `[S1]`；程序核验来源版本、Chunk 和正文偏移 17:226 完全一致。首次冷聊天 35.223 秒；独立 HTTP 普通响应热运行 2.232 秒。
- 真实 HTTP SSE 获得增量和最终引用元数据，首个内容约 0.118 秒，正常结束 `[DONE]`。max_tokens=5 时终止原因是 `length`。客户端提前关闭后健康接口仍返回 200；自动化协议测试还验证上游生成器完成清理。
- 在应用测试进程阻断所有非 loopback 地址解析和 socket 连接，负对照公网连接确实被拒绝，连接本机 Ollama 的问答仍成功并带引用，热运行 0.671 秒。此验证范围是应用进程出站阻断，系统互联网未物理断开，独立 Ollama 进程使用关闭云端的本地模型。
- 真实 qwen3:4b 生成 1 张知识卡片，提取耗时 3.139 秒。卡片引文逐字定位至本地 fixture 的 26:91；保留模型 digest、文章版本和 `unverified` 状态，原始快照逐字节不变。HTTP 提取和知识卡片列表/详情端点也实际返回 200。
- 包含第二阶段索引和卡片的备份恢复到新目录，5 篇文章、5 个版本、208887 bytes 原始快照保留，2 张历史生成卡片的引文全部重新核验；恢复后的语义构建新增 0、复用 25 个向量。第一阶段备份导入新代码也已成功迁移。
- 真实服务不可达时，混合搜索返回关键词结果及明确告警，没有依赖模拟模型输出。
- 默认 4B 模型另在相同的应用出站阻断下完成问答，27.800 秒，`grounded=true`、`finish_reason=stop`，回答为“程序必须向栈上写入数据，写入的数据大小没有被良好地控制。[S1]”。此次使用模型默认采样、16384 上下文和 4096 生成预算；先前 1800 生成预算曾在思考期间耗尽，明确返回 `length`、空最终答案和 `grounded=false`。
- 最终配置的 4B 真实 HTTP 复测：普通回答 14.609 秒并带有效引用；SSE 首个最终回答内容 24.811 秒、总计 25.073 秒，正常 `[DONE]`，提取接口 200，知识卡片列表和详情 200。思考时间会推迟首个可见回答；它与上面的 0.6B 快速结果不可混为同一模型性能。

本机已创建被忽略的 `config.local.yaml`，使用已安装 4B 模型、本地验收副本及独立管理 token；配置为 16384 上下文、4096 生成预算、模型默认采样。该文件不提交，也不覆盖仓库的通用默认模板。可直接在项目目录运行 `.venv/Scripts/ctfka.exe models status`、`search --mode hybrid "栈溢出"` 或 `serve`。便携 Ollama 保持本机运行，验收专用的 8767 HTTP 服务在验收后停止。

## 自动化测试与固定检索评估

最终 `python -m pytest -q`：**69 passed，1 warning，0 failed**。警告为现有 Starlette TestClient 对 httpx 的弃用提示。`python -m ruff check .` 通过。`python -m build` 成功生成 0.2.0 sdist 和 wheel。

覆盖旧版本迁移/备份、不可变原文、完整代码块与偏移、哈希缓存复用、模型和向量维度隔离、CJK/符号检索、RRF 加权、真实 Store 排序、JSON Schema 和精确证据、提示注入数据隔离、缺模型降级、HTTP 参数/状态码、普通与 SSE、流中错误、取消及上游关闭。

```powershell
python -m pytest -q
python -m ruff check .
python -m build
python scripts/evaluate_hybrid.py --config config.local.yaml --documents 30 --top-k 5
```

最后一条的 `--documents 30` 是上限，固定 fixture 实际只有 **10 篇合成文档、10 个查询**；不能写成 30 篇。真实本地嵌入生成 10 个 Chunk 用时 0.504 秒，增量复用 10 个用时 0.042 秒。热模型和当时本地其他推理负载会影响耗时。当前 SQLite 数据库 339968 bytes。

| 模式 | Hit@5 | Recall@5 | Top-1 | MRR@5 | p50 / p95 |
| --- | ---: | ---: | ---: | ---: | --- |
| 关键词 | 1.0 | 1.0 | 1.0 | 1.0 | 5.924 / 6.643 ms |
| 混合 | 1.0 | 1.0 | 1.0 | 1.0 | 69.978 / 85.807 ms |

覆盖 GOT、PLT、UAF、R_X86_64_JUMP_SLOT、__free_hook、malloc@plt、CVE-2021-44228、ELF、栈溢出和任意地址写。修正了评估脚本将命中文章按 fixture 顺序显示的问题，现在保留实际检索排名并有回归测试。以上指标只描述此小型固定语料，不代表真实大规模准确率或网络吞吐量。

GitHub 已推送实现提交 `81802bc`。[对应 Actions 运行](https://github.com/Zhouhang9527/CTF-Knowledge-Agent/actions/runs/40301615121)被账户账单/支出额度状态阻止，job 没有启动、steps 为空。GitHub 注释明确提示近期账户付款失败或需要提高 spending limit；远端 CI **没有通过，也没有执行代码测试**。本地 69 项测试、Ruff 和构建结果独立有效。未修改账户账单、仓库私有性或以跳过检查伪装 CI 通过。

原始验收输出保存在被忽略的 `outputs/stage2-*-report.json`；包含真实模型输出及失败记录，没有提交模型、Token、原文或运行数据。

## 实际遇到的失败与边界

- qwen3:0.6b 的首次结构化提取因输出截断未通过 JSON 校验；另一次得到空卡片和低质量字段，不能算作成功知识卡片验收。失败记录保留，原文逐字节未改变。
- 模型下载后 Windows 曾有约 15000 个 TCP TIME_WAIT 连接，导致 Ollama 内部连接返回端口占用错误。应用按失败处理，未将其报告为生成成功。
- 当前 qwen3:4b 的 `/api/show` 声明 `thinking.values=[true]`。普通对话使用 `think:true` 才正确分离思考与最终回答，默认值已据实调整。结构化提取反而在开启思考时耗尽预算、没有 JSON；现单独以可配置的 `structured_think:false` 生成 Schema 输出。调用者仍需给模型足够生成 Token，截断返回 `length`；提示和 Schema 校验不能保证模型每次成功。参见 [Ollama 思考模式](https://docs.ollama.com/capabilities/thinking)和[结构化输出](https://docs.ollama.com/capabilities/structured-outputs)。
- 向量后端为 `sqlite-linear-cosine`，每次语义搜索线性扫描对应模型向量，适用于当前本地数据规模；尚未实现 ANN/Qdrant 或 reranker。开启未实现的 reranker 配置会明确报错。
- 上下文使用保守 UTF-8 字节预算并预留生成空间。过长证据整块省略并告警，长文章提取超预算时明确失败；尚未实现跨 Chunk 聚合提取。嵌入请求关闭静默截断，超长内容可报告失败并重试。
- `grounded=true` 表示引用能定位到真实来源及精确段落，不能证明每个生成命题都被来源蕴含；卡片和模型回答仍为 `ai_extracted/ai_inferred`、`unverified`，需要人工审核。
- 完整 Web UI、AWDP 实体与 Attack/Fix 关系、多步 Agent、人工审核、更多来源适配器属于第三阶段；PDF 导入也尚未实现。当前网站扩展仍走第一阶段 Source Adapter 注册接口，核心爬取逻辑不依赖 AI 模块。
