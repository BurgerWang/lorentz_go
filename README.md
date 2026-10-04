# Lorentzian Classification 的 Go 复现与研究

研究对象是 Binance ETHUSDT USDT 永续，支持 15m、1h、4h、1d。原始 `lorentz_pine.md` 保持不变；原版计算逻辑与改进候选分别运行。当前只提供已收盘 K 线的离线研究，不连接账户或下单。

增强 Optuna 工程的两族六组条件空间、协议2控制器、来源分组及固定连续账户入口见 [ENHANCED_OPTUNA_USAGE.md](ENHANCED_OPTUNA_USAGE.md)。独立二进制为 `bin/lorentz-enhanced-optuna`；旧控制器与二进制保留。B0 已完成20请求/60账本/0搜索；1h S1 已完成512次尝试、509次Go评价/1527次账本，仅为2024开发候选。原 S2/S3/V1 未执行，默认推进顺序由 [ROBUST_TUNING_PLAN.md](ROBUST_TUNING_PLAN.md) 取代。

新增稳健离线工程使用独立 `bin/lorentz-robust-v1` 和协议3，覆盖四周期数据 bundle、明确的 exact/proxy-stress、可独立复算的完整执行事件、三 seed 分层搜索、全局预算恢复、半年计划平仓流程和固定参数未来验证。P1–P4 已通过合成 Go/Python 联动及逐阶段独立审计；四周期准备完成181项 inspect。随后获批的1h R0/B1已完成43次Go/345次账本/0trial，独立结果审计关闭：五个S1候选均未通过2025门槛，旧trial52控制仅通过初筛，B1接入/重放通过。1h W1 已执行结束，两个Classic开发候选均邻域19/24未达20/24，暂无合格主/seed，终态独立研究核算与审计关闭，无阻断；W1弃权0笔已使本版F/Q晋级条件无法满足；W2–W4/F/Q和其他周期未执行、未获授权。完整 CLI 和实际文件见 [ROBUST_TUNING_USAGE.md](ROBUST_TUNING_USAGE.md)，当前状态见 [task.md](task.md)。工程通过不能代替未来盈利验证。

## 构建与验证

Go 核心仅使用标准库，Go 1.23 或以上。Optuna 调参调度需要独立的 Python 环境。研究构建继续关闭 VCS 信息写入，以保持既有构建方式。

Git 仓库保存源码、配置、测试、参考材料和研究文档。`.gitignore` 排除本地 Python 环境、`data/` 行情数据、`results/` 研究结果与运行轨迹、`bin/` 二进制和 `build/` 生成目录；这些文件在原工作目录保留。文档中的本地结果路径不是随仓库分发的附件，克隆仓库不会获得既有研究缓存，也不应为补齐它们自动重跑研究。当前结论及检查状态见 `task.md`。

```sh
GOCACHE=/tmp/lorentz-go-cache go build -buildvcs=false -o bin/lorentz ./cmd/lorentz
GOCACHE=/tmp/lorentz-go-cache go test -race ./...
GOCACHE=/tmp/lorentz-go-cache go vet ./...
python scripts/verify_original.py
```

HTTP 测试仅使用本地模拟服务器；受限环境若禁止回环监听，需要允许该测试的监听权限。Python 用于独立参考验证和 Optuna 研究调度；Go 信号与交易账本可以独立运行。

## 数据与运行

```sh
./bin/lorentz download -start 2020-01-01 -funding-start 2024-01-01 -end 2026-10-01
./bin/lorentz signals -interval 15m -algorithm original-chart -out results/snapshot/original-15m-trace.csv
./bin/lorentz backtest -interval 15m -algorithm original-online -out results/15m.json
./bin/lorentz research -out results/snapshot/research.json
```

日期为 UTC，起点包含、终点不包含。下载使用 Binance USD-M `/fapi/v1/klines` 和 `/fapi/v1/fundingRate`，K 线保存为 CSV，资金费为 JSON。元数据绑定品种、来源、范围及行数；缓存只有身份和覆盖检查通过后才会复用。所有 K 线检查顺序、间隔、重复、缺值、OHLC、时间对齐及收盘边界，不填补坏数据。

