# 验收套件技能对照表

## 原则

1. **脚本只做夹具**：验收脚本只做夹具（建任务、断言），不当执行器（不调 `tick()`）。
   派发与裁决全部由 launchd 里的常驻执行器完成。
2. **硬规则**：原文提到 git / 分支 / 流水线 / 令牌 / `.gitlab-ci.yml` / `source: ci` 的项，
   **必须**用 `require_push: true` + `verification.source: ci` + `credentials: gitlab`
   的技能（即 `talos-code-demo` 或按它派生的变体），任务正文首行 `repo: {PILOT_REPO}`。
   `talos-acc-*` 系列只允许用于原文与 GitLab 无关的项。
   断言必须是行为证据（账本状态、评论、GitLab API 返回、state.db 里的工具调用结果），
   挂载表 / 源码 / 文件存在性不算。
3. 新增验收项必须先补本表，再写代码。
4. **清场时机**：`cleanup_task` 只允许在任务到达终态（done / blocked / gave_up / archived）
   之后调用。断言不需要等到终态的项（M2 / M3 / M4 / M16 等），断言完仍要 `wait_for_terminal()`
   再清场。`run_item()` 的 `finally` 兜底也遵守同一规则：任务未到终态就先等，等不到
   （超时）记「未验·清场前等待终态超时」并**不归档**，把任务号写进报告让人处理。

## 对照表（v2.3 §13 原文 vs 现用技能）

"覆盖"列只允许两个值：**是** / **不适用**（附理由）。

