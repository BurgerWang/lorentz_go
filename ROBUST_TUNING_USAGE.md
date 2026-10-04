# robust-v1 离线研究入口

本版本交付工程和不可变准备产物。合成 Go 验证只证明接口、预算、因果边界和账本复算；真实批次均按对应授权执行。当前获批1h R0/B1已完成并通过独立结果审计，合计43 Go/345账本/0trial；1h W1另获批准并已执行结束：3072attempts/3076 Go/36870消费账本额度（含924失败预留），成功新Go对应35946账本。两个Classic候选各邻域19/24未达20/24，无合格主/seed，终态独立研究核算与审计关闭，无阻断；W1弃权0笔已使本版F/Q晋级条件无法满足。W2–W4/F/Q和其他周期未执行、未获授权。现有 S1、候选、旧引擎和原数据不被覆盖。没有实盘、下单、下载或自动恢复旧 study。

`robust_pipeline.py --help` 的实际子命令为 `prepare`、`run`、`report`、`freeze`、`append`。支持 15m、1h、4h、1d；每周期独立合同和预算。旧 1h S1 的 R0 只适用于 1h，其他周期不能继承旧候选成绩。

## 数据和准备

完整数据 bundle 须包含 OHLCV、独立 D1、真实资金费率、资金费 mark 来源和预先冻结的代理压力情景。2021 起的多年份训练需要 `proxy-stress-v1`，使用 central/proxy-adverse；2024 后外层和 R0 仅计分 exact 记录。压力情景不是证明过的误差界。扩展包最早一笔代理缺前一已闭合 8h 桶，该点不能计分。

本次四周期 bundle 位于 `data/ETHUSDT-robust-v1/{15m,1h,4h,1d}`，各自对应 `results/robust-v1/preparation-v3/ETHUSDT/<interval>`。1h 使用新路径 `data/ETHUSDT-robust-v1/1h`：D1 和 funding 来自 `data/ETHUSDT-expanded`，目标 1h candles 来自 `data/ETHUSDT`。初次15m准备产物 `results/robust-v1/ETHUSDT/15m` 保留，因UTC日期适配修正而被 v2 取代。v2 的四周期目录也保留，因补齐冻结合同的完整helper/依赖及运行时身份、冻结旧bundle身份核对而由当前 v3 取代；不能用已失配的旧 manifest 恢复。原路径保持不变。bundle 的 `sources` 保存实际来源和哈希，`quality`/`profile` 保存覆盖、重叠说明和 native-bars 单位。

```bash
.venv/bin/python tuning/robust_pipeline.py prepare \
  --protocol tuning/protocols/robust-v1.json \
  --interval 1h --dataset data/ETHUSDT-robust-v1/1h \
  --out results/robust-v1/preparation-v3/ETHUSDT/1h --inspect
```

以上参数已经完成准备，复建时必须选择另一个新输出目录。现有冻结计划可直接只读检查：

```bash
bin/lorentz-robust-v1 robust-eval \
  -plan results/robust-v1/preparation-v3/ETHUSDT/1h/prepared/R0/fixed-Classic101.plan.json \
  -config results/robust-v1/preparation-v3/ETHUSDT/1h/prepared/R0/fixed-Classic101.config.json -inspect
```

`prepare` 独占创建新目录，已有目录会被拒绝。它保存完整 `manifest.json`、`inspect.json`、`preparation.json` 和未批准的 `approval-template.json`。Go inspect 验证各计划、配置、数据和账本数量，不发送策略评价请求。未来窗口、公共控制、目标、门槛、seed、研究上限、运行时、源码、二进制和 bundle 身份一并冻结。修改其中任一依赖须创建新合同，不能直接恢复原目录。

R0 的固定清单含 Classic101/121/228、aligned213/215 和六个旧固定控制；保留 2023 目标初始化、2020 D1 初始化，2025 全年一次连续账户、成本 1/1.5/2。另有 Classic101、aligned213 独立重放及两者旧/新引擎原 2024 三折对照，共 0 trial、17 Go 请求、51 账本。数据前缀已核对不等于引擎已经兼容；迁移记录在预算内旧/新实际对照前保持 `pending-runtime-comparison`，旧成绩不自动继承。

B1 含两族×六组的 12 个完整代表配置，初评和独立重放共 24 次，每次六半年折×两个 funding 情景×1.5 成本；另两个真正公开默认控制在 2025 exact 三成本连续评价。共 0 trial、26 Go 请求、294 账本。分支涵盖 full_history、动态退出、D1、偏离、回调、risk、年龄；控制 aligned 仅改公开默认族和算法，未借用 2024 S1 的 aligned-control。

