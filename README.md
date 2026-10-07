# 园区应急广播播发裁决服务

值守员通过 HTTP API 提交**发布** / **撤销**命令，服务按受理时间计算告警生效区间，
并对任意时刻、任意区域裁决出唯一应播发的告警。

- 技术栈：Python 3.12 · FastAPI · PostgreSQL 16 · Docker / Compose
- 服务地址：`http://localhost:8000`（交互式文档 `http://localhost:8000/docs`）

## 快速启动

```bash
docker compose up --build
```

一条命令即可启动：Compose 先拉起 PostgreSQL 并等待健康检查通过，应用启动时自动
等待数据库就绪并执行建表脚本（幂等，可重复启动）。停止并清空数据：

```bash
docker compose down -v
```

## 配置

| 环境变量       | 默认值                                                       | 说明           |
| -------------- | ------------------------------------------------------------ | -------------- |
| `DATABASE_URL` | `postgresql://broadcast:broadcast@localhost:5432/broadcast`  | 数据库连接串   |

Compose 中已注入 `DATABASE_URL=postgresql://broadcast:broadcast@db:5432/broadcast`。
端口映射：应用 `8000:8000`，数据库 `5432:5432`（均可在 `docker-compose.yml` 调整）。

本地开发（不用 Docker）：

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://broadcast:broadcast@localhost:5432/broadcast
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 接口一览

| 方法 | 路径                    | 说明                                             |
| ---- | ----------------------- | ------------------------------------------------ |
| POST | `/api/commands/publish` | 提交发布命令，返回命令回执（201 首次 / 200 重放） |
| POST | `/api/commands/revise`  | 提交修订命令，整体替换进行中告警的文案/级别/区域（201 首次 / 200 重放） |
| POST | `/api/commands/revoke`  | 提交撤销命令，返回命令回执（201 首次 / 200 重放） |
| GET  | `/api/broadcast`        | 查询指定时刻各区域应播发的告警（`at`、`area` 可选）|
| GET  | `/healthz`              | 健康检查                                          |

### 裁决与语义约定

- **生效区间**：`[受理时间 + 延迟秒数, +持续秒数)`，左闭右开；撤销时刻之后的时刻不再播发，
  之前的历史时刻仍可查询。
- **裁决顺序**：同一区域多条同时有效时，按 **级别高 → 受理时间早 → 告警号小** 取一条；
  高级别到期或被撤销后，仍有效的低级别自动恢复。
- **修订（revise）**：值守员发现进行中告警的文案、级别或覆盖区域有误时，可用独立请求号
  提交修订，输入**告警号及完整的新文案、级别、区域列表**（不是增量补丁）。
  - 仅 **已生效（受理时刻 ≥ 生效起点）、未到期（受理时刻 < 生效终点）、未撤销** 的告警
    可修订；告警不存在返回 `404`，未生效 / 已到期 / 已撤销返回 `409`。
  - 修订**从受理时刻起整体替换播发版本**：不改变原生效终点，也不改变修订前的历史；
    可对同一告警连续修订，每次形成一个新版本。按 `at` 查询时，**区域和级别都取当时版本**。
  - 空白文案、空或重复区域、越界级别（非 1~5）沿用发布校验，返回 `422`。
  - 同告警的修订与撤销**按成功提交顺序生效**：撤销先成功则之后的修订失败（409）；
    修订先成功后撤销，修订期间播发的是新版本，该段历史仍可按历史时刻查到。
- **幂等**：`request_no` 为幂等键。同请求号同载荷（并发或串行重试）返回首次回执；
  同请求号不同载荷返回 `409`。
- **事务**：命令回执、告警、区域在同一事务写入，同成同败；校验失败（422）与冲突
  （409/404）不留下任何部分状态，失败命令不占用请求号。
- **告警号**：正整数，全局不可复用（含已撤销、已过期），复用返回 `409`。

## 调用示例

### 1. 发布告警

```bash
curl -X POST http://localhost:8000/api/commands/publish \
  -H 'Content-Type: application/json' \
  -d '{
    "request_no": "cmd-20261006-0001",
    "alert_no": 1001,
    "areas": ["东区", "西区"],
    "level": 4,
    "message": "东区危化品泄漏，请立即沿疏散通道撤离",
    "delay_seconds": 0,
    "duration_seconds": 1800
  }'
```

响应 `201`：

```json
{
  "request_no": "cmd-20261006-0001",
  "command_type": "publish",
  "status": "accepted",
  "alert_no": 1001,
  "level": 4,
  "message": "东区危化品泄漏，请立即沿疏散通道撤离",
  "areas": ["东区", "西区"],
  "delay_seconds": 0,
  "duration_seconds": 1800,
  "accepted_at": "2026-10-06T08:00:00.123456Z",
  "effective_from": "2026-10-06T08:00:00.123456Z",
  "effective_to": "2026-10-06T08:30:00.123456Z"
}
```

### 2. 幂等重试与冲突

```bash
# 同请求号、同载荷（网络重试/并发重复）→ 200，返回与首次完全一致的回执
curl -X POST http://localhost:8000/api/commands/publish -H 'Content-Type: application/json' \
  -d '{"request_no":"cmd-20261006-0001","alert_no":1001,"areas":["东区","西区"],"level":4,"message":"东区危化品泄漏，请立即沿疏散通道撤离","delay_seconds":0,"duration_seconds":1800}'

# 同请求号、不同载荷 → 409
curl -X POST http://localhost:8000/api/commands/publish -H 'Content-Type: application/json' \
  -d '{"request_no":"cmd-20261006-0001","alert_no":1001,"areas":["东区"],"level":5,"message":"另一份文案","delay_seconds":0,"duration_seconds":60}'
# {"detail":"请求号 'cmd-20261006-0001' 已被不同载荷的命令使用，拒绝重复利用"}
```

