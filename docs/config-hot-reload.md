# 配置热更新设计（定稿）

> 目标：改参数不重启；**正在做的操作不被打断**，按开始时那一版跑完或安全
> 停下来；改坏了能退回上一版；改版与回退都落审计；当前生效版本与正在用的
> 版本可核对。

## 1. 切换粒度：整套切换（定死，不做逐参数）

一次改版 = 提交**一份完整配置**（与 `config/coke.json` 同构）。

- 先对整份做与启动期完全相同的校验（`PlatformConfig.from_mapping`），任一字段
  非法 → 拒绝整份，当前版不动，不存在"半新半旧"。
- 校验通过后算内容哈希、追加一条新版本、原子落盘，再向现场下发。
- 内容与当前版哈希相同的重复提交是**幂等无操作**：不产生新版本、不写审计。
- 不提供单字段 PATCH；逐参数生效时的跨字段约束（如速度上下限、提前量 < 换向
  间隔）无法保证，故明确禁止。

## 2. 生效时机：按参数类别定（分类表定死）

| 类别 | 字段 | 改版后何时生效 |
|---|---|---|
| `IMMUTABLE` 启动期固定 | `platform.name`、`platform.storage_root` | **不允许热更新**，改动即拒绝（要改必须重启） |
| `LIVE` 即时 | `limits.dust.damper_open_deg`、`damper_rate_deg_per_s`、`linkage_lag_s`；`limits.heating.reversal_interval_s` | 无论是否有操作在跑都立即下发，**下一个控制周期**读新值；在跑联动的起点时刻不改 |
| `DEFERRED_LIVE` 即时但避开在跑操作 | `limits.collector.pressure_target_pa`、`pressure_band_pa`、`regulate_rate_pa_per_s` | 空闲立即生效；有装煤操作在跑时延迟到该操作结束（装煤要按开始版目标带判 `settled`，不能中途换带） |
| `BOUND` 操作配方 | `limits.pusher.*`、`limits.guide.*`、`limits.charger.*`、`limits.quench.*`、`limits.door.*`、`limits.heating.temp_target_c`/`temp_band_c`/`waste_close_lead_s` | **操作开始时冻结到该操作**，整次操作按开始版跑完；操作安全结束后切到当前生效版 |

判定口径在 `coke/configmgr/__init__.py` 的 `classify()` 与模块顶部常量集合
（`IMMUTABLE_PATHS`/`BOUND_SECTIONS`/`BOUND_HEATING_PATHS`/
`DEFERRED_COLLECTOR_PATHS`），改版审计里带每个变更字段的类别
（`change_classes`），可直接核对"这次改版为什么这样生效"。

### 2.1 在跑操作的冻结/补发协议

- 每个操作（`charge`/`push`/`quench`/`reverse`/`clean`）入口
  `ConfigManager.begin_operation(name)`：登记 `name -> 当前版本序号`，该序号
  即本次操作"正在用的版本"。`try/finally` 保证异常也会解除冻结。
- 操作期间机器组件持有冻结版参数，迁移**不回写** BOUND 组件，也不回写
  DEFERRED 的集气管；LIVE 组件照常即时更新（只影响控制循环，不影响在跑操作
  自身的配方判定）。
- 操作出口 `end_operation(name)`：若期间发生过改版（冻结序号 ≠ 当前生效序号），
  统一补发一次 BOUND 配方与集气管参数，随后状态回到 `aligned`。
- 若新参数在物理上不安全（如新最大行程小于当前杆位），操作本身仍按旧版安全
  跑完；是否允许/如何安全停车是现场联锁职责，热更新不主动打断机械动作。

## 3. 版本模型与回退（回退幂等，定死）

版本存档：存储目录下单个原子文档 `config_versions.json`：

```json
{
  "current_seq": 3,
  "versions": [
    {"seq": 1, "parent_seq": 0, "digest": "<sha256>", "kind": "seed",      "config": { ... 完整配置 ... }},
    {"seq": 2, "parent_seq": 1, "digest": "...",        "kind": "migrate", "config": { ... }},
    {"seq": 3, "parent_seq": 2, "digest": "...",        "kind": "rollback","config": { ... }}
  ]
}
```

