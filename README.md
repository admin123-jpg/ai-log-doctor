# AI 容器日志医生（AI Docker Log Doctor）

> 自动采集 Docker 容器 / CI 流水线日志 → 检测异常 → 用 RAG 从运维知识库检索资料 → 交给大模型做根因分析 → 展示到 Web 面板。

技术栈轻、逻辑完整、AI 与 CI/CD 全覆盖，且**没有 API Key、没装 Docker 也能完整跑通**（内置降级方案，见第五节）。

---

## 一、项目覆盖的技术面

| 技术领域 | 本项目对应实现 |
|---|---|
| 容器与日志采集 | `docker-compose.yml` 起三个容器，程序通过 `docker logs` 采集 |
| Git 版本管理 | 完整 Git 工作流 + GitHub Actions |
| 大模型应用 / Prompt Engineering | 自写诊断 Prompt，支持 DeepSeek / 通义 / OpenAI 兼容接口 |
| 日志分析与智能告警 | 项目核心功能 |
| CI/CD | GitHub Actions：测试 → 构建镜像 → 推送；失败时自动 AI 诊断并评论到 PR |
| 可观测性（Prometheus / ELK） | 预留扩展位（见文末「后续扩展」） |
| RAG 检索 | 自建向量检索（Chroma + 降级方案），不依赖框架，原理透明 |
| 容器化部署 | 提供 `Dockerfile` / `docker-compose.yml`，可打包部署到任意云服务器 |

---

## 二、整体架构

```
┌──────────────────── 数据来源 ────────────────────┐
│  Docker 容器日志            CI/CD 流水线日志       │
│  (Nginx / Flask / MySQL)    (GitHub Actions)      │
└───────────────┬──────────────────┬───────────────┘
                │                  │
                ▼                  ▼
         ┌──────────────────────────────┐
         │      异常检测（关键字/正则）    │
         └──────────────┬───────────────┘
                        │
        ┌───────────────┴───────────────┐
        ▼                               ▼
┌────────────────┐            ┌──────────────────┐
│ RAG 知识检索    │  ───────▶  │   大模型诊断       │
│ Chroma/内存向量 │  参考资料   │  根因 + 处置建议   │
└────────────────┘            └────────┬─────────┘
                                       │
                                       ▼
                            ┌────────────────────┐
                            │  SQLite 诊断记录存储 │
                            └─────────┬──────────┘
                                      │
                                      ▼
                            ┌────────────────────┐
                            │  FastAPI + 前端面板  │
                            └────────────────────┘
```

---

## 三、快速开始（5 分钟跑起来）

> 详细版请看 **[docs/部署教程.md](docs/部署教程.md)**，内含每一步的验证方法。

```bash
# 1. 进入项目目录
cd ai-log-doctor

# 2. 创建虚拟环境（Windows）
python -m venv venv
venv\Scripts\activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 环境自检 —— 这一步会告诉你缺什么
python -m src.main check

# 5. 构建知识库（RAG）
python -m src.main kb

# 6. 生成演示数据
python -m src.main seed

# 7.（可选）分析 samples/app-logs 下的中间件日志（Redis/MySQL/Tomcat/Nginx/ES）
python -m src.main ingest --clear

# 8. 启动服务
python -m src.main serve
```

### 用管理脚本（推荐）

项目根目录提供了 `exec.sh`，启动/停止/重启一条命令搞定：

```bash
sh exec.sh start      # 启动（等健康检查通过才返回）
sh exec.sh stop       # 停止
sh exec.sh restart    # 重启
sh exec.sh status     # 查看状态 + 接口健康检查
sh exec.sh logs       # 实时看日志
sh exec.sh backup     # 备份数据库
```

脚本会自动选择管理方式：**有 systemd 就用 systemd**（开机自启 + 崩溃重启），
没有就降级为 `nohup` + PID 文件。命令完全一样，服务器和本地都能用。

> ⚠️ 注意：前端页面上的「生成演示数据」按钮会**清空数据库中的全部记录**
> （包括真实日志的分析结果）。操作前请先执行 `sh exec.sh backup`。

然后打开浏览器：

| 地址 | 说明 |
|---|---|
| http://127.0.0.1:8000/ | 前端诊断面板 |
| http://127.0.0.1:8000/docs | **FastAPI 接口文档，可在这里直接做 CRUD** |
| http://127.0.0.1:8000/api/kb/search?q=Connection%20refused | RAG 检索测试 |

想一键验证所有接口（含完整 CRUD）：

```bash
python scripts/api_smoke_test.py
```

---

## 四、目录结构

> 每个目录/文件是干什么的、哪个能改哪个别动、模块之间怎么互相调用 ——
> 详见 **[docs/目录结构详解.md](docs/目录结构详解.md)**。
>
> 完整文档都在 `docs/` 下：目录结构详解 · 部署教程 · 组件扫盲手册 ·
> 中间件日志源搭建 · 虚拟机部署指南（离线部署实录）。