### 3. 查询指定时刻各区域应播发的告警

```bash
# 当前时刻、全部区域
curl 'http://localhost:8000/api/broadcast'

# 指定时刻 + 指定区域（中文需 URL 编码，curl 用 -G --data-urlencode）
curl -G 'http://localhost:8000/api/broadcast' \
  --data-urlencode 'area=东区' \
  --data-urlencode 'at=2026-10-06T08:15:00Z'
```

响应 `200`（无有效告警时 `"results": []`）：

```json
{
  "at": "2026-10-06T08:15:00Z",
  "count": 1,
  "results": [
    {
      "area": "东区",
      "alert_no": 1001,
      "level": 4,
      "message": "东区危化品泄漏，请立即沿疏散通道撤离",
      "accepted_at": "2026-10-06T08:00:00.123456Z",
      "effective_from": "2026-10-06T08:00:00.123456Z",
      "effective_to": "2026-10-06T08:30:00.123456Z"
    }
  ]
}
```

### 4. 撤销告警

```bash
curl -X POST http://localhost:8000/api/commands/revoke \
  -H 'Content-Type: application/json' \
  -d '{"request_no": "cmd-20261006-0002", "alert_no": 1001}'
```

响应 `201`：

```json
{
  "request_no": "cmd-20261006-0002",
  "command_type": "revoke",
  "status": "accepted",
  "alert_no": 1001,
  "accepted_at": "2026-10-06T08:10:00.000000Z",
  "revoked_at": "2026-10-06T08:10:00.000000Z"
}
```

撤销后查询当前时刻该区域得到空结果或恢复为低级别告警；以 `at` 指定撤销前的
历史时刻仍可查到该告警。撤销不存在的告警返回 `404`：

```bash
curl -X POST http://localhost:8000/api/commands/revoke -H 'Content-Type: application/json' \
  -d '{"request_no":"cmd-20261006-0003","alert_no":999999}'
# 404 {"detail":"告警号 999999 不存在，无法撤销"}
```

### 5. 修订进行中的告警（文案 / 级别 / 区域整体替换）

值守员发现告警 1001 的文案与覆盖区域有误（级别不变），用**新的独立请求号**提交修订，
请求体给出告警号和**完整的**新文案、级别、区域列表：

```bash
curl -X POST http://localhost:8000/api/commands/revise \
  -H 'Content-Type: application/json' \
  -d '{
    "request_no": "cmd-20261006-0101",
    "alert_no": 1001,
    "areas": ["东区", "南区"],
    "level": 4,
    "message": "东区危化品泄漏范围扩大，南区一并沿疏散通道撤离"
  }'
```

响应 `201`（含受理时刻 `accepted_at`/`revised_at` 与新内容；`effective_*` 沿用原区间，
修订不改变终点）：

```json
{
  "request_no": "cmd-20261006-0101",
  "command_type": "revise",
  "status": "accepted",
  "alert_no": 1001,
  "level": 4,
  "message": "东区危化品泄漏范围扩大，南区一并沿疏散通道撤离",
  "areas": ["东区", "南区"],
  "accepted_at": "2026-10-06T08:12:00.000000Z",
  "revised_at": "2026-10-06T08:12:00.000000Z",
  "effective_from": "2026-10-06T08:00:00.123456Z",
  "effective_to": "2026-10-06T08:30:00.123456Z"
}
```

修订后的查询行为：

```bash
# 当前时刻：东区、南区播新文案；被移出列表的西区停播
curl -G 'http://localhost:8000/api/broadcast' --data-urlencode 'area=南区'

# 修订受理时刻之前的历史时刻：仍是旧文案、旧区域（修订不改历史）
curl -G 'http://localhost:8000/api/broadcast' \
  --data-urlencode 'area=西区' \
  --data-urlencode 'at=2026-10-06T08:05:00Z'
```

修订同样幂等：同请求号同载荷重试返回 `200` 与首次回执，异载荷 `409`。
状态不符时的拒绝示例：

```bash
# 告警不存在
# 404 {"detail":"告警号 999999 不存在，无法修订"}
# 已撤销 / 未生效 / 已到期
# 409 {"detail":"告警号 1001 已撤销，无法修订"}
# 空文案、空/重复区域、级别越界
# 422（与发布同一套校验，不占用请求号、不留部分状态）
```

### 6. 校验失败示例

空区域列表、空文案、非正持续时长等返回 `422`，且不产生任何状态：

```bash
curl -X POST http://localhost:8000/api/commands/publish -H 'Content-Type: application/json' \
  -d '{"request_no":"cmd-x","alert_no":1003,"areas":[],"level":3,"message":"","delay_seconds":0,"duration_seconds":0}'
# 422 {"detail":[{"type":"too_short","loc":["body","areas"],...}, ...]}
```

## 测试

服务启动后运行端到端测试（覆盖幂等并发、冲突、优先级裁决、撤销历史、
修订版本/优先级/历史、修订与撤销顺序、校验与部分状态回滚等 38 个用例）：

```bash
pip install pytest httpx
pytest tests/ -v
# 指定其他地址：TEST_BASE_URL=http://localhost:8000 pytest tests/ -v
```

## 项目结构

```
app/
  main.py          # FastAPI 入口与路由
  service.py       # 命令受理（幂等）、撤销、修订、裁决查询
  schemas.py       # 请求模型与入参校验（发布/修订共用一套内容校验）
  db.py            # 连接池、就绪等待、建表
  sql/schema.sql   # 表结构（commands / alerts / alert_areas / alert_revisions /
                   #           alert_revision_areas）
tests/test_api.py  # 端到端测试
Dockerfile
docker-compose.yml
```
