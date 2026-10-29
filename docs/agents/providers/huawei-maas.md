# Provider: huawei-maas (华为 ModelArts MaaS)

> 实测记录,2026-10-29(wave-0 CN 探针包,fnos 生产容器内执行,只读)。状态:**已落地、未排期**(降级路径)。

## 端点与判定门(实测)

Base: `https://api.modelarts-maas.com/openai/v1`(Bearer 鉴权,OpenAI 兼容)。

| 探针 | HTTP | 响应体 |
|---|---|---|
| `GET /openai/v1/models` 无 key | 400 | `{"error":{"code":"ModelArts.81002","message":"Failed to get the authorization header."}}` |
| `GET /openai/v1/models` 坏 key (`sk-0000…`) | 401 | `{"error":{"code":"ModelArts.81003","message":"Invalid authorization header."}}` |

鉴权前置、无 key 与坏 key 分属两个错误码(81002 / 81003),门是干净的。magpie
`internal/provider/presets.go` 交叉印证:huaweicloud 的 pay-as-you-go region 正是
`https://api.modelarts-maas.com/openai/v1`(另有 `plan` region `/plan/v2`)。

## 判定路由(provider/huawei_maas.py)

校验走继承的最小 chat completion(base + `/chat/completions`)。`_judge` 的华为特定分流:

- **401 ModelArts.81003** → INVALID_KEY(坏键,实测)。
- **400 ModelArts.81002** → SERVER_ERROR(retryable → wait-check),**永不** INVALID_KEY。
  这是"没带头"的门工件,对键没有任何判定信息;判 invalid 会把活键永久烧掉。
- **404** → NO_MODEL(探测模型下架/未知,wait-check,不烧键)。探测模型 id
  `DeepSeek-V3.1` 是 **UNVERIFIED**——wave-0 没有真键可列 `/models`,试点 run
  必须用活键目录复核。
- **不可解析体**(captive HTML、截断出口)→ SERVER_ERROR,永不 UNKNOWN(永久桶)。

## 语料污染发现(wave-0,GitHub code-search 抽样 10 行/查询)

| 查询 | total_count | 10 行抽样真实键字面量 |
|---|---|---|
| `MAAS_API_KEY` | 2,728 | **0** |
| `modelarts-maas` | 1,066 | **0**(唯一带值行是 `api_key = "MAAS_API_KEY"` 占位串) |
| `modelarts-maas.com` | 1,058 | 0 |

`MAAS_API_KEY` 锚是污染的:抽样命中 Canonical Ubuntu MAAS(OAuth1
`consumer_key:token_key:token_secret` 三元组)、NVIDIA deepops、讯飞星辰 MaaS
(`MAAS_APP_ID`)。收窄到域名锚后 1,058 条命中仍是文档/配置/repo 名,**零真实键字面量**
→ **键形无法确定**。

因此 pattern 设计:env 锚定(`MAAS_API_KEY`/`HUAWEI_MAAS_API_KEY`)+ 宽体类
`[A-Za-z0-9_\-]{20,}` + 占位符 lookahead;域名锚(`api.modelarts-maas.com`)查询带
Bearer/引号变体的宽 pattern——域名锚才是真正的过滤器(host-named-dork 先例)。
task 级查询里的 `MAAS_API_KEY` 一律带 `"modelarts"`/`"modelarts-maas"` 限定词。

## 免费额度:UNVERIFIED

`support.huaweicloud.com/Token-plan-maas/tokenplan-maas-0001.html`(200,页面更新时间
2026-10-28)的 Token Plan 个人版套餐明写「套餐额度仅在AI工具中生效,**不可用于API调用**」,
且页面内「免费额度/免费token/试用额度/体验额度」命中数全为 0;按量计费侧的免费试用
条款未取到(站点改版 404)。**免费档供给证据缺失。**

## 降级决定与理由

按计划 todo 7 的降级出口(「语料复测仍 <500 有效命中或键形无法确定 → 只做
provider+测试+config,不加种子」),huawei-maas 走降级路径:

- 已交付:`provider/huawei_maas.py`、`examples/config-huawei-maas.yaml`、两个测试文件、本页。
- **未加** `web/scheduler.py` 种子行。
- 备用路径(若未来要补证):MaaS 控制台
  `console.huaweicloud.com/modelarts/?#/model-studio/authmanage` 需登录;更可靠的做法是
  试点时拿一把真键打 `/openai/v1/models`(已证明真鉴权)取回目录复核探测模型 id 与键形。