```
ai-log-doctor/
├── README.md                    # 本文件
├── requirements.txt             # Python 依赖清单
├── .env.example                 # 配置模板（复制为 .env 后填写）
├── .gitignore
│
├── docker-compose.yml           # 被监控的演示环境（Nginx + Flask + MySQL）
├── nginx.conf                   # Nginx 反向代理配置
├── Dockerfile                   # 主程序镜像
├── .dockerignore
│
├── src/                         # 核心代码
│   ├── config.py                # 配置中心：读 .env，管理路径和参数
│   ├── db.py                    # 数据库层：SQLite CRUD（增删改查）
│   ├── collector.py             # 采集层：Docker 日志 / 文件日志 / 兜底样本
│   ├── detector.py              # 检测层：关键字匹配 + 严重程度判定
│   ├── rag.py                   # RAG 层：文档切片 + 向量化 + 检索
│   ├── llm.py                   # 模型层：Prompt 组装 + 大模型调用 + Mock
│   ├── pipeline.py              # 编排层：把上面五层串成一条流水线
│   ├── api.py                   # 接口层：FastAPI，对外提供 HTTP 接口
│   ├── seed.py                  # 生成演示数据
│   └── main.py                  # 命令行入口（check/kb/seed/scan/ingest/serve）
│
├── knowledge/                   # ★ 知识库 = RAG 的数据来源（共 8 个手册、51 个片段）
│   ├── docker-errors.md         #   Docker 常见错误排查手册
│   ├── nginx-errors.md          #   Nginx 常见错误排查手册
│   ├── mysql-errors.md          #   MySQL 常见错误排查手册
│   ├── redis-errors.md          #   Redis 常见错误排查手册
│   ├── tomcat-errors.md         #   Tomcat / Java 应用错误排查手册
│   ├── middleware-errors.md     #   ES / Kafka / RabbitMQ 中间件错误手册
│   ├── ci-cd-errors.md          #   CI/CD 流水线错误排查手册
│   └── python-app-errors.md     #   Python/Flask 应用错误排查手册
│
├── samples/                     # ★ 演示日志样本 = 日志数据来源
│   ├── demo-logs.json           #   各服务的故障日志样本
│   ├── ci-failed.log            #   模拟 GitHub Actions 失败日志
│   └── app-logs/                #   ★ 中间件真实风格日志（离线分析用）
│       ├── redis.log            #     Redis：RDB 失败 / fork / maxmemory / 主从断开
│       ├── mysql-error.log      #     MySQL：慢查询 / 连接数 / 死锁 / 磁盘满
│       ├── tomcat-catalina.out  #     Tomcat：OOM / 空指针 / 连接池 / 句柄耗尽
│       ├── nginx-error.log      #     Nginx：502 / 504 / SSL / 限流
│       └── elasticsearch.log    #     ES：磁盘水位 / 熔断 / 选举 / 分片失败
│
├── demo_app/                    # 被监控对象：故意出错的 Flask 应用
│   ├── app.py
│   ├── requirements.txt
│   └── Dockerfile
│
├── web/index.html               # 前端诊断面板（原生 HTML + JS，无需打包）
├── scripts/
│   ├── ci_diagnose.py           # CI 日志诊断脚本（流水线失败时调用）
│   └── api_smoke_test.py        # 接口冒烟测试（验证 CRUD 全流程）
│
├── tests/test_basic.py          # 单元测试（14 个用例）
└── .github/workflows/
    ├── ci.yml                   # CI：测试 + 构建 + 推送镜像
    └── ai-diagnose.yml          # CI 失败时的 AI 自动诊断
```

---

## 五、技术栈与依赖说明

### 用了哪些库，各自干什么

| 依赖 | 作用 | 为什么选它 |
|---|---|---|
| **FastAPI** | 写 HTTP 接口的 Web 框架 | 自动生成 `/docs` 交互式文档，参数自动校验，代码量比 Flask 少 |
| **Uvicorn** | ASGI 服务器 | FastAPI 必须靠它才能跑起来，负责监听端口、处理并发 |
| **Pydantic** | 数据模型与校验 | 定义接口的请求/响应结构，传错参数会明确报错 |
| **Requests** | HTTP 客户端 | 调用大模型 API（也用于 Embedding 接口） |
| **python-dotenv** | 读取 `.env` | 把密钥从代码里隔离出来，避免泄露到 GitHub |
| **SQLite**（Python 内置） | 数据库 | 零安装，数据库就是一个文件，语法和 MySQL 几乎一样 |
| **Chroma**（可选） | 向量数据库 | 生产级向量检索；**装不上会自动降级为内置内存版** |
| **pytest** | 单元测试 | 改代码后自动验证有没有改坏 |
| **Docker / Compose** | 容器化 | 起被监控的服务 + 打包主程序镜像 |
| **GitHub Actions** | CI/CD | 免费，和 GitHub 仓库无缝集成 |

### 关键设计：两处「自动降级」

