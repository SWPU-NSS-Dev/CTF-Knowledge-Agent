# 第三阶段：AWDP Agent 与本地管理工作台（0.3.0）

第三阶段独立实现并验收。主代理负责架构、接口契约、代码审查和真实验收，GPT-6 Luna 子代理实现存储、Agent、API/CLI 与界面。按用户最新安排，dsh 正在编写的网站来源模块**不纳入本阶段提交**，相关工作区文件保留。

## 已实现功能与主要文件

- `ctfka/knowledge/storage.py`：题目、Writeup、漏洞、根因、Attack、Fix、Exploit、Patch、知识、工具、前置条件、验证实体，及有明确端点类型的关系。原文证据按文章 ID、历史版本和字符区间逐字校验。默认候选、未验证；审核必须由管理请求显式提交。
- `ctfka/models/store.py`、`ctfka/indexing/storage.py`：可重入的第三阶段 SQLite 迁移、历史正文读取、知识卡 revision 兼容及显式列写入。通过独立修订/编辑表扩展第二阶段卡片，保留模型原始提取记录。备份包含图谱和审计。
- `ctfka/knowledge/fixtures.py`、包内 `data/challenge.md`：只能显式调用的、可幂等加载的虚构 AWDP 样例。一个挑战、两篇 Writeup、路径遍历根因与攻击、两个修复/补丁及验证步骤，共 13 个实体、18 条关系。不会随服务启动自动灌入。
- `ctfka/agent/service.py`：确定性多步检索计划，六个只读工具：`search_knowledge`、`search_writeups`、`read_article`、`find_related`、`compare_solutions`、`find_attack_fix`。记录实际调用轨迹；按题目/漏洞关键词定位图谱、读取原文和比较方案。没有 Shell、漏洞脚本或补丁执行工具。
- `ctfka/api/stage3.py`：独立第三阶段路由，复用既有管理鉴权；类型错误 422、缺失 404、旧 revision 写入 409。人工审核、知识卡编辑及 Agent 调用需要 bearer token。
- `ctfka/web/`：同 FastAPI 分发的中文本地工作台，无 CDN。检索/原文版本、Agent/轨迹/引用、来源/队列/重试、图谱创建/关系/方案比较、知识卡编辑、审核/历史、模型与索引统计。令牌仅存页面内存；采集文本、模型文字和属性使用文本节点显示。
- `ctfka/cli/main.py`、`ctfka/config.py`：graph、agent、review、demo CLI，以及可配置的 Agent 调用、时间、上下文、检索数量和关系深度设置。调用/时间/上下文/深度可设 null；循环检测仍生效，截断或降级明确告警。

## 实际运行命令

```powershell
python -m pip install -e '.[dev]'
.\scripts\start-ollama.ps1
ctfka models status
ctfka serve
```

浏览器访问 `http://127.0.0.1:8000`。管理令牌来自本机已有 `config.local.yaml`，通过工作台“管理令牌”按钮显式输入；不会嵌入页面、URL 或浏览器持久存储。

需要独立探索虚构 AWDP 样例时，指定独立数据目录，避免与个人知识库混淆：

```powershell
ctfka --data-dir ./outputs/awdp-demo demo seed-awdp
ctfka --data-dir ./outputs/awdp-demo graph entities
ctfka --data-dir ./outputs/awdp-demo graph attack-fix --query "path traversal"
ctfka --data-dir ./outputs/awdp-demo agent "AWDP题目中的路径遍历漏洞，如何比较修复方案？"
ctfka --data-dir ./outputs/awdp-demo review pending
ctfka --data-dir ./outputs/awdp-demo serve
```

`review submit --help` 提供 target type、target ID、decision、note、reviewer 与 expected revision 参数。HTTP `/docs` 是完整请求结构的可查入口。用户原有本地配置未被第三阶段验收配置覆盖。

## 可用 API

保留第一、二阶段所有路由，新增：

```text
GET/POST   /api/v1/entities
GET/PATCH  /api/v1/entities/{id}
GET        /api/v1/entities/{id}/related
GET        /api/v1/challenges
GET        /api/v1/challenges/{id}
GET/POST   /api/v1/relations
GET/PATCH  /api/v1/relations/{id}
GET        /api/v1/attack-fix?q=...&entity_id=...
GET        /api/v1/articles/{id}/versions/{version}
POST       /api/v1/agent/chat
GET        /api/v1/reviews/pending
GET        /api/v1/reviews
POST       /api/v1/reviews
PATCH      /api/v1/knowledge/{id}
GET        /api/v1/analytics
```

实体列表可按类型、题目/关键词、比赛、年份、分类、漏洞类型、来源和验证状态过滤。关系可按端点、类型和有效审核状态过滤。审核后的实体内容变更会重置为未验证；对应关系的端点修订过期后，查询和统计均视为候选，需要重新审核。知识卡编辑保留审计和原提取数据。

## 自动化、打包和独立提交验证

待提交文件通过 Git index 导出到隔离目录 `outputs/stage3-index-check`，确认 Python 从该目录导入，而不是从含 dsh 改动的工作区导入。该快照不含新网站模块。

- **pytest：90 passed、0 failed、1 warning，13.61 秒**。警告为既有 Starlette TestClient 对 httpx 的弃用提示。最终取消测试用显式事件等待模型调用开始，避免固定毫秒等待造成时序误报。
- Ruff 通过；`node --check ctfka/web/assets/workbench.js` 通过。
- 隔离快照成功构建 0.3.0 sdist 和 wheel。
- 将 wheel 单独安装到临时目标目录后，重新导入、加载包内样例并幂等 seed；验证 13 实体、18 关系、2 个方案，工作台资源完整。新来源模块未混入构建包。

