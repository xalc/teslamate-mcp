# TeslaMate MCP

[English](README.md) · [交给 AI 的部署配置 Prompt](AI_SETUP_PROMPT.zh-CN.md)

这是一个面向现有 TeslaMate 实例的 MCP 服务，包含两个相互隔离的入口：

- 只读服务：查询车辆状态、行程、轨迹、充电、电池趋势和累计统计；
- 费用服务：匹配充电记录或高速旅程，经一次人工审批后写入充电费/高速费，并保存审计记录。

服务直接读取 TeslaMate 的 PostgreSQL/MQTT 数据，不调用 Tesla API。

## 功能

只读 MCP 提供：

- 服务健康状态和车辆列表；
- 车辆当前状态；
- 行程列表、行程详情和轨迹；
- JSON 行程数据导出；
- 行驶汇总、充电汇总；
- 充电会话及明细；
- 电池与续航趋势；
- 车辆累计统计。

费用 MCP 公开已有三个充电费工具和四个高速费工具：

1. `find_charging_sessions_for_cost`：根据日期、地点、充电量等信息查找候选充电记录，不写数据库；
2. `request_charging_cost_changes`：弹出一次宿主审批，并以一个事务写入 1–20 条费用；
3. `get_charging_cost_history`：查询费用修改审计记录。

4. `find_toll_journey_candidates`：把连续的 1–20 段 drive 自动组合成高速旅程候选；
5. `request_toll_expense_changes`：原子创建、更正、补关联或作废高速费；
6. `get_toll_expense_history`：查询高速费当前状态和完整审计；
7. `get_road_trip_cost_summary`：汇总一趟旅程的高速费、已记录充电费和已知总成本。

费用写入不需要用户先在聊天中发送“确认”。Hermes 在 MCP 工具真正执行前拦截请求，直接显示只有 **Approve** 和 **Reject** 的审批卡。批量操作中任何一条无效或已变化，整批都会回滚。高速费可以关联多段 drive；无法明确匹配时保存为 `pending_match`，之后再补关联。截图原图不保存。

## 架构与端口

默认容器端口：

| 服务 | 端口 | MCP 地址 | 健康检查 |
|---|---:|---|---|
| 只读 MCP | 8766 | `/mcp` | `/healthz` |
| 费用 MCP | 8767 | `/mcp` | `/healthz` |

两个 MCP 均使用 Streamable HTTP。请只绑定到本机、内网地址或可信的 Tailscale 网络，不要直接暴露到公网。

## 安装要求

- 已正常运行的 TeslaMate；
- 可访问 TeslaMate PostgreSQL 数据库；
- 如需实时车辆状态，可访问 TeslaMate MQTT；
- Docker Compose；或 Python 3.12+ 与 `uv`；
- 如需审批卡，需使用支持 `pre_tool_call` 审批升级和飞书互动卡片的 Hermes。

## 配置密钥

在仓库根目录创建 `secrets/`，准备以下文件：

```text
secrets/db_password
secrets/mcp_token
secrets/cost_db_password
secrets/cost_token_huunter
secrets/cost_token_guoguo
secrets/cost_signing_secret
```

文件内容只放值本身，不加引号。Token 和签名密钥可使用安全随机值：

```bash
openssl rand -hex 32
```

`secrets/` 已被 `.gitignore` 和 `.dockerignore` 排除。不要把真实密码、Token、数据库导出、费用截图或备份提交到 Git。

如果不需要示例中的多个使用者，可以在 `compose.yaml` 中删除多余 actor，并改成自己的名称和 Token 文件。

## 修改部署配置

打开 `compose.yaml`，按实际环境调整：

- PostgreSQL 主机、端口、数据库名和用户名；
- MQTT 主机及认证信息；
- TeslaMate 所在的 Docker external network；
- 服务绑定的主机 IP；
- MCP 的 allowed hosts；
- 费用服务的 actor 与各自 bearer token。

仓库中的 Compose 配置是一个部署模板，其中可能带有原部署环境的网络名称或 actor 名称。新环境必须先检查并替换，不能直接假设可用。

## 初始化数据库

以 TeslaMate 数据库 owner 或具备相应建表/授权权限的管理员身份，依次执行：

```bash
psql "$TESLAMATE_DATABASE_URL" -f db/setup.sql
psql "$TESLAMATE_DATABASE_URL" -f db/setup_cost.sql
```

- `db/setup.sql` 创建只读视图、角色和权限；
- `db/setup_cost.sql` 创建费用写入角色、审计对象及受控写入函数。

两个脚本设计为可重复执行，但在生产数据库操作前仍应备份并阅读脚本。

## 启动和验证

```bash
docker compose config -q
docker compose up -d --build
docker compose ps
```

验证健康接口，将地址替换为实际绑定地址：

```bash
curl -fsS http://127.0.0.1:8766/healthz
curl -fsS http://127.0.0.1:8767/healthz
```

如启动失败，检查：

```bash
docker compose logs --tail=200
```

## 接入 Hermes 审批

1. 将 `plugins/teslamate_cost_approval` 复制到目标 Hermes profile 的 `plugins/`；
2. 在该 profile 中启用 `teslamate_cost_approval`；
3. 配置只读 MCP URL 和费用 MCP URL；
4. 为费用 MCP 配置与 actor 对应的 bearer token；
5. 重启 Hermes，并确认插件加载成功；
6. 先调用费用匹配工具，再用一条测试费用触发审批卡；
7. 确认审批卡只有 **Approve** 和 **Reject**，批准后检查数据库费用和审计记录。

插件会校验审批卡对应的批次，并在请求畸形、拒绝或超时时拒绝写入。

## 对话使用示例

查询故事和行程：

```text
找出我最近一个月最长的三次行程，并结合出发地、目的地、时间和充电记录讲讲发生了什么。
```

录入单笔费用：

```text
这张截图是 2026-08-10 的充电订单，实付 38.60 元。请匹配 TeslaMate 记录并发起写入审批。
```

录入多笔费用：

```text
请识别截图里的全部订单，逐笔匹配 TeslaMate 充电会话，列出匹配依据后用一次审批整批写入。
```

费用修改历史：

```text
查一下最近 20 条充电费用修改记录，包括修改人、原金额、新金额和时间。
```

录入高速费与查询旅程成本：

```text
这张 ETC 截图是今天宝鸡到西安的高速费 89.20 元，请匹配行程并发起审批。
这趟宝鸡回西安，充电加高速总共花了多少？
```

## 本地开发与测试

```bash
uv sync --extra test
uv run --extra test pytest -q
```

提交前检查：

```bash
git status --short
git grep -nE '(BEGIN (RSA|OPENSSH) PRIVATE KEY|Bearer [A-Za-z0-9._-]{20,})'
```

## 安全原则

- 只读数据库角色启用只读事务、查询超时，并仅访问经过筛选的视图；
- 费用角色不能直接修改 TeslaMate 表，只能调用带审计的受控数据库函数；
- 每次写入都需要一次宿主审批，不提供永久允许选项；
- 路线和充电记录可能包含精确坐标，MCP 地址和 Token 应视为敏感信息；
- 原始账单截图不落库，只把必要的来源摘要写入审计记录。

需要让 AI 协助安装时，请把 [AI_SETUP_PROMPT.zh-CN.md](AI_SETUP_PROMPT.zh-CN.md) 的内容和本仓库地址一起交给它。