原始四周期缓存 `data/ETHUSDT/` 覆盖 2020-01-01 至 2026-10-01，共 313,055 根；精确资金费自 2024-01-01 起，共 3,012 次。早期资金费率真实存在，但部分关联结算 `markPrice` 为空；原始下载仍会明确拒绝这种缺值。扩展缓存 `data/ETHUSDT-expanded/` 为 4h/日线另行准备：缺失结算价格时使用官方 8h 标记价格整点开盘价代理，逐条写入 `mark_price_source=binance-mark-kline-open-8h` 和 K 线桶开盘时间 `mark_price_time`，不使用成交价格、不省略资金费。已有精确价格优先使用。扩展记录共 6,600 条，其中 3,403 条价格为估算、3,197 条为真实关联结算价格；元数据及 Optuna 汇总保存估算计数，完整来源见扩展缓存 README。

资金费覆盖检查采用 ETHUSDT 最长八小时结算间隔加时间容差，并核对缓存行数；如果交易所采用更短间隔，该上界检查不能单独证明每个结算点都完整。可信 API 分页和保存的来源元数据也是依据。

## 复现边界与候选

- `original-chart`：使用固定的图表末端索引，复现历史加载路径，包括反向历史标签、绝对窗口/倒序循环、跨根 FIFO 及原始退出逻辑。
- `original-online`：在提供的历史起点上逐根更新末端索引，用于因果运行基线。它不等于先加载旧图表再追加实时报价，也不等于每次重新加载图表。
- `aligned-knn`：只使用已成熟的四根向前收益标签，对齐历史特征；真正选择距离最近的 K 个样本，滚动窗口且每四根采样。特征、过滤和信号转换仍继承原版。

默认邻居为 8，历史参数 2000，特征为 RSI(14,1)、WT(10,11)、CCI(20,1)、ADX(20)、RSI(9,1)。累计归一化依赖历史起点，Kernel 默认读取 27 根；投票值不是概率。Go 的逻辑、初始化和状态已测试；已完成四份官方 TradingView 导出的有限对照，存在 WT 初始化、部分 prediction/事件及裁剪窗口偏差，尚未证明完整等价（见 reference/tradingview/README.md）。仅支持 close/hlc3/ohlc4 作为 source，参数使用合法且更严格的子集；不复现绘图、图表统计和盘中 rollback。

## 成交与收益定义

收盘信号在下一根开盘成交。最多一个持仓，不加仓；反向入场先平旧仓再开新仓。默认初始资金 10,000 USDT，每次以当前权益建立 1 倍名义仓位，持有期间数量固定，按净权益复投。默认手续费每边 5 bps、逆向滑点每边 2 bps 是研究假设，可通过参数修改，不是已核实的账户费率。

资金费在 `(开仓时间, 平仓时间]` 内按历史标记价格逐笔结算，正费率多头支付、空头收取。`signals` 使用原始退出提示和反转；`four-bars` 额外在实际入场四根后退出。两者都在评分期末按最终收盘价、含费用和滑点平仓。原指标没有持仓账本，所以这里的可执行交易规则与其界面统计不同。

报告包含逐笔交易、权益曲线、胜率、净收益、平均单笔权益收益、收益因子及最大回撤；所有胜率按扣成本后的单笔盈亏判断。最大回撤采用开盘、执行边界和收盘权益采样，不能确定盘中真实回撤；偿付检查保守使用 OHLC 极值，包括扣资金费之后。没有模拟杠杆、强平、订单簿或真实成交。

## 首轮实验与结论

七个配置预先确定：原版逐根基线、对齐 KNN 的 K=8/16/32、K8 的 25% 投票门限、EMA200 过滤，以及实际持有四根退出。候选只在验证段选择：至少 30 笔、净收益为正、净收益和胜率同时高于基线；符合条件时取净收益最高者。后续保留测试只运行基线与已选候选。

这是有限窗口的首轮实验：每个时间段最多使用 10,000 根初始化历史，并只交易该时间段末尾至多 2,000 根。验证段在 2024 年，保留测试在 2025-01-01 至 2026-10-01 范围内；各周期实际评分时间不同：

