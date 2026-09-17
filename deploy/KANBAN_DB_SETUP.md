# 看板数据库路径配置（P0 必做）

## 问题

Hermes 的看板数据库路径在两处有不同默认值：

| 入口 | 默认路径 | 来源 |
|------|---------|------|
| Hermes CLI / 网关 / Agent | `~/.hermes/kanban.db` | `hermes_cli/kanban_db.py:493` — `kanban_home() / "kanban.db"` |
| Talos 执行器 | `~/.hermes/kanban/kanban.db` | `talos/executor/constants.py:33-35` — `HOME / "kanban" / "kanban.db"` |

如果不统一，`hermes kanban` 命令会操作一个 9 任务影子库，而执行器操作 600+ 任务的真实库——所有 CLI 侧的操作（创建任务、查看状态、添加评论）都写到了错误的文件。

## 路径解析优先级（所有入口共用）

```
1. HERMES_KANBAN_DB 环境变量（最高优先级，直接指定文件路径）
2. HERMES_KANBAN_HOME 环境变量（指定根目录，default board → <root>/kanban.db）
3. 默认：~/.hermes/kanban.db（Hermes CLI）/ ~/.hermes/kanban/kanban.db（Talos 执行器）
```

**没有 config.yaml 配置键可以指定看板数据库路径。** 唯一的统一方式是设置 `HERMES_KANBAN_DB` 环境变量。

## 配置方法

### 方式一：写入 `~/.hermes/.env`（推荐）

Hermes CLI（`hermes_cli/main.py:574`）和网关（`gateway/run.py:1529`）在启动时加载 `~/.hermes/.env`，使用 `override=True`，会覆盖 shell 中的旧值。

```bash
echo 'HERMES_KANBAN_DB=/home/talos/.hermes/kanban/kanban.db' >> ~/.hermes/.env
```

**生效条件**：仅对新启动的 Hermes 进程生效。已运行的 Hermes 桌面应用 / 网关需要重启才能加载新值。

### 方式二：写入 shell profile（补充）

```bash
echo 'export HERMES_KANBAN_DB=/home/talos/.hermes/kanban/kanban.db' >> ~/.zshrc  # 或 ~/.bashrc
```

确保从终端启动的 `hermes kanban` 命令也能正确解析。

### 方式三：写入 `talos.env`（执行器必做）

执行器通过 `run-executor.sh` 加载 `talos.env`，不经过 Hermes 的 `.env` 加载逻辑。

```bash
# /etc/hermes/talos.env 或 ~/.hermes/talos.env
HERMES_KANBAN_DB=/home/talos/.hermes/kanban/kanban.db
```

**三个位置都设同一个值。** 方式一覆盖 CLI/网关/Agent，方式二覆盖终端会话，方式三覆盖执行器。

## 旧库归档

如果 `~/.hermes/kanban.db` 已存在（影子库），必须改名归档，不能删除：

```bash
mv ~/.hermes/kanban.db ~/.hermes/kanban.db.archived-$(date +%Y%m%d)
mv ~/.hermes/kanban.db-wal ~/.hermes/kanban.db.archived-$(date +%Y%m%d)-wal 2>/dev/null || true
mv ~/.hermes/kanban.db-shm ~/.hermes/kanban.db.archived-$(date +%Y%m%d)-shm 2>/dev/null || true
```

**注意**：如果有 Hermes 进程仍在运行（未加载新的 `HERMES_KANBAN_DB`），它会在旧路径自动新建一个空库。必须重启所有 Hermes 进程后才能彻底消除影子库。

## 验证命令

一条命令确认当前连的是哪个库：

```bash
hermes kanban list --json | python3 -c "import sys,json; print(f'CLI sees {len(json.load(sys.stdin))} tasks')"
# 对比：
sqlite3 ~/.hermes/kanban/kanban.db "SELECT COUNT(*) FROM tasks"
# 两个数字应该一致（或 CLI 数字 ≤ DB 数字，因为 CLI 可能只显示未归档任务）
```

如果 CLI 返回的任务数远少于 DB 中的任务数，说明 CLI 仍连着影子库——检查 `HERMES_KANBAN_DB` 是否生效。

## 为什么只能有一个库

- 执行器把任务状态、裁决结果、心跳、令牌吊销记录都写入库中
- CLI 的 `hermes kanban create / list / show` 也读写同一个库
- 如果两个入口连不同的库：CLI 创建的任务执行器看不到，执行器完成的任务 CLI 查不到
- WAL（Write-Ahead Logging）和文件锁按路径工作——两个路径就是两个独立的锁域，无法协调并发
