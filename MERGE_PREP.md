# 第三组：合并前材料

## 1. 全量 diff：feat/executor-v1 相对 main

43 files changed, 9284 insertions(+), 377 deletions(-)

### 按文件分组（最终行数 + 改动摘要）

#### 执行器核心代码

| 文件 | 最终行数 | 改动摘要 |
|------|---------|---------|
| `talos/executor/adjudicate.py` | 870 | 裁决逻辑：check_artifacts/check_git_pushed/check_verification/check_deliverables + check_model_calls（零次模型调用）+ Verdict 分流（pass/degraded/unmet/error/instance_not_started）|
| `talos/executor/loop.py` | 751 | 主循环：tick 顺序（I8）、_adjudicate_exited、_heartbeat_live_containers、_dispatch、_self_check（6 项 + 影子库告警）、_handle_kernel_recycled、_is_run_adjudicated（I7 幂等）|
| `talos/executor/spawn.py` | 504 | 容器拉起：make_spawn_fn、_generate_container_config、NO_PROXY 传递、HOST_FORWARD 传递、挂载表白名单（I9）|
| `talos/executor/finalize.py` | 360 | 终局处理：complete_task/_record_task_failure/block_task 三路路由 + instance_not_started → block_task（环境缺陷）+ 评论分段标注 |
| `talos/executor/declarations.py` | 331 | Declaration dataclass + load_declarations（frontmatter 解析、多 skill 合并 OR 语义、requires_model_call 字段）|
| `talos/executor/credentials.py` | 297 | 令牌生命周期：铸造任务级令牌、scope/access_level 校验、吊销、cleanup_orphan_tokens |
| `talos/executor/reap.py` | 229 | 容器回收：list_exited/running_containers、reap、reap_orphans（哨兵死后回收）|
| `talos/executor/sentinel.py` | 193 | 哨兵进程：SIGTERM → docker kill、alive 文件检测、寿命管理 |
| `talos/executor/constants.py` | 189 | 常量：KANBAN_DB（无默认值，HERMES_KANBAN_DB 必设）、_resolve_kanban_db、_check_shadow_db、log_event |
| `talos/executor/collect.py` | 155 | 容器产出收集：state.db、result.json、workspace 拷出 |
| `talos/executor/archive.py` | 132 | 归档：run bundle 打包到 archived/ |
| `talos/executor/redact.py` | 97 | 脱敏：env 值替换（KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL）|
| `talos/executor/main.py` | 12 | 入口 |

#### 测试代码

| 文件 | 最终行数 | 改动摘要 |
|------|---------|---------|
| `tests/test_v2_1.py` | 1484 | 76 条行为测试：裁决/终局/幂等/哨兵/令牌/声明/回收 |
| `tests/test_adjudicate.py` | 941 | 早期裁决测试 |
| `tests/test_zero_model_calls.py` | 292 | 6 条：零次模型调用→blocked/不重拉、非零缺产物→重拉、关开关→跳过 |
| `tests/test_db_path_selfcheck.py` | 110 | 6 条：HERMES_KANBAN_DB 未设→退出、影子库告警/归档文件不触发 |
| `tests/conftest.py` | 77 | TALOS_TEST_DB fail-safe、conn/make_task/make_run fixtures |
| `tests/integration/test_adjudicate_flow.py` | 243 | 集成测试 |
| `tests/acceptance/*.py` | 3348 | 6 个验收脚本（acc_batch1/2/3、acc_real、acc_run、acc_v2）|

#### 部署文件

| 文件 | 最终行数 | 改动摘要 |
|------|---------|---------|
| `deploy/talos.env.template` | 78 | 环境变量模板（TALOS_NO_PROXY、TALOS_HOST_FORWARD、TALOS_MODEL_*）|
| `deploy/install-launchd.sh` | 73 | macOS launchd 安装脚本 |
| `deploy/KANBAN_DB_SETUP.md` | 85 | 账本路径配置说明 |
| `deploy/PREFLIGHT_CHECKLIST.md` | 82 | 8 项上线必检 |
| `deploy/install-skills.sh` | 54 | skill 安装脚本 |
| `deploy/com.talos.executor.plist` | 51 | launchd plist |
| `deploy/container-config.template.yaml` | 48 | 容器 config.yaml 模板 |
| `deploy/run-executor.sh` | 28 | 执行器启动脚本 |
| `deploy/talos-executor.service` | 41 | systemd service |