| 周期 | 保留测试起点 UTC | 终点 UTC，不包含 | 基线交易数 | 胜率 | 净收益 |
|---|---|---|---:|---:|---:|
| 15m | 2026-09-10 04:00 | 2026-10-01 | 62 | 40.32% | -8.86% |
| 1h | 2026-07-09 16:00 | 2026-10-01 | 67 | 32.84% | -11.54% |
| 4h | 2025-11-01 16:00 | 2026-10-01 | 68 | 36.76% | -16.87% |
| 1d | 2025-01-01 00:00 | 2026-10-01 | 15 | 26.67% | -32.25% |

验证段只选出了 4h 的 `aligned-k8-hold4`：35 笔、胜率 54.29%、净收益 +16.35%。它在保留测试只有 28 笔、胜率 35.71%、净收益 -25.98%，未证明升级成功。其他周期没有满足预定选型条件的候选；日线样本尤其少。不要把这些有限窗口结果扩大解释成完整历史表现。

完整参数、实际时间、候选验证结果和测试摘要在 `results/snapshot/research.json`；每个已测试配置另有逐笔交易与权益 JSON，`*-chart.json` 是图表重载路径的独立诊断，不参与选型。之后若基于这些测试结果继续设计，当前测试段已成为开发信息，不能继续称为未见过的最终保留样本。

## 来源与下一步

