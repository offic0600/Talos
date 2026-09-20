# 验收套件技能对照表

## 原则

验收脚本只做夹具（建任务、断言），不当执行器（不调 `tick()`）。
派发与裁决全部由 launchd 里的常驻执行器完成。
新增验收项必须先补本表，再写代码。

## 对照表（v2.3 §13 原文 vs 现用技能）

| 项号 | v2.3 §13 原文要验的契约（一句话） | 现用技能 | 技能声明覆盖 | 备注 |
|------|-----------------------------------|----------|-------------|------|
| M1 | 执行器以服务常驻；kill -9 后 10 秒内被拉起；重启后不重复派发/裁决 | 无（纯人工观察） | N/A | manual：观察 launchd 重启 |
| M2 | ready 任务 → 一个 tick 内被领取并拉起容器，挂载表 = §5.1 白名单，无 kanban 目录 | talos-acc-pass | 是 | verification=none, requires=[] |
| M3 | 容器 env 无 HERMES_KANBAN_*；工具清单无 kanban_* | talos-acc-pass | 是 | 同 M2 |
| M4 | 上下文文件含 hermes kanban context 原文 + 声明摘要 + 收尾要求 | talos-acc-pass | 是 | 同 M2 |
| M5 | 声明 3 个产物只做 2 个 → unmet → 回 ready → 评论含 ⛔ | talos-acc-unmet | 是 | verification=none, requires=[]；body 写死 result.json 缺一个产物 |
| M6 | 承 M5：run#2 上下文含 run#1 的 error → 补做 → pass → done | talos-acc-unmet | 是 | 同 M5，两轮 |
| M7 | 每次裁决恰好一条 [执行器] 评论，author = talos-executor | talos-acc-pass | 是 | 验评论条数 = 裁决次数 |
| M8 | verification.source: ci：流水线 success → 通过；failed → unmet → 重拉 | talos-code-demo | 是 | **已恢复**：ci + git_branch + requires=[repo,branch]；body 首行 `repo: {PILOT_REPO}` |
| M9 | source: ci 且流水线 15 分钟无终态 → defect 超时 → degraded | talos-code-demo | 是 | manual；**缺 repo: 行**（人工执行时补） |
| M10 | git ls-remote 核对 sha 一致才通过；自报错误 sha → unmet | talos-acc-sha-mismatch | 是 | verification=none；body 写死 result.json 含错误 sha |
| M11 | source: evidence 的执行单元：证据账本不可读 → defect 降级 done | 不适用 | N/A | 黑盒下无法无侵入复现；由 tests/test_adjudicate.py 单元测试覆盖 |
| M12 | 声明 frontmatter 非法 → 不拉容器、blocked、评论「校验器故障」 | dd1-broken-skill | 是 | 故意坏掉的 skill（无 git/verification 声明） |
| M13 | 连续 2 次 unmet → blocked（内核熔断），评论「转人工」 | talos-acc-unmet | 是 | max_retries=2；两轮均 unmet |
| M14 | 结果文件 status=blocked → 记失败，评论含 summary → 回 ready | talos-acc-blocked | 是 | verification=none；body 写死 result.json status=blocked |
| M15 | 结果文件缺失 → unmet，problem=「结果文件缺失」 | talos-acc-no-result | 是 | verification=none；不写 result.json |
| M16 | 心跳：任务运行 > 2×租约 TTL 不被回收；last_heartbeat_at 每 tick 更新 | talos-code-demo | 是 | **缺 repo: 行**（长运行任务，人工执行时补） |
| M17 | 超时：max_runtime_seconds=60 → 内核判超时 → 哨兵收 SIGTERM → 容器被 kill | talos-acc-timeout | 是 | verification=none, requires=[]；worker 执行 sleep 120 |
| M18 | 裁决先于回收（I8）：裁决函数注入 30s sleep，哨兵存活 | talos-code-demo | 是 | manual；**缺 repo: 行**（人工执行时补） |
| M19 | 凭据：令牌名为 talos-<task_id>-<run_id>、scope write_repository；结束后已吊销 | talos-acc-pass | 部分 | **技能声明无 gitlab credential**；依赖执行器为 talos-code-demo 类任务 mint token。M19 用 acc-pass 不 mint token → 验的是"无 credential 时不 mint"路径。如需验 mint 路径需改用 talos-code-demo + repo: |
| M20 | 分支保护：短期令牌推 main 被拒；推 talos/<task_id> 成功 | talos-acc-pass | 部分 | 同 M19：用 acc-pass 不触发 gitlab credential mint。验的是 git-credentials 文件是否存在和令牌权限 |
| M21 | 归档目录含 trace JSONL、contract JSONL、state.db、result.json、inspect.json | talos-acc-pass | 是 | 验归档文件清单 |
| M22 | 两个不同执行单元（ci+git_branch / none+attachment）由同一执行器各跑一次 | talos-code-demo + talos-doc-demo | 是 | **已恢复**：code 任务用 talos-code-demo + repo: 首行；doc 任务用 talos-doc-demo (requires=[]) |
| M23 | 绑定参数（I11）：缺 repo: 而 requires 含 repo → 不拉容器、blocked | talos-code-demo | 是 | **故意不加 repo:** 测试 I11 block |
| M24 | 哨兵寿命：裁决注入 400s sleep（>旧上限 300）→ 哨兵存活、内核不回收 | talos-code-demo | 是 | manual；**缺 repo: 行**（人工执行时补） |
| M25 | 脱敏：归档/日志/验收日志中 grep 不到任何 API 密钥 | talos-acc-pass | 是 | 验脱敏，技能无关 |
| M26 | 子任务上限：自报 15 个 → 看板里恰好 10 个 + executor.jsonl 有 error | talos-acc-subtasks | 是 | verification=none, requires=[]；body 写死 15 个子任务 |
| M27 | 账本路径强制：(a) 未设置 HERMES_KANBAN_DB → 退出码非零；(b) 影子库告警 | 无（纯环境检查） | N/A | manual：停启执行器观察自检 |
| M28 | 制品来源过滤：篡改 artifacts[].repo → verdict.artifacts 为空 | talos-acc-tamper | 是 | verification=none；body 写死篡改后的 result.json |
| A1 | 对抗：worker 用 sqlite 改任务状态 → 容器内无数据库 → 裁决照常 | talos-acc-pass | 是 | 验容器隔离 |
| A2 | 对抗：自报 verification_ran=true 但不跑测试 → 流水线结果决定 | talos-acc-tamper | 是 | verification=none；body 写死虚假 self_check |
| A3 | 对抗：修改 .gitlab-ci.yml → 路径保护拦 | talos-acc-pass | 部分 | **技能声明无 git/verification**；验的是容器内路径保护，不依赖技能的 git push |

## 改动记录

- **M8**：从 `talos-acc-pass` 恢复为 `talos-code-demo` + `repo: {PILOT_REPO}` 首行。原因：删 repo: 是 I11 正常行为（M23 专测此场景），不是死锁。
- **M22**：写代码任务从 `talos-acc-pass` 恢复为 `talos-code-demo` + `repo: {PILOT_REPO}` 首行。原因同 M8。
- **M9/M16/M18/M24**：用 `talos-code-demo` 但 body 缺 `repo:` 行——manual 项，人工执行时需补。
- **M19/M20**：用 `talos-acc-pass` 而非 `talos-code-demo`——不触发 gitlab credential mint。如需验完整凭据生命周期，需改用 `talos-code-demo` + `repo:`。
