# Project tasks
Updated: 2026-10-04 07:58 Asia/Bangkok

## Goal and completion criteria
当前授权目标 **in-progress**：在当前项目根目录初始化 Git，提交源码／配置／测试／参考材料／文档，并通过 SSH 推送到 `git@github.com:BurgerWang/lorentz_go.git`；检查提交内容及远端历史，不强推、不覆盖远端已有工作。行情数据、约105GB本地研究结果／轨迹、环境、二进制及生成目录保留本地并排除入库，不重启任何研究。

此前研究目标 **verified-complete**：用户批准的冻结P4真实批次、独立Astra/xhigh研究核算及结果审计全部关闭。上限768attempts/920Go/1872账本；实际768attempts/296Go/616账本，0运行失败/未知，两次新进程全经济/state重放一致。仅A/B classic-original×seed17/42/73、classifier64＋management64及冻结24邻域/三成本，无扩域/补尝试/额度转移。按预定门槛六中央全部邻域拒绝，**0候选晋级**；本合同研究不进入2024–2025或其他后续批次。
P0–P3及P4工程/完整prepare/inspect/独立工程审计已verified-complete。所有v1/P0–P3实现、数据、预算、研究产物与拒绝保留，所有冻结源码/helper/二进制/manifest不改；不扩品种周期、不实盘部署、不补搜W1，不自动进入2024–2025或其他后续阶段。此次 Git 提交／推送由2026-10-04用户单独授权。


