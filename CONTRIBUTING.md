# 模块分工与协作约定

协作仓库：[SWPU-NSS-Dev/CTF-Knowledge-Agent](https://github.com/SWPU-NSS-Dev/CTF-Knowledge-Agent)。所有新分支、PR、Issue 和验收记录使用此地址。旧个人仓库地址仅保留为历史记录。

本文件按模块划分工作范围。除已明确承担来源扩展的 dsh 外，尚未指定 GitHub 账号的负责人标为“待认领”，不虚构人员或自动配置 CODEOWNERS。实际认领时，在对应 Issue / PR 中写明负责人、范围、接口和依赖，并同步本表。

## 当前模块分工

| 模块 | 目录 / 文件 | 负责人或执行方式 | 工作内容与验收边界 |
| --- | --- | --- | --- |
| 架构与集成 | `ARCHITECTURE.md`、`docs/stage4*-architecture.md`、阶段验收报告 | 本会话主代理；维护者最终审阅 | 定义接口、审查边界、协调共享文件、真实端到端验收、按增量提交 |
| 新网站来源 | `ctfka/crawler/sources/`、对应 `tests/test_source_*.py` 和 fixtures、`docs/adapters.md` | dsh；后续站点可逐站认领 | 网站独立 Adapter、游标/增量、正文/代码保真；现有 [PR #1](https://github.com/SWPU-NSS-Dev/CTF-Knowledge-Agent/pull/1) |
| HTTP 与访问边界 | `ctfka/crawler/http.py`、`ctfka/security/`、对应 HTTP 测试 | 待认领；本轮由 GPT-6 Luna 实现 | 连接池、robots、Retry-After、重定向和 SSRF；修改会影响全部来源，单独说明回归范围 |
| 发现与采集调度 | `ctfka/scheduler/`、`ctfka/models/automation.py`、调度测试 | 待认领；本轮由 GPT-6 Luna 实现 | DiscoveryJob / CrawlJob 职责、持久租约、暂停/重启、失败隔离；不能加入 LLM 调用 |
| 质量与确定性索引 | 质量模块、`ctfka/indexing/`、相关 Store 接入和测试 | 待认领；本轮由 GPT-6 Luna 实现 | 分类/评分/重评分、主索引资格、符号与中文检索；保留低分原文 |
| 本地 AI 与处理队列 | `ctfka/llm/`、`ctfka/processing/`、处理任务存储和测试 | 待认领；本轮由 GPT-6 Luna 实现 | 模型身份、结构化输出、证据核验、独立 ProcessingJob、重试和幂等 |
| 知识图谱与审核 | `ctfka/knowledge/`、图谱和审核测试 | 待认领；本轮由 GPT-6 Luna 实现 | 实体/关系、历史证据、候选和人工审核、端点 revision、人工修改保护 |
| RAG 与只读 Agent | `ctfka/retrieval/`、`ctfka/agent/`、对应协议测试 | 待认领；本轮由 GPT-6 Luna 实现 | 检索/引用/降级、只读工具；采集文章中的命令只作为资料，不执行 |
| API / CLI / 管理界面 | `ctfka/api/`、`ctfka/cli/`、`ctfka/web/`、API/CLI/界面测试 | 待认领；本轮由 GPT-6 Luna 实现 | 对接已实现服务、鉴权与校验、真实状态展示、兼容旧接口 |
| 评估与发布验证 | `scripts/` 中的评估工具、评估数据说明、打包/CI、阶段报告 | 待认领；主代理复验 | 分开报告真实和合成语料、性能/引用/关系质量、备份迁移、构建包验证 |

同一网站文件只由一名实现者修改。新增网站优先认领新的来源文件及测试，通过注册/entry point 接入；不为增加站点修改核心调度或存储协议。

## 分支与 PR

1. 每个独立功能使用独立分支，例如 `feat/source-<site>`、`feat/discovery-scheduler`、`feat/content-quality`、`feat/automatic-graph`。每名协作者使用自己的 checkout / worktree。
2. 修改前检查远端分支、开放 PR、本模块 Issue 和 `AGENTS.md`；在 PR 中列出准确目录范围。接口依赖尚未合并时，明确列出依赖 PR / 基础 SHA，不把依赖伪装成本 PR 新增功能。
3. 小增量提交，每个增量保持可运行。向组织仓库推送自己的功能分支并发起 PR；其他人的分支由其负责人维护。不要强推 main，不重置、删除或暂存别人的未完成文件。
4. PR 描述写清触发场景、改变后的行为、关键接口、迁移与兼容性、实际执行的测试和已知限制。不能只写“测试通过”；记录命令、数量和必要环境。
5. 集成者复验后合并。发生文件重叠时，先确定该文件本轮的唯一写入者；另一模块通过接口请求或独立后续提交对接。

本轮 v0.4 开发位于 `feat/v04-continuous`。它基于已独立复验的 dsh 来源分支；PR #1 的合并与 v0.4 的集成顺序须明确，避免重复合入或覆盖 dsh 后续提交。主代理统一整理本轮子代理共享工作树中的已验收增量；子代理只修改各自明确分配的文件，不自行暂存或提交其他模块。

## 共享文件与接口

以下文件是协作交叉点，不能因模块任务而同时各写一份：

- `ctfka/config.py`：新增字段先说明类型、默认值和兼容行为；既有配置继续可读，持续公网任务必须明确启用。
- `ctfka/models/store.py`：扩展优先放入职责清晰的 mixin / 模块，集成者协调导入、初始化及 ingest 钩子；原文和历史版本不被处理任务覆盖。
- `ctfka/scheduler/worker.py`：保持 Adapter 接口兼容，发现、抓取、AI 处理分离；共享租约/暂停/重试语义不能绕过。
- `ctfka/api/app.py`、`ctfka/cli/main.py`：优先增加独立 router / 服务模块，统一协调生命周期和共享实例；不要重复启动同一处理器。
- 数据库 schema：迁移可重入，说明旧备份恢复、新旧状态及失败重入测试；不同模块使用独立版本化迁移，复用既有图谱。

跨模块约定先写进架构文档或 PR 接口说明：输入/输出字段、异常与状态码、版本/时间单位、幂等键、取消和恢复语义。文章文本、网页和模型输出属于不可信数据，不能修改插件配置或审核状态。

## 最低验收

先运行本模块有意义的测试；集成前再运行全量回归、Ruff、前端语法检查和必要的构建验证。新增来源需有离线 fixture 回归及显式有界的真实来源记录。模型相关功能同时验证模型不可用时的归档/全文检索、恢复后的任务续跑，以及精确证据和人工审核保护。

```powershell
python -m pytest -q
python -m ruff check .
node --check ctfka/web/assets/workbench.js
python -m build
```

不把令牌、个人配置、模型、运行数据库、采集原文或输出日志加入 Git。远端 CI 是否通过以当前组织仓库实际运行结果为准；历史上 Runner 曾因计费/额度问题未启动，这不等于测试通过，也不能未经核验就认定迁移后仍然受阻。