| 项号 | v2.3 §13 原文要验的契约（一句话） | 现用技能 | 覆盖 | 证据 |
|------|-----------------------------------|----------|------|------|
| M1 | 执行器以服务常驻；kill -9 后 10 秒内被拉起；重启后不重复派发/裁决 | 无（纯人工观察） | 不适用 | manual：观察 launchd 重启，不建任务 |
| M2 | ready 任务 → 一个 tick 内被领取并拉起容器，挂载表 = §5.1 白名单，无 kanban 目录 | talos-acc-pass | 是 | `require_push: false, source: none`（无 git/分支/流水线/令牌） |
| M3 | 容器 env 无 HERMES_KANBAN_*；工具清单无 kanban_* | talos-acc-pass | 是 | 同 M2 |
| M4 | 上下文文件含 hermes kanban context 原文 + 声明摘要 + 收尾要求 | talos-acc-pass | 是 | 同 M2 |
| M5 | 声明 3 个产物只做 2 个 → unmet → 回 ready → 评论含 ⛔ | talos-acc-unmet | 是 | `require_push: false, source: none`（无 git/分支/流水线/令牌） |
| M6 | 承 M5：run#2 上下文含 run#1 的 error → 补做 → pass → done | talos-acc-unmet | 是 | 同 M5，两轮 |
| M7 | 每次裁决恰好一条 [执行器] 评论，author = talos-executor | talos-acc-pass | 是 | 验评论条数 = 裁决次数 |
| M8 | verification.source: ci：流水线 success → 通过；failed → unmet → 重拉 | talos-code-demo | 是 | `require_push: true, source: ci, credentials: gitlab`；body 首行 `repo: {PILOT_REPO}` |
| M9 | source: ci 且流水线在 timeout_s 内无终态 → defect 超时 → degraded | talos-ci-short-timeout | 是 | 触发方式：短超时（verification.timeout_s=45）+ docker stop gitlab-runner 使 pipeline pending。停 runner 在本 GitLab 实例有共享 runner 时不可复现（pipeline 仍可能被共享 runner 执行）。`require_push: true, source: ci, credentials: gitlab`；body 首行 `repo: {PILOT_REPO}` ✅已补 |
| M10 | git ls-remote 核对 sha 一致才通过；自报错误 sha → unmet | talos-code-demo | 是 | `require_push: true, source: ci, credentials: gitlab`；body 首行 `repo: {PILOT_REPO}` ✅已改（原 talos-acc-sha-mismatch） |
| M11 | source: evidence 的执行单元：证据账本不可读 → defect 降级 done | 不适用 | 不适用 | 黑盒下无法无侵入复现；由 tests/test_adjudicate.py 单元测试覆盖 |
| M12 | 声明 frontmatter 非法 → 不拉容器、blocked、评论「校验器故障」 | dd1-broken-skill | 是 | 故意坏掉的 skill（无 git/verification 声明） |
| M13 | 连续 2 次 unmet → blocked（内核熔断），评论「转人工」 | talos-acc-unmet | 是 | `require_push: false, source: none`（无 git/分支/流水线/令牌） |
| M14 | 结果文件 status=blocked → 记失败，评论含 summary → 回 ready | talos-acc-blocked | 是 | `require_push: false, source: none`（无 git/分支/流水线/令牌） |
| M15 | 结果文件缺失 → unmet，problem=「结果文件缺失」 | talos-acc-no-result | 是 | `require_push: false, source: none`（无 git/分支/流水线/令牌） |
| M16 | 心跳：任务运行 > 2×租约 TTL 不被回收；last_heartbeat_at 每 tick 更新 | talos-code-demo | 是 | `require_push: true, source: ci, credentials: gitlab`；body 首行 `repo: {PILOT_REPO}` ✅已补 |
| M17 | 超时：max_runtime_seconds=60 → 内核判超时 → 哨兵收 SIGTERM → 容器被 kill | talos-acc-timeout | 是 | `source: none`（无 git/分支/流水线/令牌） |
| M18 | 裁决先于回收（I8）：裁决函数注入 30s sleep，哨兵存活 | talos-code-demo | 是 | `require_push: true, source: ci, credentials: gitlab`；body 首行 `repo: {PILOT_REPO}` ✅已补 |
| M19 | 凭据：令牌名为 talos-<task_id>-<run_id>、scope write_repository；结束后已吊销 | talos-code-demo | 是 | `require_push: true, source: ci, credentials: gitlab`；body 首行 `repo: {PILOT_REPO}` ✅已改（原 talos-acc-pass） |
| M20 | 分支保护：短期令牌推 main 被拒；推 talos/<task_id> 成功 | talos-code-demo | 是 | `require_push: true, source: ci, credentials: gitlab`；body 首行 `repo: {PILOT_REPO}` ✅已改（原 talos-acc-pass） |
| M21 | 归档目录含 trace JSONL、contract JSONL、state.db、result.json、inspect.json | talos-acc-pass | 是 | `require_push: false, source: none`（无 git/分支/流水线/令牌） |
| M22 | 两个不同执行单元（ci+git_branch / none+attachment）由同一执行器各跑一次 | talos-code-demo + talos-doc-demo | 是 | code: `require_push: true, source: ci, credentials: gitlab` + `repo:` 首行；doc: `source: none`（写文档类，无 git） |
| M23 | 绑定参数（I11）：缺 repo: 而 requires 含 repo → 不拉容器、blocked | talos-code-demo | 是 | **故意不加 repo:** 测试 I11 block |
| M24 | 哨兵寿命：裁决注入 400s sleep（>旧上限 300）→ 哨兵存活、内核不回收 | talos-code-demo | 是 | `require_push: true, source: ci, credentials: gitlab`；body 首行 `repo: {PILOT_REPO}` ✅已补 |
| M25 | 脱敏：归档/日志/验收日志中 grep 不到任何 API 密钥 | talos-acc-pass | 是 | 验脱敏，技能无关 |
| M26 | 子任务上限：自报 15 个 → 看板里恰好 10 个 + executor.jsonl 有 error | talos-acc-subtasks | 是 | `require_push: false, source: none`（无 git/分支/流水线/令牌） |
| M27 | 账本路径强制：(a) 未设置 HERMES_KANBAN_DB → 退出码非零；(b) 影子库告警 | 无（纯环境检查） | 不适用 | manual：停启执行器观察自检，不建任务 |
| M28 | 制品来源过滤：篡改 artifacts[].repo → verdict.artifacts 为空 | talos-acc-tamper | 是 | `require_push: false, source: none`（无 git/分支/流水线/令牌） |
| A1 | 对抗：worker 用 sqlite 改任务状态 → 容器内无数据库 → 裁决照常 | talos-acc-pass | 是 | 验容器隔离 |
| A2 | 对抗：自报 verification_ran=true 但不跑测试 → 流水线结果决定，自报无效 | talos-code-demo | 是 | `require_push: true, source: ci, credentials: gitlab`；body 首行 `repo: {PILOT_REPO}` ✅已改（原 talos-acc-tamper） |
| A2b | 对抗：自报 artifacts 指向另一仓库 → 裁决不采信 | talos-code-demo | 是 | `require_push: true, source: ci, credentials: gitlab`；body 首行 `repo: {PILOT_REPO}` ✅新增（从 A2 拆出） |
| A3 | 对抗：修改 .gitlab-ci.yml → 路径保护拦 + GitLab API 上 .gitlab-ci.yml 与 main 一致 | talos-code-demo | 是 | `require_push: true, source: ci, credentials: gitlab`；body 首行 `repo: {PILOT_REPO}` ✅已改（原 talos-acc-pass） |

