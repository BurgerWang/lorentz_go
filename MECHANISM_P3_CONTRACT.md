# P3 工程及固定对照合同

状态：P3 工程/合成检查/首批准备已获用户授权；真实历史批次未批准。
依赖：`MECHANISM_V2_PLAN.md` 第5节，已关闭审计的 v2 P1/P2。
本合同不覆盖或改写 v1/P0/P1 的实现、数据、产物、预算或拒绝。

## 范围和变体

仅 ETHUSDT 1h、P2保留的原 A=A-B-S、原 B=B-original。各结构保留完整
schema5 配置，不规范化公共控制、不移动 D1/envelope 上下文、不改入场、
风险、退出、source、费用、资金费、history_start 或全局索引。
完整实验配置外层为 `mechanism-p3-config-v1`，包含 version、variant、strategy；
strategy 原封保留 schema5。只接受 Classic-extended/original-online/sample_stride=4。

| 变体 | 原始方向/候选规则 |
|---|---|
| classic-original | 调用冻结 Classic 完整原路径；额外FIFO重放只披露索引，不影响策略结果 |
| rq-direction | sign(KernelRQ[t]−KernelRQ[t−1])；用自身 classic.kernel_h/r/x，关闭核过滤时仍计算核 |
| momentum-four | sign(Source[t]−Source[t−4])；Source沿原source字段 |
| classic-recent | 候选 [max(0,t−B+1),t]，B=原max_bars_back；每bar先删除全队列窗口外样本 |

简单基线的明确解释选择：保留原全向量 readiness，再与自身方向 readiness 相交。
特征值不参与基线方向，但初始化/不可用门控一致；这样不会同时解除 B 的 D1 链式初始化。
RQ需要两个有限核值及有限差值（按原核首值t=x+1，首方向t=x+2）；动量至少t≥4，
当前和t−4 source及差值有限。Prediction为−1/0/+1，Neighbors=0，队列为空。
不可用、过滤失败和零方向保持原Signal，但持仓年龄和退出继续推进。

新方向在原 runInputs 状态机入口注入，重新计算 signal-change、BarsHeld、EarlyFlip、
StartLong/Short、EndLong/Short；动态退出的上次入场来自新方向，核转折与前bar有效性沿原合同。
内部回调选择不是 aligned 分类器变更，对外仍限定上述Classic合同。
随后完整执行原 daily/risk/main/pullback/SelectEntries 与唯一Go现金账本。

recent保留 backward标签 −sign(Source[j]−Source[j−4])、Lorentz距离、每bar
lastDistance=−1、d≥lastDistance、原四分位距离阈值/FIFO弹出及跨bar重复入队。
绝对相位保持实际原Classic的 j%4!=0，不改为 j%stride==0，不添加aligned成熟标签限制。
每bar同步过滤 labels/distances/indices 全部三元组，保持余项相对顺序；队列不保证索引有序，
不能仅清理队首。query不就绪时也删过期票并重算vote，readiness只阻止Signal更新。
include_full_history不改变recent窗口，仅在original保留实际旧语义；没有expanding或aligned第四控制。

## 独立构建、协议和检查

新增文件以 mechanism_p3 构建标签隔离，三个精确 overlay 派生到 build/mechanism-p3。
原 strategy/run.go、cmd/lorentz/{main,robust_eval}.go 原地字节保持；共享状态机与账本复用，
实例runner不使用全局可变模式。旧默认构建和全部旧bin保留，新引擎为 bin/lorentz-mechanism-p3。
构建manifest绑定实际Go依赖源码/生成overlay/构建脚本/Go版本与命令/新binary，执行manifest再绑定
Python依赖/runtime、完整配置、计划、来源、bundle及本合同。旧引擎缓存不导入新预算。

新入口 mechanism-eval（协议3、基础 robust-eval-v1，新增 mechanism-p3-go-v1 标识）和
mechanism-config-validate；ready声明四变体，inspect验证并回显完整wrapper、6账本、evaluation_calls=0。
result回显wrapper；Python解包strategy仅用于复用旧账本协议检查，实际缓存保留外层variant。
独立 mechanism-indicator-v1 JSONL 记录每bar绝对index/time、完整FIFO索引、raw direction、ready、
prediction/signal/年龄/过滤/Start/End/search/neighbor统计，绑定path/SHA/rows/variant。
这是daily/risk/最终入场筛选之前的指示器状态；最终执行仍由经济trace体现。

相关验收：手算方向/零/不可用/过滤/年龄/固定及动态退出；有限特征距离扰动不改变基线；
全部变体prefix/as-of；recent重复乱序票、非4倍数起点、未就绪无新增票仍到期清理；
新入口daily/pullback前缀因果；original逐Point/Event兼容及新旧binary合成经济trace一致；
严格wrapper、状态trace篡改/缺失拒绝；批准、预算、缓存、未知消费不重试、持久halt及源码保护。
本工程只使用隔离合成数据，真实P1经济兼容保留为后续已预算的原模式请求检查。
相关检查及独立Astra/xhigh工程审计关闭后，才交付未批准真实批次。

## 首批固定名单、预算和结果解释

顺序 A的四变体 → B的四变体 → replay-A-classic-original → replay-B-classic-original。
共8唯一wrapper、10Go请求，每请求同一P1连续2021-01-01至2024-01-01账户，
2020初始化；central/proxy-adverse × 成本[1,1.5,2]，总上限60账本、0trial。
内部半年不平仓、不重置策略/权益；费用5bps/滑点2bps每边随成本倍率化，1x复投。
原配置的新引擎请求也计入预算并对保存P1全指标/经济trace核兼容，不另加市场评价。
两重放各新进程且独立缓存键，对全经济响应/六trace及机制sidecar核一致。
inspect=8进程、覆盖10合同，合成评价另列，不记真实历史消费。失败/缓存节省不追加实验，
未知消费且不重试；协议/身份/trace/现金或兼容错误持久停批，不能恢复成普通完成。
缓存读取/JSON解析/核验与已建立预算后的逐项冻结身份检查均属于该停批边界。
每个原版本响应在下一请求前立即核保存P1兼容，两次重放亦立即核；错误响应不进入成功缓存。
保留既有4GiB每请求trace及8GiB自由空间保护；顺序观测，不预先承诺时长或并发。

报告复用市场输出，0新评价。classifier原版/recent须满足全部v2开发门槛；
对各自两个简单方向基线，在两场景1.5成本均全程收益更高、≥4/6半年不差、DD不更差。
每骨架最多留一个classifier版本，按最差g、中位g、较低全程DD、原复杂度及稳定来源键排序；
配置复杂度相同则原记忆库优先于新增recent变体。无合格增量则停止Lorentz局部搜索。
简单基线表现有价值只报告，不自动另开策略项目；P4/Optuna不继承本授权。
这是暴露历史的开发优先级比较，尚非显著性、跨seed增量、未来有效性或采用结论。