## Completed
- **Git初始化及提交边界检查 verified-complete**：原目录main／SSH origin已建立；177文本文件约4.7MiB，源码／配置／测试／依赖锁／参考署名齐备。`.gitignore`和README说明本地大体量文件留存边界；Astra/xhigh独立暂存审计关闭，无blocking。核全部index blob与工作树逐字节一致及24个冻结helper／plan身份，常见凭据扫描无候选；未重跑研究或修改冻结代码。仅新改文档／忽略规则空白检查通过；既有Pine／CSV换行和空白原样保留。实际提交与远端推送仍待完成。
- **v2 P4真实执行/核算/独立结果审计 verified-complete**：768个native COMPLETE/12块×64，920预算记录全complete，0failed/interrupted/reserved，halted=null；实际296Go/616账本。搜索217Go/434账本＋551缓存attempts；144邻域73Go/146账本；6压力4Go/24账本；预定首末两重放2Go/12账本，全经济/state一致。6中央4唯一配置、全三成本门槛通过，但0邻域合格、0研究对象。工程/实际来源/冻结身份均保持，`execution/summary.json` 顶层independent_result_audit=closed。
- **v2 P4独立Astra/xhigh核算与审计关闭**：research独立270158关系检查/0差异，核768native/六维反馈/排序及来源、920预算行、1872报告出现次数(含缓存)/11232半年、188独立exit路径与归因文件。audit核全部616经济trace＋296state身份，10完整原始现金账户/530335事件覆盖4唯一中央两资金费/全部6种场景×成本组合及两全classifier键偏移不可行样本，6配置state210384bar；未逐一独立重算全部616现金或完整特征归一化/距离，复用冻结生产验算与闭合工程检查。无本批终点强平样本，不虚构新覆盖。`execution/{research-check.json,result-audit.json,audit-evidence/}`已保存，经济投影2c6f3dfd1aac5e4f49fb0ff9bb786cdb6f3fcfe6abd2ec58a5dd752bf80aa086保持，最终无阻断。
- **v2 P4拒绝及解释**：A三seed均13/24<20，growth保持率62.1606%/64.2766%/64.2766%<70%；B17/42=11/24、B73=10/24，增长过线仍拒绝。A每组11不可行邻点全部DD超线；B集中在DD/正半年/最差半年/复合收益，交易数量未造成本批拒绝。A分类器neighbors82→83、CCI41→42且ATR21→14；B分类器全部保留prior-anchor，收益改善主要来自持仓48→96，不能称分类器增量。A17与A42/73参数不同，1.5/2成本经济相同但1成本不同；B17=42全参数与经济相同。A10笔pullback贡献60.42%净利、最大持仓417小时；中央收益改善不挽救邻域拒绝，不推翻旧W1结论或成为未来验证/采用依据。
- **v2 P4工程 verified-complete**：仅新增 `tuning/mechanism_p4{,_domain,_search}.py`、两组Python测试及 `MECHANISM_P4_CONTRACT.md`；复用原P3 Go引擎，旧实现/数据/预算不改。冻结A/B各classifier4键＋management4键/原网格±一格；特征镜像、D1/envelope、类型/维数/其余开关固定。实现12串行64attempts原生journal、六维有程度native constraints、来源/锚点保留、独立P4缓存、预算和未知不重试/耐久halt；24邻点先冻结、失败保留分母与真实成交路径诊断、三成本压力、预定首末新进程重放及完整响应即时核验。
- **v2 P4检查及独立Astra/xhigh工程审计关闭**：22唯一Python检查通过（9 Contract最终24.952s＋1实际合成133.942s＋12搜索/恢复合成检查复用有效10整套/2定向结果）；同一审计修复后post流程1项1.567s定向通过。真实合成累计4Go/16账本，首轮核算通过但损坏数值反例不够可表示，改为1%后拒绝通过，未弱化生产容差。auditor独立定向复核两P2：24邻点短名匹配；complete及complete-with-failures两已审计终态重入保持summary字节、不加载Evaluator、不新增消费/停批。重放不一致在成功缓存前failed+halt，坏summary持久halt，未知/缓存中断恢复均复核通过。最终无开放发现；`p4-prepared/engineering-audit.json` 绑定最终manifest；研究合同/13,122纯编译及全部中央可生成24邻点由research_p0（Astra/xhigh）核验，报告持久化 `contract-research.json`。
- **v2 P4完整准备 verified-complete、0市场消费**：`results/mechanism-v2/ETHUSDT/1h/p4-prepared/` 保存完整13,122 wrapper（A/B各6,561）及逐一Go batch validate；38完整代表配置×search/full两计划共76实际Go inspect，覆盖原锚点/各轴边界/组合上下极值，全部evaluation_calls=0，不冒称13,122独立数据inspect。lineage/domains/plans/pipeline/manifest/preparation/false批准模板完整；26子预算总和768attempts/920Go/1872账本。最终CLI inspect及load_run通过，false模板真实run在建立execution/市场预算/study前拒绝；准备验收当时无execution，市场Go/账本/trial消费全0；现有真实执行另列。
- **v2 P3真实执行/核算/独立结果审计 verified-complete**：10complete/10Go/60实际账本/0trial；0failed/interrupted/reserved/cache替代，halted=null。8唯一配置及两独立新进程重放按冻结顺序完成；两原版与P1全经济响应/6trace完全一致，两重放经济/state trace一致。执行终态 `p3-prepared/execution/summary.json` 顶层audit closed；research-check/result-audit/resource已持久化，经济内容身份5080d369969ae724823fe6e3bab452ebe9e31b95ec39b573a43ed5db8b39102f。
- **v2 P3结果**：唯一配置仅原A/B达全部v2门槛，且在两场景1.5成本较各自两个基线总收益更高/DD更低，A均5/6半年不差、B均4/6，符合开发增量资格；recent均数值门槛失败且只3/6不差。A recent中央净68.9386%虽高于原55.4107%，但DD35.6215%、最差半年−13.8565%、仅2正半年；B recent净−9.5995%/DD32.1692%。冻结规则保留原A/B，stop_lorentz_local_search=false仅支持后续提案，不授权P4；旧W1邻域拒绝不变。
- **v2 P3独立核算及审计覆盖**：research Astra/xhigh复核全部60报告/360半年/六维违约/三成本/16增量，审计 Astra/xhigh核全部60经济trace＋10state身份和预算，8完整原始账户覆盖全部6个修改配置/两场景/三成本/跨半年资金费；原版复用完全一致的P1抽审证据。8唯一state全280512bar核方向/窗口相位/票和Signal/年龄/Start/End，共享readiness/filter mask一致；未逐bar独立重算全向量归一化/邻居距离接受，复用闭合工程反例。全部终点已空仓，无本批强平事件；保留先前该行为验收，不虚构覆盖。墙钟136.3927s，waited-child maxRSS112648KiB，非同时总内存。
- **v2 P3工程/准备 verified-complete（准备时真实批次未批准，现已另获批）**：新增tagged Go `indicator/mechanism_p3.go`、`strategy/mechanism_p3.go`、`cmd/lorentz/mechanism_p3.go` 及两组Go测试；`tuning/build_mechanism_p3.py` 生成3个精确overlay/新binary/实际build身份；新增 `tuning/mechanism_p3{,_client}.py`、`test_mechanism_p3.py` 与 `MECHANISM_P3_CONTRACT.md`。旧文件/旧bin/P0/P1 helpers保持。四版本共享原状态机、下游daily/risk/pullback和账本；recent完整乱序FIFO逐项到期清理，冻结绝对j%4!=0与backward标签；基线保留原向量readiness并叠加自身readiness，有限特征距离不参与方向。侧car为最终入场过滤前指示器状态，不误当成交。
- **v2 P3相关检查及独立Astra/xhigh工程审计关闭**：9新Go测试及相关包测试/race、vet通过；10唯一Python测试通过（初7项35.566s含2实际合成联动，修复后8Contract定向0.299s复用未变联动）。合成协议实际6Go/36账本＋4inspect，原版新旧引擎全经济响应/6trace一致，独立重放经济/state trace一致；mock预算另计不称Go实际评价。auditor独立4项反例0.020s通过，关闭两项P2：缓存读取/JSON/身份错误及逐项source检查均持久halt且不重finish终态；original/P1兼容及replay接收后立即核，错误1请求即failed+halt，不先执行全批。最终无阻断；审计 `results/mechanism-v2/ETHUSDT/1h/p3-prepared/engineering-audit.json` 绑定最终manifest。
- **v2 P3首批完整准备 verified-complete（执行前0真实评价快照）**：`results/mechanism-v2/ETHUSDT/1h/p3-prepared/` 含8原策略完全保留的wrapper、10请求matrix、lineage、连续plan、8实际Go inspect/10合同（每项6账本、evaluation_calls=0）、manifest、false模板及验收metadata；CLI inspect/冻结identity通过，false批准的真实CLI run在创建execution/预算前被拒绝。准备验收时无execution、市场消费0；现有真实execution另列，旧P1仍可load_run核验；Go实际35生产源码及生成overlay等41个build输入绑定，新旧默认config相同。
- **v2 P1/P2 verified-complete（执行、核算及独立结果审计关闭）**：13complete/13Go/78实际账本/0trial，0failed/interrupted/reserved/cache替代、halted=null；11配置及两次独立绕缓存重放按冻结顺序实际计算，两次全指标/经济trace一致。P2复用结果、0新增评价；全四格、两场景三成本、六半年MTM/跨界/归因/持仓/盈利集中度/相邻配对交互已汇总。`p1-prepared/execution/summary.json` 顶层 `independent_result_audit=closed`，`result-audit.json` 为最终独立审计报告。
- **v2 P2结论**：6/11唯一配置通过全部新连续开发数值门槛（A四格、B-M-H4、B-original）；修改格改善资格为0，仅A-original=A-B-S与B-original保留为后续研究对象。B-M-H4虽全程收益/DD较原B改善，但两资金费场景均仅2/6半年不差，不达预定4/6。旧W1邻域拒绝保持，不称修复原候选、未来验证或分类器增量证明。完整指标见下方及summary。
- **v2 P1独立Astra/xhigh核算与审计**：research核78报告账户的六段复利/交易/跨界/分项/门槛及配对；独立auditor核全部78trace身份、两重放全经济响应与12trace，抽查8完整账户独立复算现金/实际价格/费用/funding/全程及半年DD，覆盖跨界资金费、4h/48h、三成本与付费终点强平，旧50来源身份保持。无剩余实质发现；其余账户复用生产核验和独立报告算术，不声称78账户均经第二遍原始trace复算。墙钟148.976s，waited-child maxRSS101516KiB；不代表同时总内存或并发实测。
- **v2 P0独立Astra/xhigh工程审计关闭**：auditor未参与实现，独立只读SQL六公式核全部2966 COMPLETE/21cache/2945唯一/193可行，核11配置/13inspect合同/false模板及50个旧源文件身份。发现P2持久halt恢复可能被误记普通失败完成；主代理补run/request停批检查，原auditor独立重跑恢复反例通过并关闭。最终报告 `results/mechanism-v2/ETHUSDT/1h/p1-prepared/engineering-audit.json` 绑定最终manifest，无剩余实质阻断；未重复全项目审计。
- **v2 P0 verified-complete（工程、检查、首批准备及独立审计关闭）**：新增 `tuning/mechanism_{v2,sources,metrics}.py` 及两组Python测试、`backtest/mechanism_test.go`；旧helper/Go实现/二进制/预算/数据均保持。新模块实现旧六维有程度反馈、连续MTM半年/全程DD/交易归属/成本与归因、矩阵、有限局部域冻结规则及独立固定请求预算/缓存/恢复/批准入口；不实现搜索或P3变体。
- **v2 P0重评分与首批准备检查通过（零真实评价）**：全40journal/2966COMPLETE（2945唯一、21cache）六维可行性与旧标志逐一一致；193可行记录/174唯一，原拒绝不变。research_p0 Astra/xhigh独立公式与来源核验关闭；主代理集成提取验证父来源/只读SQL/旧全响应并核源文件前后身份。aligned预留 `4436b4ee82e331c6cde698964655b4b381a2ebad4d57b9fd44ecaae185447a47` 不进入首批。
- **v2 P0准备产物**：`results/mechanism-v2/ETHUSDT/1h/p1-prepared/` 含完整11配置、13请求matrix、来源/全量重评分、连续plan、local-rules、inspect及manifest/未批准template。11个Go inspect进程覆盖11唯一配置，两次重放复用完全相同已检合同，共13请求/78账本合同均evaluation_calls=0；CLI只读inspect/旧冻结helper与source身份检查通过。准备验收时 `approved=false`、无execution或新市场预算消费；该零评价快照保持，后获批P1消费单列。
- **v2 P0相关检查通过**：新19项Python（17主检查70.445s＋配对晋级/交互及持久halt恢复2项；最终Rules 5项0.023s）、旧初始化/链式D1/envelope/k上限2项；Go race backtest/strategy/indicator/cmd，vet通过；已有四bar/风险退出优先级/再入场及新增迟到pullback实际成交后4h退出通过。包含真实Go合成连续36月/两场景三成本/独立重放及六段对账、缺trace拒绝、预算缓存/未知状态不重试。首轮适配遗漏已修复，实际合成Go共3次/18账本（首次失败1/6＋最终2/12），mock预留另计；真实历史Go=0、Optuna=0。
- **W1后计划verified-complete（仅文档）**：`MECHANISM_V2_PLAN.md`定义P0零真实评价、v1冻结保护、有程度且不软化硬门槛的反馈、连续MTM边界、A/B各四格及B原48h/公共控制/重放。P1上限13Go/78账本/0trial；P2复用结果0新增评价；P3最多10Go/60账本；P4最多768attempts/920Go/1872账本，后两阶段均有晋级条件且需另行冻结批准。独立Astra/xhigh计划审阅无实质阻断，局部域与停止条件两处措辞澄清；预算算术、identity长度和文档结构自检通过。没有新增策略评价或修改冻结实现，不代表v2工程/研究已验收。
- **W1穿透分析verified-complete**：两research Astra/xhigh核40journal/3072尝试与Optuna机制，主代理复算6条完整连续trace交易/费用/资金费分组；独立audit_upgrade_plan核关键trace/邻域/语义及最终文字，全部关闭。报告 `W1_ANALYSIS.md`：193可行记录去重174配置仅2个classifier字段模板（B的D1上下文仍变化），C1仅2可行、C3为0；二值约束丢失失败程度，C3重开分类器宽域。A13笔pullback贡献56.92%净利润、2023主入场贡献为负；B6笔48h退出合计−2495.35美元，折间强平改变跨界结果。两中央Classic使用最早1000索引池，并非最近历史；邻域完整开发可行率A18/24、B17/24，原19/24拒绝保留。本次新增评价0；新协议/机制实验只是建议。
- **1h W1 verified-complete（执行与核算完成；无合格候选、研究不晋级）**：3072attempts/3076 Go/36870消费账本额度，含77初始化拒绝请求的924预留；成功新计算2999Go/35946账本，21cache不另算市场样本，29重复特征Go前拒绝。两族三seed的36 C1组完成，Classic17/42完成C2/C3，其余4分支因C1无可行锚点合法跳过，不转移1536尝试余额。两个Classic中央候选的2021–2023 proxy-stress固定连续1.5成本central净收益55.4107%/20.0647%，是开发历史；各邻域19/24低于20/24、连续检查虽过仍弃权，2024H1主/所有seed为现金0笔，outer仅2公共控制。research逐目标/48邻域/连续账户/预算核算一致；独立Astra/xhigh审计40原生journal/全部3072试验与cache、630重要trace身份及8完整现金/费用/funding/DD账户，无阻断（未逐一复算35946账户）。批次及scope审计标记已关闭于 `results/robust-v1/preparation-v3/ETHUSDT/1h/execution/{W1,W-search,W-local}/summary.json`；详情 `/tmp/lorentz-w1-final-audit/result.json`。
- **1h B1 verified-complete（接入/重放/计时，非盈利验收）**：26Go/294账本/0trial，全部complete、12组独立重放指标/trace一致；research核对全部294 Go/报告账户、场景/来源/预算。Astra独立审计核294 trace身份并抽查4账户完整现金/funding/DD，无阻断。墙钟443.6745s，Go requests累计352.1023s（strategy345.8870+ledger6.2112）；load是会话ready时间，不能逐请求相加。waited-child maxRSS135272KiB，未验证同时总内存/并发能力。结果 `results/robust-v1/preparation-v3/ETHUSDT/1h/execution/B1/summary.json`。
- **1h R0 verified-complete（执行/核算，候选全未通过）**：17Go/51账本/0trial，无失败/缓存；两独立重放、Classic101/aligned213旧/新三折兼容通过。全部11×3报告/365日网格与半年门槛经研究核算；独立Astra审计核对45新trace身份、抽查4账户完整现金/费用/funding/DD，无阻断。五Pareto三成本全负，旧control52初筛通过但不升级/采用；另6legacy账本无trace。结果 `results/robust-v1/preparation-v3/ETHUSDT/1h/execution/R0/summary.json`。
- **v1 P1工程 verified-complete**：`tuning/robust_data.py` 和 `cmd/lorentz/robust_eval.go` 实现不可变 bundle、四周期/profile、D1重叠/初始化、因果资金费场景及来源披露、受保护历史后继/旧候选迁移。10项数据测试及真实Go逐周期合成联动通过。Astra/xhigh独立审计关闭来源篡改和链式预热/stride相位问题。新入口协议3，旧协议/默认行为保留。
- **v1 P2工程 verified-complete**：Go原账本流式输出交易、费用、资金费、执行边界与收盘权益；`tuning/robust_trace.py` 独立核对完整输入事件、现金/费用/净收益/胜率和全采样路径DD、UTC日收益与计划平仓资金接续。7项真实Go联动通过；Astra/xhigh独立风险/保本、实际资金接续、trace写失败及删事件/改来源反例复核关闭。DD为观察到的执行边界与收盘路径，不声称逐tick真实盘中DD。
- **v1 P3工程 verified-complete**：`tuning/robust_search.py`/`robust_budget.py` 实现版本化目标、36月六半年内层/半年outer、两族三seed C1–C3、确定性收缩/邻域、截止profile/父来源、SQLite全局及子study预算、缓存/中断/原生journal恢复。15项测试、有界真实Go合成中断恢复、768尝试合成callback编排覆盖及独立Astra/xhigh审计通过。4进程预算与跨startup分段恢复确定性通过；该工程验收未做真实策略搜索，后续W1状态见当前记录。
- 四周期真实数据bundle已生成于 `data/ETHUSDT-robust-v1/{15m,1h,4h,1d}`，只复制/披露现有来源。全包6600笔费率含3197 exact/3403 proxy；最早缺前一完整8h桶的proxy不允许计分。目标candles来自原包，D1/funding/raw marks来自expanded；历史价格/成交量差异不改写。
- **v1 P4工程 verified-complete**：`tuning/robust_pipeline.py`、`robust_report.py`、`tuning/protocols/robust-v1.json` 实现统一CLI、代表接入/重放、分批依赖、外层/固定报告、共享5000次bootstrap、依赖预警、实际时刻固定冻结/未来12月后继追加。13项合成检查及独立Astra/xhigh审计关闭；冻结旧bundle身份与完整helper/Python/Optuna/Numpy绑定修复、反例拒绝及真实Go合成future inspect通过。P3的Numpy合同字段由原auditor定向复核关闭，未改算法/目标/门槛。
- 四周期 R0/B1 **准备与inspect verified-complete、0策略评价**：最终 `results/robust-v1/preparation-v3/ETHUSDT/{interval}`；1h R0=17Go/51账本、B1=26/294、58inspect、145独立文件；其余各B1=26/294、41inspect、83文件，共181inspect。完整配置/计划/来源/迁移、manifest、inspect和未批准模板实际存在，主代理及Astra独立复核通过；准备验收时模板全false、无execution；随后1h执行状态见当前W1及R0/B1记录。v2与初次15m旧准备全部保留，因合同依赖修复已失配，不能恢复。
- 存量主线 **M1–M6、增强Optuna T1–T4、B0和1h S1 verified-complete**。旧验收复用，不重做旧研究。B0实际20Go/60账本/0trial，10组独立重放一致；S1实际512尝试/509Go/1527账本、3缓存、0失败；两族候选 Classic101/121/228、aligned213/215均为2024开发筛选。旧S2冻结文件保留，S2/S3/V1未执行。
- M6历史22/22请求、0搜索，RVOL连续账户基准净收益−6.5431%/93笔/DD25.6484%，1.5/2成本−12.4321%/−17.9543%，`keep_disabled`；结果及审计对账保留 `results/upgrade-v1/m6/{retrospective/summary,replay-check}.json`。这些回顾结果本轮未重算。