## 明确批准与执行

`approval-template.json` 默认 `approved: false`，本身不授权。用户审定完整 manifest、允许批次、exact/proxy-stress 口径和预算后，在单独批准文件中填入 `authorized_by: "user"`、实际授权说明 `authorization_reference`、`approved: true` 和允许的 `batches`。`manifest_sha256` 和获批批次的完整预算与 funding 口径必须精确匹配模板；`funding_modes` 是逐批映射，R0 为 `["exact"]`，B1 为 `["exact", "proxy-stress-v1"]`。不要以文件存在、保存计划或一次合成测试代替真实研究授权。

```bash
.venv/bin/python tuning/robust_pipeline.py run \
  --run results/robust-v1/preparation-v3/ETHUSDT/1h --batch R0 --approval results/robust-v1/preparation-v3/ETHUSDT/1h/approval-template.json
.venv/bin/python tuning/robust_pipeline.py run \
  --run results/robust-v1/preparation-v3/ETHUSDT/1h --batch B1 --approval results/robust-v1/preparation-v3/ETHUSDT/1h/approval-template.json
```

以上设计示例使用未批准模板，会拒绝执行；模板仍保持未批准。用户随后明确批准1h R0/B1，单独文件 `results/robust-v1/preparation-v3/ETHUSDT/1h/approval-R0-B1-20261002.json` 仅包含这两批。已执行命令为：

```bash
.venv/bin/python tuning/robust_pipeline.py run \
  --run results/robust-v1/preparation-v3/ETHUSDT/1h --batch R0 \
  --approval results/robust-v1/preparation-v3/ETHUSDT/1h/approval-R0-B1-20261002.json
.venv/bin/python tuning/robust_pipeline.py run \
  --run results/robust-v1/preparation-v3/ETHUSDT/1h --batch B1 \
  --approval results/robust-v1/preparation-v3/ETHUSDT/1h/approval-R0-B1-20261002.json
```

两批已经完成，不需重复执行；上述批准文件不能启动W/F/Q或其他周期。批准一次匹配批次后可在硬上限内连续执行，不逐 trial 请求。每次评价先在 `execution/research-budget.sqlite` 原子预留；失败、超时、未知完成都消费对应上限，不自动重试。执行产物存在但预算库丢失会拒绝恢复，禁止自动重建计数。独立重放强制真实发送；普通完全同合同成功缓存可复用，但不是新市场样本，省下额度不用于额外研究。身份、数据、协议或独立现金对账故障停止整个研究；正常权益保护失败计 FAIL。

逐 outer 入口 `--batch W1`、`W2`、`W3`、`W4` 实际串接该截止的完整搜索、local、exact outer，每批 4608 尝试/4765 请求/57087 账本；批准 W1 不会启动 W2–W4。`--batch F1` 实际串接最终搜索/local/Q，共4608/4764/57084。聚合研究上限仍由同一持久预算约束。

当前1h W1批准文件为 `results/robust-v1/preparation-v3/ETHUSDT/1h/approval-W1-20261002.json`，仅授权W1上述预算与训练proxy-stress-v1、外层exact。已经执行的完整命令如下；作业于2026-10-03 04:16:29 Asia/Bangkok退出0，勿重复执行或用剩余额度扩搜。作业句柄与恢复检查见 `task.md`，子阶段summary及W1总summary已生成；W1的result_status=partial指四个outer只完成W1，不能误称其他三窗已评价或整套回顾门槛已通过。

```bash
.venv/bin/python -u tuning/robust_pipeline.py run \
  --run results/robust-v1/preparation-v3/ETHUSDT/1h --batch W1 \
  --approval results/robust-v1/preparation-v3/ETHUSDT/1h/approval-W1-20261002.json
```

其余分阶段批次同样使用 `run --run ... --batch <批次> --approval <批准文件>`：

| 批次 | 尝试 / Go 请求 / 账本上限 | 依赖 |
| --- | --- | --- |
| W-search | 18432 / 18432 / 221184 | B1 成功，1h 的 R0 兼容对照完成 |
| W-local | 0 / 608 / 7104 | 对应完整 W1–W4 搜索 |
| W-outer | 0 / 20 / 60 | 对应内层检查，禁止外层重选 |
| F-search | 4608 / 4608 / 55296 | W1–W4 回顾门槛通过 |
| F-local | 0 / 152 / 1776 | F1 完整搜索 |
| Q2026 | 0 / 4 / 12 | F1 主候选/挑战者预先冻结 |