`reference/` 保留作者精确 v2 库和来源版本信息。Pine 衍生文件保留作者 jdehorty 署名，适用 [Mozilla Public License 2.0](https://mozilla.org/MPL/2.0/)。作者项目：[TradingView](https://cn.tradingview.com/script/WhBzgfDu-Machine-Learning-Lorentzian-Classification/)。

尚未完成稳定盈利升级。已接入原版 Optuna 参数搜索，见下文；后续参数选择需嵌套时间验证或新数据检验，并取得独立参考向量。具体工作状态见 `task.md`。

## Optuna 参数搜索

安装到项目环境，锁定已验证的 Optuna 5.0.0 和依赖；原版 Go 算法与成本账本不变。

```sh
UV_CACHE_DIR=/tmp/lorentz-uv-cache uv venv .venv --python /usr/bin/python
UV_CACHE_DIR=/tmp/lorentz-uv-cache UV_LINK_MODE=copy uv pip install --python .venv/bin/python -r tuning/requirements.lock
.venv/bin/python -m unittest discover -s tuning -p 'test_*.py'
```

四周期启用计划在 `tuning/plans/ETHUSDT-{15m,1h,4h,1d}.json`，结束日期均为 2025-01-01 UTC（不含）：

| 周期 | 初始化起点 UTC | 实际评分起点 UTC | 评分折 |
|---|---|---|---|
| 15m / 1h | 2023-01-01 | 2024-01-01 | 2024 年三个四个月折 |
| 4h | 2021-01-01 | 2022-01-01 | 2022、2023、2024 三个年度折 |
| 1d | 2019-11-27 | 2020-09-22 | 截至 2022-01-01、2023-07-01、2025-01-01 的三个连续折 |

日线公开行情始于 2019-11-27，2019 年仅 35 根，保留搜索空间最长 300 根预热后无法在 2019 年实际评分。扩展说明见 [tuning/plans/pending/README.md](tuning/plans/pending/README.md)，原 4h/日线 2024 计划保留在 `tuning/plans/legacy-2024/`。

每折从空仓开始并在末尾含成本平仓。指标累计归一化与投票队列跨折保留，账本不跨折持仓。净收益目标为三个独立区间收益的复合，胜率按总盈利交易数/总交易数计算；`max_fold_drawdown_pct` 只是各折回撤最大值，不是连续账户全区间回撤。

这些时间段是开发数据。所有试验都会使用计划中的全部评分区间选择参数，不能称为独立样本外检验，也不是“每段只用此前数据选参”的嵌套 walk-forward。旧测试已经看过，不能通过改名恢复独立性。预热最低 200 根并核对实际配置需求，固定历史起点不等于从上市以来初始化。

```sh
.venv/bin/python tuning/optimize.py --plan tuning/plans/ETHUSDT-4h.json --out results/optuna-4h --trials 100
```

`--trials` 是总预算，包含失败试验；相同命令再次运行只补到该数量。`--startup-trials` 默认 32，随后使用多目标 TPE；`--sampler grouped` 可试用分组多变量模式，默认 `independent`。采样种子按试验编号派生，使串行续跑不重播随机启动阶段。改变种子、采样模式、脚本、Go 二进制、数据、依赖版本、计划或评价超时需要新的输出目录；增加总预算可以在原目录续跑。每个目录只允许一个控制器，四周期使用不同目录；当前不提供同一 study 的多 worker。

目录身份保存在 `run-contract.json`，读写 journal 前先检查；换周期也会拒绝，原日志、汇总与候选保持原样。修复前没有目录合同的试跑保留供查阅，使用修复后的程序需要新输出目录。程序不会为旧目录补造合同或迁移旧 study。

输出和 journal 路径统一为绝对路径。控制器持有目录锁后可清理已遗留的 journal 符号链接锁；不认识的锁文件会报错并保留。若日志只有末条未写完且没有换行，先完整备份为 `study.journal.recovery-*.bak` 并落盘，再恢复至最后已提交记录；完整记录损坏则保留原件、停止运行。恢复后的 `RUNNING` trial 记为 `FAIL` 并消耗预算，已有完成试验和约束保留。

向控制器发送 SIGTERM 会清理评价子进程、释放目录锁并以 143 退出；已进入试验的中断按失败计入预算。SIGKILL 或断电无法执行进程清理，重启时检查日志和遗留锁。

搜索范围是预先定义的研究子集，不是所有合法 Pine 参数的穷举：

| 参数组 | 搜索内容 |
|---|---|
| 模型与历史 | Source=close/hlc3/ohlc4；Neighbors=1–100；MaxBarsBack=500/1000/2000/4000；IncludeFullHistory 开关 |
| 特征 | 前 2–5 槽，每槽 RSI/WT/CCI/ADX；RSI A=5–40/B=1–8，WT A=4–30/B=3–30，CCI A=8–50/B=1–8，ADX A=5–40/B 固定 1 |
| 通用过滤 | 波动、Regime、ADX 开关；启用时 Regime 阈值 −0.5–1.0（步长 0.1），ADX 阈值 10–40 |
| 均线 | EMA/SMA 开关；启用时周期 20–300（步长 10） |
| 核回归 | 入场过滤开关、h=3–30、r=0.25–16（步长 0.25）、x=2–40、平滑开关、启用平滑时 lag=1–2 |
| 出场 | 原版固定／动态出场；动态模式强制关闭 EMA、SMA、核平滑 |

未激活字段保持默认值，ADX B 不搜索；显示和统计参数不进入搜索。四根跨度不变，不优化费用、仓位或使用 `aligned-knn`。默认配置独立评价并作为基线，也以等效配置加入第一笔试验。

结果写入 `study.journal` 与 `summary.json`。仅在总交易数至少 30、至少两段净收益为正、复合净收益为正、且胜率与净收益都严格超过默认基线时，才导出达标帕累托候选的完整参数 JSON。没有达标候选会明确记录，完成预算不代表策略升级成功。日线尤其可能缺乏足够交易，程序不会降低最低交易数来凑结果。

导出配置可以使用同一计划精确重放，或用于现有信号命令：

```sh
./bin/lorentz optimize-eval -plan tuning/plans/ETHUSDT-4h.json -config results/optuna-4h/candidate-trial-3.json
./bin/lorentz signals -interval 4h -config tuning/default-original-online.json -out results/default-4h-signals.csv
```

候选文件名按实际试验编号，以 `summary.json` 为准。`signals` 和 `backtest` 新增 `-config`，要求完整 JSON；键名必须精确，重复键（含转义后同名）和大小写变体均拒绝，计划、请求与嵌套特征也适用。显式命令行模型参数覆盖配置，其余保持文件值。它们原有的 `-bars/-score-bars` 窗口与调参计划不同，直接使用默认窗口回测不能复现调参汇总。`optimize-eval -plan` 无 `-config` 时作为长驻 NDJSON 评价器，加载一次数据，每个请求计算一组完整配置。

先测完整评价耗时再安排正式搜索预算。原版历史循环在部分配置下会随历史长度扩大，15m 全区间比 4h 更昂贵；不使用短历史替代完整评分进行 Hyperband/ASHA 剪枝。数据身份绑定仅用于防止混用试验；不能替代 TradingView 数值核对或证明实盘优势。

当前首轮预算为每周期总计 64 次（含失败），startup=32、seed=42、independent 多目标 TPE；输出分别为 `results/optuna-round1/{15m,1h,4h,1d}/`。启动前 `--trials 0` 只建立合同、完整默认基线和评价耗时，不运行搜索 trial；随后同一目录以 `--trials 64` 开始首轮搜索。完成预算后只将达标候选作为开发筛选结果，另做后续时间验证。

当前优先范围为 15m 和 1h；4h/日线首轮结果保留，后续工作延后。调度在不同周期之间使用独立进程并行；每个周期使用一个 Optuna 控制器和一个 Go 评价器，内部 trial 顺序执行，不支持同一 study 的多个 worker 绕过输出目录锁。运行进度以 journal/日志为准，`summary.json` 在控制器结束时更新。

## 版本化离线升级

旧 `bin/lorentz`、`optimize-eval`、完整旧 JSON、缓存、study 和候选保留。升级构建使用 `go build -buildvcs=false -o bin/lorentz-upgrade ./cmd/lorentz`。增强入口使用独立策略 schema 和 JSON-line 协议2；省略 `-config` 时串行处理完整配置：

```sh
./bin/lorentz-upgrade enhanced-eval -plan tuning/plans/ETHUSDT-1h.json -config <完整增强配置.json>
.venv/bin/python tuning/enhanced.py --matrix <冻结矩阵.json> --budget <获批配置数> --out <新目录> --binary bin/lorentz-upgrade
```

| 策略版本 | 新增合同 | 默认/边界 |
|---|---|---|
| 1 | 独立包络、已收盘D1过滤 | 默认关闭；偏离事件仅上下文，D1只过滤当根新入场 |
| 2 | main/pullback/both、首次回调状态机 | 回调默认关闭；一次阶段至多一次，账本单仓、不加仓 |
| 3 | classic-extended、动态命名特征组 | Classic标签/队列保持；2–15维、无重复槽；新特征因果滚动z |
| 4 | signals/four-bars退出合同、ATR风险 | 风险默认关闭；收盘确认后下一实际开盘退出，无盘中保证 |
| 5 | aligned-extended、独立model参数 | 默认仍Classic控制；aligned的成熟四根标签/top-k、Lorentzian/Euclidean、等权/倒距离、独立投票/排序年龄变体 |

1–4版完整字段仍按原版本解码/导出；不自动升级或回退。5版新默认保持关闭增强的Classic控制。`classifier.family`必须匹配`classic.algorithm`；aligned使用滚动窗口，禁止inactive full-history。`model.score`是[-1,1]有符号投票强度，未经校准不叫概率；旧整数prediction独立保留。投票和排序半衰期分别以预测起点的实际根数计算，禁止同时启动两种年龄变体。下采样保持绝对索引相位，标签只有`j+4<=t`才成熟。Python命名组/模型助手只生成完整Go配置，不另算模型。

包络参数独立于主kernel：h=8、r=8、x=25、ATR长度60、近1.5/远8；H/L/C kernel计算TR，再SMA种子Wilder ATR，默认有效数据index85开始ready。偏离为(close-center)/ATR；零ATR/缺失输入不得形成增强入场。D1使用最后两根完成RQ中心的方向，平坦/未就绪不放新仓。增强事件决策及可用时刻CloseTime+1；旧Point.Time仍CloseTime，成交在下一开盘。

D1 OHLC必须与完整低周期日聚合一致。缓存volume 1h有3日/15m有5日不同（最大3.21186%），仅作诊断，原数据不修改；D1方向只用价格。RVOL使用所选低周期成交量除以前A根均量，明确排除当前根；ATR/价格、kernel偏离、已完成D1斜率各自保存当时归一化向量，不未来重缩放。

ATR风险距离使用入场决策根已完成ATR及实际滑点成交价；风险线只收紧。保本线计入入场/退出费用、预期滑点及已结算资金费，但未来跳空和资金费仍可能造成净亏损。信号退出、反转优先于确认风险退出，最大持仓限制最后。four-bars仍是信号/反转加最大持仓根数。M5b部分退出/盘中订单及R1均未实现或启动。

有限矩阵控制器不搜索Optuna；目录锁、完整合同、严格JSON、失败预算和中断恢复适用。RUNNING调用中断后消耗预算，恢复为FAIL，不自动重跑。M1十次、M2六次、M3六次、M4五次、M5a五次均使用独立结果目录；后续批次须另获预算，不从旧study授权推导。实际状态/研究结论见`task.md`。

### 嵌套时间评价

`tuning/validation/exposure.json`将2020至2026-10-01缓存均标为暴露。2024三折参与开发，2025–2026已查看区间亦不独立；工具拒绝把已知2026-10-01以前的历史改名unused-final。当前没有可证明未使用的最终验证数据。

`tuning/validation/walkforward-template.json`是**未运行提案**：trial10控制加RVOL/ATR两个候选，2025两个已暴露外窗、各两个历史内折，固定门槛及成本，明确22次账本评价预算。不得未经授权运行。只校验合同/覆盖可使用：

```sh
./bin/lorentz-upgrade walkforward-eval -plan tuning/validation/walkforward-template.json -inspect
# 实际研究须另获22次预算授权并使用独立新目录
.venv/bin/python tuning/walkforward.py --plan tuning/validation/walkforward-template.json --budget 22 --out <新目录> --binary bin/lorentz-upgrade
```

本轮实际冻结计划 `tuning/validation/m6-retrospective-v1.json`使用Classic、RVOL和aligned-Euclidean三份配置；用户已另批22次、0搜索。它与示例模板分开，结果写入 `results/upgrade-v1/m6/retrospective/`。候选跨分类器族的比较是完整策略选择，不解释为只改特征的消融；所有窗口均标记回顾性。

预算计数是每个内折的候选及控制账本调用、两个账户的每外折空仓对照，以及两个连续账户的三档成本调用；整批不算一次。inspect只校验数据/配置，不产生策略结果。控制器预保留整批预算；失败、超时、TERM或中断均消耗，恢复不重跑；实际结果和恢复COMPLETE均须匹配协议、完整计划及三项数据身份。

内折结束不晚于外窗开始，先冻结选择再评价外窗。同净收益并列按声明顺序，无合格候选回退冻结基线。参数切换首个外窗收盘生效；边界开盘执行前窗最后决策。分类状态从固定起点因果重算，现金/持仓/风险线/资金费连续；仅最终结束平仓，旧折间空仓结果另列。所有候选必须共享风险和退出合同。

报告包含净收益/胜率/期望/完整交易数/连续账户回撤、窗口表现、多空及事件贡献、持仓、前三笔正收益集中度、成本压力和信号/成交/交易计数。窗口回撤仅收盘权益；全账户另采执行边界。滑点估计不再次从净账本扣除；资金费收付仅按每笔净额分拆，不能恢复交易内抵消的结算流。成本比率分母为绝对滑点前毛波动，零分母null、可超过100%。采用标准预先写入计划；回顾嵌套不能消除暴露偏差。

`reference/tradingview/README.md`记录真实有限对照及未解决的Classic差异；当前没有TradingView全等价结论。工程检查、开发集改善和独立最终市场验证分别报告。

本轮获批评价与结果审计记录位于 `results/upgrade-v1/`。M1–M5a共32次固定配置评价，0新增Optuna；M6另22次账本评价完成。过滤/回调/ATR退出未获采用支持；RVOL及Euclidean仅保留开发候选。M6两外窗均选RVOL，以下为2025**连续账户**回顾结果：

| 费用/滑点倍数 | 选择策略净收益 | 净胜率 | 完整交易 | 连续账户最大回撤 | Classic控制净收益 |
|---|---:|---:|---:|---:|---:|
| 1 | −6.5431% | 45.1613% | 93 | 25.6484% | −41.8123% |
| 1.5 | −12.4321% | 43.0108% | 93 | 28.3748% | −45.2536% |
| 2 | −17.9543% | 38.7097% | 93 | 31.2287% | −48.4941% |

三个成本情景均未达绝对正净收益门槛，决定`keep_disabled`。两外窗同配置，不能宣称参数切换带来收益；连续账户回撤也不能用最大单折回撤代替。全部为暴露区间，独立最终验证待未使用数据。