## Remaining
- [ ] **Git 发布 in-progress**：配置本仓库提交身份与 SSH origin，检查暂存内容及独立审核，提交并推送 main，随后核对远端提交和工作区状态。
- **P4本轮无未完成项**：P4结果核算及独立结果审计已关闭，0候选晋级。本冻结A/B局部改参路线拒绝，不补搜索、不扩域、不降低门槛，也不自动推进固定2024–2025回顾。下一具体动作仅在用户提出并批准新的研究目标后制定独立方案；不把本task或审计关闭当新授权。

- [ ] **后续研究未授权／本版F/Q晋级条件不满足**：W1弃权0笔已违反第10.2节每半年至少15笔，后续窗口无法补回，本冻结版本不能晋级F/Q。W2–W4及其他周期仍未执行、未授权，不能称已评价或凭推导标取消；若另获批可作既定窗口诊断，不能挽救当前每窗硬门槛。下一步等待具体新研究目标/批次授权，不补搜或降低门槛。真正未来验证未开展；2020–2026-10-01现有历史按暴露回顾处理，固定冻结后需未使用完整12月及另批批准，样本不足拒绝，不承诺盈利。
- [ ] 存量Classic外部逐值parity未证明，差异见 `reference/tradingview/README.md`；实时持久Engine/盘中保护/部署不在本轮范围。

