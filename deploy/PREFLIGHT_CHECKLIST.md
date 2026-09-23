# 上线必检清单

> 每次部署 Talos 执行器到新环境时逐条核对。全部通过后方可上线。

## 1. 看板数据库路径一致性（P0）

**为什么必检**：CLI 和执行器如果连不同的数据库，所有 CLI 侧操作写到影子库，执行器看不到。

```bash
# 执行器用的库
grep HERMES_KANBAN_DB /etc/hermes/talos.env    # 或 ~/.hermes/talos.env

# CLI 用的库（新开的 shell，不继承 talos.env）
hermes kanban list --json | python3 -c "import sys,json; print(len(json.load(sys.stdin)))"

# 直接查库
sqlite3 $(grep HERMES_KANBAN_DB /etc/hermes/talos.env | cut -d= -f2) "SELECT COUNT(*) FROM tasks"
```

**通过标准**：三个数字一致（或 CLI ≤ DB，因为 CLI 可能不显示归档任务）。

**不通过的处置**：按 `deploy/KANBAN_DB_SETUP.md` 配置 `HERMES_KANBAN_DB`。

## 2. 旧影子库不存在（P0）

```bash
ls -la ~/.hermes/kanban.db 2>/dev/null && echo "⚠️ 影子库仍存在" || echo "✅ 无影子库"
```

**不通过的处置**：`mv ~/.hermes/kanban.db ~/.hermes/kanban.db.archived-$(date +%Y%m%d)`，然后重启所有 Hermes 进程。

## 3. Docker worker 镜像存在

```bash
docker images hermes-worker:latest --format "{{.Repository}}:{{.Tag}} {{.Size}}"
```

**不通过的处置**：`docker build -t hermes-worker:latest -f ~/.hermes/hermes-agent/Dockerfile.worker ~/.hermes/hermes-agent`

## 4. GitLab 可达 + 令牌有效

```bash
source /etc/hermes/talos.env
curl -s -o /dev/null -w "%{http_code}" -H "PRIVATE-TOKEN: $TALOS_GITLAB_ADMIN_TOKEN" "$TALOS_GITLAB_URL/api/v4/user"
# 期望: 200
```

## 5. 模型推理服务可达

```bash
source /etc/hermes/talos.env
curl -s -o /dev/null -w "%{http_code}" "$TALOS_MODEL_BASE_URL/models"
# 期望: 200 或 401（需要认证头，但服务在）
```

## 6. 执行器 launchd/systemd 托管

```bash
# macOS
launchctl list | grep com.talos.executor

# Linux
systemctl status talos-executor
```

**通过标准**：服务存在且状态为 running。

## 7. gitlab-runner 运行中

```bash
gitlab-runner verify
```

## 8. 分支保护规则已设

```bash
source /etc/hermes/talos.env
curl -s -H "PRIVATE-TOKEN: $TALOS_GITLAB_ADMIN_TOKEN" \
  "$TALOS_GITLAB_URL/api/v4/projects/<project_id>/protected_branches" | python3 -m json.tool
```

**通过标准**：`main` 分支在列表中，push_access_level ≥ 40 (Maintainer)。

## 9. 本机开发（macOS + VPN）

> 仅本机开发环境需要，服务器部署跳过本节。

macOS Docker Desktop 的容器虚拟机看不到宿主机 VPN 网卡。如果模型网关在
VPN 内网，需要在宿主机上运行 TCP 端口中转进程，让容器通过 host-gateway
访问模型网关。

安装与验证方式见 [`dev-macos/README.md`](dev-macos/README.md)。

```bash
# 确认中转进程在监听（替换 <port> 为 TALOS_HOST_FORWARD 里的端口号）
nc -z 127.0.0.1 <port> && echo "✅ 端口中转存活" || echo "⚠️ 端口中转未运行"
```

**不通过的处置**：`launchctl load ~/Library/LaunchAgents/com.talos.dev-port-relay.plist`
