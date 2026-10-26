# 第一阶段验收报告

日期：2026-10-26（Asia/Shanghai）。本报告记录实际执行结果。

## 已实现与主要文件

- CTF Wiki、GitHub、HTML、RSS/Atom 独立网站模块；`module:Class` 与 `ctfka.sources` 插件注册。
- 配置化并发、速率、超时、大小及重试；无默认文章量和 HTML 深度上限；速率为 0 时不主动插入延迟。
- 异步连接池、HTML 发现缓存落临时磁盘、GitHub commit/blob 增量、ETag/304 条件请求。
- SQLite WAL/FTS5 与版本迁移、不可变原文、内容版本、精确去重与 SimHash 近重复候选。
- 持久队列、优先级、租约恢复、所有权 token 校验、退避重试与错误分类、常驻 worker。
- Markdown/HTML 解析、中文与技术符号搜索、CLI、FastAPI、管理 token、备份与跨目录恢复。
- 固定已校验目标 IP、逐跳检查重定向、TLS 证书验证；采集内容仅作为数据保存。

主要实现：`ctfka/models/store.py`、`ctfka/crawler/sources/`、`ctfka/crawler/registry.py`、`ctfka/crawler/http.py`、`ctfka/scheduler/worker.py`、`ctfka/ingestion/parser.py`、`ctfka/api/app.py`、`ctfka/cli/main.py`。架构和插件文档见 `ARCHITECTURE.md`、`docs/adapters.md`。

## 环境、命令与测试

Windows 11 build 26200；Python 3.12.12；SQLite 3.51.1，实际使用项目 `.venv`。

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m build --wheel --no-isolation
.\.venv\Scripts\python.exe scripts\evaluate.py --documents 300
```

最终结果：**40 passed，0 failed**；Ruff 通过；wheel 构建成功。有 1 条 Starlette TestClient 使用 httpx 的弃用提示，不影响测试结果。

覆盖：版本/去重、中文/符号检索、代码保真、HTML 嵌套清理、备份完整性/路径、schema 迁移、恢复与租约隔离、GitHub 多级目录/删除集合/失败游标、ETag 冷热缓存、IP 固定/重定向、API 鉴权/后台错误、CLI 配置传播/失败退出码。

## 真实来源验收

实际运行 `CrawlService.crawl()` 的 discover → queue → fetch → parse → store → index 完整流程。此次显式设置 CTF Wiki max_items=1、GitHub max_items=3，仅用于小量验收；workers=4、来源 concurrency=2、requests_per_second=0、timeout=15 秒。网络使用本机显式 SOCKS5 代理，地址及凭证不写入仓库。

| 来源 | 实际内容 | 完成/失败 | 耗时 |
| --- | --- | --- | --- |
| CTF Wiki | 栈溢出原理文章 | 1/0 | 1.693 秒 |
| ctfs/write-ups-2014 | 根 README、贡献指南、volga-quals-2014/README | 3/0 | 4.522 秒 |

来源：<https://ctf-wiki.org/pwn/linux/user-mode/stackoverflow/x86/stackoverflow-basic/>；GitHub commit 为 `b02bcbb2737907dd0aa39c5d4df1d1e270958f54`。本次采集包括仓库文档和赛事索引，不代表下载了全部题解。

最终 4 篇文档、4 个版本、4 个 completed 任务，原文共 208,738 字节。本机数据为 `outputs/acceptance-socks-data`，报告为 `outputs/live_final_report.json`，均被 Git 忽略。

同机直连 CTF Wiki 成功，但 GitHub Raw 抓取失败；显式 SOCKS5 成功。测试过程中发现并修复 HTML 嵌套清理异常及 HTTP CONNECT 的 TLS 主机名不匹配，随后用最终网络模式重新验收。没有关闭 TLS 校验。

## API、CLI 与持久性验收

真实 CLI 服务在 `127.0.0.1:8766` 接受 HTTP 请求：health 200；“栈溢出”和“Volga”各命中 1 篇；按 ID 读取文章 200；未带 token 重建索引 401，带有效 token 为 200；统计为 4 篇；worker running、error=null。验收服务已停止。

```powershell
.\.venv\Scripts\ctfka.exe --data-dir outputs\acceptance-socks-data search '栈溢出' --limit 1
.\.venv\Scripts\ctfka.exe --data-dir outputs\acceptance-socks-data backup create outputs\acceptance-backup.zip
.\.venv\Scripts\ctfka.exe --data-dir outputs\acceptance-restore-data backup restore outputs\acceptance-backup.zip
```

备份跨目录恢复后：4 篇文章均存在，中文检索命中，原文路径已重定位且文件全部可读。

还运行两个独立 Python 进程验证持久队列：第一进程 claim 一条明确标记的离线 fixture 任务后退出，未完成；租约到期后第二进程回收相同任务 ID 并完成入库。恢复 1 条，最终 completed=1。这是进程重启离线验收，不是线上采集。

本机报告：`outputs/api_acceptance_report.json`、`outputs/durability_report.json`。

## 性能与检索评估

固定数据集 `fixtures/retrieval_eval.json`，使用临时数据库。实际结果：

| 指标 | 结果 |
| --- | --- |
| 合成文档量 | 300 |
| 本地解析、原文写入和索引总耗时 | 2.726857 秒 |
| 本地入库吞吐量 | 110.017 篇/秒 |
| 固定查询数 | 10 |
| 查询 p50/p95 | 2.975/4.118 ms |
| Hit@5 / Recall@5 | 1.0/1.0 |

覆盖 GOT、PLT、UAF、R_X86_64_JUMP_SLOT、__free_hook、malloc@plt、CVE、ELF、栈溢出、任意地址写。小型合成语料结果不代表真实大语料准确率，也不是网络爬取吞吐量。原始结果在本机 `outputs/retrieval_benchmark.json`。

## 可用 API

```text
GET  /health
GET  /api/v1/search
GET  /api/v1/articles
GET  /api/v1/articles/{id}
GET  /api/v1/sources
PUT  /api/v1/sources/{name}
POST /api/v1/crawl/jobs
GET  /api/v1/crawl/jobs
GET  /api/v1/crawl/status
POST /api/v1/crawl/jobs/{id}/retry
GET  /api/v1/stats
POST /api/v1/index/rebuild
POST /api/v1/backup
```

管理路由要求 bearer token；未配置 token 返回 503。运行服务的 `/docs` 提供完整 OpenAPI 描述。

## 已知边界与下一阶段

- 第一阶段 MVP 已完成。PDF、Ollama、Embedding、混合检索、RAG/SSE、OpenAI 兼容对话、AWDP 关系、人审和完整 Web UI 尚未实现。下一阶段先做本地模型状态、分块、向量和带证据引用的 RAG。
- 网络层默认直连，不读取环境代理变量；`options.proxy_url` 可显式指定 SOCKS5/SOCKS5h。HTTP CONNECT 代理暂不支持，配置会明确报错。
- robots、访问边界及数据完整性保障保留。网络吞吐受远端网站、配额及本机网络影响。
- 未找到可确认的自定义子代理原文件或恢复版本，没有宣称完成“原文件复原”。实际实现使用 GPT-6 Luna 子代理，并新增项目级 Luna 配置与分工说明。
- 独立实现采用标准库 SQLite 和显式版本迁移，未引入 SQLAlchemy/Alembic；没有复制已有项目。

仓库：<https://github.com/Zhouhang9527/CTF-Knowledge-Agent>（私有）。主代理负责架构、契约、审查与验收；GPT-6 Luna 实现代码及修复。凭证、模型、原文数据集和运行输出未纳入 Git。
