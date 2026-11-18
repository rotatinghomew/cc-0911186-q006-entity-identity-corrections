# 跨来源身份映射修订下的事实归属与冻结口径设计

## 1. 问题与约束

同一学习者在报名（enrollment）、缴费（payment）、学习终端（attendance）使用不同外部标识，
平台通过身份映射把来源事实归并到统一学习者（person）。人工核验会产生四类修订：

| 修订 | 含义 |
| --- | --- |
| correct 修正 | 同一来源标识改挂到另一个 person（原映射录入错误） |
| split 拆分 | 一组标识实际属于两个真实学习者，部分标识改挂新 person |
| merge 合并 | 两个 person 经核验确认为同一人，吸收方保留，被吸收方标记 merged |
| revoke 撤销 | 无法确认来源标识与 person 的关系，映射失效，事实进入待查 |

两个失败极端都不可接受：

- **只改当前映射表**：旧 DWD 事实仍带过时编号，校区报表人数变化无法解释；
- **全量迁移历史事实到新编号**：已结算（冻结）月份被追溯改写，冻结口径失去意义。

因此事实归属必须同时由三条时间线解释：

1. **映射生效时间（valid time，业务时间）**：映射在业务上自何时起有效；
2. **事实产生时间（事实 occurred_at）**：事实在来源端发生的时刻；
3. **报表冻结范围（freeze）**：哪些期间的报表已结算，结算结果连同当时的身份依据一起固化。

## 2. 总体原则

1. **原始事实不可变**：`facts` 只追加/版本化，永不因身份修订被 UPDATE/DELETE 归属编号（来源 retract 事件除外，其本身也是一次可审计事实）。
2. **映射双时间建模（SCD2 + 审计时间）**：每条映射版本带业务有效期 `[valid_from, valid_to)` 与系统记录期 `[record_from, record_to)`。
   业务时间回答“这条映射当时生效吗”，记录时间回答“系统在修订前知道的是哪一版”，支持补录与回溯核验。
3. **归属是解析结果，不是事实字段**：事实只携带来源标识；归属写入仅追加的 `fact_attribution`，
   每次重新解析插入新版本，旧版本保留，天然回答“当时为何归这个人”。
4. **冻结快照固化身份依据**：报表冻结时把每个事实当时采用的映射版本/归属复制到 `frozen_fact_basis`。
   之后映射再怎么修订，冻结期数字与依据不变；未冻结期间可按修订重算。
5. **冲突入待查池，绝不静默合并或丢弃**：来源标识复用、一标多主、核验撤销导致无法唯一确认身份时，
   事实进入 `suspense_entry` 并在报表中单列“待确认”，不分配给任何 person，也不从数据中消失。
6. **合并只允许人工显式发起**：系统任何自动路径都不会把两个 person 合并；merge 修订必须带修订单号并经校验。

## 3. 数据模型（SQLite DDL 对应见 `lakeapp/storage.py`）

### 3.1 统一实体 person

```
person(person_id PK, status: active|merged|pending,
       merged_into, created_at, created_revision_id)
```

### 3.2 修订单 identity_revision（每次人工核验一单，仅追加）

```
identity_revision(
  revision_id PK, action: create|correct|split|merge|revoke,
  reason, payload JSON,              -- 修订的完整入参，审计留痕
  created_at, created_by)
```

### 3.3 映射版本 identity_mapping（双时间 SCD2）

```
identity_mapping(
  mapping_id PK, revision_id FK,
  source, source_key, person_id,
  valid_from, valid_to,              -- 业务有效期，半开区间，9999-12-31 表示至今
  record_from, record_to,            -- 系统记录期，修订落库时关闭旧版本
  status: active|revoked|superseded)
```

- 同一 `(source, source_key)` 当前至多一条 `record_to=9999` 的记录版本；
- 同一来源标识在重叠业务有效期内指向不同 person 即构成冲突，登记到 `mapping_conflict`，不允许自动二选一；
- 来源标识被复用到第二个 person 时，旧 person 的版本必须先闭区间（`valid_to` 截断），
  新版本从复用时刻生效；事实按 `occurred_at` 落在哪个区间来归属，两段历史各自归属正确的人。

### 3.4 冲突登记 mapping_conflict（仅追加，解决前保留）

```
mapping_conflict(conflict_id PK, source, source_key, person_a, person_b,
                 valid_from, valid_to, detected_revision_id,
                 status: open|resolved, resolved_revision_id)
```

### 3.5 事实归属 fact_attribution（仅追加，事实与映射之间的解析轨迹）

```
fact_attribution(
  attribution_id PK,
  source, business_key,              -- 指向 facts
  person_id,                          -- 可空：待确认时不归属
  mapping_id,                         -- 采用的映射版本，可空
  resolution: attributed|conflict|unmapped|revoked|retracted,
  valid_from, valid_to,               -- 本归属版本的系统有效期（当前版 valid_to=9999）
  reason,                             -- 触发原因：ingest / revision-xxx
  created_at, revision_id)
```

