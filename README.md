# 机器人3 复刻版

基于对"机器人3.zip"（微信自动化机器人）的逆向分析，**完整复刻**的现代化 Python 实现。

## 项目规模

| 指标 | 数值 |
|------|------|
| Python 源文件 | 90 个 |
| 代码总行数 | 30,021 行 |
| 测试文件 | 27 个 |
| 测试数量 | 597 个（全部通过） |

## 技术栈

| 原软件 | 复刻版 | 说明 |
|--------|--------|------|
| 易语言 | Python 3.10+ | 主开发语言 |
| C++ DLL Hook (weixin.dll) | ctypes DLL注入 + 内存Hook | 微信Hook注入层 |
| C++ DLL Hook (qq.dll) | ctypes DLL注入（复用注入器） | QQ Hook |
| Node.js/V8 (node.dll) | PyMiniRacer/execJS/Python降级 | V8脚本引擎 |
| SQLite + wxsqlite3 (AES-256) | pysqlcipher3 + aiosqlite | 加密数据库 |
| AMQP (RabbitMQ) | aio-pika（降级内存队列） | 消息队列 |
| Memcached | aiomcache（降级内存字典） | 缓存层 |
| Apache Solr | aiohttp Solr API（降级内存搜索） | 全文搜索 |
| E2EE (e2eeE.com:8443) | TLS + AES-CBC + HMAC-SHA256 | 端到端加密 |
| AntiReplay | 时间窗口 + nonce LRU缓存 | 防重放攻击 |
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

# 生产模式 (需要微信3.9.12.56，仅Windows)
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
├── run.py                          # 主启动脚本
├── requirements.txt                # Python依赖
├── config/                         # 配置层
│   ├── settings.py                 # 全局配置 (对应原 config.ini)
│   ├── instance_config.py          # 实例配置 (对应原 app/c680X/config.ini)
│   └── gbk_config.py               # GBK编码INI解析器
├── core/                           # 核心引擎
│   ├── engine.py                   # 主调度引擎 (事件系统+健康检查)
│   ├── message_pipeline.py         # 消息管道 (分片+限速+ACK)
│   ├── instance_manager.py         # 多实例管理
│   ├── thread_pool.py              # 线程池 (asyncio + ThreadPoolExecutor双模式)
│   └── websocket_manager.py        # WebSocket连接管理
├── database/                       # 数据库层
│   ├── models.py                   # SQLAlchemy数据模型
│   ├── db_manager.py               # 异步数据库管理器
│   ├── encrypted_db.py             # AES-256加密数据库 (pysqlcipher3)
│   ├── manager.py                  # aiosqlite直接实现
│   └── migrations.py               # 数据库迁移+种子数据
├── wechat/                         # 微信接口层
│   ├── hook_interface.py           # Hook抽象接口 (API[0]~API[24])
│   ├── wechat_client.py            # 双模式客户端 (Mock + RealWeChatClient)
│   ├── dll_injector.py             # DLL注入器 (CreateRemoteThread+LoadLibrary)
│   ├── memory_hook.py              # 内存Hook (消息拦截+WM_COPYDATA回调)
│   ├── wechat_offsets.py           # 微信3.9.12.56偏移量表
│   ├── message_types.py            # 消息类型定义
│   └── contact_manager.py          # 联系人管理
├── qq/                             # QQ接口层
│   ├── qq_client.py                # QQ客户端 (Mock + RealDLL注入)
│   ├── qq_hook_interface.py        # QQ Hook接口定义
│   └── qq_offsets.py               # QQ NT 9.9.15偏移量表
├── modules/                        # 业务模块
│   ├── bookkeeping.py              # 记账模块 (keyword解密+银行白名单+后端同步)
│   ├── jizhang_config.py           # 记账多配置管理 (jizhang_c1/c12)
│   ├── message_splitter.py         # 消息分片器 (70行+1秒间隔)
│   ├── script_engine.py            # V8脚本引擎 (PyMiniRacer降级)
│   ├── auto_reply.py               # 自动回复
│   ├── group_manager.py            # 群管理
│   └── task_scheduler.py           # 定时任务 (cron)
├── infrastructure/                 # 基础设施层
│   ├── cache.py                    # Memcached缓存 (降级内存)
│   └── search.py                   # Solr全文搜索 (降级内存)
├── api/                            # HTTP API层
│   ├── server.py                   # FastAPI主服务器
│   ├── deps.py                     # 依赖注入
│   └── routes/                     # API路由
│       ├── message.py              # 消息API
│       ├── contact.py              # 联系人API
│       ├── group.py                # 群管理API
│       └── instance.py             # 实例管理API
├── security/                       # 安全模块
│   ├── crypto.py                   # AES/RSA/DES加密
│   ├── keyword_decoder.py          # keyword AES-ECB解密器
│   ├── license.py                  # 许可证验证 (对应原run.vef)
│   ├── firewall.py                 # IP防火墙
│   ├── e2ee.py                     # E2EE端到端加密 (e2eeE.com:8443)
│   └── anti_replay.py              # 防重放机制
├── network/                        # 网络通信层
│   ├── http_client.py              # HTTP客户端
│   ├── message_queue.py            # RabbitMQ消息队列 (降级内存队列)
│   ├── ack_manager.py              # AckMessage确认机制
│   └── updater.py                  # 自动更新
├── web/                            # Web管理界面
│   └── index.html                  # 单文件SPA
└── tests/                          # 测试 (597项)
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

