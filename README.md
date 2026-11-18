# 跨来源实体映射修订下的事实归属与冻结口径

报名、缴费和学习终端的实体映射、事实归属和冻结报表基线。

仓库内的实现使用 Python 3.11 标准库和 SQLite，数据只写入命令行指定的本地数据库及导出目录。
`lakeapp.storage` 保存原始接收、业务状态、双时间映射版本和冻结依据；`lakeapp.contract` 负责输入约定；
`lakeapp.ingest` 接收来源批次；`lakeapp.derive` 生成当前基线支持的事实并触发归属解析；
`lakeapp.identity` 管理映射版本、人工核验修订与归属解析；`lakeapp.attribution` 落地仅追加的归属轨迹和待查池；
`lakeapp.freeze` 处理报表冻结与未冻结期间重算；`lakeapp.query` 提供身份血缘与修订影响面查询；
`lakeapp.publish` 读取并写出可追溯版本；`lakeapp.cli` 提供本地操作入口。

## 运行

在 Python 3.11 环境中执行 `python3 -m unittest discover -s tests -v` 运行测试，执行
`python3 -m compileall -q lakeapp tests` 检查构建和语法。示例数据是本地合成记录，不依赖外部服务或联网。

## 现有契约

输入批次包含来源名、批次标识、游标和记录数组。原始记录以 JSON 保存，重复批次不会再次产生事实；
业务键相同但内容不同的记录会保留冲突信息。事实查询按业务日期和版本清单读取，清单中的文件摘要与行数用于发现导出内容被替换。
数据库初始化由运行入口完成，已有数据不会因重新打开而丢失。

## 身份映射与事实归属口径

设计细节见 [`docs/design.md`](docs/design.md)，要点：

- **双时间映射（SCD2）**：`identity_mapping` 同时记录业务有效期 `valid_from/valid_to` 和系统记录期
  `record_from/record_to`。事实按 `occurred_at` 命中业务有效期，修订落库只开闭记录期，不覆盖旧版本。
- **事实不可变、归属仅追加**：`facts` 不随身份修订改写；每次解析结果写入新的 `fact_attribution` 行，
  旧行保留，可回放一条事实当时归谁、依据哪条映射和哪次核验。
- **修订动作**：`create / correct / split / merge / revoke` 均带修订单（`identity_revision`）并在单事务内
  开闭映射版本、重算受影响事实。合并只能人工显式发起。
- **冲突与待查**：来源标识复用导致的一标多主登记 `mapping_conflict`；无法唯一确认身份的事实
  （unmapped/conflict/revoked）进入 `suspense_entry`，不归属任何 person 也不从数据消失，报表中单列待确认。
- **冻结与重算**：`freeze_report` 把当时每个事实的归属依据逐行固化到 `frozen_fact_basis`；
  之后的修订只影响未冻结期间，冻结期 `recompute_report` 会被拒绝，历史结算数字与身份依据不变。
- **影响面**：`revision_impact` 列出一次修订改变的事实归属，并逐项标注落在冻结（不可重算）还是
  可重算的报表期间，解释校区报表人数变化。

## CLI 动作

`receive / facts / progress / publish / read` 为基线动作；新增：

| 动作 | 用途 |
| --- | --- |
| `person` | 登记统一学习者 |
| `revision` | 提交一次映射修订（action + payload + reason） |
| `revisions` | 列出全部修订单 |
| `conflicts` | 查看一标多主冲突登记 |
| `suspense` | 查看待查池中身份未确认的事实 |
| `explain` | 回放一条事实的完整身份血缘 |
| `impact` | 查询一次修订影响哪些冻结/可重算报表 |
| `freeze` | 冻结期间报表并固化身份依据 |
| `recompute` | 重算未冻结期间报表（冻结期拒绝） |
| `frozen-basis` | 回读冻结时逐行采用的身份依据 |

## 目录职责

`storage.py` 事务和表结构，`contract.py` 记录约束，`ingest.py` 接收和来源进度，
`derive.py` 从原始记录生成事实并触发归属，`identity.py` 双时间映射、修订与解析，
`attribution.py` 归属轨迹与待查池，`freeze.py` 冻结与重算，`query.py` 血缘与影响面，
`publish.py` 版本清单与回读，`cli.py` 把这些业务操作组合给本地使用者。
测试覆盖正常接收、重复批次、版本读取，以及修正重算、冻结保留、标识复用、冲突待查、
撤销不丢事实、人工合并、拆分和血缘回放等安全性质。
