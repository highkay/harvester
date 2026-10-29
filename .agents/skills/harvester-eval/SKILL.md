---
name: harvester-eval
description: |
  综合评估 harvester 采集管线的三大维度：搜索范围（search_types）、产出（valid keys）、定时任务频率（cron）。
  用于判断「砍/恢复 search_types 是否影响产出」「运行周期（单轮时长 vs 调度间隔）是否过载」「调度频率是否合理」，
  并给出「撤销/保持 search_types」和「降频到多少」的具体动作。
  当用户提到「评估产出、搜索范围、定时任务频率、运行周期、harvester 评估、扫描优化、频率优化、
  yield evaluation、search scope、frequency tuning、scan optimization、评估一下效果」时使用本技能。
---

# harvester-eval — 采集管线综合评估

对 harvester（GitHub API key 采集管线，跑在 `admin@192.168.1.18`，DB 在容器 `harvester-web` 内）
做三维评估：搜索范围、产出、定时频率。核心目标是回答三个问题：

1. 搜索范围（`[code]` vs `[code, issues, commits]`）是否最优？
2. 产出是否随频率增长（还是高频只重复挖同一批 key）？
3. 调度频率是否与运行时长/吞吐匹配（是否过载）？

## 数据源与访问

```bash
ssh admin@192.168.1.18
cd /home/admin/harvester

# 跑 SQL（把 SQL 放进 python -c，注意转义）
docker compose exec -T harvester-web python -c "import sqlite3; c=sqlite3.connect('/app/data/harvester.db'); ..."

# 调度 API
curl -s http://127.0.0.1:8002/api/schedule \
  -H "Authorization: Bearer $(grep -E '^WEB_AUTH_KEY=' .env | cut -d= -f2-)"
```

关键表：
- `run_records`：`provider_name, status, valid_keys_found, started_at, finished_at, duration_seconds, config_file`
- `push_logs`：`provider_name, status, keys_count, added_count, ignored_count, error_message`
- `provider_group_mapping` / `gpt_load_config`：推送目标（provider → gpt-load group）

## 铁律（本技能的核心教训，必须先读）

1. **`valid-keys.txt` 是部分快照，不是最终结果**。check 阶段在运行后期还会陆续找到 key，文件不是实时刷新的。
   判断产出**必须用 `run_records.valid_keys_found`**（run 完成后才落库），否则会误判（曾因此误报「掉 53%」）。
2. **`run_records.started_at` 是 UTC**，本地时间 = UTC + 8。
3. **运行配置是快照**：`/app/data/runtime/config-<provider>-<uuid>.yaml`，在 run 启动时从 `examples/` 生成。
   改 `examples/` 不影响正在跑的 run，只影响下一次 run。
4. **`valid_keys_found` 是跨任务合计**：例如 qwen-cn 的 valid=30 包含 qwen-intl 的 key
   （push_logs 里会拆成 qwen-cn=14 + qwen-intl=16 两行）。

## 第一部分：搜索范围评估（search_types）

判断 `[code]` vs `[code, issues, commits]` 哪个更好。

### A/B 方法
1. 改某 provider 的 `search_types`（`examples/config-<p>.yaml`）。
2. `docker compose cp` 进容器，触发一次完整 run。
3. **等它 `completed`（不是 running）**，用 `run_records.valid_keys_found` 对比基线。

### 实测结论（可直接复用，不必重测）
- **砍到 `[code]` 不掉产出**：qwen-cn [code]=30 vs 全配置=28/30；deepseek 3 vs 3。
- 砍 issues/commits 省 **3 倍查询数**（84→28 查询），但**页面数只降 ~25%**（code 搜索翻页更多）。
- **gather 才是瓶颈，不是 search**：砍搜索类型不会缩短运行时长（时长由并发 worker 决定）。

### 判定
- 若 `[code]` 产出 == 全配置产出 → 用 `[code]`（省搜索配额、无损失）。
- **只有实测证明某 provider 掉产出时，才恢复 `[code, issues, commits]`**。不要凭「理论上 issues/commits 有 key」就恢复。

## 第二部分：产出评估（yield）

判断产出是稳定、增长还是衰减，以及频率是否带来新增。

### 方法
1. 拉每个 provider 的 `valid_keys_found` 时间序列。
2. 看趋势：稳定 / 衰减 / 饱和。
3. 看 push_logs 的 `added` vs `ignored`：
   - `ignored` 占大头 = 重复挖同一批 key（没新增）。
   - `added` 少 = 新增 key 少。

### 判定
- 稳定 + `ignored` 高 → **key 不 churn，高频无意义**。
- 衰减（如 tavily 91→48→23→19）→ 资源枯竭，降频或停挖。

## 第三部分：频率评估（cron）

判断调度频率是否与运行时长、吞吐匹配。

### 方法
1. 拉每个 provider 的 run 时长（`duration_seconds`）。
2. 对比调度间隔（cron）。
3. 数并发 run 数（`status='running'` 的行数）。
4. 算吞吐：gather ~500 links/min（4 worker），单个 `[code]` run ~74k 链接 ≈ 2.5h 独占。

### 判定
- **时长 >> 间隔**（如 10h run / 4h 间隔 = 2.5x）→ 并发过载，run 共享 worker 互相拖慢
  （单轮从早期 4-5h 恶化到 8-13h）。
- 并发 run 越多，单轮越慢（固定 4 gather / 2 check / 1 search worker 被瓜分）。
- key churn 以「天」计 → **daily 通常足够**（吞吐算下来 daily 负载 ~60%，12h 已 ~120% 过载）。

### 优化动作（按优先级）
1. 全体回 `[code]`（省搜索、减 gather 链接 25%）。
2. 降频：每 4h → 每 12h 或 daily，错峰。
3. 治本（需改代码）：串行化 run（一次一个 provider），或加 gather worker。

## 常用查询（可直接套用）

```sql
-- 运行历史（时长+产出，权威数据）
SELECT provider_name, valid_keys_found, duration_seconds, started_at
FROM run_records WHERE status='completed' ORDER BY started_at DESC LIMIT 20;

-- 当前并发 run
SELECT provider_name, started_at FROM run_records WHERE status='running' ORDER BY started_at;

-- 推送结果（added vs ignored + 失败原因）
SELECT provider_name, status, keys_count, added_count, ignored_count, error_message
FROM push_logs ORDER BY id DESC LIMIT 20;
```

## 评估输出模板

每次评估结束时，产出三张表 + 一个动作清单：

1. **search_types 对比**：`[code]` vs 全配置的 valid 数 → 决定撤销/保持。
2. **产出趋势**：每 provider 的 valid 时间序列 + push added/ignored → 决定是否降频/停挖。
3. **频率健康度**：时长 vs 间隔 vs 并发数 → 决定新 cron。

动作清单直接给：改哪些 config 的 search_types、cron 改成什么（`0 1,13 * * *` 这种具体值）、
以及「是否要 git revert / commit」。