### 微信Hook注入
- **DLL注入器**: CreateRemoteThread + LoadLibraryW 完整实现
- **内存Hook**: 消息接收函数拦截，WM_COPYDATA回调通知
- **API[0]~API[24]**: 25个微信内部API函数指针表
- **版本偏移量**: 微信3.9.12.56各函数内存偏移量表
- **双模式**: Mock模式(开发测试) + Real模式(真实注入)

### QQ Hook
- 复用DLL注入器，支持QQ NT 9.9.15
- Mock + Real双模式客户端
- 独立的QQ API命令枚举和偏移量表

### 记账模块 (jizhang)
- **keyword AES解密**: AES-ECB加密的配置数据解密
- **多配置并行**: jizhang_c1/c12多套独立配置
- **银行白名单**: 仅允许白名单渠道记账
- **后端同步**: HTTP/HTTPS实时+批量同步到后端服务器
- **GBK配置解析**: 解析GBK编码的INI配置文件
- **消息分片**: 70行分界，1秒间隔
- **AckMessage**: 消息发送确认，超时重试
- **统计报表**: 按群/银行/用户三维度，日报/周报/月报

### 基础设施
- **AES-256加密数据库**: pysqlcipher3，降级普通SQLite
- **RabbitMQ消息队列**: aio-pika，降级内存队列
- **Memcached缓存**: aiomcache，降级内存字典
- **Solr全文搜索**: aiohttp Solr API，降级内存搜索
- **线程池**: ThreadPoolExecutor 50线程（与原软件一致）

### 安全
- **E2EE端到端加密**: TLS + AES-CBC + HMAC-SHA256
- **防重放**: 时间窗口(300s) + nonce LRU缓存(10000条)
- **AES/RSA/DES加密**: 完整加密算法库
- **许可证验证**: 对应原run.vef
- **IP防火墙**: 黑白名单

### 脚本引擎
- **V8引擎**: PyMiniRacer优先，execJS次之，Python降级
- **消息处理**: JS脚本处理消息，提供上下文API
- **函数注册**: Python函数供JS调用

### 其他
- **多实例管理**: 每实例独立配置/数据库/线程池
- **自动回复**: 关键词/正则/全匹配，时间段控制
- **群管理**: 欢迎新成员、广告检测撤回、定时公告
- **定时任务**: cron解析，任务持久化
- **实时推送**: WebSocket消息推送
- **Web管理**: 深色主题SPA

## 对应原软件映射

| 原软件组件 | 复刻版组件 |
|-----------|-----------|
| 机器人172wo.exe | run.py + FastAPI |
| c6802.hx.dll | core/engine.py + modules/* |
| weixin.dll | wechat/dll_injector.py + memory_hook.py + wechat_client.py |
| qq.dll | qq/qq_client.py + qq_hook_interface.py |
| node.dll | modules/script_engine.py |
| sqlite3.dll + wxsqlite3 | database/encrypted_db.py + db_manager.py |
| Libcurl.dll | network/http_client.py |
| AMQP Client | network/message_queue.py (RabbitMQQueue) |
| CacheProxy | infrastructure/cache.py (MemcachedCache) |
| Apache Solr | infrastructure/search.py (SolrSearch) |
| E2EE (e2eeE.com:8443) | security/e2ee.py |
| AntiReplay | security/anti_replay.py |
| config.ini (GBK) | config/gbk_config.py + settings.py |
| app/c680X/config.ini | config/instance_config.py |
| app/jizhang_cX/ | modules/jizhang_config.py |
| keyword (AES) | security/keyword_decoder.py |
| run.vef | security/license.py |
| AutoUpdater.exe | network/updater.py |
| AckMessage | network/ack_manager.py |
| msg_split | modules/message_splitter.py |
| thread post=50 | core/thread_pool.py (RealThreadPool) |
| API[0]~API[24] | wechat/hook_interface.py (APICommand枚举) |
| data.db | database/models.py |

## 运行测试

```bash
# 全部测试
python -m pytest tests/ -q

# 带覆盖率
python -m pytest tests/ --cov=. --cov-report=term-missing

# 仅运行记账模块测试
python -m pytest tests/test_bookkeeping.py tests/test_jizhang_config.py -v
```

## 文档

- [ARCHITECTURE.md](ARCHITECTURE.md) — 完整架构与功能说明
- [BOOKKEEPING_RULES.md](BOOKKEEPING_RULES.md) — 记账模块规则整理
- [BOOKKEEPING_RULES.html](BOOKKEEPING_RULES.html) — 记账模块规则（HTML版）