#### Skills

| 文件 | 最终行数 | 改动摘要 |
|------|---------|---------|
| `skills/talos-code-demo/SKILL.md` | 64 | 写代码+推送+CI |
| `skills/talos-doc-demo/SKILL.md` | 53 | 写文档 |
| `skills/talos-retry-demo/SKILL.md` | 51 | 重拉测试 |
| `skills/talos-ci-short-timeout/SKILL.md` | 44 | CI 短超时 |
| `skills/talos-evidence-test/SKILL.md` | 31 | 证据账本测试 |

#### 其他

| 文件 | 最终行数 | 改动摘要 |
|------|---------|---------|
| `docs/dd2/ACC_RESULT.md` | 1021 | R2-R8 + BOT-1 收口对照表 28/28 |
| `talos/observability/forwarder.py` | 348 | ES trace 转发 |
| `talos/plugins/__init__.py` | 53 | 插件注册 |
| `pyproject.toml` | 21 | 项目配置 |

---

## 2. 死代码与残留清单

| # | 类型 | 位置 | 说明 | 建议 |
|---|------|------|------|------|
| 1 | **废弃函数** | `adjudicate.py:617` `_query_ls_remote()` | 标记 `[DEPRECATED]`，P0-1 后被 `_gitlab_branch_sha` 替代。无任何调用点（代码+测试均无引用）| 等 Claude 通读后决定删除 |
| 2 | **废弃函数** | `adjudicate.py:648` `_check_repo_reachable()` | 标记 `[DEPRECATED]`，无任何调用点 | 等 Claude 通读后决定删除 |
| 3 | **调试开关** | `adjudicate.py:759` `TALOS_ADJ_SLEEP` | 测试 hook：注入 sleep 验证哨兵寿命（M18/M24）。生产代码中的环境变量条件分支 | 等 Claude 通读后决定是否移至测试 fixture |
| 4 | **.bak 文件** | `talos/executor/adjudicate.py.bak-20260915` | 9 月 15 日手动备份 | 合并前删除 |
| 5 | **.bak 文件** | `talos/executor/finalize.py.bak-20260915` | 同上 | 合并前删除 |
| 6 | **.bak 文件** | `tests/test_v2_1.py.bak-20260915` | 同上 | 合并前删除 |
| 7 | **未跟踪文件** | `skills/talos-m5-three-artifacts/` | R8 临时创建的 skill | 合并前决定是否保留 |
| 8 | **未跟踪文件** | `docs/dd2/DISPATCH_INCIDENT_RCA.md` | RCA 报告 | 等 Claude 合入设计文档时一并处理 |

**说明**：以上均为「自查发现、列出来等 Claude 决策」的项目。没有自行删除任何代码。

---

## 3. PR 描述：不变量 I1–I11 逐条满足说明

### I1 — 执行器与调度器不含任务类型知识

**实现**：执行器代码中不出现按 skill 名或任务类型分支的逻辑。所有类型知识在 skill 的 frontmatter 声明里。
**证据**：`grep -rn 'if.*code\|if.*doc\|if.*talos-' talos/executor/` → 零命中。裁决函数 `adjudicate()` 只读 `Declaration` 对象的属性，不检查 skill 名。验收项 M22（两个不同执行单元由同一执行器各跑一次通过）。

### I2 — 执行器是看板数据库的唯一写者

**实现**：容器不挂载看板目录。挂载表白名单（`spawn.py` 挂载列表）不含 kanban。
**证据**：`spawn.py` 挂载表：`/work`（仓库 clone）、`/tmp/hermes-worker-home`（HERMES_HOME）、`/tmp/.../config.yaml`（配置）、context 文件。无 kanban 目录。验收项 M2（`docker inspect` 挂载表 = §5.1 白名单，无 kanban）。

### I3 — 裁决只有一个权威来源——执行器