## Decisions and goal adjustments
- 2026-10-04用户明确授权在现有项目根目录初始化、提交并通过SSH推送到 `BurgerWang/lorentz_go`。远端只读检查无已有refs；仅设置本仓库身份，复用用户已有项目的BurgerWang提交身份，不修改全局Git配置。大体量研究产物、行情及本地环境不入Git但保留原位置，研究结论和旧冻结字节保持。
- P4终态零候选，按冻结邻域硬门槛保留拒绝并结束本批；中央收益/三成本通过或seed重复均不能覆盖稳定性失败。所有旧研究产物及拒绝继续保留，后续批次没有授权。
- 2026-10-03 14:29用户在明确交付P4单批预算后“好，请继续”授权真实P4固定768attempts/920Go/1872账本；新 `approval-P4.json` 绑定最终manifest及用户消息。此授权不延伸到2024–2025、扩域/追加预算或采用；准备时零市场消费的preparation/冻结文档快照保持，实际消费另列execution。
- P4域冻结为4＋4键，既满足每块≤6且81组合覆盖64尝试，也保留全部既有结构/D1上下文；真实效果未知。旧P3 qualified anchor只作为prior-anchor来源，不伪装P4 trial/缓存；工程合成native trial与真实市场预算分列。最终未批准manifest仅因同一审计终态修复更新1个新helper绑定，全部原配置/域/76inspect/来源/计划未变；auditor精确重建初版manifest确认后复用有效检查，不重复准备或历史评价。
- 2026-10-03用户“好，请继续”授权P4下一步工程与完整准备，真实批次单独待批；采用新模块保留前序冻结合同，复用既有Go P3 original引擎与现金核验，不新增Go算法。
- 2026-10-03用户“同意批准”单独批准冻结P3真实10/60/0批次；沿原manifest与工程审计执行，不继承授权到P4或Optuna。
- P3终态：核算及独立结果审计已关闭，冻结规则仅保留原A/B，不采用recent。仅支持P4有限局部提案，不继承真实批次授权或推翻旧W1拒绝；先工程/域/配置/inspect及审计，后真实单批批准。
- P3工程验收2026-10-03已关闭：新外层实验合同隔离schema5；明确选择基线沿原全向量readiness，以免同时解除B D1初始化。未跑市场前冻结排序同复杂度优先original；所有真实兼容/增量判定留已预算、另批授权的10/60/0。
- 2026-10-03用户授权P3下一步工程与准备；沿前次交付分两次批准，真实10/60/0批次仍待完整产物及独立工程审计后另批。不改冻结Go源码/helper，采用新实验文件及独立构建注入，保留旧路径字节与经济行为。
- P1/P2终态：修改结构无合格改善，按预定规则保留原A/B为P3研究对象；该资格不推翻旧W1拒绝，也不授权P3/P4或证明分类器有效。
- W1后采用独立mechanism-v2研究合同：首轮只选入场×退出，复用Go已有实际持仓4小时上限及完整连续账本；P2汇总器先在P0实现以免重复市场评价。旧二值约束、C3宽域及fold-reset结果保持，所有修正只进新合同；P0已完成；2026-10-03用户同意具体P1首批，后续阶段不继承该授权。
- R0实际五Pareto三成本全部负收益，按预先第6节规则，旧S2/S3候选路线cancelled，不放大或重启旧赢家。旧control52仅通过2025初筛，保持对照身份，不事后升级为最终候选或采用；分类器整体有效性尚未由该有限结果判定。
- 新计划替代旧S2→S3→V1默认顺序，旧文件/成绩保留而不继承新授权。R0的2023目标/2020D1初始化及来源截止2025-01-01保持；不可变准备迁移记录保留原pending状态，实际结果checks已关闭Classic101/aligned213预定旧/新兼容对照，不替其他9配置虚构单独兼容请求。
- 历史多年份 exact 覆盖不足，约2023-11-01才有完整exact；2021起36月流程必须显式proxy-stress，两种场景不称精确资金费或证明误差界。R0/外层/Q评分仅exact。真实采用口径须逐批用户批准。
- 新 `scheduled-flat-v1` 是半年末含成本平仓、下窗空仓并接续资金的换参流程；原M6跨窗持仓账户和最终固定参数连续账户独立标记。Go仍是唯一策略和账本实现；Python复算和同比例路径接续不另实现策略。
- 正常权益保护触发按配置FAIL消费预留；数据/身份/协议/现金对账错误停止研究。超时/未知请求消费预留且不重试。预算文件丢失且已有执行状态拒绝恢复；不补缓存节省额度。
- 冻结旧bundle必须与冻结时身份一致才能检查后继；所有实际传递实现及Python/Optuna/Numpy版本绑定合同，修复后的产物使用新v3而不重写v2。P3算法、目标、空间、门槛和预算未改。
- 既有业务规则保留：费用每边5bps、滑点2bps、1x复投、收盘决策下一实际开盘、资金费(entry,exit]；原M6切换仍限同Risk/Exit合同，风险不会保证未来跳空净保本。仅已实现六特征组，实际最多7维，底层15维容量不扩大授权。