- 版本序号单调递增；历史**只追加、永不重写/删除**；每版存完整正文，回退不
  依赖外部配置文件。
- 启动时存档存在即以存档当前版为准；不存在则用启动配置作为种子版 v1 落盘。
- **回退语义固定为"撤销最近一次正向迁移"**：以当前版父版本的内容生成一条新
  版本（`kind="rollback"`，新版本序号，父指针指向当前版），因此回退链可一直
  追到种子版，审计链完整。
- **幂等**：
  - 内容哈希相同的迁移 → 无操作，不产生版本/审计；
  - 当前版本身就是回退产物（`kind == "rollback"`）时再次回退，或种子版
    回退 → 无操作，返回当前版并带 `idempotent: true`；
  - 回退后要再回退，必须先有新的正向迁移（回退始终撤销"最近一次"迁移）。
- 每次有效改版/回退的结果同时返回变更点（路径 → before/after）。

## 4. 审计与核对

- 审计走现有事件总线（组件名 `config`），与操作审计同一条流水、同样落盘：
  - `config_migrated`：`seq`、`parent_seq`、`digest`、`kind`、`reason`、
    `changed_paths`、`change_classes`、`changes`、迁移时的 `pinned`；
  - `config_rollback`：同上，另带 `restored_from_seq`；
  - 被拒绝的改版（非法 / 改了 IMMUTABLE 字段）经控制台走统一错误落盘
    （`error`，warning），直接调用则抛 `ConfigError`。
  - 幂等无操作不写审计。
- 操作完成事件（`*_complete`）新增 `config_seq`：该操作实际使用的版本号。
- 控制台 `GET /api/config` 返回：
  - `effective`：当前生效版本摘要；
  - `in_use_seqs`：现场实际在用的版本集合（当前版 ∪ 各在跑操作冻结版）；
  - `pinned`：在跑操作 → 冻结版本号；
  - `state`：`aligned`（在跑操作全部冻结在当前版/无在跑）或 `draining`
    （存在按旧版在跑的操作，等其结束自动对齐）；
  - `consistent`：所有在跑操作的冻结版是否都等于当前生效版；
  - `versions`：版本链（最新在前）与完整 `config` 正文（供"取回当前版→改→
    整版提交"的控制台用法）。
- `GET /api/status` 的 `config` 块带同一份核对信息。
- 版本内容可自证：`effective.digest` 等于存档该版正文的 SHA-256
  （规范 JSON：排序键、无空白）。

## 5. 接口

| 入口 | 说明 |
|---|---|
| `POST /api/config/migrate` | 整版提交；body 必须是完整配置对象。成功 200；非法/改 IMMUTABLE 409 |
| `POST /api/config/rollback` | 回退上一版（幂等） |
| `GET /api/config` | 当前配置正文 + 版本链 + 在跑冻结核对 |
| `coke versions` | 命令行查看版本链与核对状态 |
| `coke migrate --file <new.json>` | 命令行整版热切换 |
| `coke rollback` | 命令行回退（幂等） |

## 6. 典型时序

```
空闲 v1
  migrate(整版)            -> v2，LIVE/BOUND 全部下发，aligned
  begin op("quench")      -> 冻结 v2
  migrate(整版)            -> v3：除尘/换向间隔立即生效；
                             熄焦配方与集气管不动；state=draining
  op 按 v2 跑完 / 安全停车 -> end：补发 v3 配方与集气管，state=aligned
  rollback                -> v4（内容=v2 父版内容，parent=v3，kind=rollback）
  rollback                -> 幂等无操作，停在 v4
```

## 7. 并发与持久化

- 所有版本操作与平台操作共用平台的 `RLock`；`ConfigManager` 内部另有可重入
  锁，迁移与 begin/end 串行化，迁移的下发与落盘在同一临界区内完成。
- `config_versions.json` 走现有 `JsonStore`：临时文件 + `fsync` +
  `os.replace` 原子替换，掉电不留半截版本。
- 重启后以存档当前版恢复，并把生效参数完整重放一遍；操作内存态不跨重启
  （与现有平台计数/换向恢复模型一致）。
