# 机器人3 复刻版 — 完整架构与功能说明

> 基于"机器人3.zip"（微信自动化机器人）逆向分析后的完整复刻实现。
> 源码 30,021 行（90个Python文件） + 测试 27个文件（597项全部通过） + Web界面 + 文档。

---

## 目录

1. [系统总览](#1-系统总览)
2. [项目结构](#2-项目结构)
3. [配置层](#3-配置层)
4. [数据库层](#4-数据库层)
5. [核心引擎层](#5-核心引擎层)
6. [消息管道](#6-消息管道)
7. [微信接口层](#7-微信接口层)
8. [业务模块层](#8-业务模块层)
9. [HTTP API 层](#9-http-api-层)
10. [WebSocket 实时通信](#10-websocket-实时通信)
11. [安全模块](#11-安全模块)
12. [网络通信层](#12-网络通信层)
13. [Web 管理界面](#13-web-管理界面)
14. [启动与部署](#14-启动与部署)
15. [原软件映射表](#15-原软件映射表)

---

## 1. 系统总览

### 1.1 定位

"机器人3 复刻版"是对原易语言微信自动化机器人（内部代号 `c6802`）的 Python 现代化重写。原软件通过 C++ DLL 注入微信 3.9.12.56 实现消息自动化，复刻版保留了相同的 Hook 架构和功能模块，同时用 FastAPI + SQLAlchemy + asyncio 替换了原有的易语言 GUI + SQLite 直连方案。

### 1.2 技术栈

| 层次 | 原软件 | 复刻版 | 选型理由 |
|------|--------|--------|---------|
| 主开发语言 | 易语言 (E-Language) | Python 3.10+ | 生态丰富，异步原生支持 |
| Web 框架 | 无（本地GUI） | FastAPI + Uvicorn | 自动文档、异步原生、高性能 |
| 数据库 | SQLite + wxsqlite3 | SQLite + SQLAlchemy 2.0 async | 标准ORM，支持加密扩展 |
| HTTP 客户端 | libcurl | httpx | 异步原生，连接池管理 |
| 消息队列 | AMQP (RabbitMQ) | asyncio.Queue 本地实现 | 单机足够，零外部依赖 |
| 缓存 | Memcached | 内存缓存 (dict) | 单机场景下足够高效 |
| 搜索 | Apache Solr | SQLite FTS / 内存过滤 | 无需额外服务 |
| 脚本引擎 | Node.js/V8 (node.dll) | Python 内置 eval | 无需额外运行时 |
| 日志 | 无 | loguru | 结构化日志，文件滚动 |
| 数据验证 | 无 | Pydantic v2 | 类型安全，自动文档 |

### 1.3 架构分层图

```
┌──────────────────────────────────────────────────────────┐
│                    Web 管理界面 (HTML/JS)                  │
│                 web/index.html (666行SPA)                  │
├──────────────────────────────────────────────────────────┤
│                    HTTP API 层 (FastAPI)                   │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌───────────────┐   │
│  │ message │ │ contact │ │  group  │ │   instance    │   │
│  │ routes  │ │ routes  │ │ routes  │ │    routes     │   │
│  └────┬────┘ └────┬────┘ └────┬────┘ └──────┬────────┘   │
│       └───────────┴───────────┴─────────────┘            │
│                   WebSocket /ws                           │
├──────────────────────────────────────────────────────────┤
│                    核心引擎层 (CoreEngine)                  │
│  ┌──────────┐ ┌───────────────┐ ┌──────────────────────┐  │
│  │ 线程池    │ │  消息管道      │ │   实例管理器          │  │
│  │ ThreadPool│ │ MessagePipeline│ │ InstanceManager     │  │
│  └──────────┘ └───────────────┘ └──────────────────────┘  │
│  ┌──────────────────────────────────────────────────────┐ │
│  │              事件系统 + 健康检查                       │ │
│  └──────────────────────────────────────────────────────┘ │
├──────────────────────────────────────────────────────────┤
│                    业务模块层 (Modules)                     │
│  ┌───────────┐ ┌───────────┐ ┌───────────┐ ┌───────────┐ │
│  │  记账模块  │ │ 自动回复   │ │  群管理    │ │ 定时任务   │ │
│  │Bookkeeping│ │ AutoReply │ │ GroupMgr  │ │Scheduler  │ │
│  └───────────┘ └───────────┘ └───────────┘ └───────────┘ │
├──────────────────────────────────────────────────────────┤
│                  微信接口层 (WeChat)                        │
│  ┌─────────────────┐ ┌──────────────┐ ┌───────────────┐   │
│  │ HookInterface   │ │ WeChatClient │ │ ContactManager│   │
│  │ (抽象接口)       │ │ (Mock+真实)  │ │  (缓存+DB)    │   │
│  └─────────────────┘ └──────────────┘ └───────────────┘   │
├──────────────────────────────────────────────────────────┤
│              数据库层 + 安全层 + 网络通信层                   │
│  ┌──────────┐ ┌────────────┐ ┌──────────┐ ┌───────────┐  │
│  │ SQLite   │ │ AES/RSA    │ │ HttpClient│ │MsgQueue   │  │
│  │ Manager  │ │ License    │ │ Updater  │ │Firewall   │  │
│  └──────────┘ └────────────┘ └──────────┘ └───────────┘  │
├──────────────────────────────────────────────────────────┤
│                    配置层 (Config)                         │
│         settings.py (全局) + instance_config.py (实例)      │
└──────────────────────────────────────────────────────────┘
```

### 1.4 数据流

```
微信消息到达
  → WeChatClient.message_callback (消息回调)
  → MessagePipeline.enqueue (入队)
  → MessagePipeline._parse (解析消息类型/发送者)
  → MessagePipeline._route (路由到已注册的处理器)
  → BookkeepingModule / AutoReplyModule / GroupManagerModule (业务处理)
  → MessagePipeline._send (发送回复，含分片+限速)
  → WeChatClient.send_text/send_image (调用微信API)
  → WebSocketManager.broadcast (推送到Web界面)
  → Database (持久化)
```

---

## 2. 项目结构

```
robot3-replica/
│
├── run.py                          # 主启动脚本 (命令行参数 / 信号处理 / uvicorn)
├── requirements.txt                # Python 依赖清单
├── .env                            # 环境变量配置
├── pytest.ini                      # pytest 配置
│
├── config/                         # ── 配置层 ──
│   ├── __init__.py
│   ├── settings.py                 # 全局配置 (对应原 config.ini)
│   └── instance_config.py          # 实例配置 (对应原 app/c680X/config.ini)
│
├── core/                           # ── 核心引擎层 ──
│   ├── __init__.py
│   ├── engine.py                   # CoreEngine 主调度引擎
│   ├── message_pipeline.py         # 消息处理管道 (分片/限速/ACK)
│   ├── instance_manager.py         # 多实例管理器
│   ├── thread_pool.py              # 优先级线程池
│   └── websocket_manager.py        # WebSocket 连接管理
│
├── database/                       # ── 数据库层 ──
│   ├── __init__.py                 # 共享 Base / Database 引擎
│   ├── models.py                   # SQLAlchemy ORM 模型 (7张表)
│   ├── db_manager.py               # SQLAlchemy async 数据库管理器
│   ├── manager.py                  # aiosqlite 直接实现 (轻量方案)
│   └── migrations.py               # 建表 / 种子数据 / 迁移
│
├── wechat/                         # ── 微信接口层 ──
│   ├── __init__.py
│   ├── message_types.py            # 消息类型枚举 + Pydantic 模型
│   ├── hook_interface.py           # Hook 抽象接口 (API[0]~API[24])
│   ├── wechat_client.py            # 双模式客户端 (Mock + 真实Hook)
│   └── contact_manager.py          # 联系人管理 (缓存+DB)
│
├── modules/                        # ── 业务模块层 ──
│   ├── __init__.py
│   ├── bookkeeping.py              # 记账模块 (群消息解析+统计+后端同步)
│   ├── auto_reply.py               # 自动回复 (关键词/正则/全匹配)
│   ├── group_manager.py            # 群管理 (欢迎/撤回/公告)
│   └── task_scheduler.py           # 定时任务 (cron+间隔)
│
├── api/                            # ── HTTP API 层 ──
│   ├── __init__.py
│   ├── server.py                   # FastAPI 主服务器
│   ├── deps.py                     # 依赖注入
│   └── routes/
│       ├── __init__.py
│       ├── message.py              # 消息 API (发送/历史/WebSocket)
│       ├── contact.py              # 联系人 API (列表/搜索/备注/同步)
│       ├── group.py                # 群管理 API (列表/成员/公告/统计)
│       └── instance.py             # 实例管理 API (CRUD/启停/配置/记账)
│
├── security/                       # ── 安全模块 ──
│   ├── __init__.py
│   ├── crypto.py                   # AES 加密 + RSA 签名
│   ├── license.py                  # 许可证验证 (run.vef)
│   └── firewall.py                 # IP 黑/白名单防火墙
│
├── network/                        # ── 网络通信层 ──
│   ├── __init__.py
│   ├── http_client.py              # 异步 HTTP 客户端 (重试/超时/代理)
│   ├── message_queue.py            # 本地异步消息队列 (发布订阅)
│   └── updater.py                  # 自动更新器 (GitHub/Gitee Release)
│
├── web/                            # ── Web 管理界面 ──
│   └── index.html                  # 单文件 SPA (深色主题)
│
├── tests/                          # ── 测试套件 ──
│   ├── conftest.py                 # pytest 夹具
│   ├── run_all.py                  # 自定义测试运行器
│   ├── test_database.py            # 数据库测试 (12项)
│   ├── test_message_pipeline.py    # 消息管道测试 (13项)
│   ├── test_thread_pool.py         # 线程池测试 (12项)
│   ├── test_security.py            # 安全模块测试 (18项)
│   ├── test_network.py             # 网络模块测试 (19项)
│   ├── test_wechat.py              # 微信接口测试 (21项)
│   ├── test_config.py              # 配置测试 (10项)
│   ├── test_api_health.py          # API健康测试 (5项)
│   ├── test_api_instance.py        # 实例API测试 (16项)
│   ├── test_api_message.py         # 消息API测试 (14项)
│   ├── test_api_contact.py         # 联系人API测试 (10项)
│   ├── test_api_group.py           # 群管理API测试 (7项)
│   ├── test_api_websocket.py       # WebSocket测试 (8项)
│   ├── test_bookkeeping.py         # 记账模块测试 (13项)
│   ├── test_auto_reply.py          # 自动回复测试 (15项)
│   ├── test_group_manager.py       # 群管理测试 (13项)
│   └── test_task_scheduler.py      # 定时任务测试 (16项)
│
├── data/                           # 运行时数据 (自动创建)
│   ├── db/                         # SQLite 数据库文件
│   │   ├── data.db                 # 主库 (Instance 表)
│   │   └── {instance_id}_data.db   # 实例库 (各自独立)
│   └── logs/                       # 日志文件 (按大小滚动)
```

---

## 3. 配置层

### 3.1 全局配置 (`config/settings.py`)

`Settings` 类继承 `pydantic_settings.BaseSettings`，通过环境变量或 `.env` 文件加载配置，对应原软件的 `data/config.ini`。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `app_name` | str | "机器人3-复刻版" | 应用名称 |
| `app_version` | str | "1.0.0" | 版本号 |
| `api_host` | str | "0.0.0.0" | API监听地址 |
| `api_port` | int | 3000 | API监听端口 |
| `wechat_version` | str | "3.9.12.56" | 目标微信版本 |
| `msg_max_lines` | int | 70 | 消息最多行数（触发分片） |
| `msg_sleep_sec` | float | 1.0 | 发送间隔（秒） |
| `msg_split_enabled` | bool | True | 是否启用长消息分片 |
| `thread_pool_size` | int | 50 | 工作线程数 |
| `db_encrypt_key` | str | "" | 数据库加密密钥（空=不加密） |
| `license_id` | str | "" | 许可证ID（对应 run.vef） |
| `ip_whitelist_enabled` | bool | False | 是否启用IP白名单 |
| `log_level` | str | "INFO" | 日志级别 |
| `log_max_size_mb` | int | 10 | 单个日志文件最大MB |
| `log_retention_days` | int | 30 | 日志保留天数 |
| `backend_url` | str | "" | 后端API地址 |
| `update_repo` | str | "" | 更新仓库地址 |

**关键方法**：

- `ensure_dirs()` — 确保 `data/`、`logs/`、`db/`、`web/` 目录存在
- 全局单例 `settings` — 所有模块通过 `from config.settings import settings` 获取

### 3.2 实例配置 (`config/instance_config.py`)

`InstanceConfig` 对应原软件 `data/app/c680X/config.ini`，每个机器人实例有独立配置。

| 字段 | 类型 | 说明 |
|------|------|------|
| `instance_id` | str | 实例ID（如 c6801、c6802） |
| `display_name` | str | 显示名称 |
| `wxid` | str | 当前登录的微信ID |
| `jizhang_enabled` | bool | 是否启用记账模块 |
| `jizhang_domain` | str | 记账后端API域名 |
| `jizhang_keyword` | str | AES加密的功能配置 |
| `msg_split_enabled` | bool | 消息分片开关 |
| `msg_max_lines` | int | 消息最多行数 |
| `msg_sleep_sec` | float | 发送间隔 |
| `thread_post_count` | int | 发送线程数 |
| `db_path` | Path | 实例数据库路径 |

**关键方法**：

- `from_ini(path)` — 从INI文件加载（兼容原软件GBK编码格式）
- `save_ini(path)` — 保存为INI文件
- `to_dict()` — 转为字典

---

## 4. 数据库层

### 4.1 数据库架构

系统采用 **主库 + 实例库** 双层架构：

```
data/db/
├── data.db                    # 主库 — 存储 Instance 表（实例注册信息）
├── c6801_data.db              # 实例1库 — 存储 Contact/Message/Group 等
├── c6802_data.db              # 实例2库
└── demo_data.db               # demo实例库
```

每个实例拥有独立数据库文件，实现 **数据隔离**。主库仅存储实例注册信息，便于启动时恢复。

### 4.2 数据模型 (`database/models.py`)

使用 SQLAlchemy 2.0 声明式语法（`Mapped` / `mapped_column`），共定义 7 张核心表：

#### 主库表

| 表名 | 说明 | 关键字段 |
|------|------|---------|
| `instances` | 实例注册表 | instance_id, display_name, wxid, status(running/stopped), config_json |

#### 实例库表

| 表名 | 说明 | 关键字段 |
|------|------|---------|
| `contacts` | 联系人/群 | wxid(PK), nickname, remark, avatar, type(1=个人/2=群聊/3=公众号), instance_id |
| `messages` | 消息记录 | id, instance_id, msg_id, sender_wxid, receiver_wxid, content, msg_type, is_received, raw_xml |
| `groups` | 群信息 | id, instance_id, group_wxid, group_name, member_count, announcement, owner_wxid |
| `group_members` | 群成员 | id, group_id, wxid, display_name, join_time |
| `bookkeeping_records` | 记账记录 | id, instance_id, group_id, user_wxid, amount, bank_name, description, status |
| `task_logs` | 任务日志 | id, instance_id, task_type, status, result, created_at |

#### 枚举常量

```python
class ContactType:    PERSON=1  GROUP=2  OFFICIAL=3
class MessageType:    TEXT / IMAGE / FILE / VIDEO / VOICE / SYSTEM
class InstanceStatus: RUNNING="running"  STOPPED="stopped"
class TaskStatus:     PENDING / RUNNING / SUCCESS / FAILED
```

### 4.3 数据库管理器

系统提供两套数据库管理器，按需选用：

#### `database/db_manager.py` — SQLAlchemy Async 方案

`DatabaseManager` 类基于 `create_async_engine` + `aiosqlite`：

| 方法 | 功能 |
|------|------|
| `init_main_db()` | 初始化主库（data.db），创建 Instance 表 |
| `init_instance_db(instance_id)` | 初始化实例库（{id}_data.db），创建6张业务表 |
| `init_db(instance_id=None)` | 一键初始化（主库+实例库） |
| `get_session(db_name)` | 获取异步 Session（从引擎池） |
| `health_check()` | 健康检查（验证连接可用性） |
| `close_all()` | 关闭所有引擎连接 |

特性：
- 按数据库名懒加载引擎并缓存（`_engines` 字典）
- 通过 `event.listens_for` 钩子用 loguru 记录每条 SQL
- 支持 `instance_db_name(instance_id)` 计算实例库名

#### `database/manager.py` — aiosqlite 直接方案

`DatabaseManager` 类直接操作 aiosqlite，更轻量，提供 8 张表的完整 CRUD：
- `init()` / `close()` — 初始化与关闭
- `insert_contact()` / `list_contacts()` / `search_contacts()` — 联系人操作
- `insert_message()` / `list_messages()` / `get_recent_messages()` — 消息操作
- `insert_group()` / `list_groups()` / `list_group_members()` — 群操作
- `insert_bookkeeping()` / `list_bookkeeping()` / `bookkeeping_stats()` — 记账操作
- `upsert_instance()` / `list_instances()` / `get_instance()` — 实例操作
- `insert_task_log()` / `list_task_logs()` — 任务日志
- `add_firewall_ip()` / `list_firewall_ips()` / `remove_firewall_ip()` — 防火墙

### 4.4 迁移与种子数据 (`database/migrations.py`)

| 函数 | 功能 |
|------|------|
| `create_tables(db_manager)` | 创建主库所有表 |
| `create_instance_tables(db_manager, instance_id)` | 创建实例库所有表 |
| `seed_default_data(db_manager, instance_id)` | 插入种子数据（文件传输助手、微信团队等6个系统联系人），幂等 |
| `upsert_instance_record(db_manager, config)` | 插入或更新实例注册记录 |
| `upgrade(db_manager, instance_id, seed)` | 迁移入口（建表+种子） |

---

## 5. 核心引擎层

### 5.1 CoreEngine (`core/engine.py`)

系统主调度引擎，协调所有子系统的启动、运行和关闭。

**核心职责**：

| 职责 | 实现方式 |
|------|---------|
| 初始化子系统 | `start()` 中依次启动数据库、线程池、消息队列、实例管理器 |
| 消息调度 | `_dispatch_loop()` 定时遍历实例，触发 tick 事件 |
| 健康检查 | `_health_check_loop()` 定时检查数据库/实例/线程池状态 |
| 事件系统 | `on(event, callback)` / `emit(event, data)` 支持同步和异步监听器 |
| 实例管理 | 封装 InstanceManager 的创建/启停/状态查询 |
| 消息收发 | `send_text()` / `send_image()` / `send_file()` 代理到实例的微信客户端 |
| 仪表盘 | `dashboard_stats()` 汇总实例数、消息数、在线状态 |

**事件类型**：

| 事件名 | 触发时机 | 数据 |
|--------|---------|------|
| `tick` | 每秒调度循环 | `{timestamp}` |
| `health` | 每60秒健康检查 | `{db_ok, instances, threads}` |
| `message.received` | 收到新消息 | `{instance_id, message}` |
| `message.sent` | 消息发送成功 | `{instance_id, wxid, text}` |
| `instance.started` | 实例启动 | `{instance_id}` |
| `instance.stopped` | 实例停止 | `{instance_id}` |

**生命周期**：

```python
engine = CoreEngine()
await engine.start()           # 启动所有子系统
# ... 运行中 ...
await engine.stop()            # 优雅关闭所有子系统
```

**Mock 模式**：`engine.mock = True` 时，实例使用 `MockWeChatClient` 模拟消息收发，无需真实微信。

### 5.2 线程池 (`core/thread_pool.py`)

`ThreadPoolManager` 基于 `concurrent.futures.ThreadPoolExecutor` + 优先级堆（`heapq`）。

**设计**：
- 任务包装为 `_PrioritizedTask`（含 priority / seq / future / fn / args）
- `heapq` 按元组比较：先 priority（越小越优先），再 seq（FIFO 兜底）
- 调度线程（dispatcher）从堆中取出任务提交给 ThreadPoolExecutor
- 信号量限制在途任务数 ≤ max_workers，保障高优先级任务先执行

| 方法 | 功能 |
|------|------|
| `submit_task(fn, *args, priority=0, **kwargs)` | 提交任务，返回 Future |
| `submit_async(fn, *args, priority=0, **kwargs)` | async 适配，返回 asyncio.Future |
| `get_status()` | 返回 `{queued, in_flight, submitted, completed, failed}` |
| `shutdown()` | 关闭线程池 |

### 5.3 实例管理器 (`core/instance_manager.py`)

`InstanceManager` 管理多个机器人实例的完整生命周期。

**InstanceRuntime** 数据结构：
```python
@dataclass
class InstanceRuntime:
    instance_id: str
    config: InstanceConfig
    pipeline: MessagePipeline
    status: str  # running / stopped
    started_at: Optional[datetime]
    send_callback: Optional[SendCallback]
```

| 方法 | 功能 |
|------|------|
| `create_instance(config)` | 创建实例（建库+种子数据+持久化） |
| `start_instance(instance_id)` | 启动实例（初始化微信客户端+消息管道） |
| `stop_instance(instance_id)` | 停止实例（卸载Hook+停止管道） |
| `get_instance_status(instance_id)` | 查询状态 |
| `list_instances()` | 列出所有实例 |
| `reload_config(instance_id)` | 热加载配置（不重启实例） |
| `load_instances()` | 从主库恢复已注册实例 |
| `set_send_callback(instance_id, callback)` | 设置发送回调 |

---

## 6. 消息管道

### 6.1 MessagePipeline (`core/message_pipeline.py`)

异步消息处理管道，实现 **入队 → 解析 → 路由 → 处理 → 发送** 的完整流程。

```
消息入队 (enqueue)
    │
    ▼
  解析 (_parse) ─── 解析消息类型、发送者、群信息
    │
    ▼
  路由 (_route) ─── 按 msg_type 分发到已注册的处理器
    │
    ▼
  处理 (handler) ── 业务模块处理（记账/自动回复/群管理）
    │
    ▼
  发送 (_send) ──── 分片 + 限速 + ACK确认
```

**核心特性**：

| 特性 | 实现 |
|------|------|
| 消息队列 | `asyncio.Queue` 异步队列 |
| 消息分片 | `_split_message(content, max_lines)` 按行分片，超过 `msg_max_lines` 自动拆分 |
| 发送限速 | 分片间按 `msg_sleep_sec` 间隔 `asyncio.sleep()`，防风控 |
| 处理器注册 | `register_handler(msg_type, handler)` 按消息类型注册，支持默认处理器 |
| 消息确认 | `AckMessage` 机制，`on_ack(callback)` 注册回调，`ack()` 在 PROCESSING/PROCESSED/SENT/FAILED 各阶段回执 |
| 发送回调 | `SendCallback` 类型注入，实际发送动作由外部注入（解耦微信客户端） |

**ACK 状态流转**：

```
ENQUEUED → PROCESSING → PROCESSED → SENT
                ↓            ↓
              FAILED       FAILED
```

**管道控制**：

| 方法 | 功能 |
|------|------|
| `enqueue(message)` | 消息入队 |
| `register_handler(msg_type, handler)` | 注册消息处理器 |
| `on_ack(callback)` | 注册 ACK 回调 |
| `set_send_callback(callback)` | 设置发送回调 |
| `start()` / `stop()` | 启动/停止管道 |
| `get_status()` | 管道状态（队列长度/已处理数/失败数） |

---

## 7. 微信接口层

### 7.1 消息类型 (`wechat/message_types.py`)

```python
class MessageType(Enum):
    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    VIDEO = "video"
    VOICE = "voice"
    CARD = "card"        # 名片
    LINK = "link"        # 链接
    SYSTEM = "system"    # 系统消息
    EMOJI = "emoji"      # 表情
    LOCATION = "location" # 位置
```

**MessageData** Pydantic 模型：

| 字段 | 类型 | 说明 |
|------|------|------|
| `msg_id` | str | 消息ID |
| `sender_wxid` | str | 发送者wxid |
| `receiver_wxid` | str | 接收者wxid |
| `content` | str | 消息内容 |
| `msg_type` | MessageType | 消息类型 |
| `raw_xml` | str | 原始XML |
| `timestamp` | float | 时间戳 |
| `is_group` | bool | 是否群消息 |
| `group_wxid` | str | 群wxid（群消息时） |
| `at_users` | list[str] | @的用户列表 |

**派生属性**：
- `content_body` — 群消息去除 `wxid:\n` 前缀后的实际内容
- `actual_sender_in_group` — 群消息中的实际发送者

**SendResult** 模型：`success: bool`, `msg_id: str`, `error: str`，含 `ok()` / `fail()` 工厂方法。

### 7.2 Hook 抽象接口 (`wechat/hook_interface.py`)

`WeChatHookInterface` 抽象基类定义了所有微信操作的统一接口，对应原软件 `weixin.dll` 导出的 `init` / `api` / `loadWindow` / `uninstall` 四个函数。

**抽象方法**：

| 方法 | 对应原软件 | 功能 |
|------|-----------|------|
| `init(instance_id)` | `init()` | 初始化Hook，加载配置，准备内存地址 |
| `load_window()` | `loadWindow()` | 查找并绑定微信窗口句柄 |
| `api(command, params)` | `api()` | 核心API入口，接收指令并执行 |
| `send_text(wxid, text)` | API[5]~API[9] | 发送文本消息 |
| `send_image(wxid, path)` | API[5]~API[9] | 发送图片 |
| `send_file(wxid, path)` | API[5]~API[9] | 发送文件 |
| `get_contacts()` | API[15]~API[19] | 获取联系人列表 |
| `get_groups()` | API[15]~API[19] | 获取群列表 |
| `get_group_members(group_wxid)` | API[15]~API[19] | 获取群成员 |
| `get_login_info()` | API[0]~API[4] | 获取登录信息 |
| `uninstall()` | `uninstall()` | 卸载Hook，恢复原始函数 |
| `set_message_callback(callback)` | — | 注册消息接收回调 |

**APICommand 常量**（对应原软件 API[0]~API[24] 的 25 个槽位）：

```python
class APICommand:
    # 登录/账户 (API[0]~API[4])
    GET_LOGIN_INFO     = "api_0"
    GET_LOGIN_STATUS   = "api_1"
    GET_QR_CODE        = "api_2"
    GET_SELF_INFO      = "api_3"
    LOGOUT             = "api_4"
    # 消息发送 (API[5]~API[9])
    SEND_TEXT           = "api_5"
    SEND_IMAGE          = "api_6"
    SEND_FILE           = "api_7"
    SEND_CARD           = "api_8"
    SEND_LINK           = "api_9"
    # 消息接收 (API[10]~API[14])
    RECV_TEXT           = "api_10"
    RECV_IMAGE          = "api_11"
    RECV_FILE           = "api_12"
    RECV_VIDEO          = "api_13"
    RECV_VOICE          = "api_14"
    # 联系人/群 (API[15]~API[19])
    GET_CONTACT_LIST    = "api_15"
    GET_GROUP_LIST      = "api_16"
    GET_GROUP_MEMBERS   = "api_17"
    UPDATE_REMARK       = "api_18"
    GET_CONTACT_INFO    = "api_19"
    # 高级功能 (API[20]~API[24])
    SEND_GROUP_MSG      = "api_20"
    REVOKE_MSG          = "api_21"
    SEND_AT             = "api_22"
    DB_DECRYPT          = "api_23"
    MEMORY_SEARCH       = "api_24"
```

### 7.3 微信客户端 (`wechat/wechat_client.py`)

`WeChatClient` 实现 `WeChatHookInterface`，支持双模式运行：

#### Mock 模式（开发测试）

- 内置 6 个测试联系人（张三/李四/王五/服务通知/文件传输助手/微信团队）+ 3 个测试群
- 定时器随机生成消息触发回调（模拟收到消息）
- `send_text()` / `send_image()` 等方法返回成功结果，记录调用日志
- `psutil` 检测微信进程（Mock模式下不检测）
- 长消息自动分片 + 发送限速

#### 真实 Hook 模式（生产环境）

- 通过 `ctypes.WinDLL` 加载 `weixin.dll`
- 绑定 DLL 的 `init` / `api` / `loadWindow` / `uninstall` 四个导出函数
- `api()` 方法通过 ctypes 调用 DLL 的 api 函数，传入 JSON 格式的命令和参数
- `psutil` 检测微信进程是否运行
- 指数退避自动重连机制（微信崩溃/重启后自动恢复）

**关键方法实现**：

| 方法 | Mock 模式 | 真实模式 |
|------|----------|---------|
| `init()` | 返回 True | `ctypes` 调用 `dll.init()` |
| `load_window()` | 返回 True | `ctypes` 调用 `dll.loadWindow()` |
| `api(cmd, params)` | 返回模拟数据 | `ctypes` 调用 `dll.api(cmd_json)` |
| `send_text()` | 记录到 `sent_texts` | 调用 `api(SEND_TEXT, {wxid, text})` |
| `get_contacts()` | 返回内置列表 | 调用 `api(GET_CONTACT_LIST)` |
| `uninstall()` | 返回 True | `ctypes` 调用 `dll.uninstall()` |

### 7.4 联系人管理 (`wechat/contact_manager.py`)

`ContactManager` 采用 **内存缓存 + 数据库持久化** 双层结构。

| 方法 | 功能 |
|------|------|
| `sync_contacts(client)` | 从微信同步联系人/群到本地数据库 |
| `get_contact(wxid)` | 获取联系人信息（先查缓存，再查DB） |
| `search_contacts(keyword)` | 模糊搜索（按昵称/备注/wxid/全拼） |
| `update_remark(wxid, remark)` | 修改备注（同步微信+本地） |
| `_cache_contact(contact)` | 写入内存缓存 |
| `_invalidate_cache()` | 清除缓存 |

**Contact ORM 模型**字段：wxid(PK), nickname, remark, avatar, is_group, group_name, member_count, alias, remark_quanpin, sync_time。

---

## 8. 业务模块层

### 8.1 记账模块 (`modules/bookkeeping.py`)

对应原软件 `jizhang` 模块，从群消息中解析记账指令并记录。

**指令格式**：`记账 金额 银行名称 备注`（金额可负数）

**BookkeepingRecord ORM**：id, group_id, user_wxid, amount, bank_name, description, status, created_at。

| 方法 | 功能 |
|------|------|
| `handle_message(message)` | 处理群消息，解析记账指令 |
| `_parse_command(content)` | 正则解析"记账 100 工商银行 工资" |
| `_create_record(...)` | 创建记账记录入库 |
| `get_stats_by_group(group_id)` | 按群统计 |
| `get_stats_by_bank(group_id)` | 按银行统计 |
| `get_daily_report(group_id, date)` | 日报 |
| `get_weekly_report(group_id, start_date)` | 周报 |
| `get_monthly_report(group_id, year, month)` | 月报 |
| `sync_to_backend()` | 异步同步到后端API（jizhang_domain） |
| `retry_failed_sync()` | 重试失败的同步 |

**正则解析**：`r"记账\s+(-?\d+\.?\d*)\s+(\S+)\s*(.*)"`，支持负数金额、银行名称、可选备注。

### 8.2 自动回复 (`modules/auto_reply.py`)

规则引擎，支持三种匹配模式 + 时间段控制 + 随机延迟。

**AutoReplyRule ORM**：id, name, match_type, pattern, reply_type, reply_content, reply_path, enabled, time_start, time_end, min_delay, max_delay, priority, scope, hit_count。

| 匹配类型 | 说明 | 示例 |
|---------|------|------|
| `KEYWORD` | 关键词包含匹配 | pattern="你好" → 匹配包含"你好"的消息 |
| `REGEX` | 正则匹配 | pattern=r"^签到$" → 精确匹配"签到" |
| `EXACT` | 全匹配 | pattern="你好" → 仅匹配"你好" |

| 回复类型 | 说明 |
|---------|------|
| `TEXT` | 回复文本（reply_content） |
| `IMAGE` | 回复图片（reply_path） |
| `FILE` | 回复文件（reply_path） |

**核心方法**：

| 方法 | 功能 |
|------|------|
| `handle_message(message)` | 处理消息，匹配规则并回复 |
| `add_rule(rule)` | 添加规则 |
| `update_rule(id, updates)` | 更新规则 |
| `delete_rule(id)` | 删除规则 |
| `list_rules()` | 列出所有规则 |
| `_match(message, rule)` | 执行匹配（keyword/regex/exact） |
| `_check_time(rule)` | 检查当前时间是否在规则生效时间段内 |
| `_random_delay(rule)` | 生成随机延迟（min_delay ~ max_delay 之间） |

**匹配优先级**：priority 数值越大越先匹配；命中首条即回复，不再继续匹配。

### 8.3 群管理 (`modules/group_manager.py`)

综合群管理功能，包括欢迎、广告检测、定时公告。

**GroupConfig ORM**：group_wxid(PK), group_name, welcome_enabled, welcome_text, anti_ad_enabled, anti_ad_keywords, anti_ad_regex, announcement, announcement_cron, member_count。

**GroupEvent ORM**：id, group_wxid, event_type, target_wxid, content, msg_id, created_at。

| 方法 | 功能 |
|------|------|
| `handle_message(message)` | 处理群消息（检测入群/广告） |
| `_welcome_new_member(message)` | 监听系统入群消息，发送欢迎语 |
| `_detect_advertisement(message)` | 检测广告/违规内容 |
| `_revoke_message(msg_id)` | 调用 `REVOKE_MSG` 撤回消息 |
| `send_announcement(group_wxid, text)` | 发送群公告 |
| `send_at_all(group_wxid, text)` | @所有人 |
| `get_group_stats(group_wxid)` | 群统计（成员数/事件数/活跃度） |
| `_check_announcement_schedule()` | 检查定时公告是否到期 |

**违规检测策略**：
- 关键词匹配：`anti_ad_keywords` 逗号分隔（默认：加微,代购,免费领,点击链接,http://,https://）
- 正则匹配：`anti_ad_regex` 可选
- 命中后调用 `REVOKE_MSG` 撤回 + 发送提示

### 8.4 定时任务 (`modules/task_scheduler.py`)

自实现 cron 解析 + 间隔任务，支持持久化。

**ScheduledTask ORM**：id, name, task_type(cron/interval), schedule, command, params(JSON), enabled, last_run, next_run, run_count, error_count, last_error。

**Cron 解析**：5字段（分 时 日 月 周），支持：
- `*` — 任意值
- 具体值：`5`
- 逗号列表：`1,3,5`
- 范围：`1-5`
- 步长：`*/15` 或 `1-10/2`
- 周日：`7` 自动转换为 `0`

**间隔任务**：`schedule` 字段为 `"30s"` / `"5m"` / `"2h"` 或纯数字（秒）。

| 方法 | 功能 |
|------|------|
| `add_task(name, task_type, schedule, command, params)` | 添加任务 |
| `register_handler(command, handler)` | 注册命令处理函数 |
| `remove_task(id)` | 删除任务 |
| `list_tasks()` | 列出所有任务 |
| `get_task_status(id)` | 任务状态 |
| `start()` / `stop()` | 启动/停止调度器 |
| `_calc_next_run(schedule, task_type, now)` | 计算下次运行时间 |
| `_restore_tasks()` | 从数据库恢复任务（重启后） |
| `_dispatch_loop()` | 调度循环（检查到期任务并执行） |

**持久化与恢复**：任务保存到数据库，重启后自动恢复。过期任务（next_run < now）在恢复时立即执行或跳到下个周期。

---

## 9. HTTP API 层

### 9.1 FastAPI 服务器 (`api/server.py`)

`create_app(mock=False)` 创建并配置 FastAPI 应用。

**中间件与全局配置**：

| 配置 | 说明 |
|------|------|
| CORS | `allow_origins=["*"]`，全开放 |
| 异常处理 | 全局 `Exception` handler，返回 500 + 错误详情 |
| 静态文件 | 挂载 `/web` 到 `web/` 目录 |
| Swagger | `/docs` 自动API文档 |
| 生命周期 | `lifespan` 管理引擎启动/停止 |

**路由挂载**：

```python
app.include_router(message.router)    # /api/message/*
app.include_router(contact.router)    # /api/contact/*
app.include_router(group.router)      # /api/group/*
app.include_router(instance.router)   # /api/instance/*
```

### 9.2 API 端点完整清单

#### 系统端点

| 方法 | 路径 | 说明 | 请求体/参数 |
|------|------|------|------------|
| GET | `/api/health` | 健康检查 | — |
| GET | `/api/dashboard` | 仪表盘统计 | — |
| GET | `/` | 根路径信息 | — |
| GET | `/docs` | Swagger API文档 | — |
| GET | `/web/index.html` | Web管理界面 | — |

#### 实例管理 (`/api/instance`)

| 方法 | 路径 | 说明 | 请求体 |
|------|------|------|--------|
| GET | `/api/instance/list` | 实例列表 | — |
| POST | `/api/instance/create` | 创建实例 | `{instance_id, display_name, wxid, config}` |
| POST | `/api/instance/{id}/start` | 启动实例 | — |
| POST | `/api/instance/{id}/stop` | 停止实例 | — |
| GET | `/api/instance/{id}/status` | 实例状态 | — |
| PUT | `/api/instance/{id}/config` | 更新配置 | `{config: {...}}` |
| GET | `/api/instance/{id}/bookkeeping/records` | 记账记录 | `?limit=100` |

#### 消息 (`/api/message`)

| 方法 | 路径 | 说明 | 请求体 |
|------|------|------|--------|
| POST | `/api/message/send-text` | 发送文本 | `{instance_id, wxid, text}` |
| POST | `/api/message/send-image` | 发送图片 | `{instance_id, wxid, file_path, text}` |
| POST | `/api/message/send-file` | 发送文件 | `{instance_id, wxid, file_path, file_name}` |
| GET | `/api/message/history` | 消息历史 | `?instance_id=&wxid=&limit=50` |
| GET | `/api/message/received` | 最近消息 | `?limit=100&direction=in` |

#### 联系人 (`/api/contact`)

| 方法 | 路径 | 说明 | 请求体/参数 |
|------|------|------|------------|
| GET | `/api/contact/list` | 联系人列表 | `?instance_id=&limit=500` |
| GET | `/api/contact/search` | 搜索联系人 | `?instance_id=&keyword=&limit=50` |
| PUT | `/api/contact/remark` | 修改备注 | `{instance_id, wxid, remark}` |
| POST | `/api/contact/sync` | 同步联系人 | `{instance_id}` |

#### 群管理 (`/api/group`)

| 方法 | 路径 | 说明 | 请求体/参数 |
|------|------|------|------------|
| GET | `/api/group/list` | 群列表 | `?instance_id=` |
| GET | `/api/group/{group_wxid}/members` | 群成员 | — |
| POST | `/api/group/send-announcement` | 群公告 | `{instance_id, group_wxid, announcement}` |
| GET | `/api/group/{group_wxid}/stats` | 群统计 | `?instance_id=` |

### 9.3 依赖注入 (`api/deps.py`)

```python
def get_engine(request: Request) -> CoreEngine:
    """从 app.state 获取核心引擎"""
    return request.app.state.engine
```

所有路由通过 `Depends(get_engine)` 获取引擎实例，实现松耦合。

---

## 10. WebSocket 实时通信

### 10.1 WebSocketManager (`core/websocket_manager.py`)

管理 WebSocket 连接，支持按实例分组和全局广播。

**ClientConnection** 数据结构：
```python
@dataclass
class ClientConnection:
    ws: WebSocket
    instance_id: Optional[str]  # None = 全局连接
    connected_at: float
    is_alive: bool
```

| 方法 | 功能 |
|------|------|
| `connect(ws, instance_id)` | 接受连接并注册 |
| `disconnect(client)` | 移除连接 |
| `broadcast(instance_id, data)` | 广播到指定实例的所有连接 |
| `broadcast_all(data)` | 全局广播 |
| `total_connections()` | 当前总连接数 |

### 10.2 WebSocket 端点

| 路径 | 说明 |
|------|------|
| `WS /ws` | 全局WebSocket，推送所有实例消息 |
| `WS /ws/message/{instance_id}` | 实例特定WebSocket，只推送该实例消息 |

**心跳协议**：
- 客户端发送 `{"type": "ping"}`
- 服务端回复 `{"type": "pong"}`

**消息推送格式**：
```json
{
  "type": "message",
  "instance_id": "demo",
  "data": {
    "msg_id": "...",
    "sender_wxid": "...",
    "content": "...",
    "msg_type": "text",
    "is_received": true
  }
}
```

---

## 11. 安全模块

### 11.1 加密工具 (`security/crypto.py`)

`CryptoUtils` 类提供 AES 配置加密和 RSA 签名验证。

#### AES 配置加密

对应原软件 `config.ini` 中 `keyword` 字段的加密机制。

| 方法 | 功能 |
|------|------|
| `encrypt_config(data, secret)` | 加密配置（dict/str/bytes → base64字符串，IV+密文） |
| `decrypt_config(encrypted, secret)` | 解密配置（base64 → 原始数据） |
| `generate_key()` | 生成随机AES密钥 |
| `generate_license_id()` | 生成许可证ID（对应 run.vef） |

**加密参数**：
- 算法：AES-256-CBC
- 密钥派生：PBKDF2（salt=`robot3_replica_salt_v1`，迭代100,000次）
- IV：每次随机生成，附在密文前
- 输出：Base64编码

#### RSA 签名验证

| 方法 | 功能 |
|------|------|
| `generate_rsa_keypair()` | 生成RSA密钥对 |
| `sign_data(data, private_key)` | 用私钥签名 |
| `verify_signature(data, signature, public_key)` | 用公钥验签 |

### 11.2 许可证验证 (`security/license.py`)

`LicenseManager` 对应原软件 `run.vef` 许可证文件验证。

**验证流程**：

```
1. verify_file()      — 校验 run.vef 文件 RSA 签名
2. check_ip_whitelist() — IP 白名单检查
3. verify_online()    — 在线向后端API校验（可选）
4. verify_offline()   — 本地缓存校验（在线不可用时降级）
```

| 方法 | 功能 |
|------|------|
| `verify_file()` | 校验许可证文件签名 |
| `check_ip_whitelist(ip)` | IP白名单检查 |
| `verify_online()` | 在线验证（连接后端API） |
| `verify_offline()` | 离线验证（本地缓存） |
| `get_status()` | 获取验证状态 |
| `save_cache()` | 保存验证缓存到 `license_cache.json` |

### 11.3 IP 防火墙 (`security/firewall.py`)

`IPFirewall` 类实现 IP 黑/白名单管理，持久化到数据库。

| 方法 | 功能 |
|------|------|
| `add_black_ip(ip, note)` | 添加黑名单IP |
| `remove_black_ip(ip)` | 移除黑名单IP |
| `is_black_ip(ip)` | 检查是否在黑名单 |
| `add_white_ip(ip, note)` | 添加白名单IP |
| `is_white_ip(ip)` | 检查是否在白名单 |
| `check_access(ip)` | 综合检查（黑名单优先，白名单次之） |

**特性**：
- 支持 CIDR 格式（如 `192.168.1.0/24`）
- 内存缓存 + 数据库持久化（启动时加载）
- 使用 `ipaddress` 标准库进行CIDR匹配

---

## 12. 网络通信层

### 12.1 HTTP 客户端 (`network/http_client.py`)

`HttpClient` 基于 `httpx.AsyncClient`，对应原软件 `libcurl`。

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `base_url` | "" | 基础URL |
| `timeout` | 30.0 | 超时时间（秒） |
| `max_retries` | 3 | 最大重试次数 |
| `retry_backoff` | 0.5 | 重试退避基数（指数退避） |
| `retry_statuses` | (500,502,503,504) | 触发重试的状态码 |
| `proxy` | None | 代理地址 |
| `default_headers` | {} | 默认请求头 |
| `cookies` | None | Cookie持久化 |
| `verify` | True | SSL证书验证 |

| 方法 | 功能 |
|------|------|
| `get(url, params, headers)` | GET请求 |
| `post(url, json, data, headers)` | POST请求 |
| `put(url, json, headers)` | PUT请求 |
| `delete(url, headers)` | DELETE请求 |
| `close()` | 关闭客户端 |

**重试机制**：指数退避 + 随机抖动，`delay = retry_backoff * (2 ** attempt) + random(0, 0.1)`。

### 12.2 消息队列 (`network/message_queue.py`)

`MessageQueue` 基于 `asyncio.Queue`，对应原软件 AMQP（RabbitMQ）。

**特性**：
- 发布/订阅（topic模式）
- 多消费者（同一topic多个回调同时消费）
- 消息确认（ack/nack）
- 死信队列（nack超过最大重试次数的消息进入死信队列）
- 最近消息缓存

| 方法 | 功能 |
|------|------|
| `publish(topic, payload)` | 发布消息 |
| `subscribe(topic, callback)` | 订阅（返回consumer_tag） |
| `unsubscribe(consumer_tag)` | 取消订阅 |
| `ack(message_id)` | 确认消息 |
| `nack(message_id)` | 否认消息（触发重投或死信） |
| `get_dead_letters()` | 获取死信队列 |
| `start()` / `stop()` | 启动/停止 |

**Message** 数据结构：
```python
@dataclass
class Message:
    id: str           # UUID
    topic: str
    payload: Any
    created_at: float
    retry_count: int
    consumer_tag: str
```

### 12.3 自动更新 (`network/updater.py`)

`AutoUpdater` 从 GitHub/Gitee Release 获取更新，对应原软件 `AutoUpdater.exe`。

| 方法 | 功能 |
|------|------|
| `check_update()` | 检查更新（获取最新Release） |
| `compare_versions(v1, v2)` | 语义化版本比较 |
| `download_update(url, path)` | 下载更新包 |
| `get_changelog()` | 获取更新日志 |

**UpdateInfo** 包含：version, name, body(changelog), published_at, html_url, assets, download_url, download_size。

**版本比较**：支持 `v1.2.3` / `1.2.3` / `1.2` 等格式，按 major.minor.patch 逐段比较。

---

## 13. Web 管理界面

### 13.1 界面概述 (`web/index.html`)

单文件 SPA（666行），GitHub 暗色主题风格，原生 HTML + CSS + JavaScript 实现，无外部依赖。

### 13.2 功能页面

| 页面 | 功能 |
|------|------|
| **仪表盘** | 运行实例数、今日消息数、在线状态等统计卡片 |
| **实例管理** | 实例列表、启动/停止按钮、配置编辑 |
| **消息记录** | 实时消息流（WebSocket）、发送消息表单 |
| **联系人** | 搜索、列表、备注修改 |
| **群管理** | 群列表、群成员、群公告 |
| **记账** | 记录列表、统计图表 |
| **设置** | 全局配置、安全设置 |

### 13.3 技术特性

- **深色主题**：`#0d1117` 背景，`#58a6ff` 主色调（GitHub暗色风格）
- **响应式设计**：`@media (max-width: 768px)` 移动端适配
- **实时通信**：WebSocket连接 `/ws` 接收实时消息
- **API调用**：原生 `fetch()` 调用后端REST API
- **无框架依赖**：纯原生JavaScript，无Vue/React/Angular

---

## 14. 启动与部署

### 14.1 启动脚本 (`run.py`)

```bash
# Mock模式（开发测试，无需微信）
python run.py --mock

# 指定端口
python run.py --port 8080

# 生产模式（需要微信3.9.12.56）
python run.py

# 开发热重载
python run.py --reload

# 多工作进程
python run.py --workers 4

# 指定日志级别
python run.py --log-level DEBUG
```

### 14.2 命令行参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--host` | 0.0.0.0 | 监听地址 |
| `--port` | 3000 | 监听端口 |
| `--mock` | False | Mock模拟模式 |
| `--log-level` | INFO | 日志级别（DEBUG/INFO/WARNING/ERROR） |
| `--reload` | False | 开发模式热重载 |
| `--workers` | 1 | 工作进程数 |

### 14.3 启动流程

```
main()
  ├── parse_args()           # 解析命令行参数
  ├── setup_logging()        # 配置loguru（控制台+文件滚动）
  ├── settings.ensure_dirs() # 确保data/logs/db/web目录存在
  ├── _ensure_mock_instance()  # Mock模式下预创建demo实例
  └── run()
       ├── create_app(mock)  # 创建FastAPI应用
       ├── uvicorn.Config()  # 配置Uvicorn
       ├── setup_signal_handlers()  # 注册SIGINT/SIGTERM/SIGBREAK
       └── server.run()      # 启动服务
```

### 14.4 生命周期管理

**应用启动**（`lifespan`）：
1. `settings.ensure_dirs()` — 确保目录
2. `app.state.engine = engine` — 注入引擎到app.state
3. `app.state.ws_manager = ws_manager` — 注入WS管理器
4. `await engine.start()` — 启动核心引擎
   - 初始化数据库
   - 启动消息队列
   - 加载已注册实例
   - 启动健康检查循环
   - 启动调度循环

**应用关闭**：
1. `await engine.stop()` — 停止核心引擎
   - 停止所有实例
   - 停止消息队列
   - 关闭数据库连接
2. Uvicorn 退出

**信号处理**：
- `SIGINT` (Ctrl+C) / `SIGTERM` / `SIGBREAK` → 触发 `server.should_exit = True` → 优雅关闭

### 14.5 访问地址

| 地址 | 说明 |
|------|------|
| `http://localhost:3000/` | 根路径（返回应用信息） |
| `http://localhost:3000/web/index.html` | Web管理界面 |
| `http://localhost:3000/docs` | Swagger API文档 |
| `http://localhost:3000/api/health` | 健康检查 |
| `ws://localhost:3000/ws` | 全局WebSocket |
| `ws://localhost:3000/ws/message/{instance_id}` | 实例WebSocket |

---

## 15. 原软件映射表

| 原软件组件 | 复刻版组件 | 说明 |
|-----------|-----------|------|
| `机器人172wo.exe` | `run.py` + `api/server.py` | 主程序入口 + HTTP服务器 |
| `c6802.hx.dll` | `core/engine.py` + `modules/*` | 核心业务逻辑 |
| `weixin.dll` | `wechat/wechat_client.py` | 微信Hook模块 |
| `qq.dll` | （可扩展） | QQ Hook模块（预留接口） |
| `node.dll` (V8) | Python内置 `eval()` | 脚本引擎 |
| `ewe.dll` (易语言运行时) | Python运行时 | 开发语言运行时 |
| `sqlite3.dll` + `wxsqlite3` | `database/` (SQLAlchemy + aiosqlite) | 数据库 |
| `Libcurl.dll` | `network/http_client.py` (httpx) | HTTP客户端 |
| `libeay32.dll` (OpenSSL) | `pycryptodome` | 加密库 |
| AMQP Client (RabbitMQ) | `network/message_queue.py` | 消息队列 |
| `CacheProxy_*` (Memcached) | 内存缓存 (dict) | 缓存层 |
| Apache Solr | SQLite FTS / 内存过滤 | 搜索引擎 |
| `AutoUpdater.exe` | `network/updater.py` | 自动更新器 |
| `data/config.ini` | `config/settings.py` | 全局配置 |
| `data/app/c680X/config.ini` | `config/instance_config.py` | 实例配置 |
| `data/run.vef` | `security/license.py` | 许可证验证 |
| `data/data.db` | `database/models.py` (Contact表) | 联系人数据库 |
| `data/users.db` | `database/models.py` (Instance表) | 用户/实例数据库 |
| `data/appdata.db` | `database/models.py` (各业务表) | 应用数据 |
| `data/app/c680X/data.db` | `{instance_id}_data.db` | 实例独立数据库 |
| `一键禁止微信自动升级.bat` | `wechat/wechat_client.py` (版本检测) | 防微信升级 |
| API[0]~API[24] (25个槽位) | `wechat/hook_interface.py` APICommand | Hook函数指针表 |
| `e2ee_Cache*` 系列 | `network/message_queue.py` (ACK机制) | 端到端加密缓存 |
| `FirewareAddBlackIP` 等 | `security/firewall.py` | IP防火墙 |
| keyword字段AES加密 | `security/crypto.py` `encrypt_config()` | 配置加密 |
| `DB_Execute` / `DB_Query` 等 | `database/manager.py` CRUD方法 | 数据库操作 |
| `BeginTransation` / `CommitTransation` | SQLAlchemy Session事务 | 事务管理 |
| `CreateAMQPClient` / `CreateConsume` | `MessageQueue.publish()` / `subscribe()` | 消息队列 |
| jizhang模块 | `modules/bookkeeping.py` | 记账 |
| `CreateThread` / `BindThreadPool` | `core/thread_pool.py` | 线程池 |
| `AppendThread` | `ThreadPoolManager.submit_task()` | 任务提交 |
| `msg_split` status=1 | `MessagePipeline._split_message()` | 消息分片 |
| `sleep_time` sec=1 | `MessagePipeline` 发送间隔 | 发送限速 |
| `thread` post=50 | `thread_pool_size=50` | 线程数 |

---

## 附录：测试覆盖

| 测试模块 | 测试数 | 覆盖范围 |
|---------|-------|---------|
| test_database | 12 | ORM建表、CRUD、多实例隔离、种子数据 |
| test_message_pipeline | 13 | 入队、分片、限速、处理器注册、ACK |
| test_thread_pool | 12 | 任务提交、优先级、并发、状态 |
| test_security | 18 | AES/RSA、许可证、防火墙、CIDR |
| test_network | 19 | 消息队列、HTTP客户端、版本比较 |
| test_wechat | 21 | 消息类型、Mock客户端、Hook接口、API命令 |
| test_config | 10 | 默认配置、目录创建、INI读写、格式兼容 |
| test_api_health | 5 | 健康检查、仪表盘、文档、Web界面 |
| test_api_instance | 16 | 实例CRUD、启停、配置、记账、边界情况 |
| test_api_message | 14 | 文本/图片/文件发送、历史、超长文本 |
| test_api_contact | 10 | 列表、搜索、同步、备注 |
| test_api_group | 7 | 群列表、成员、统计、公告 |
| test_api_websocket | 8 | 连接、心跳、广播、实例特定 |
| test_bookkeeping | 13 | 指令解析、负数、统计、日报/周报/月报 |
| test_auto_reply | 15 | 关键词/正则/全匹配、时间段、延迟、CRUD |
| test_group_manager | 13 | 欢迎、广告检测、撤回、统计、公告 |
| test_task_scheduler | 16 | cron解析、间隔任务、持久化、恢复 |
| **总计** | **222** | **全部通过** |