## Handoff and verification
Git当前检查点：独立审计 `audit_p0`（原Astra/xhigh）已关闭，暂存177文件；origin=`git@github.com:BurgerWang/lorentz_go.git`，main初次提交与push待执行。未运行Go评价／Optuna，研究终态保持。

当前P4交接：真实执行、独立研究核算与结果审计全部verified-complete，无阻断，原生research_p4_results/audit_p0（Astra/xhigh）均已完成。session48478退出0已收取，不重启run；日志 `/tmp/lorentz-mechanism-P4-20261003.log`。RUN `results/mechanism-v2/ETHUSDT/1h/p4-prepared/`，最终交付 `execution/{summary.json,research-check.json,result-audit.json,resource.json,budget.sqlite,audit-evidence/}`；summary status=complete，independent_result_audit=closed，audit_closure明确生产pending快照已被终态审计覆盖。没有新增后续研究授权。

实际768attempts/296Go/616账本，920complete，0failed/interrupted/reserved，halted=null；6中央×24邻点、全部6压力与两replay齐备。624预算行复用缓存(551搜索＋73后检查)，不新增市场样本/补额度。最终manifest SHA `0afe60aa71cb07dc4c2318eab801e6c13c6fdd8b503fe642f07f7df58cb81411`，approval-P4 true/template false及旧P0–P3来源均保持。关闭审计只改metadata/报告引用，9经济字段(version/status/evidence/search/centrals/checks/selected_research_objects/budget/later_stage_authorized)投影仍 `2c6f3dfd1aac5e4f49fb0ff9bb786cdb6f3fcfe6abd2ec58a5dd752bf80aa086`。resource：2026-10-03 14:30:08→15:20:44 Asia/Bangkok，墙钟3035.277803s（50分35秒），waited-child maxRSS143900KiB（约140.53MiB），非同时总内存/并发实测。

