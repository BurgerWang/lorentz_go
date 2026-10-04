# 增强搜索与连续验证用法

本文件对应 [ENHANCED_OPTUNA_PLAN.md](ENHANCED_OPTUNA_PLAN.md) 的工程入口。**B0 固定20请求/60账本/0trial已单独获批并完成，20/20成功、10独立重放一致，结果审计关闭；1h S1两族已按每族256trial/256Go/768账本上限完成（startup32/seed42/independent），实际总512trial/509Go/1527账本，3缓存，结果审计关闭；S2/S3、V1与15m搜索仍须逐批授权。** 所有当前历史均按已暴露数据处理，开发可行候选和回顾评价不构成独立最终验证，也不自动启用策略。

在项目根目录执行命令，使用已有 `.venv`（Optuna 5.0.0）。新文件集中在 `tuning/spaces/enhanced-v1/`；结果放入新的 `results/enhanced-optuna-v1/`，不恢复旧 study。修改空间、数据、二进制、依赖、预算或冻结规则须另建合同与目录。

## 构建与 Go 入口

新研究二进制单独构建，保留 `bin/lorentz`、`bin/lorentz-upgrade` 及其已绑定结果。已有新批次也不能通过原位重建二进制绕过身份检查。

```bash
go build -buildvcs=false -o bin/lorentz-enhanced-optuna ./cmd/lorentz

# 无行情运算：输出默认 schema5 或验证完整配置。
bin/lorentz-enhanced-optuna config-validate
bin/lorentz-enhanced-optuna config-validate -config CONFIG.json
bin/lorentz-enhanced-optuna config-validate -batch < CONFIGS.json
```

`CONFIGS.json` 是完整配置数组。新搜索统一编译完整 schema5；Go 仍支持旧 schema1–4，不原地升级旧配置。

```bash
# 获批后才执行单配置三折评价；也占请求和账本预算。
bin/lorentz-enhanced-optuna enhanced-eval \
  -plan tuning/plans/ETHUSDT-1h.json -config CONFIG.json

# 省略 -config 时为长驻协议2 JSON-lines 接口。
bin/lorentz-enhanced-optuna enhanced-eval \
  -plan tuning/plans/ETHUSDT-1h.json
```

长驻接口先发 `ready`，输入为 `{"id":1,"config":完整配置对象}`，结果带同一请求 ID、完整配置、计划和数据/D1 身份。stdout 是协议，诊断写 stderr。Python 只生成配置、控制预算和核对协议；策略与交易账本由唯一 Go 实现计算。

## B0：固定接入与独立重放

`B0-fixed-20.json` 冻结 **20 次 Go 请求、60 次三折账本、0 次 Optuna trial**：八个 1h 配置各执行一次并独立重放一次（16 请求），两个 15m 控制同样执行（4 请求）。每个重放真实调用 Go，绕过配置缓存；失败或未知在途请求仍消费其预留额度。

```bash
# 只校验配置、数据、协议和合同；不运行策略账本。
.venv/bin/python tuning/enhanced_check.py \
  --matrix tuning/spaces/enhanced-v1/B0-fixed-20.json \
  --binary bin/lorentz-enhanced-optuna \
  --out results/enhanced-optuna-v1/B0 \
  --max-go-evaluations 20 --max-ledger-evaluations 60 \
  --timeout 600 --inspect

# 已完成B0的原执行命令；保留完整状态，不额外追加控制或计时请求。
.venv/bin/python tuning/enhanced_check.py \
  --matrix tuning/spaces/enhanced-v1/B0-fixed-20.json \
  --binary bin/lorentz-enhanced-optuna \
  --out results/enhanced-optuna-v1/B0 \
  --max-go-evaluations 20 --max-ledger-evaluations 60 --timeout 600
```

保存结果见 [B0 summary.json](results/enhanced-optuna-v1/B0/summary.json)。该批请求累计215.079秒，load0.316秒；失败0、0缓存，无未完成请求。检查 `summary.json` 的逐请求状态、`replay_equal`、预算和计时。B0 不保证策略有效；用于确定代表性配置的实际耗时，不能以最快样例估算整个搜索批次。

## S1–S3：显式预算与冻结阶段

