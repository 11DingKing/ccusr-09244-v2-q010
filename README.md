# 机器人作业数据回流服务

这是一个面向机器人作业数据团队的服务端应用，负责管理机型、场景、技能、作业记录、人工标注和数据集。服务使用 FastAPI 提供本地 HTTP 接口，以 SQLite 保存业务数据；质量评分、数据集审核、版本、订阅和统计分析均在同一进程内完成。

## 目录

- `main.py`：应用入口、健康检查和路由注册。
- `app/models`：业务实体及其关系。
- `app/routers`：基础资源、作业、数据集和分析接口。
- `app/services`：评分、统计、策略目录与时间窗口工具。
- `app/seed_data.py`：可重复执行的示例数据初始化逻辑。
- `scripts/init_sample_data.py`：初始化脚本的兼容入口。

## 变更时间线（审计）

服务为作业数据维护不可变的变更时间线，回答“某条作业在某个时刻由谁改过哪些字段”。

- **覆盖动作**：作业创建、局部更新、删除尝试；标注关联的创建、更新与删除。被数据集引用的作业会被拒绝删除。
- **记录内容**：操作者、角色、请求来源、客户端请求 ID、业务发生时间、字段级脱敏前后差异。
- **关联标识**：同一次请求产生的多项变化（如删除作业时级联删除标注）共享 `X-Correlation-ID`；未提供时服务自动生成并经响应头 `X-Correlation-ID` 返回。
- **拒绝留痕**：越权、无变化、目标不存在、被引用拦截等失败请求只记录拒绝事实（`success=false`、`reason_code`），不携带差异，也不会伪装成成功变更。
- **不可变与顺序**：`change_events` 仅允许追加，SQLite 触发器在数据库层禁止 UPDATE/DELETE；`(happened_at, seq)` 双键稳定排序，业务发生时间在取得写锁时打点，服务重启后顺序可还原。
- **查询**：`GET /api/v1/operations/{id}/timeline` 与 `GET /api/v1/change-events`，支持时间区间（闭区间）、动作、成功与否、关联标识过滤和签名游标翻页；游标与查询条件绑定，串用会被拒绝。
- **敏感载荷**：轨迹、感知记录、抓取结果、环境/硬件状态以及标注文字描述对普通调用方只暴露 `{"redacted": true, "present": ...}`；完整前后快照仅 `admin` 角色可见。

写接口通过请求头识别调用方（缺省角色为只读的 `viewer`）：

| Header | 说明 |
| --- | --- |
| `X-Operator-Id` | 操作者标识，缺省 `anonymous` |
| `X-Operator-Role` | `viewer` / `editor` / `admin`；仅 editor、admin 可写，未知角色按 viewer 处理 |
| `X-Request-Source` | 请求来源（如标注台、回流入库任务） |
| `X-Correlation-ID` | 一次请求的关联标识 |
| `X-Request-Id` | 客户端请求 ID（可选，原样留痕） |

## 配置与运行

默认数据库文件为项目根目录的 `robot_data.db`，可以通过 `DATABASE_URL` 指定 SQLite 文件。安装依赖后运行 `python3 main.py`，服务默认监听 `8000` 端口；`GET /health` 返回服务状态，接口文档位于 `/docs`。

初始化示例数据可执行 `python3 scripts/init_sample_data.py`。该命令会重建本地数据库并写入机型、场景、技能、作业、标注及数据集示例。

## 验证

运行 `python3 -m pytest -q` 执行服务和领域工具测试，运行 `python3 -m compileall -q app main.py scripts` 检查编译。测试只使用临时 SQLite 数据库，不需要额外服务。
