# 机器人作业数据回流服务

这是一个面向机器人作业数据团队的服务端应用，负责管理机型、场景、技能、作业记录、人工标注和数据集。服务使用 FastAPI 提供本地 HTTP 接口，以 SQLite 保存业务数据；质量评分、数据集审核、版本、订阅和统计分析均在同一进程内完成。

## 变更时间线

作业记录的每次变化都会写入只追加的 `operation_change_events` 表（数据库触发器禁止更新和删除），覆盖创建、局部更新、标注关联和删除尝试。每条事件记录操作者、业务发生时间、请求来源、关联标识和脱敏后的前后差异；同一请求产生的多项变化共享关联标识，被拒绝的请求只记录拒绝事实（无前后差异）。删除需要 `X-Role: admin`，且被数据集引用的作业记录禁止删除。

- `GET /api/v1/operations/{id}/timeline`：按 (业务发生时间, 单调序号) 升序稳定翻页，支持 `since`/`until` 时间区间、`event_type`、`outcome` 过滤和 `cursor` 游标；游标绑定查询条件，篡改或跨作业复用会返回 400。作业删除后时间线仍可查询。
- 请求头：`X-Operator`（操作者）、`X-Role`（`operator` 默认 / `auditor` / `admin`）、`X-Occurred-At`（业务发生时间，ISO 8601）、`X-Source-System`（来源系统）、`X-Correlation-Id`（关联标识）。
- 普通调用方只能看到事件元数据；`auditor`/`admin` 可查看脱敏差异（序列号部分遮蔽，轨迹等大载荷只保留类型与内容指纹，原文不落地）。

## 目录

- `main.py`：应用入口、健康检查和路由注册。
- `app/models`：业务实体及其关系。
- `app/routers`：基础资源、作业、数据集、分析和变更时间线接口。
- `app/services`：评分、统计、策略目录、时间窗口与变更时间线工具。
- `app/seed_data.py`：可重复执行的示例数据初始化逻辑。
- `scripts/init_sample_data.py`：初始化脚本的兼容入口。

## 配置与运行

默认数据库文件为项目根目录的 `robot_data.db`，可以通过 `DATABASE_URL` 指定 SQLite 文件。安装依赖后运行 `python3 main.py`，服务默认监听 `8000` 端口；`GET /health` 返回服务状态，接口文档位于 `/docs`。

初始化示例数据可执行 `python3 scripts/init_sample_data.py`。该命令会重建本地数据库并写入机型、场景、技能、作业、标注及数据集示例。

## 验证

运行 `python3 -m pytest -q` 执行服务和领域工具测试，运行 `python3 -m compileall -q app main.py scripts` 检查编译。测试只使用临时 SQLite 数据库，不需要额外服务。