已完成 S1 的 [保存比较报告](results/enhanced-optuna-v1/S1-comparison.json) 含237个不同可行配置、六个冻结对照。下一批 [Classic S2空间](results/enhanced-optuna-v1/frozen/S2-1h-classic-extended.json) 与 [aligned S2空间](results/enhanced-optuna-v1/frozen/S2-1h-aligned-extended.json) 已准备，锚点分别228/101、215/213；仅文件准备，没有S2评价或额外预算授权。

每个周期、分类器族、阶段使用独立 study 目录。下表为 **1h 每族** 额度，Classic与aligned的S1已各完成一批，S2/S3仍为待批准提案；15m 的冻结示例不是 1h 预算授权的延伸。

| 阶段 | trial 尝试上限 | Go 请求上限 | 三折账本上限 | startup / seed |
|---|---:|---:|---:|---|
| S1 | 256 | 256 | 768 | 32 / 42 |
| S2 | 128 | 128 | 384 | 24 / 42 |
| S3 | 128 | 128 | 384 | 24 / 42 |

trial 尝试包含开始执行的种子、失败和重复配置；缓存命中仍占 trial，但不新增 Go／账本预留。完整成功结果才进入配置缓存。上游没有可行候选时不启动下一阶段，保留下一阶段额度，不自动扩预算。

开发双目标为净收益和净胜率；示例约束为至少 30 笔交易、至少 2 个盈利折、净收益严格大于 0、最大单折回撤不超过 30%。这些开发汇总不是连续账户回撤；阈值随空间冻结，不能因后续结果不理想而原位修改。

各 study summary 保存种子对照及候选相对对照差异。跨族、跨阶段可使用 `search_report.py --summaries SUMMARY... --out REPORT.json` 汇总同开发合同的现有控制、预算、计时和差异；只读已保存结果，无新增评价。若要覆盖 default/52/54/10 与 aligned 同族对照，传入两个 S1 summary 及后续需比较的 S2/S3 summary。Classic/aligned 的差异属于整套策略比较，不能解释为纯特征效果。

```bash
# 先查看一个族的冻结合同；换 aligned-extended 检查另一族。
.venv/bin/python tuning/optimize_enhanced.py \
  --plan tuning/plans/ETHUSDT-1h.json \
  --space tuning/spaces/enhanced-v1/S1-1h-classic-extended.json \
  --binary bin/lorentz-enhanced-optuna \
  --out results/enhanced-optuna-v1/S1-1h-classic-extended \
  --max-trials 256 --max-go-evaluations 256 --max-ledger-evaluations 768 \
  --startup 32 --seed 42 --sampler independent --timeout 600 --inspect

# 已完成两族S1的原执行命令，保留本批完整状态与额度。
for family in classic-extended aligned-extended; do
  .venv/bin/python tuning/optimize_enhanced.py \
    --plan tuning/plans/ETHUSDT-1h.json \
    --space "tuning/spaces/enhanced-v1/S1-1h-${family}.json" \
    --binary bin/lorentz-enhanced-optuna \
    --out "results/enhanced-optuna-v1/S1-1h-${family}" \
    --max-trials 256 --max-go-evaluations 256 --max-ledger-evaluations 768 \
    --startup 32 --seed 42 --sampler independent --timeout 600 || break
done
```

`--inspect` 加载和核对合同，不评价策略或启动 Optuna。`--until-trials 64` 可先停在累计第 64 个 trial 尝试的边界；恢复时保留全部硬预算和其他参数，去掉该选项，继续使用同一目录。该值是累计上限，不是追加数量。

S2/S3 必须从**新增强 study 导出的 feasible 候选**冻结；旧 trial52/54/10 仅可作为已有冻结种子，不能伪装成新阶段的可行结果。S2 从同族 S1 可行 Pareto 集合确定最多两个不同锚点，依次按最高净收益、其余最高胜率选取；S3 保留 S1/S2 来源，至少需要 S2 来源。无足够来源时工具停止，不制造锚点。