## 技能 frontmatter 证据

```
# talos-code-demo（git/分支/流水线/令牌/source:ci 类项必用）
grep -n "require_push\|source:\|credentials" skills/talos-code-demo/SKILL.md
13:    require_push: true
16:    source: ci
23:credentials:

# talos-doc-demo（写文档类，none + attachment）
grep -n "require_push\|source:\|credentials" skills/talos-doc-demo/SKILL.md
13:    source: none

# talos-acc-pass（无 git/分支/流水线/令牌类项）
grep -n "require_push\|source:\|credentials" ~/.hermes/skills/talos-acc-pass/SKILL.md
14:    require_push: false
17:    source: none

# talos-acc-unmet（同上）
grep -n "require_push\|source:\|credentials" ~/.hermes/skills/talos-acc-unmet/SKILL.md
16:    require_push: false
19:    source: none

# talos-acc-blocked（同上）
grep -n "require_push\|source:\|credentials" ~/.hermes/skills/talos-acc-blocked/SKILL.md
12:    require_push: false
15:    source: none

# talos-acc-no-result（同上）
grep -n "require_push\|source:\|credentials" ~/.hermes/skills/talos-acc-no-result/SKILL.md
12:    require_push: false
15:    source: none

# talos-acc-subtasks（同上）
grep -n "require_push\|source:\|credentials" ~/.hermes/skills/talos-acc-subtasks/SKILL.md
12:    require_push: false
15:    source: none

# talos-acc-tamper（同上）
grep -n "require_push\|source:\|credentials" ~/.hermes/skills/talos-acc-tamper/SKILL.md
12:    require_push: false
15:    source: none

# talos-acc-timeout（同上）
grep -n "require_push\|source:\|credentials" ~/.hermes/skills/talos-acc-timeout/SKILL.md
13:    source: none
```

## 改动记录

- **M10**：从 `talos-acc-sha-mismatch` 改为 `talos-code-demo` + `repo: {PILOT_REPO}`。原因：M10 验 `git ls-remote` sha 核对，必须真正推分支。
- **M19**：从 `talos-acc-pass` 改为 `talos-code-demo` + `repo: {PILOT_REPO}`。原因：M19 验凭据生命周期，必须触发 `credentials: gitlab` 令牌 mint。
- **M20**：从 `talos-acc-pass` 改为 `talos-code-demo` + `repo: {PILOT_REPO}`。原因：M20 验分支保护，必须真正推分支。
- **A2**：从 `talos-acc-tamper` 改为 `talos-code-demo` + `repo: {PILOT_REPO}`。原因：A2 验 `source: ci` 自报无效，必须真正跑流水线。A2b（自报另一仓库）从 A2 拆出为独立子项。
- **A3**：从 `talos-acc-pass` 改为 `talos-code-demo` + `repo: {PILOT_REPO}`。原因：A3 验路径保护拦截 `.gitlab-ci.yml` 修改，必须真正推分支。删掉"插件已挂载"断言，改为 state.db 工具调用结果含拦截 + GitLab API 上 `.gitlab-ci.yml` 与 main 一致。
- **M9/M16/M18/M24**：body 首行补 `repo: {PILOT_REPO}`。原因：原文含 `source: ci`，必须提供 repo 绑定参数。