以下为central资金费/1.5成本；完整六账户与归因见research-check。全部三成本均通过，但邻域要求至少20/24及growth保持率≥70%。

| 中央对象 | 净收益% | 全程DD% | 退出交易 | 可行邻点 | growth保持率% | 结果 |
|---|---:|---:|---:|---:|---:|---|
| A17 | 71.6115 | 11.3797 | 153 | 13/24 | 62.1606 | 拒绝两门槛 |
| A42/73 | 71.6115 | 11.3797 | 153 | 各13/24 | 各64.2766 | 拒绝两门槛 |
| B17/42 | 33.4175 | 17.1960 | 151 | 各11/24 | 各128.5385 | 可行率拒绝 |
| B73 | 31.7424 | 17.3523 | 152 | 10/24 | 133.4186 | 可行率拒绝 |

A1成本需分列：A17净86.1253%/154笔，A42/73净90.9398%/153笔；不能把三个seed称全成本经济一致。全部邻点执行成功，门槛不可行与运行FAIL区分；交易路径变化分别23/24、22/24、22/24、24/24、24/24、24/24，零变化仍留分母。最终选中列表空，不从邻域换赢家。

以下工程/准备验收属于执行前快照：工程/相关检查/完整prepare/inspect/独立工程审计verified-complete、当时0市场消费，`preparation.json`及冻结文档保持该快照，实际消费只看当前execution。最终load_run及来源身份通过。actual合成4Go/16账本另计；session7010、83630、97200、18874退出0已收取，不重启。检查日志 `/tmp/lorentz-mechanism-P4-go-recheck.log`、`/tmp/lorentz-mechanism-P4-contract-final.log`、`/tmp/lorentz-mechanism-P4-prepare.log`、`/tmp/lorentz-mechanism-P4-terminal-recheck.log`；inspect `/tmp/lorentz-mechanism-P4-inspect.json`；false批准预期exit1日志 `/tmp/lorentz-mechanism-P4-false-approval.log`。

有效测试入口：`PYTHONPATH=tuning .venv/bin/python -m unittest tuning.test_mechanism_p4.Contract -v` 最终9项通过；同模块 `SyntheticIntegration` 最终1项通过；同模块 `Contract.test_complete_post_search_neighbors_pressure_replays_resume_and_closed_idempotence` 终态修复后1项通过。`tuning.test_mechanism_p4_search` 的12唯一恢复/预算/native检查复用原10整套＋新增2定向通过结果，auditor独立复核关键反例；没有要求未变Go/race/vet再次运行，原P3工程检查仍有效。

只读检查（不调用评价）：
```bash
.venv/bin/python tuning/mechanism_p4.py report --run results/mechanism-v2/ETHUSDT/1h/p4-prepared
```
本轮已从false模板创建独立 `approval-P4.json` 并绑定用户消息/最终manifest和固定768/920/1872；下列入口已执行完成，不再次run：
```bash
.venv/bin/python tuning/mechanism_p4.py run --run results/mechanism-v2/ETHUSDT/1h/p4-prepared --approval results/mechanism-v2/ETHUSDT/1h/p4-prepared/approval-P4.json
```
实际执行及全矩阵开发核算/真实资源/独立结果审计已闭合，下一步不运行后续阶段；不能把已暴露开发成绩称未来验证/采用资格。前序P3终态如下。

当前P3交接：工程/准备/独立工程审计、已批准实际执行、结果核算及独立结果审计全部verified-complete，research_p0/audit_p0（Astra/xhigh）已返回关闭，无阻断。session66007退出0已收取，不重启run。根目录 `results/mechanism-v2/ETHUSDT/1h/p3-prepared/`，final manifest SHA7256b535a4ffeecd8e9ab1aa23447cb32f2e273ab343a967ed6ab3af447249f3保持；approval-P3 true/template false；preparation及冻结文档头部保持执行前0评价快照。最终交付 `execution/{summary.json,research-check.json,result-audit.json,resource.json,budget.sqlite}`。

summary顶层independent_result_audit=closed，audit_closure明确覆盖comparison.selection_status中的生产时pending快照；仅关闭审计metadata及添加只读报告引用，auditor绑定的全部records/reports/checks/comparison/budget经济字段身份保持5080d369969ae724823fe6e3bab452ebe9e31b95ec39b573a43ed5db8b39102f。最终load_run/authorize身份核验通过，旧P1来源/50源文件和所有冻结工程输入保持；最终只读report exit0，`/tmp/lorentz-mechanism-P3-final-report.json` 经济投影与独立审计完全一致。只读报告入口，不执行完成批次：
```bash
.venv/bin/python tuning/mechanism_p3.py report --run results/mechanism-v2/ETHUSDT/1h/p3-prepared
```

顺序A原/RQ/4bar/recent → B四版本 → replay A/B original，实际10Go/60账本/0trial。每项2021-01-01→2024-01-01连续账户/2020初始化，central/proxy-adverse×成本1/1.5/2；内部半年不强平、不重置。2026-10-03 13:24:52→13:27:08 Asia/Bangkok，墙钟136.3927s，waited-child maxRSS112648KiB（约110.01MiB，非同时总内存）。60经济trace逻辑859162352字节，10indicator sidecar逻辑171211462字节，非磁盘实际分配。日志 `/tmp/lorentz-mechanism-P3-20261003.log`；结果核算/审计新增评价0，P4/Optuna消费0。未重复未变工程测试，复用既有9新Go、10唯一Python、race/vet及闭合工程审计。

以下为central/1.5成本；完整门槛含两资金费场景三成本，增量含两个自身基线×两场景。完整归因/成本/funding/六半年/跨界暴露与state诊断见持久报告。