```bash
# 每族分别执行；这里示范 Classic。
.venv/bin/python tuning/freeze_stage.py \
  --stage S2 --family classic-extended \
  --summaries results/enhanced-optuna-v1/S1-1h-classic-extended/summary.json \
  --base-space tuning/spaces/enhanced-v1/S1-1h-classic-extended.json \
  --out results/enhanced-optuna-v1/frozen/S2-1h-classic-extended.json

# S2 完成并获得 S3 方案授权后，先冻结联合空间。
.venv/bin/python tuning/freeze_stage.py \
  --stage S3 --family classic-extended \
  --summaries results/enhanced-optuna-v1/S1-1h-classic-extended/summary.json \
              results/enhanced-optuna-v1/S2-1h-classic-extended/summary.json \
  --base-space tuning/spaces/enhanced-v1/S3-1h-classic-extended-template.json \
  --out results/enhanced-optuna-v1/frozen/S3-1h-classic-extended.json
```

可在冻结前通过 `--domains APPROVED-DOMAINS.json` 指定获批范围；不提供时使用该阶段默认域。S3 template 本身不是依据开发结果缩小后的最终空间。禁止续跑时扩大同名 categorical 域，或看过验证结果后重定搜索域。

```bash
# S2 两族各自冻结并获批后运行；先加 --inspect 核对每个合同。
stage=S2
for family in classic-extended aligned-extended; do
  .venv/bin/python tuning/optimize_enhanced.py \
    --plan tuning/plans/ETHUSDT-1h.json \
    --space "results/enhanced-optuna-v1/frozen/${stage}-1h-${family}.json" \
    --binary bin/lorentz-enhanced-optuna \
    --out "results/enhanced-optuna-v1/${stage}-1h-${family}" \
    --max-trials 128 --max-go-evaluations 128 --max-ledger-evaluations 384 \
    --startup 24 --seed 42 --sampler independent --timeout 600 || break
done
```

S2 完成后冻结两族 S3，并取得 S3 授权，再将 `stage=S2` 改为 `stage=S3` 执行同一命令。`random`、`independent`、`grouped` 是可选 sampler；本项目没有等预算多 seed 的代表性性能结论，不宣称哪种最佳。每个目录有独占锁，**不支持同 study 并行**。

## V1：来源分组、匹配基线与固定账户

先冻结准备文件，工具仅做配置／数据检查和 Go inspect，不运行选择或固定账户。候选及全部父阶段使用的评分截止不得晚于首个外窗起点；更晚的搜索结果不能塞回旧外窗。数据快照、周期、初始化、费用、二进制及开发合同须匹配。

```bash
.venv/bin/python tuning/validation_groups.py \
  --summaries results/enhanced-optuna-v1/S1-1h-classic-extended/summary.json \
              results/enhanced-optuna-v1/S1-1h-aligned-extended/summary.json \
              results/enhanced-optuna-v1/S2-1h-classic-extended/summary.json \
              results/enhanced-optuna-v1/S2-1h-aligned-extended/summary.json \
              results/enhanced-optuna-v1/S3-1h-classic-extended/summary.json \
              results/enhanced-optuna-v1/S3-1h-aligned-extended/summary.json \
  --template tuning/spaces/enhanced-v1/validation-1h-template.json \
  --baseline-source tuning/spaces/enhanced-v1/global-classic-1h-source.json \
  --binary bin/lorentz-enhanced-optuna \
  --out results/enhanced-optuna-v1/V1-plans --fixed-candidates --timeout 600
```

模板沿用两个 2025 回顾外窗、各两个内折和三档成本。按完整 Risk／Exit 合同分组，浮点参数不同也属于不同组；组内基线只在冻结全局 Classic 配置上替换该组完整 Risk／Exit。每组最多三个确定性代表，最多四组；没有可行候选即停止验证。

全局 Classic 是模板及 `global-classic-1h-source.json` 绑定的冻结控制。存在完全相同的组基线时复用其连续对照；否则另做三个成本账户。`--fixed-candidates` 另外准备最多两个固定候选，各需三个账户，不能用组内动态选择账户收益替代固定候选收益。

单组预算为 `2*S + 2*O + (C+1)*sum(I_o)`。示例中 C=1/2/3 对应 **18/22/26** 次账本；四组三候选最多 104，加全局固定对照 3、两个固定候选 6，总示例上限 **113**。实际数量以 Go inspect 和 `manifest.json` 的 `ledger_budget`／`total_ledger_budget` 为准；不能直接把 113 当作任意布局的执行预算。

