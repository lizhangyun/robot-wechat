# 机器人3 — 记账模块（jizhang）完整规则整理

> 本文档基于对原软件"机器人3"（内部代号 c6802，产品名"机器人172wo"）的逆向分析，
> 结合其配置文件、数据库结构、DLL 字符串提取，以及复刻版实现，完整梳理记账模块的所有规则。

---

## 目录

1. [配置规则](#1-配置规则)
2. [数据库存储规则](#2-数据库存储规则)
3. [消息触发规则](#3-消息触发规则)
4. [指令解析规则](#4-指令解析规则)
5. [记录创建规则](#5-记录创建规则)
6. [统计规则](#6-统计规则)
7. [报表规则](#7-报表规则)
8. [后端同步规则](#8-后端同步规则)
9. [多实例规则](#9-多实例规则)
10. [安全加密规则](#10-安全加密规则)
11. [数据隔离规则](#11-数据隔离规则)
12. [消息确认与去重规则](#12-消息确认与去重规则)
13. [原软件 vs 复刻版对照表](#13-原软件-vs-复刻版对照表)

---

## 1. 配置规则

### 1.1 配置文件位置

记账模块配置存储在每个实例的 `config.ini` 文件中，位于 `data/app/{instance_id}/config.ini`。

原软件中存在两个实例配置：

| 实例 | 配置路径 | 后端域名 |
|------|---------|---------|
| c6801 | `data/app/c6801/config.ini` | `http://jacn1.huoxing111.com/6802cishi/` |
| c6802 | `data/app/c6802/config.ini` | `https://jizhang105.tztz.eu.org/6802cishi/` |

### 1.2 [jizhang] 配置段

```ini
[jizhang]
keyword=3A2F5B...（AES加密的十六进制字符串）
domain=http://jacn1.huoxing111.com/6802cishi/
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `keyword` | AES加密的HEX字符串 | 记账功能配置数据，包含触发关键词、银行名称映射等，运行时AES解密后使用 |
| `domain` | URL字符串 | 记账后端API服务器地址，每条记账记录同步到此后端 |
| `enabled` | 布尔值(0/1) | 是否启用记账模块（复刻版新增，原软件通过 keyword 是否为空隐式判断） |

### 1.3 独立记账配置目录

原软件中还存在两个独立的记账模块配置目录，对应不同的记账配置实例：

```
data/app/jizhang_c1/    # 记账模块配置1
data/app/jizhang_c12/   # 记账模块配置12
```

这两个目录的存在表明记账模块支持**多套独立配置并行运行**，`jizhang_c1` 和 `jizhang_c12` 可能对应不同的记账场景或不同的群组集合。

### 1.4 keyword 字段加密机制

- **加密算法**: AES（对称加密）
- **存储格式**: 十六进制字符串（如 `3A2F5B...`）
- **密钥用途**: 解密后可能包含：
  - 记账触发关键词列表
  - 银行/渠道名称白名单
  - 功能开关配置
  - 数据库加密密钥（传给 wxsqlite3 的 `sqlite3_key()` 函数）
- **不同实例的 keyword 不同**: c6801 的 keyword 较短，c6802 的 keyword 更长，表明配置内容可能有差异

---

## 2. 数据库存储规则

### 2.1 数据库引擎

- **引擎**: SQLite + wxsqlite3
- **加密**: AES-256 加密（通过 wxsqlite3 扩展）
- **加密函数链**: `wxsqlite3_config` → `wxsqlite3_config_cipher` (配置为 AES-256) → `sqlite3_key()`
- **密钥来源**: `config.ini` 中 `[jizhang]` 段的 `keyword` 字段，运行时 AES 解密后传入

### 2.2 数据库文件分布

| 数据库文件 | 加密 | 用途 |
|-----------|------|------|
| `data/data.db` | 明文 | 联系人/群聊列表（表名前缀 `qq77193535_`） |
| `data/users.db` | AES加密 | 用户账户信息 |
| `data/appdata.db` | AES加密 | 应用配置数据 |
| `data/app/c6801/data.db` | AES加密 | 实例1业务数据（含记账记录） |
| `data/app/c6802/data.db` | AES加密 | 实例2业务数据（含记账记录） |

### 2.3 记账数据表字段

从 DLL 中提取到的 SQL 字段：

| 字段名 | 类型 | 说明 |
|--------|------|------|
| `bank_name` | TEXT | 银行/渠道名称（如"工商银行"、"微信"、"支付宝"） |
| `group_id` | INTEGER/TEXT | 群组标识，用于群维度数据隔离 |
| `status` | TEXT/INTEGER | 记录状态（pending/confirmed/rejected） |

复刻版扩展的完整字段：

| 字段名 | 类型 | 说明 |
|--------|------|------|
| `id` | INTEGER PK | 自增主键 |
| `instance_id` | TEXT | 实例ID（如 c6801） |
| `group_wxid` | TEXT | 群wxid（如 `12345678@chatroom`） |
| `group_name` | TEXT | 群名称 |
| `sender_wxid` | TEXT | 记账人wxid |
| `sender_name` | TEXT | 记账人昵称 |
| `amount` | REAL | 金额（可为负数） |
| `bank_name` | TEXT | 银行/渠道名称 |
| `remark` / `description` | TEXT | 备注说明 |
| `raw_msg` | TEXT | 原始消息内容 |
| `msg_id` | TEXT | 消息ID（用于去重） |
| `status` | TEXT | 记录状态（pending/confirmed/rejected） |
| `sync_status` | INTEGER | 后端同步状态（0=未同步, 1=已同步, 2=同步失败） |
| `created_at` | DATETIME | 记账时间 |

### 2.4 数据库操作 API

原软件 `c6802.hx.dll` 封装了完整的数据库操作层：

| 函数 | 功能 | 记账场景用途 |
|------|------|------------|
| `DB_Execute` | 执行SQL（INSERT/UPDATE/DELETE） | 插入/更新记账记录 |
| `DB_Query` | 查询返回结果集 | 查询记账记录列表 |
| `DB_QueryFirst` | 查询首行 | 查询单条记账记录 |
| `DB_QueryPages` | 分页查询 | 分页获取记账历史 |
| `DB_QueryToUserData` | 查询映射到对象 | 记账记录结构化读取 |
| `DB_SaveObject` | 保存对象到数据库 | 保存记账记录对象 |
| `DB_GetConn` | 从连接池获取连接 | 多线程并发记账 |
| `DB_GetError` | 获取错误信息 | 记账失败诊断 |
| `BeginTransation` | 开启事务 | 批量记账原子操作 |
| `CommitTransation` | 提交事务 | 批量记账确认 |
| `BindDatabasePool` | 绑定连接池 | 多实例数据库隔离 |
| `BeginQueryCache` | 开启查询缓存 | 统计查询加速 |
| `DeleteQueryCache` | 删除查询缓存 | 数据更新后清缓存 |

### 2.5 索引规则

复刻版中定义的索引（对应原软件的查询优化）：

```sql
-- 按实例+群组合索引（群维度查询加速）
CREATE INDEX ix_bookkeeping_instance_group ON bookkeeping_records(instance_id, group_wxid);

-- 按用户索引（用户维度统计加速）
CREATE INDEX ix_bookkeeping_user ON bookkeeping_records(sender_wxid);

-- 按时间索引（报表时间范围查询加速）
CREATE INDEX ix_bookkeeping_created ON bookkeeping_records(created_at);

-- 按银行索引（银行维度统计加速）
CREATE INDEX ix_bookkeeping_bank ON bookkeeping_records(bank_name);
```

---

## 3. 消息触发规则

### 3.1 消息类型过滤

| 规则 | 说明 |
|------|------|
| 仅处理文本消息 | `msg_type == TEXT`，忽略图片/文件/语音/视频/系统消息 |
| 仅处理群消息 | `is_group == True` 且 `group_wxid` 不为空，私聊消息不触发记账 |
| 必须以触发关键词开头 | 消息正文（去除群前缀后）必须以配置的关键词起始 |

### 3.2 群消息前缀处理

微信群消息格式为 `发送者wxid:\n消息正文`，记账模块需要：

1. 提取实际发送者 wxid（从消息前缀）
2. 提取消息正文（去除 `wxid:\n` 前缀后的内容）
3. 对正文进行记账指令解析

### 3.3 消息去重规则

| 规则 | 说明 |
|------|------|
| 去重依据 | `msg_id`（微信消息唯一标识） |
| 缓存上限 | 5000 条已处理消息ID |
| 溢出策略 | 超过上限时保留最近一半（2500条），清理旧记录 |
| 重复处理 | 同一 `msg_id` 的消息第二次处理时直接返回 None，不重复记账 |

---

## 4. 指令解析规则

### 4.1 指令格式

```
{触发关键词} {金额} {银行/渠道名称} [备注]
```

**默认触发关键词**: `记账`

### 4.2 正则解析规则

```
正则: ^\s*(?P<keyword>[^\s]+)\s+(?P<amount>-?\d+(?:\.\d+)?)\s+(?P<bank>\S+)\s*(?P<remark>.*)$
```

| 分组 | 规则 | 示例 |
|------|------|------|
| `keyword` | 非空白字符序列，必须匹配配置的触发关键词 | `记账` |
| `amount` | 可选负号 + 数字 + 可选小数部分 | `100`, `-50`, `88.5` |
| `bank` | 非空白字符序列（不含空格） | `工商银行`, `微信`, `支付宝` |
| `remark` | 剩余任意字符（可含空格，可为空） | `工资`, `退款`, `午餐` |

### 4.3 解析有效性判定

以下情况判定为**无效指令**，不触发记账：

| 情况 | 示例 | 结果 |
|------|------|------|
| 不以触发关键词开头 | `今天天气不错` | 忽略 |
| 缺少银行字段 | `记账 100` | 忽略 |
| 金额非数字 | `记账 abc 工商银行 备注` | 忽略 |
| 空字符串 | `` | 忽略 |
| 关键词后无内容 | `记账` | 忽略 |

### 4.4 金额规则

| 规则 | 说明 |
|------|------|
| 正数 | 表示收入/入账（如 `记账 100 工商银行 工资`） |
| 负数 | 表示支出/退款/扣减（如 `记账 -50 微信 退款`） |
| 支持小数 | 如 `记账 88.5 支付宝 测试记录` |
| 零值 | 技术上允许，但业务上可能无意义 |

### 4.5 银行/渠道名称规则

- 银行名称为**不含空格的连续字符串**
- 常见渠道名称（从测试数据和配置推断）：工商银行、建设银行、微信、支付宝
- 银行名称用于按渠道维度分组统计
- 原 software 的 `keyword` 加密配置中可能包含**银行名称白名单**（仅允许特定渠道名称）

---

## 5. 记录创建规则

### 5.1 创建流程

```
群消息到达
  → 消息类型检查（仅 TEXT）
  → 群消息检查（仅 is_group=True）
  → msg_id 去重检查
  → 提取消息正文（去群前缀）
  → 正则解析记账指令
  → 解析失败 → 返回 None（不处理）
  → 解析成功 → 创建 BookkeepingRecord
  → 写入数据库（事务提交）
  → 标记 msg_id 为已处理
  → 发送确认消息到群
  → 异步同步到后端API
```

### 5.2 记录字段填充规则

| 字段 | 填充来源 |
|------|---------|
| `instance_id` | 当前实例配置 |
| `group_wxid` | 消息的 `group_wxid` |
| `group_name` | 群名缓存中查找，无则为空 |
| `sender_wxid` | 消息的 `sender_wxid` |
| `sender_name` | 消息的 `actual_sender_in_group`，无则用 wxid |
| `amount` | 正则解析的 `amount` 转为 float |
| `bank_name` | 正则解析的 `bank` |
| `remark` | 正则解析的 `remark` |
| `raw_msg` | 原始消息正文 |
| `msg_id` | 消息的 `msg_id` |
| `sync_status` | 初始为 0（未同步） |
| `created_at` | 当前 UTC 时间 |

### 5.3 确认消息规则

记账成功后，自动向群内发送确认消息，格式：

```
记账成功 ✓
金额: {amount}
渠道: {bank_name}
备注: {remark 或 "无"}
```

### 5.4 记录状态流转

```
pending（待确认）
    ↓
confirmed（已确认）—— 默认状态，记账即确认
    ↓
rejected（已拒绝）—— 手动驳回（原软件可能支持管理员操作）
```

---

## 6. 统计规则

### 6.1 统计维度

支持三种维度的统计聚合：

| 维度 | 分组键 | 用途 |
|------|--------|------|
| `group` | `group_wxid` | 按群统计各群的记账总额 |
| `bank` | `bank_name` | 按银行/渠道统计各渠道流水 |
| `user` | `sender_wxid` | 按记账人统计个人贡献 |

### 6.2 统计指标

每个维度的统计结果包含以下指标：

| 指标 | 计算方式 |
|------|---------|
| `count` | 记录笔数（COUNT） |
| `total_amount` | 总净额 = 所有记录金额之和（SUM） |
| `income` | 总收入 = 所有正数金额之和（SUM WHERE amount > 0） |
| `expense` | 总支出 = 所有负数金额之和（SUM WHERE amount < 0，负值） |

### 6.3 统计查询 SQL 逻辑

```sql
SELECT
    {dimension_column} AS key,
    COUNT(*)           AS count,
    SUM(amount)        AS total_amount,
    SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) AS income,
    SUM(CASE WHEN amount < 0 THEN amount ELSE 0 END) AS expense
FROM bookkeeping_records
WHERE {可选时间范围条件}
GROUP BY {dimension_column}
```

### 6.4 时间范围筛选

统计支持可选的时间范围筛选：

| 参数 | 说明 |
|------|------|
| `start` | 起始时间（`created_at >= start`） |
| `end` | 结束时间（`created_at <= end`） |
| 无参数 | 统计全部记录 |

---

## 7. 报表规则

### 7.1 报表类型

| 类型 | 时间范围 | 标题 |
|------|---------|------|
| 日报 (daily) | 最近 24 小时 | `【记账日报】` |
| 周报 (weekly) | 最近 7 天 | `【记账周报】` |
| 月报 (monthly) | 最近 30 天 | `【记账月报】` |

### 7.2 报表内容格式

```
【记账{日报|周报|月报}】
统计区间: {start.strftime('%m-%d %H:%M')} ~ {end.strftime('%m-%d %H:%M')}
记录笔数: {count}
总收入: {income:.2f}
总支出: {abs(expense):.2f}
净额: {total:.2f}

按渠道:
  {bank_name}: {amount:.2f}
  ...（按金额绝对值降序排列）

按人员:
  {user_name}: {amount:.2f}
  ...（按金额绝对值降序排列）
```

### 7.3 报表排序规则

- **按渠道**：按金额绝对值降序（`sorted(by_bank.items(), key=lambda x: -abs(x[1]))`）
- **按人员**：按金额绝对值降序（`sorted(by_user.items(), key=lambda x: -abs(x[1]))`）

### 7.4 空数据规则

当统计区间内无记账记录时，报表返回：

```
【记账{日报|周报|月报}】
区间内无记账记录。
```

### 7.5 群维度报表

报表支持按群筛选（`group_wxid` 参数），仅统计指定群内的记账记录；不传则统计全部群。

---

## 8. 后端同步规则

### 8.1 同步架构

```
本地记账记录 → HTTP POST → 后端API（domain配置）→ 后端服务器存储/统计
```

### 8.2 后端域名配置

| 实例 | 后端域名 | 协议 |
|------|---------|------|
| c6801 | `http://jacn1.huoxing111.com/6802cishi/` | HTTP |
| c6802 | `https://jizhang105.tztz.eu.org/6802cishi/` | HTTPS |

- URL 路径 `/6802cishi/` 为固定后缀，`cishi` 可能意为"次世"或版本标识
- c6801 使用 HTTP（不安全），c6802 使用 HTTPS（加密传输）
- 通信库: libcurl + OpenSSL (libeay32.dll)

### 8.3 同步端点

```
POST {domain}/api/jizhang/record
Content-Type: application/json
Body: {记账记录JSON}
```

### 8.4 同步触发时机

| 时机 | 说明 |
|------|------|
| 实时同步 | 每条记账记录创建后立即异步同步（`asyncio.create_task`，不阻塞主流程） |
| 批量同步 | 手动触发 `sync_unsynced()`，批量同步所有未成功记录（单次最多 500 条） |

### 8.5 同步状态管理

| 状态码 | 含义 | 说明 |
|--------|------|------|
| 0 | 未同步 | 初始状态，新记录默认 |
| 1 | 已同步 | 后端返回 HTTP 200 |
| 2 | 同步失败 | 后端返回非200 或网络异常 |

### 8.6 同步重试规则

- 同步失败的记录（`sync_status=2`）可通过批量同步重新尝试
- 批量同步查询条件: `WHERE sync_status != 1`（包含 0=未同步 和 2=失败）
- 单次批量最多 500 条
- 无自动重试定时器（需手动触发或外部调度）

### 8.7 同步降级规则

| 条件 | 行为 |
|------|------|
| `domain` 为空 | 跳过同步，返回 False |
| `httpx` 未安装 | 跳过同步，记录警告日志 |
| 网络超时 | 10 秒超时，标记为同步失败（status=2） |
| 后端返回非200 | 标记为同步失败（status=2） |

---

## 9. 多实例规则

### 9.1 实例隔离

| 维度 | 隔离方式 |
|------|---------|
| 配置文件 | 每实例独立 `data/app/{instance_id}/config.ini` |
| 数据库 | 每实例独立 `data/app/{instance_id}/data.db`（AES加密） |
| 后端服务 | 每实例独立 `domain` 配置，连接不同后端服务器 |
| 线程池 | 每实例 50 个工作线程（`thread post=50`） |

### 9.2 记账模块多配置

原软件中存在两套独立的记账配置目录：

| 目录 | 可能用途 |
|------|---------|
| `data/app/jizhang_c1/` | 记账配置实例1（可能对应 c6801 的记账规则） |
| `data/app/jizhang_c12/` | 记账配置实例12（可能对应另一个记账场景或群组集合） |

这表明记账模块支持：
- 不同实例使用不同的记账规则
- 同一实例可能绑定多套记账配置
- `c1` 和 `c12` 的编号差异暗示存在 c2~c11 等更多配置的可能性

### 9.3 线程模型

| 配置项 | 值 | 说明 |
|--------|-----|------|
| `thread post` | 50 | 每实例 50 个发送线程 |
| 线程管理函数 | `AppendThread` / `CreateThread` / `BindThreadPool` | 线程池创建和管理 |
| 消息队列 | AMQP (RabbitMQ) | 异步解耦：消息接收 → 队列 → 工作线程处理 → 队列 → 发送 |

### 9.4 消息分片配置

| 配置项 | 值 | 说明 |
|--------|-----|------|
| `msg_split status` | 1 | 启用消息分片 |
| `msg 消息最多行数` | 70 | 单条消息最多70行，超出自动分片发送 |
| `sleep_time sec` | 1 | 消息发送间隔1秒 |

记账确认消息和报表消息如超过70行，会自动分片发送。

---

## 10. 安全加密规则

### 10.1 配置加密

| 对象 | 加密算法 | 密钥管理 |
|------|---------|---------|
| `keyword` 字段 | AES | 十六进制存储，运行时解密 |
| `config.ini` 文件 | 无（明文GBK编码） | 仅 keyword 字段内容加密 |

### 10.2 数据库加密

| 对象 | 加密算法 | 密钥来源 |
|------|---------|---------|
| `users.db` | AES-256 (wxsqlite3) | `keyword` 解密后传入 `sqlite3_key()` |
| `appdata.db` | AES-256 (wxsqlite3) | 同上 |
| `app/c680X/data.db` | AES-256 (wxsqlite3) | 同上 |
| `data.db` | 无（明文） | 不加密 |

### 10.3 通信加密

| 通信链路 | 加密方式 |
|---------|---------|
| c6801 → 后端 | HTTP（明文，不安全） |
| c6802 → 后端 | HTTPS（libcurl + OpenSSL） |
| E2EE 服务 | e2eeE.com:8443 端到端加密 |
| 防重放 | AntiReplay 机制 |

### 10.4 完整加密算法库

原软件内置的加密算法（可能用于记账数据保护）：

| 算法 | 用途 |
|------|------|
| AES | 数据库加密、配置加密 |
| RSA | 非对称加密（可能用于许可证验证） |
| DES / 3DES | 对称加密 |
| MD5 / SHA1 / SHA256 / SHA512 | 哈希校验 |
| HMAC | 消息认证码 |
| Base64 | 编码转换 |

---

## 11. 数据隔离规则

### 11.1 群维度隔离

记账数据通过 `group_id` / `group_wxid` 实现群维度隔离：

```sql
-- 查询特定群的记账记录
SELECT * FROM bookkeeping WHERE group_id = {group_id}

-- 按群统计
SELECT group_id, SUM(amount), COUNT(*) FROM bookkeeping GROUP BY group_id
```

### 11.2 实例维度隔离

每个实例的记账数据存储在独立的加密数据库中，实例之间互不可见：

```
c6801 的记账记录 → data/app/c6801/data.db（AES加密）
c6802 的记账记录 → data/app/c6802/data.db（AES加密）
```

### 11.3 后端数据隔离

每个实例连接独立的后端服务器，同步的记账数据在后端也是隔离的：

```
c6801 → http://jacn1.huoxing111.com/6802cishi/   （后端服务器A）
c6802 → https://jizhang105.tztz.eu.org/6802cishi/ （后端服务器B）
```

---

## 12. 消息确认与去重规则

### 12.1 AckMessage 确认机制

原软件使用 `AckMessage` 机制确保消息可靠传输：
- 消息发送后等待 ACK 确认
- 未收到 ACK 的消息会重试发送
- 记账确认消息同样受此机制保护

### 12.2 去重缓存管理

| 规则 | 说明 |
|------|------|
| 缓存数据结构 | `set[str]`（msg_id 集合） |
| 最大缓存数量 | 5000 条 |
| 溢出清理策略 | 超过5000条时，保留最近2500条，丢弃旧的2500条 |
| 清理时机 | 每次新增已处理 msg_id 后检查 |

### 12.3 群名缓存

| 规则 | 说明 |
|------|------|
| 缓存数据结构 | `dict[str, str]`（group_wxid → group_name） |
| 更新方式 | 外部调用 `set_group_name()` 手动更新 |
| 用途 | 记账记录中填充 `group_name` 字段 |
| 缓存未命中 | `group_name` 填充为空字符串 |

---

## 13. 原软件 vs 复刻版对照表

| 规则维度 | 原软件（易语言） | 复刻版（Python） |
|---------|----------------|-----------------|
| 开发语言 | 易语言 (E-Language) | Python 3.10+ |
| 数据库 | SQLite + wxsqlite3 (AES-256) | SQLite + aiosqlite (无加密) |
| 配置格式 | INI (GBK编码) | INI (GBK兼容) + Pydantic模型 |
| 触发关键词 | keyword 字段 AES 加密存储 | 明文配置，默认 "记账" |
| 后端同步 | libcurl + OpenSSL | httpx (asyncio) |
| 消息队列 | AMQP (RabbitMQ) | asyncio.Task (协程) |
| 线程模型 | 50个工作线程 | asyncio 事件循环 |
| 指令格式 | `记账 金额 银行 备注` | 同左（正则一致） |
| 统计维度 | bank_name, group_id, status | group, bank, user |
| 报表类型 | 未确认（可能有日报） | 日报/周报/月报 |
| 数据加密 | AES-256 全链路加密 | 无加密（明文SQLite） |
| 多实例 | c6801, c6802 独立运行 | 支持多实例，配置隔离 |
| 记账配置目录 | jizhang_c1, jizhang_c12 | 无独立目录，配置统一管理 |
| 确认消息 | AckMessage 机制 | 直接发送文本 |
| 去重机制 | 不明确 | msg_id 集合（5000条上限） |
| API暴露 | 无（内部DLL调用） | REST API + WebSocket |

---

## 附录：完整记账流程时序

```
用户在群内发送消息
    │
    ▼
微信客户端接收消息
    │
    ▼
weixin.dll Hook 拦截消息
    │
    ▼
c6802.hx.dll 消息管线处理
    │
    ├── 消息类型检查 (TEXT?)
    ├── 群消息检查 (is_group?)
    ├── msg_id 去重检查
    ├── 提取消息正文 (去群前缀)
    │
    ▼
记账指令解析 (正则匹配)
    │
    ├── 解析失败 → 转交其他模块处理
    │
    ▼ 解析成功
创建 BookkeepingRecord
    │
    ├── 填充字段 (instance_id, group_wxid, amount, bank_name, ...)
    ├── sync_status = 0 (未同步)
    │
    ▼
写入数据库 (BeginTransation → INSERT → CommitTransation)
    │
    ▼
标记 msg_id 为已处理
    │
    ├── 发送确认消息到群 (msg_split 分片 + sleep_time 限速)
    │
    ▼
异步同步到后端 (asyncio.create_task)
    │
    ├── POST {domain}/api/jizhang/record
    ├── 成功 → sync_status = 1
    ├── 失败 → sync_status = 2
    │
    ▼
流程结束
```

---

*文档生成时间: 2026-07-15*
*基于: 机器人3 逆向分析报告 + robot3-replica 复刻版实现*