| 版本 | A净收益% | A DD% | B净收益% | B DD% | 完整v2门槛及增量资格 |
|---|---:|---:|---:|---:|---|
| classic-original | 55.4107 | 13.2089 | 20.0647 | 17.1960 | 两者通过并保留，A 5/6、B 4/6半年不差 |
| rq-direction | 3.3133 | 45.1486 | −40.6792 | 55.0574 | 两者未通过；仅方向控制 |
| momentum-four | 12.6972 | 41.3252 | −44.2629 | 55.7225 | 两者未通过；仅方向控制 |
| classic-recent | 68.9386 | 35.6215 | −9.5995 | 32.1692 | 两者拒绝，只有3/6半年不差 |

原版评分期票仍来自初始化最早1000索引范围，recent票年龄≤999bar，全局history/窗口/绝对phase一致。sidecar在daily/risk/最终入场过滤前，Signal年龄不能当实际持仓。P3结论只限两个冻结骨架/方向对照。P4当前另获单批768/920/1872授权，跨seed开发检查进行中；真正未来未使用验证未开展。以下P1终态为已关闭的前序交接。

前序P1交接：v2 P0/P1/P2均verified-complete，原生research_p0和audit_p0（Astra/xhigh）已完成并关闭，主代理集成及最终身份检查通过。P1 session69837已退出0并已收取，不再运行。完整根目录 `results/mechanism-v2/ETHUSDT/1h/p1-prepared/`；最终交付为 `execution/{summary.json,result-audit.json,research-check.json,resource.json,budget.sqlite}`。summary顶层审计closed；pairing中的pending字样保留生产时快照，由顶层audit_closure明确覆盖，经济字段保持auditor绑定身份。manifest、冻结代码/引擎/数据/配置/计划及原50来源身份未变，approval-P1.json只授权已完成P1，template仍false。不可重跑完成批次或改冻结计划头部的旧准备状态；当前状态以本task与执行终态为准。

本批实际13Go/78账本/0trial、11唯一配置＋2重放，全部complete，无失败/缓存替代/未知，halted=null。时间2026-10-03 11:26:32至11:29:01 Asia/Bangkok，墙钟148.976s，waited-child maxRSS101516KiB（约99.14MiB，非同时总内存）；78trace逻辑大小1,122,092,647字节，非实际磁盘分配空间。日志 `/tmp/lorentz-mechanism-P1-20261003.log`；资源已持久化于execution，P2及结果审计0新增评价。

已执行固定顺序：A四格（M-S/B-S/M-H4/B-H4）→B四格→B-original（48h）→Public-classic/aligned→A-original=A-B-S及B-original各一次独立新进程绕缓存重放。每请求同一2021-01-01至2024-01-01连续36月账户，2020起初始化、central/proxy-adverse × 成本[1,1.5,2] =6账本；内部半年不强平、不重置。公共控制保持原准备完整配置（含ADX b=2及daily初始化2019-11-27）；A/B四格仅变化约定入场/退出及依赖停用字段，B原daily特征上下文冻结。

以下为central/1.5成本，门槛列综合两场景三成本；完整78账户、六半年、交易归因/多空/退出/持仓/费用/funding/集中度及配对交互在summary，旧fold诊断只复用完全相同原配置，不补跑或拼接。

| 配置 | 净收益% | 全程DD% | 退出交易 | 全部v2数值门槛 | 研究资格 |
|---|---:|---:|---:|---|---|
| A-M-S | 28.5368 | 10.6129 | 155 | 通过 | 无改善资格 |
| A-B-S（原A） | 55.4107 | 13.2089 | 160 | 通过 | 保留原结构 |
| A-M-H4 | 28.5368 | 10.6129 | 155 | 通过 | 无改善资格 |
| A-B-H4 | 22.2600 | 10.9478 | 168 | 通过 | 无改善资格 |
| B-M-S | 22.3999 | 33.3368 | 150 | 未通过 | 不保留 |
| B-B-S | 190.2823 | 57.6345 | 186 | 未通过 | 不保留 |
| B-M-H4 | 21.4095 | 15.5047 | 163 | 通过 | 仅2/6半年不差，不晋级 |
| B-B-H4 | −31.9163 | 36.8012 | 249 | 未通过 | 不保留 |
| B-original | 20.0647 | 17.1960 | 154 | 通过 | 保留原结构 |
| Public-classic | −87.0720 | 87.6549 | 830 | 未通过 | 控制 |
| Public-aligned | −73.3931 | 74.6490 | 380 | 未通过 | 控制 |

机制结论：A-M-S与A-M-H4六经济trace一致，此main路径的持有上限不产生变化；原A加pullback在signals下增加26.8738个百分点，但H4下减少6.2769个百分点，入场×退出存在明确配对交互（central/1.5）。原A换H4虽DD减少，净收益由55.4107%降为22.2600%；B-M-H4全程较原B收益略高/DD更低，却未覆盖足够半年，因此按冻结规则不替代原B。B-B-S高收益伴随57.63%DD而被拒绝。proxy-adverse/1.5原A净55.3639%/DD13.2092%，原B净19.9967%/DD17.2200%。修改格晋级0，原A/B仅可提出P3研究；旧W1邻域拒绝不变。这是暴露历史开发证据，尚未检验分类器相对方向基线的增量，未使用未来验证仍未开展。

P3合同已落实：核方向使用各自classic.kernel_h/r/x，动量使用source[t]−source[t−4]；过滤前替换原始方向并一致重算状态变化/年龄/动态退出。recent完整FIFO剔除窗口外票，保留backward标签、距离/阈值、history_start与绝对索引/stride，无expanding。工程/测试/独立审计及完整准备已关闭；10Go/60账本/0trial真实批次执行/核算/独立结果审计现已关闭，继续条件沿计划第5节，无增量停止局部搜索。详细合同见 `MECHANISM_P3_CONTRACT.md`。

