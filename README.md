# 机器人3 复刻版

基于对"机器人3.zip"（微信自动化机器人）的逆向分析，完整复刻的现代化实现。

## 技术栈

| 原软件 | 复刻版 | 说明 |
|--------|--------|------|
| 易语言 | Python 3.10+ | 主开发语言 |
| C++ DLL Hook | C++ DLL (ctypes接口) + 模拟模式 | 微信Hook |
| Node.js/V8 | 内嵌Python脚本 | 脚本引擎 |
| SQLite + wxsqlite3 | SQLite + SQLAlchemy | 数据库 |
| AMQP (RabbitMQ) | asyncio.Queue | 消息队列 |
| Memcached | 内存缓存 | 缓存层 |
| libcurl | httpx | HTTP客户端 |
| — | FastAPI + Uvicorn | HTTP API |
| — | 原生HTML/JS | Web管理界面 |

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# Mock模式启动 (无需微信)
python run.py --mock

# 指定端口
python run.py --mock --port 8080

# 生产模式 (需要微信3.9.12.56)
python run.py

# 开发热重载
python run.py --reload
```

启动后访问：
- Web管理界面: http://localhost:3000/web/index.html
- API文档: http://localhost:3000/docs
- 健康检查: http://localhost:3000/api/health

## 项目结构

```
robot3-replica/
├── run.py                     # 主启动脚本
├── requirements.txt           # Python依赖
├── config/                    # 配置层
│   ├── settings.py            # 全局配置 (对应原 config.ini)
│   └── instance_config.py     # 实例配置 (对应原 app/c680X/config.ini)
├── core/                      # 核心引擎
│   ├── engine.py              # 主调度引擎 (事件系统+健康检查)
│   ├── message_pipeline.py    # 消息管道 (分片+限速+ACK)
│   ├── instance_manager.py    # 多实例管理
│   ├── thread_pool.py         # 优先级线程池
│   └── websocket_manager.py   # WebSocket连接管理
├── database/                  # 数据库层
│   ├── models.py              # SQLAlchemy数据模型 (7张表)
│   ├── db_manager.py          # 异步数据库管理器
│   ├── manager.py             # aiosqlite直接实现
│   └── migrations.py          # 数据库迁移+种子数据
├── wechat/                    # 微信接口层
│   ├── hook_interface.py      # Hook抽象接口 (API[0]~API[24])
│   ├── wechat_client.py       # 双模式客户端 (Mock+真实Hook)
│   ├── message_types.py       # 消息类型定义
│   └── contact_manager.py     # 联系人管理
├── modules/                   # 业务模块
│   ├── bookkeeping.py         # 记账模块 (对应原jizhang)
│   ├── auto_reply.py          # 自动回复
│   ├── group_manager.py       # 群管理
│   └── task_scheduler.py      # 定时任务 (cron)
├── api/                       # HTTP API层
│   ├── server.py              # FastAPI主服务器
│   ├── deps.py                # 依赖注入
│   └── routes/                # API路由
│       ├── message.py         # 消息API
│       ├── contact.py         # 联系人API
│       ├── group.py           # 群管理API
│       └── instance.py        # 实例管理API
├── security/                  # 安全模块
│   ├── crypto.py              # AES加密 (对应原keyword加密)
│   ├── license.py             # 许可证验证 (对应原run.vef)
│   └── firewall.py            # IP防火墙
├── network/                   # 网络通信层
│   ├── http_client.py         # HTTP客户端 (对应原libcurl)
│   ├── message_queue.py       # 消息队列 (对应原AMQP)
│   └── updater.py             # 自动更新 (对应原AutoUpdater)
├── web/                       # Web管理界面
│   └── index.html             # 单文件SPA
└── tests/                     # 测试
```

## API端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/health | 健康检查 |
| GET | /api/dashboard | 仪表盘统计 |
| GET | /api/instance/list | 实例列表 |
| POST | /api/instance/create | 创建实例 |
| POST | /api/instance/{id}/start | 启动实例 |
| POST | /api/instance/{id}/stop | 停止实例 |
| GET | /api/instance/{id}/status | 实例状态 |
| PUT | /api/instance/{id}/config | 更新配置 |
| GET | /api/instance/{id}/bookkeeping/records | 记账记录 |
| POST | /api/message/send-text | 发送文本 |
| POST | /api/message/send-image | 发送图片 |
| POST | /api/message/send-file | 发送文件 |
| GET | /api/message/history | 消息历史 |
| GET | /api/contact/list | 联系人列表 |
| GET | /api/contact/search | 搜索联系人 |
| PUT | /api/contact/remark | 修改备注 |
| POST | /api/contact/sync | 同步联系人 |
| GET | /api/group/list | 群列表 |
| GET | /api/group/{id}/members | 群成员 |
| POST | /api/group/send-announcement | 群公告 |
| WS | /ws | 全局WebSocket |
| WS | /ws/message/{id} | 实例消息推送 |

## 核心特性

- **多实例管理**: 支持同时运行多个微信机器人实例，每个实例独立配置和数据库
- **消息管道**: 异步消息处理，支持长消息分片、发送限速、消息确认(ACK)
- **双模式**: Mock模式(无微信模拟) + 真实Hook模式(ctypes调用DLL)
- **记账模块**: 群消息解析记账，按群/银行/用户维度统计
- **自动回复**: 关键词/正则/全匹配规则引擎，时间段控制，随机延迟
- **群管理**: 欢迎新成员、广告检测撤回、定时群公告
- **定时任务**: 自实现cron解析，任务持久化，重启恢复
- **安全体系**: AES加密、许可证验证、IP黑白名单
- **实时推送**: WebSocket消息实时推送
- **Web管理**: 深色主题SPA管理界面

## 对应原软件映射

| 原软件组件 | 复刻版组件 |
|-----------|-----------|
| 机器人172wo.exe | run.py + FastAPI |
| c6802.hx.dll | core/engine.py + modules/* |
| weixin.dll | wechat/wechat_client.py |
| qq.dll | (可扩展) |
| node.dll | Python内置 |
| sqlite3.dll + wxsqlite3 | SQLAlchemy + aiosqlite |
| Libcurl.dll | httpx |
| AMQP Client | network/message_queue.py |
| CacheProxy | 内存缓存 |
| config.ini | config/settings.py |
| app/c680X/config.ini | config/instance_config.py |
| run.vef | security/license.py |
| AutoUpdater.exe | network/updater.py |
| data.db | database/models.py |