新手最容易被环境卡住，所以项目做了两处降级：

1. **向量库降级**：装了 `chromadb` 就用 Chroma，装不上就用内置内存向量库（纯 Python 实现的余弦相似度），功能完全不受影响。
2. **模型降级（Mock 模式）**：没配 `LLM_API_KEY` 时，用内置规则库生成诊断结论。**整条链路（采集→检测→RAG→诊断→入库→前端）依然完整可跑**，你不会因为「没 Key」就看不到任何东西。

---

## 六、数据来源说明

**本项目所有数据都是可解释、可追溯的**（这一点很关键，下面逐条说明来源）：

### 1. 日志数据来源

| 来源 | 文件 | 说明 |
|---|---|---|
| 真实容器日志 | `docker logs` | Docker 可用时，采集 `demo-nginx` / `demo-app` / `demo-mysql` 的真实输出 |
| 中间件日志（离线） | `samples/app-logs/*.log` | Redis / MySQL / Tomcat / Nginx / ES 五个中间件日志，模拟真实格式，用 `ingest` 命令批量分析 |
| 手写故障样本 | `samples/demo-logs.json` | Docker 不可用时兜底；包含数据库连接失败、OOM、超时、权限不足等典型故障 |
| CI 流水线日志 | `samples/ci-failed.log` | 模拟 GitHub Actions 构建失败（pip 依赖装不上）的真实输出格式 |
| 应用主动产生 | `demo_app/app.py` | 提供 `/db`、`/boom`、`/slow`、`/oom` 接口，可随时「一键制造故障」 |

### 2. 知识库来源（RAG 的检索对象）

`knowledge/*.md` 共 8 个手册、51 个知识片段（Docker / Nginx / MySQL / Redis / Tomcat /
ES-Kafka-RabbitMQ / CI-CD / Python），内容是我整理的运维排错经验：
现象 → 常见原因 → 排查命令 → 解决方案。

这些文档可以**自行扩展**：把你学到的新知识写成 `## 标题` 开头的 Markdown 加进去，执行 `python -m src.main kb --force` 重建即可。

### 3. 数据库数据来源

`data/doctor.db`（SQLite）里的 `diagnoses` 表，记录来源有三种：
- **自动生成**：`python -m src.main seed` 生成 34 条历史记录（随机分布在过去 7 天，状态随机）
- **扫描产生**：点击前端「立即扫描」或调用 `POST /api/scan`
- **手动提交**：在 `/docs` 里调用 `POST /api/diagnoses` 提交任意一段日志

表结构见 `src/db.py` 顶部注释。

---

## 七、接口清单

启动服务后访问 `/docs` 可交互式测试：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/config` | 查看当前配置（密钥打码） |
| GET | `/api/stats` | 统计数据 |
| GET | `/api/kb/info` | 知识库状态 |
| POST | `/api/kb/rebuild` | 重建知识库 |
| GET | `/api/kb/search?q=xxx` | **RAG 检索测试** |
| POST | `/api/diagnoses` | **C** - 提交日志触发诊断 |
| GET | `/api/diagnoses` | **R** - 分页列表（支持 service/status/severity/keyword 筛选） |
| GET | `/api/diagnoses/{id}` | **R** - 单条详情 |
| PUT | `/api/diagnoses/{id}` | **U** - 更新（常用：标记已解决） |
| DELETE | `/api/diagnoses/{id}` | **D** - 删除 |
| POST | `/api/scan` | 立即扫描容器日志 |
| POST | `/api/seed` | 重新生成演示数据 |

---

## 八、后续扩展方向

- **接入 Prometheus**：用 `prometheus-client` 暴露指标，配 Grafana 看板
- **接入 K8s**：把 `collector.py` 的 `docker logs` 换成 `kubectl logs`，即可采集 Pod 日志
- **知识库闭环**：每次 AI 诊断后把结论回写进 `knowledge/history-cases.md`，系统越用越准
- **告警通知**：在 `pipeline.py` 里加一步，调用钉钉/飞书/企业微信机器人
- **真实 Embedding**：配置 `EMBED_API_KEY` 后，RAG 检索准确度会明显提升

---

## 九、常见问题

**Q：没有大模型 API Key 能跑吗？**
能。会自动进入 Mock 模式，用规则库生成诊断，全流程可跑通。

**Q：Docker 没装能跑吗？**
能。采集层会自动降级到 `samples/demo-logs.json` 的样本日志。

**Q：chromadb 装不上怎么办？**
不用管，程序会自动降级为内置内存向量库。也可以从 `requirements.txt` 里注释掉它。

**Q：改了知识库文档后没生效？**
执行 `python -m src.main kb --force` 强制重建，或调用 `POST /api/kb/rebuild`。

**Q：Windows 终端中文乱码？**
执行 `chcp 65001` 切换为 UTF-8，或设置环境变量 `PYTHONIOENCODING=utf-8`。

---

## 十、许可

本项目基于 [MIT License](LICENSE) 开源。