P1旧终态仅只读报告，不再执行其run：
```bash
.venv/bin/python tuning/mechanism_v2.py report --run results/mechanism-v2/ETHUSDT/1h/p1-prepared
```
最终集成检查：load_run冻结合同通过；审计关闭只改metadata及报告引用，auditor经济内容身份保持；预算13/78/0、两replay通过、旧template false及P1approval true、P3/P4授权false。上述只读report exit0，最终输出 `/tmp/lorentz-mechanism-P1-final-report.json` 与持久summary经济身份一致；没有改P0代码或重跑旧测试/历史研究。

检查日志：`/tmp/lorentz-mechanism-{tests,pairing,init,go,vet,prepare,inspect}.log`。新19＋旧2项Python/相关Go race/vet通过；原auditor独立定向复核1项通过；CLI inspect身份通过，原源文件/冻结helper identity保持。未重新核算80余GiB旧W1 trace（复用前序核算），本次只读取已成功指标；不把旧重评分称全W1新连续评价。

当前代码：独立 `bin/lorentz-robust-v1`（SHA d3fd4f7eded9fe2a3a7b7e0526fa84836864ea0628076516765ef3326ea73197），旧 `bin/lorentz-enhanced-optuna` SHA21d43228097625ce1cdd35b9f5e35682984914230231467631f6b8ef95be12be保持；旧 `bin/lorentz`/`lorentz-upgrade`亦保留。挂载无可用Git仓库，使用 `-buildvcs=false`；仅用户提供全局AGENTS适用。

检查：先前完整Python113项通过 `/tmp/lorentz-robust-python-regression.log`，随后P2完整性7项和P3恢复15项分别通过 `/tmp/lorentz-robust-p2-complete.log`、`/tmp/lorentz-robust-p3-recovery.log`；P4最终13项通过11.637s。Go race除market本地监听首次sandbox拒绝外通过，market获准单包race通过 `/tmp/lorentz-robust-market-race.log`；最新vet通过，有限原版参考通过但不证明TradingView逐值等价。最新完整Python121项通过43.419s `/tmp/lorentz-robust-python-final.log`；定向P4 13项/P3 15项通过 `/tmp/lorentz-robust-p4-final.log`、`/tmp/lorentz-robust-p3-runtime.log`。

Native agents：前序research_sources、audit、audit_search及Sol组件工程均完成；research_results Astra/xhigh已完成R0/B1全部指标、门槛、时长及量级核算，原audit Astra/xhigh已关闭R0/B1独立结果审计；现已分别完成有界W1首批只读核算和独立启动状态审查；启动审查无阻断（含12trace身份及1账户完整现金/funding/DD复算），终态研究核算及独立审计已关闭，无阻断；无合格候选属于本次真实研究结果。主代理负责运行/集成/task.md；本轮未修改任何冻结实现或二进制，不重跑旧测试/研究。1h R0 session34299已退出0，日志 `/tmp/lorentz-robust-R0-1h-20261002.log`；结果 `results/robust-v1/preparation-v3/ETHUSDT/1h/execution/R0/summary.json`，R0/B1结束时共享预算 `execution/research-budget.sqlite` 为43Go/345账本/0trial（R0 17/51+B1 26/294），0失败/未知/缓存替代，当时W/F/Q预算使用全0。R0/B1独立research/audit均已关闭；B1 session53835已退出0，日志 `/tmp/lorentz-robust-B1-1h-20261002.log`；资源观测 `/tmp/lorentz-robust-B1-1h-resource-20261002.json`（waited-child峰值RSS，不代表同时总内存/并发实测）。批准文件 `results/robust-v1/preparation-v3/ETHUSDT/1h/approval-R0-B1-20261002.json` 只包含R0/B1。

存量续接：原成果 `results/upgrade-v1/`、`results/optuna-round1/`、`results/enhanced-optuna-v1/` 全保留；完整旧用法/合同见 `UPGRADE_PLAN.md`、`ENHANCED_OPTUNA_PLAN.md`、`ENHANCED_OPTUNA_USAGE.md` 与各summary/replay-check。不重算旧有效验收。当前动作：W1执行、终态核算及独立审计均已关闭，停在无合格候选结果；不得重复启动W1或耗用剩余额度扩搜。新批准文件 `results/robust-v1/preparation-v3/ETHUSDT/1h/approval-W1-20261002.json` 仅含W1，R0/B1原批准保留；W2–W4/F/Q未启动。W1实际墙钟21231.4624s（5h53m51s），waited-child maxRSS191336KiB，不代表同时总内存或并发实测。

W1终态：**session30340已退出0并已重接收取结果**，不再运行。日志 `/tmp/lorentz-robust-W1-1h-20261002.log`，资源 `/tmp/lorentz-robust-W1-1h-resource-20261002.json`；原生journal/trace在 `execution/W-search/W1/`，子阶段终态在 `execution/{W-search,W-local,W-outer}/W1/summary.json`，总批次 `execution/W1/summary.json`。预算共享3072attempts/3119Go/37215消费账本额度＝R0+B1 0/43/345＋W1 3072/3076/36870；无reserved/interrupted/halt，W2–W4/F/Q消费0。search3072尝试＝2966COMPLETE（2945新Go＋21cache）＋106FAIL（77成熟邻居初始化不足，消耗77Go/924账本预留；29重复特征在Go前拒绝）。local52Go/600账本，outer2Go/6账本；成功新Go响应对应35946账本（21cache非新增计算），不能把36870预留额度称实际完成账本。summary `result_status=partial`指仅完成四outer中的W1，本批status=complete；各候选邻域/主与seed实际弃权不能当通过。必要条件推导：PLAN10.2／pipeline.py:447,478规定弃权窗0笔且每窗至少15笔，因此当前版本不能F/Q晋级；未生成完整四窗正式gate或bootstrap，不能冒称其余三窗已评价。原工程/启动审查有效复用；终态research/audit已独立关闭（40journal、48邻域、630重要trace身份及8完整账户）；本次复核新增评价0，未修改冻结实现/数据。CLI只读 `report --run results/robust-v1/preparation-v3/ETHUSDT/1h --batch W1` exit0；独立auditor详情 `/tmp/lorentz-w1-final-audit/result.json`。trace逻辑大小81.2496GiB，非实际磁盘分配空间。
