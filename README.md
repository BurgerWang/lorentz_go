# lorentz_go

Lorentzian Classification 的 Go 实现，以及围绕它搭建的离线回测、调参与研究框架，研究对象是 Binance USD-M 永续合约。

本仓库只包含策略核心与框架代码：不附带行情数据、运行结果和调参产物，也不连接账户或下单。它是一个研究工具，不构成投资建议。

## 灵感来源

本项目受 [jdehorty](https://www.tradingview.com/u/jdehorty/) 在 TradingView 上开源的指标 [Machine Learning: Lorentzian Classification](https://www.tradingview.com/script/WhBzgfDu-Machine-Learning-Lorentzian-Classification/) 启发。该指标用洛伦兹距离代替欧氏距离做近似最近邻分类，再用 Nadaraya-Watson 核回归等条件过滤信号。分类器、特征工程和核回归部分移植自他的 Pine Script 源码及配套库 `MLExtensions`、`KernelFunctions`，包络线部分移植自他的 Nadaraya-Watson: Envelope (Non-Repainting)。

原始 Pine 源码保留在 `lorentz_pine.md` 和 `reference/` 下，版本信息见 `reference/provenance.json`。

`reference/tradingview/` 下的对照基线取自 [artificial-intelligence-edge/lorentzian-classification](https://github.com/artificial-intelligence-edge/lorentzian-classification)（MIT），用于逐项核对本实现与 TradingView 输出是否一致。

## 在原指标基础上做的升级

原指标是一个图表指标：信号依赖图表加载的历史起点，没有持仓账本，界面上的统计也不含交易成本。本项目把它改造成可以因果运行、可以核算成本的研究系统。

**分类器**

- 三种运行方式分开实现：`original-chart` 复现图表加载路径，`original-online` 逐根推进作为因果基线，`aligned-knn` 是改进版。
- `aligned-knn` 只使用已经成熟的前向收益标签，在滚动窗口内选出真正距离最近的 K 个样本，并支持最小投票比例门限。
- 模型变体：欧氏距离对照、按距离反比加权投票，以及按样本年龄对投票权重或近邻排序做半衰期衰减。
- 扩展特征：相对成交量、ATR 占价格比、价格相对核回归中枢的偏离（以 ATR 为单位）、日线核回归斜率，统一使用因果滚动标准化。
- 修正了 WaveTrend 的 EMA 初始化，使其与 Pine 的 `ta.ema`（SMA 种子）一致，并用 `scripts/chartreference` 对四份 TradingView 导出逐项核对。

**策略层**

- 核回归 ATR 包络线，以及基于包络线的回踩入场状态机，可与主信号单独或同时使用。
- 日线核回归上下文：小周期只读取已经收盘的日线，避免前视。
- 退出方式可选：原版信号退出，或固定持有根数。
- 风控：ATR 止损、移动止损、盈利达到指定 R 倍数后保本。

**回测账本**

- 收盘出信号、下一根开盘成交；手续费和逆向滑点按边计。
- 资金费按历史结算记录逐笔入账，并记录结算价格来自真实值还是代理值。
- 权益在开盘、成交边界和收盘采样计算回撤；用 OHLC 极值做偿付检查。
- 可导出完整执行事件流，供独立复算。

**数据**

- Binance USD-M K 线和资金费下载器，带限流重试。
- K 线校验顺序、间隔、重复、缺值和 OHLC 关系，不填补坏数据；缓存只有在品种、来源和覆盖范围都核对一致时才复用。

**调参与验证框架（`tuning/`）**

- Optuna 多目标搜索，按时间分折评价，支持中断后续跑和日志恢复。
- 带条件依赖的搜索空间编译器，Python 只负责调度，指标、信号和账本计算全部在 Go 里完成。
- 嵌套 walk-forward 验证，并要求显式声明每段数据的使用情况。
- 稳健性协议：多随机种子、成本倍数压力、资金费情景、区块自助法，以及统一的试验预算账本。

**研究原型（`research/`，Python）**

- v2：加入资金费、持仓量、主动买卖比、多空比等衍生品特征，标签按交易成本设门槛，KNN 样本库有成熟期约束；配有逻辑回归、LightGBM、动量基线和 Deflated Sharpe Ratio。
- v3：双速趋势突破、ATR 移动止损、资金费 carry 调节仓位。
- v4：加入 ADX 震荡过滤和双轨移动止损。
- v5：截面动量加 carry 的多空市场中性组合，篮内按波动率倒数分配权重。

各版本的入口脚本把数据严格分成开发集和留出集，留出集需要显式设置环境变量才能读取，并且只允许运行一次。

## 目录

| 路径 | 内容 |
|---|---|
| `indicator/` | 特征、核回归、包络线、分类器引擎 |
| `strategy/` | 策略配置、入场与退出逻辑 |
| `backtest/` | 交易账本、风控、诊断统计、执行事件流 |
| `market/` | 数据类型、下载、存储、校验、跨周期对齐 |
| `cmd/lorentz/` | 命令行入口和各评价协议 |
| `scripts/` | 与原版的对照验证脚本、资金费数据准备脚本 |
| `tuning/` | Optuna 调度、搜索空间、验证协议（Python） |
| `research/` | v2–v5 研究原型及测试（Python） |
| `reference/`、`lorentz_pine.md` | 原作者 Pine 源码和 TradingView 对照基线 |

## 使用

Go 核心只依赖标准库，需要 Go 1.23 或以上。

```sh
go build -o bin/lorentz ./cmd/lorentz
go test ./...

# 下载行情和资金费（保存到 data/，该目录不入库）
./bin/lorentz download -start 2020-01-01 -funding-start 2024-01-01 -end 2026-10-01

# 输出信号轨迹，或运行带成本的回测
./bin/lorentz signals  -interval 1h -algorithm original-online -out results/signals.csv
./bin/lorentz backtest -interval 1h -algorithm aligned-knn -out results/backtest.json

# 与 TradingView 对照基线逐项比较
go run ./scripts/chartreference
```

调参框架需要独立的 Python 环境。它的测试会调用真实的 Go 可执行文件，路径通过环境变量传入：

```sh
python -m venv .venv && .venv/bin/pip install -r tuning/requirements.txt
.venv/bin/python tuning/optimize.py --plan tuning/plans/ETHUSDT-1h.json --out results/optuna-1h --trials 100

LORENTZ_TEST_BINARY=$PWD/bin/lorentz LORENTZ_ROBUST_TEST_BINARY=$PWD/bin/lorentz \
  .venv/bin/python -m unittest discover -s tuning -p 'test_*.py'
```

研究原型：

```sh
python -m venv research/.venv && research/.venv/bin/pip install -r research/requirements.txt
PYTHONPATH=research research/.venv/bin/python -m pytest research/tests
PYTHONPATH=research research/.venv/bin/python -m lz.download
```

依赖真实行情或本地历史研究产物的测试，在对应文件不存在时会自动跳过。

## 许可

本项目自有代码以 [MIT License](LICENSE) 发布。

`lorentz_pine.md`、`reference/` 下的 Pine 源码，以及文件头标注了 jdehorty 版权的 Go 文件（移植自 Pine 的指标逻辑），版权归原作者 jdehorty 所有，继续适用 [Mozilla Public License 2.0](https://mozilla.org/MPL/2.0/)。`reference/tradingview/` 下的对照数据来自上游仓库，适用其 MIT 许可。