```powershell
python -m pytest -q
python -m ruff check .
node --check ctfka/web/assets/workbench.js
python -m build
```

测试覆盖旧阶段回归、迁移中断重入、图谱类型与历史证据、候选/审核/端点过期、全局分页、并发 revision、卡片连续编辑及原提取保留、备份恢复、真实 Store 多步检索、中文自然问句、上下文整块省略、调用上限、全流程超时/取消、未知工具/参数、引用伪造/未知标签及无模型降级。

## 真实验收结果

本机环境延续第二阶段：Windows、Python 3.12.12、RTX 5060 Laptop 8 GiB、Ollama 0.40.11；实际模型 `qwen3:4b`，digest `359d7dd4bcdab3d86b87d73ac27966f4dbb9f5efdfcc75d34a8764a09474fae7`。使用模型默认采样、16384 上下文和 4096 生成预算，所有应用和模型访问均在本机。

验收库由第二阶段备份恢复，再显式加入虚构 AWDP 样例。共 6 篇文章，原始快照 210494 bytes；初始 13 实体、18 关系。额外的 UI 文本安全测试创建了 1 个无证据实体，最终 14 实体。没有将 fixture 宣称为实际比赛、实时采集或人工确认的技术事实。

1. 真实 HTTP 由漏洞定位到两种修复；反向由 Patch 查到对应 Attack，两次都返回 2 个比较结果。
2. 真实审核将关系设为已验证，再编辑 Fix 端点：关系变为 `candidate`、标记端点过期，已验证过滤和统计不再返回它；旧 revision 写入返回 409。伪造验证状态、布尔 revision、错误引文分别返回 422；无 token 写入返回 401。原始快照逐字节不变。
3. 工作台浏览器在桌面 1280×900 和手机 390×844 进行验收。实际完成中文搜索、打开 CTF Wiki 原文、比较两种方案、提交带依据的审核、创建实体；标题包含 `<img ... onerror=...>` 时显示为字面文字，页面未生成 img 元素。长 URL 已修复为换行，手机 scrollWidth 与视口宽度相同。
4. 网页 Agent 使用不存在的模型做真实降级请求，显示中文确定性证据报告、两种候选方案、5 次工具轨迹、精确引用及模型不可用提示，没有模拟 AI 成功。
5. 真实 4B Agent 不传 entity_id，仅用中文自然问句完成 `search_knowledge → search_writeups → compare_solutions → read_article ×2`。最终运行 **38.400 秒**，2 个比较、2 个实际引用，全部按返回的文章 ID 和历史版本重新核验，`inference_status=ai_inferred`。回答显式提示候选关系，并分别指出 Fix A 的 symlink compatibility 和 Fix B 的 remote preview unavailable 取舍。
6. 知识卡真实 HTTP 编辑后 revision 从 1 到 2，GET 可见新 revision，状态重置未验证，原始提取保留，编辑进入审计历史。年份字符串筛选实际返回对应实体。
7. 包含图谱与审计的新备份恢复到新目录：6 篇文章、14 实体、18 关系、3 条审核/编辑审计、卡片 revision 均保留；来源路径正确重定位，历史引文逐字匹配，仍能比较 2 个方案。

原始结果保存在未提交的 `outputs/stage3-http-report.json`、`stage3-agent-live-report.json`、`stage3-backup-report.json` 和 `stage3-wheel-report.json`。模型、令牌、采集数据、数据库和运行输出不纳入 Git。

## 发现的问题、修复与边界

- 开发中捕获并修复迁移半途残留表/索引导致不能重入、第二阶段卡片裸 INSERT 列数兼容、人工审核队列/历史的全局分页、过期关系统计、历史证据行 ID 误当文章 ID、无引用回答误标 grounded、JS 语法错误及手机 URL 溢出。
- 首次真实模型运行虽给出可定位引用，但误把历史攻击描述当作修复后的问题。已保留该次输出，随后将带标题、取舍、补丁、状态及原文标签的比较记录优先供给模型，并程序性添加候选提示；复测正确给出两种取舍。**引用匹配不能证明每个模型命题正确**，仍需人工核实。
- 图谱当前通过显式 seed、表单或 API 维护，不会自动从任意新文章生成完整可信 Attack/Fix 图谱；没有自动将 AI 结论审核通过。Agent 是确定性只读多步规划器，不是任意自主执行器。
- 图谱遍历和关键词识别仍是本地 SQLite 实现；复杂自然语言可能找不到目标，可传 entity_id 精确定位。达到配置的检索数量/上下文预算会明确告警；当前真实运行包含 entity/relationship 截断告警，未声称完整返回整个知识库。
- 新来源扩展由 dsh 并行开发，本阶段独立提交不包含其代码或测试结果，也不宣称新站点抓取成功。原有适配器接口保持兼容。
- PDF、ANN/Qdrant、大规模语料性能评估、多人身份体系和自动关系提取仍未实现。UI 的审核人是本机管理用户填写的审计标识，不是独立登录身份。
- 第二阶段 GitHub Actions 受账户账单/支出额度阻止；第三阶段推送后另行记录实际远端结果，不能用本地测试冒充远端 CI 通过。