以下仅输出可审阅的逐计划命令，不执行评价。批准实际 manifest 总额及具体计划后，再执行所打印的命令；输出目录逐计划独立。

```bash
.venv/bin/python - <<'PY'
import json, shlex
from pathlib import Path
p = Path('results/enhanced-optuna-v1/V1-plans/manifest.json')
m = json.loads(p.read_text())
print('实际账本总额:', m['total_ledger_budget'])
for e in m['plans']:
    print(shlex.join([
        '.venv/bin/python', 'tuning/continuous_enhanced.py',
        '--kind', e['kind'], '--plan', e['plan'], '--provenance', e['provenance'],
        '--binary', 'bin/lorentz-enhanced-optuna',
        '--out', 'results/enhanced-optuna-v1/V1/' + e['name'],
        '--budget', str(e['ledger_budget']), '--timeout', '600']))
PY
```

`continuous_enhanced.py` 在每次执行前复核来源和 inspect，再预留整个计划预算；再次运行相同命令只恢复状态，不自动重播未知账户。直接 Go `walkforward-eval` 或 `fixed-continuous-eval` 不检查 Python 来源侧文件，正式 V1 应使用上述来源校验控制器。

```bash
# 准备工具生成固定计划后，可单独 inspect，不评价策略。
bin/lorentz-enhanced-optuna fixed-continuous-eval \
  -plan results/enhanced-optuna-v1/V1-plans/fixed-candidate-0.json -inspect
```

固定计划仍含完整外窗、暴露和初始化合同，但 `baseline` 是一个完整 schema5 配置、`candidates=[]`、`budget=3`。每成本只调用一次连续 Go 账本，内折不选择候选、不重置账户；结果为 `continuous_scenarios[*].fixed`，状态 `fixed_evaluation_only`。仓位、资金费、风险线、收盘确认／下一开盘执行及最终平仓语义保持原实现。该文件只有准备出固定候选时存在。

结果须逐成本列出组内每窗选择／基线回退，以及相对匹配基线、全局 Classic 的净收益、胜率和连续回撤差异。固定候选单独核对同一套冻结绝对门槛和成本比较；Go 固定入口的 `fixed_evaluation_only` 不代替这些比较，也不宣布采用通过。

```bash
# 仅读取已保存的 Go 结果，无新增行情评价。
.venv/bin/python tuning/validation_report.py \
  --manifest results/enhanced-optuna-v1/V1-plans/manifest.json \
  --results-dir results/enhanced-optuna-v1/V1 \
  --out results/enhanced-optuna-v1/V1-comparison.json
```

## 恢复、输出与结论边界

- 保留 `run-contract.json`、`study.journal`、`summary.json` 与候选文件。合同绑定完整空间、计划、数据/D1、数据快照、二进制、依赖、sampler、seed、约束及预算；漂移或不兼容非空目录拒绝恢复。
- 请求发送前持久化 Go／账本预留。TERM/KILL 后未知 RUNNING trial 转 FAIL，消费原 trial 编号及全部预留，不隐式重算；已 COMPLETE 的结果核对后可复用。同样的终态历史支持边界恢复，未知失败历史不承诺与不中断成功历史的后续采样轨迹一致。
- 协议错误（JSON、ID、配置、计划、身份或非有限数值等）持久化 `halted_protocol`，恢复不会继续发新请求。错误需调查后另获授权处理，不能删除停止标志继续原批。正常评价失败保留 FAIL 和预算，在冻结剩余额度内继续。
- 搜索 summary 列出 trial 状态、缓存、请求和账本预留，以及加载／采样／Go 请求时间；candidate JSON 可直接送 Go 重放，但独立重放须另计明确预算。没有合格候选不自动补跑。
- 三档成本固定为每边费用 5bps／滑点 2bps 的 1／1.5／2 倍，保留资金费、1x 暴露和权益保护。当前历史不可改称 unused-final；截至 2026-10-01 的已知暴露有硬保护。工程合成测试通过不代表 ETHUSDT 收益、真实性能或采用门槛通过。