同一事实的当前归属只有一条 `valid_to=9999`；修订后关闭旧行、插入新行，旧行即“当时依据”。

### 3.6 待查池 suspense_entry

```
suspense_entry(entry_id PK, source, business_key,
               reason: unmapped|conflict|revoked,
               opened_at, opened_revision_id,
               cleared_at, cleared_revision_id, cleared_person_id)
```

### 3.7 冻结报表 report_freeze 与 frozen_fact_basis

```
report_freeze(report_name, period, campus_id, frozen_at, revision_id, manifest_version PK 组)
frozen_fact_basis(report_name, period, campus_id, source, business_key,
                  person_id, mapping_id, resolution, frozen_at, revision_id)
```

冻结动作把当时的归属解析结果逐行复制固化；`manifests` 中的冻结快照附 `as_of_revision_id`。

## 4. 归属解析规则

对事实 `f`（来源 `s`、标识 `k`、发生时间 `t=occurred_at`）：

1. 取 `(s,k)` 全部映射版本，要求 `valid_from <= t < valid_to`（业务时间命中）；
2. 当前解析还要求 `record_to=9999...` 且 `status=active`（系统当前认知）；
3. 命中 0 条 → `unmapped` → 进待查池，不归属 person；
4. 命中 >1 且指向不同 person → `conflict` → 登记/引用冲突单，进待查池；
5. 命中映射状态为 `revoked` → `revoked` → 进待查池；
6. 唯一命中 active 映射 → `attributed`，写 `fact_attribution(person_id, mapping_id)`。

“按修订时点回看”（as-of）解析：把第 2 步替换为 `record_from <= as_of < record_to`，
用于回答“某修订落库那一刻，这条事实归谁”。冻结快照本质就是冻结时刻的 as-of 解析固化。

## 5. 修订处理与影响面

一次修订在单事务内完成：写 `identity_revision` → 开闭映射版本（merge/split 同时调整 person 状态）→
对业务有效期与事实发生时间仍有交集的事实重新解析，追加 `fact_attribution` 新版本、刷新待查池。

影响面查询 `revision_impact(revision_id)`：

- 取该修订触发的全部归属变化（新旧 person 不同、或 attributed↔待查之间变化）；
- 按事实发生时间归月、结合校区得到受影响的 `(report_name, period, campus_id)`；
- 对照 `report_freeze` 标注每项 **frozen（不可重算，冻结依据保留）/ open（可按修订重算）**；
- 输出人数与金额的前后对比，解释校区报表人数变化。

## 6. 冻结与重算

- `freeze_report`：校验期间未冻结 → 按当前归属汇总 → 写 `report_freeze`、逐行写 `frozen_fact_basis`、发布带 `as_of_revision_id` 的 manifest。
- `recompute_report`：期间已冻结则拒绝并返回冻结修订号；未冻结则按当前归属重新汇总并发布新版本 manifest。
- 冻结后到达的修订只改变未冻结期间；冻结期间若被追问，直接回读 `frozen_fact_basis` 展示“当时采用的身份依据”。

## 7. 血缘查询

`explain_identity(source, business_key)` 返回：

1. 事实当前归属与全部历史归属版本（person、映射版本、resolution、触发修订、时间）；
2. 被采用映射版本的完整信息及其修订单（action、reason、操作人、时间）；
3. 待查/冲突记录（若有）；
4. 该事实出现过的冻结报表及冻结时依据。

即对一条事实可以完整回放“何时、因为哪次核验、依据哪条映射、归到了谁”。

## 8. 关键安全性质（测试对应）

| 性质 | 保障机制 | 测试 |
| --- | --- | --- |
| 不合并两个真实学习者 | 重叠有效期指向不同 person 即冲突入池；merge 仅人工显式发起 | `test_conflicting_mappings_go_to_suspense`、`test_manual_merge_is_explicit` |
| 身份不明的事实不消失 | unmapped/conflict/revoked 一律进 suspense 且报表单列 | `test_revoked_mapping_keeps_fact_in_suspense` |
| 冻结期不被追溯改写 | freeze 固化 basis，recompute 拒绝冻结期 | `test_frozen_report_keeps_identity_basis` |
| 未冻结期可重算且变化可解释 | 归属仅追加 + revision_impact | `test_correction_recomputes_open_period` |
| 标识复用不串人 | 业务有效期截断 + 按 occurred_at 区间解析 | `test_reused_source_key_resolves_by_occurred_time` |
| 归属可回放 | fact_attribution 全版本 + 修订单 + 冻结 basis | `test_explain_identity_lineage` |