搜索实际调用 Optuna 和同一 Go 评价器，每族三 seed，C1 六组各64、C2 192、C3 192，共768。`--until-attempts N` 是持久化检查点，不增加预算、不放宽门槛。每个截止用截止前历史编译 profile，父来源递归检查 cutoff。邻域先冻结全部合法点，普通失败保留在分母，不补点、不以邻域赢家替换中央候选。W 外层半年付费平仓、下窗空仓、资金接续，全部实际执行事件复算连续 DD（观察到的执行边界与已闭合 bar 全路径，不声称不可见的真实逐 tick DD）；这属于 `scheduled-flat-v1`，与 M6 跨窗持仓和最终固定账户分开报告。主流程及三个 seed（弃权现金）均保存。

## 报告和未来

```bash
.venv/bin/python tuning/robust_pipeline.py report \
  --run results/robust-v1/preparation-v3/ETHUSDT/1h --batch R0
```

`report` 只查看已经保存的结果，不重新评价行情，也不对全批次重新独立复算；1h R0/B1已经有完整结果，可以读取。结果在 `execution/<批次>/summary.json`；逐笔、funding、执行边界和收盘事件在相邻 `traces`。报告区分工程合成、已暴露回顾、真正冻结后未来；逐账户披露成本、funding、年度/季度、长短、持仓/空仓、Top3/Top5 正收益贡献、滚动 30/90/180 日、邻域和全部 seed。收益集中诊断不虚拟删除交易，不称为新可执行收益。

完整共同 UTC 日网格包括空仓日。固定 stationary bootstrap 为 5000 次、期望块长 7/14/28 日、seed `20261002+L`、Type7 分位数；候选/控制/成本共用索引，保存运行时与索引身份。统计限5000日/32条共同路径；每次策略发送前保守估计 trace 上限4GiB、剩余存储至少8GiB，不满足即停止，不自动删除证据。主流程绝对基准/1.5倍六个单侧95%下界须全部正；相对控制与2倍仅报告。训练连续路径 p95 持仓超过28日，或29–56阶 ACF 连续三阶超 `2/sqrt(N)` 时阻止通过。该条件统计不消除自适应搜索选择偏差，不是未来获利概率。

仅 Q2026 固定主候选通过后才能冻结未来：

```bash
.venv/bin/python tuning/robust_pipeline.py freeze \
  --run results/robust-v1/preparation-v3/ETHUSDT/1h --out results/robust-v1/preparation-v3/ETHUSDT/1h/future-freeze.json
```

起点是实际冻结后第一个尚未暴露完整 UTC 日，严格晚于实际时刻并保留至少 2026-10-02 的历史暴露边界。CLI 不接受倒填冻结时间。终点为起点后12个日历月，至少50笔、从冻结起点连续四个三日历月季度至少3/4正、三成本/DD及相同六个 bootstrap 下界；不足样本保持未完成，不可择盈利日期提前通过。

追加新 bundle 时不覆盖旧包，逐条检查整个历史前缀、指标初始化、D1/funding 和冻结引擎/接口。`append --frozen <冻结文件> --dataset <新包> --attestation <用户用途声明> --out <新目录>` 只准备新合同；声明须绑定冻结哈希、`authorized_by: "user"`、`data_use: "prospective"`、`no_post_freeze_parameter_changes: true`、`no_selection_on_future_segment: true`。未到完整终点、部分日、旧暴露 final 或修改历史前缀都会拒绝。新未来批次仍需单独批准 `run --batch future`，上限3请求/9账本。没有定时器，不承诺无人值守运行。

## 已执行验证

工程验证使用隔离生成的合成行情与真实 Go 二进制，覆盖报告/现金接续、费用和资金费复算、共享 bootstrap、依赖预警、不可变冻结、未到终点拒绝、批准绑定和代表配置合法性。运行：

```bash
.venv/bin/python -m unittest discover -s tuning -p 'test_robust_delivery.py' -v
```

准备阶段的 `preparation.json` 与 `inspect.json` 仍记录0策略评价；后续实际使用见1h的 `execution/research-budget.sqlite` 与两批 `summary.json`：R0 17Go/51账本、B1 26Go/294账本，0trial、0失败/未知/缓存替代，独立研究与审计关闭。R0五Pareto三成本均亏损，旧control52通过2025初筛，不能直接采用；预定Classic101/aligned213兼容与重放通过。B1 12组重放通过，墙钟443.67s、Go请求累计352.10s；load是会话初始化，不能逐响应相加。RSS观测是waited-child统计，未验证并发或同时总内存。R0有45份新trace加6个无trace legacy账本，B1有294份trace。全部结果为暴露历史回顾，真正未来验证尚未执行。