**实现**：容器内插件只保留 skill 保护与路径保护，无 complete 拦截。结果文件里 worker 的自报只作参考。
**证据**：`adjudicate.py` `check_git_pushed()`：sha 从 GitLab API 取（`_gitlab_branch_sha`），不从 result.json 取。`_get_self_reported_sha()` 只用于比对，不一致即 problem。验收项 M10（SHA 不匹配篡改）。

### I4 — 证据不在被检查者手里

**实现**：裁决函数的输入只来自 GitLab API、ls-remote（已废弃，改用 GitLab API）、宿主机拷出的文件。
**证据**：`adjudicate.py:394` `# P0-1: sha from GitLab API (authoritative), NOT ls-remote, NOT self-reported`。裁决输入：`CollectedBundle`（宿主机拷出）+ `Declaration`（执行器注入）+ GitLab API 调用。不读容器内进程的声明作为终局依据。

### I5 — 不再有「契约未满足却 done」

**实现**：problem → 重拉（_record_task_failure）；达上限 → blocked（内核熔断）；只有 defect 才降级（degraded → done）。
**证据**：`finalize.py` 路由：`pass → done`、`degraded → done`、`unmet → _record_task_failure`（重拉）/ `block_task`（达上限）、`error → block_task`、`instance_not_started → block_task`。验收项 M13（连续 2 次 unmet → blocked，不存在 done 记录）。

### I6 — 凭据由执行器派发时铸造、任务结束失效

**实现**：容器 env 与挂载里的令牌都是本任务签发的。执行器持有的唯一长期凭据在 managed 配置。任务结束后吊销。
**证据**：`credentials.py` `mint_task_token()`：用 admin PAT 签发项目级 access token，有效期 = 任务最长运行时间 + 裁决超时。`reap()` 调 `revoke_task_token()`。`cleanup_orphan_tokens()` 启动时清理。验收项 M19/M20（凭据生命周期、短期令牌推分支）。BOT-1 验证：令牌 talos-t_3f03761d-3810 吊销后 404。

### I7 — 每一步都可重复执行

**实现**：以 `task_runs.id`（run_id）为幂等键。裁决前先查该 run 是否已有裁决记录。
**证据**：`loop.py` `_is_run_adjudicated()`：检查 task_runs.metadata 是否含 verdict key，或 status 是否已 terminal。`_adjudicate_exited()` 调用此函数，已裁决的 run 跳过 finalize。验收项 M1（重启不重复派发、不重复裁决）。

### I8 — 裁决先于回收

**实现**：每个任务有一个执行器拥有的哨兵进程，活到裁决落账为止。每个 tick 先裁决已退出的容器，再调内核派发。
**证据**：`sentinel.py`：哨兵进程监控 alive 文件 mtime，超时自行退出。`loop.py` tick 顺序：_adjudicate_exited → _heartbeat_live_containers → reap_orphans → _dispatch。验收项 M18（裁决注入 30s sleep，哨兵存活、任务不被回收）。

### I9 — 容器只写、不读账本；只出、不进机密

**实现**：挂载表白名单。进容器的只有干净 clone、上下文文件、本任务凭据。出来的只有结果文件、state.db、trace。
**证据**：`spawn.py` 挂载列表：`/work`（clone）、`/tmp/hermes-worker-home`（HERMES_HOME）、config.yaml、context 文件。无 kanban、无长期凭据。`redact.py`：归档/日志/验收三处脱敏。

### I10 — 执行器不解析产物内容当指令

**实现**：结果文件只按 schema 读字段。产物文件不执行、不 eval。
**证据**：`collect.py` `CollectedBundle`：result_json 按 `§5.4 schema` 读取（status/summary/artifacts/sha 字段）。`adjudicate.py` 不 eval 任何产物内容。`_get_self_reported_sha()` 只取 sha 字段做比对。

### I11 — 绑定参数由执行器注入，执行组件只用不选

**实现**：执行组件声明 `requires`。拉起前缺任一绑定 → defect，不拉容器，block_task。裁决只认执行器注入的绑定值。
**证据**：`declarations.py` `Declaration.requires` 字段 + `load_declarations()` 从 frontmatter 读取。`spawn.py` `_generate_container_config()`：绑定参数段写入 config.yaml。`adjudicate.py` `_get_injected_repo()`：从 Declaration.deliverables 取 repo URL，不从 result.json 取。验收项 M5/M20（绑定参数验证）。
