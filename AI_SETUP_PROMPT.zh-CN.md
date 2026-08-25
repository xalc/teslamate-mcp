# TeslaMate MCP：交给 AI 的部署配置 Prompt

下面的 Prompt 可直接复制给具备终端和文件操作能力的 AI。先把方括号中的信息替换为你的环境；不知道的项目保留“自动探测”。

```text
你是一名谨慎的 Linux、Docker、PostgreSQL、TeslaMate 和 MCP 运维工程师。请阅读仓库中的 README.md、README.zh-CN.md、compose.yaml、db/setup.sql、db/setup_cost.sql、plugins/teslamate_cost_approval 以及测试代码，然后在目标机器上完成 TeslaMate MCP 的部署或配置。

仓库地址：[GITHUB_REPOSITORY_URL]
目标目录：[TARGET_DIRECTORY，默认 /opt/application/teslamate-mcp]
TeslaMate 部署位置：[TESLAMATE_DIRECTORY 或 自动探测]
TeslaMate Docker 网络：[TESLAMATE_DOCKER_NETWORK 或 自动探测]
服务绑定地址：[BIND_ADDRESS，推荐 127.0.0.1 或 Tailscale IP]
只读 MCP 端口：[默认 8766]
费用 MCP 端口：[默认 8767]
Hermes 目录/Profile：[HERMES_PROFILE_PATH 或 暂不接入]
费用使用者 actor：[例如 huunter；如有多人请列出]

目标：
1. 部署只读 TeslaMate MCP；
2. 部署带审计的充电费用与高速费用 MCP；
3. 如提供 Hermes Profile，安装审批插件并配置 MCP；
4. 费用写入必须直接触发一次性审批卡，卡片只允许 Approve 或 Reject；
5. 支持一次审批原子写入 1–20 条费用；
6. 完成真实健康检查和最小烟雾测试。

必须遵守：
- 先只读检查实际环境，不凭空猜测数据库、网络、容器、端口或 Profile 名称。
- 检查目标目录及其父目录中的 AGENTS.md 或等价项目指令，并遵守它们。
- 不停止、不重启、不修改现有 TeslaMate，除非部署确实需要且先解释影响。
- 不读取后在聊天中输出任何密码、Token、Cookie、私钥或完整连接串。
- 不把 secrets/、.env、数据库导出、截图、备份、日志中的凭据提交到 Git。
- 使用安全随机值生成 Token 和签名密钥；文件权限限制为仅服务账号可读。
- 不把 MCP 暴露在公网；只允许 localhost、可信内网或 Tailscale 地址。
- 数据库变更前阅读 SQL，确认目标库，说明将创建的角色、视图、表和函数，并保留可恢复方案。
- 保留现有用户改动；不得执行 git reset --hard、git clean -fd 或覆盖未知文件。
- 所有配置改动先备份；只做完成目标所需的最小修改。
- 不声称成功，除非命令、健康接口、日志或测试提供了证据。

请按以下流程执行：

A. 环境盘点
- 检查 OS、Docker/Compose、uv/Python、git 版本。
- 找到 TeslaMate Compose 项目、PostgreSQL/MQTT 容器、数据库名、网络名和服务可达地址。
- 检查 8766/8767 是否占用，以及目标绑定地址是否存在。
- 如果接入 Hermes，找到准确的 Profile、插件目录、MCP 配置和服务重启方式。
- 输出简短的“探测结果 + 拟修改对象 + 风险”，然后继续安全且可逆的步骤；只有遇到会改变架构或可能中断现有服务的歧义时才询问我。

B. 获取并检查代码
- 克隆或更新仓库，但不要覆盖本地未提交改动。
- 阅读中英文 README、Compose、SQL、源码暴露的 MCP tools 和审批插件。
- 运行测试：uv run --extra test pytest -q。
- 检查 Git 跟踪文件中没有 secrets、备份或凭据。

C. 配置
- 创建 secrets/，生成独立的只读 MCP Token、各 actor 的费用 Token、费用签名密钥，并安全保存。
- 使用实际探测结果修改 compose.yaml；不要保留与目标机器无关的示例 IP、网络名或 actor。
- allowed hosts 只加入实际需要的地址。
- 保证只读服务使用受限只读数据库账号；费用服务只通过受控函数写入。
- 先运行 docker compose config -q，确认配置有效。

D. 数据库
- 明确连接的是 TeslaMate 目标数据库，而不是其他 PostgreSQL 实例。
- 以数据库 owner 执行 db/setup.sql 和 db/setup_cost.sql。
- 验证角色权限：只读账号不能写；费用账号不能直接改表，只能调用受审计函数。
- 不使用真实费用记录做破坏性试写；优先使用事务回滚或安全的权限检查。

E. 启动与验证
- 执行 docker compose up -d --build。
- 检查 docker compose ps、两个 /healthz、容器最新日志。
- 枚举 MCP tools，确认只读工具存在。
- 确认费用服务公开 7 个工具：3 个充电费工具，以及 find_toll_journey_candidates、request_toll_expense_changes、get_toll_expense_history、get_road_trip_cost_summary。

F. Hermes 接入（如果提供了 Profile）
- 将 plugins/teslamate_cost_approval 安装到准确的 Profile 并启用。
- 配置两个 MCP URL；费用 MCP 使用该 actor 的 Token。
- 重启对应 Hermes 服务并检查加载日志。
- 先执行不写入的费用匹配。
- 发起一条明确标记的测试写入请求，确认无需聊天中的手动“确认”即可直接弹出审批卡。
- 不要替我点击批准。请停在审批卡，让我选择 Approve 或 Reject；如果我批准，再验证费用值和审计历史。
- 再验证两条费用可由一次审批原子写入；如缺少安全测试数据，说明验证方法，不制造真实账单。

G. 交付报告
最终只给我简洁、可核验的结果：
- 实际部署路径和 Git commit；
- 实际服务地址（Token 打码）；
- 创建/修改的文件；
- 测试数量及结果；
- 健康检查、容器状态和关键日志结论；
- Hermes 审批是否验证；
- 尚未完成或需要我操作的事项；
- 回滚方法。

如果这是已有运行环境的升级，请先比较差异并保持当前 secrets 和数据库数据；不要重新初始化或覆盖它们。
```

## 仅配置 AI 客户端时的简化 Prompt

如果服务已经运行，只想让 AI 客户端添加 MCP，可使用：

```text
请把两个 TeslaMate MCP 接入当前 AI/Hermes Profile：

- 只读 MCP URL：[READ_ONLY_MCP_URL]
- 费用 MCP URL：[COST_MCP_URL]
- 认证方式：HTTP Bearer Token；从我指定的安全文件读取，不要在回复、日志或配置 diff 中显示 Token。
- 审批插件来源：仓库 plugins/teslamate_cost_approval。

要求：
1. 先确认准确的 Profile 和现有 MCP 配置格式；
2. 备份配置后做最小修改；
3. 启用 teslamate_cost_approval 插件；
4. 重启准确的服务并检查插件/MCP 加载日志；
5. 验证只读查询；
6. 发起费用请求时不要先让我在聊天中手动确认，应直接弹 Approve/Reject；
7. 不要代替我批准写入；
8. 最终报告改动文件、服务状态和验证证据，所有 Token 必须打码。
```
