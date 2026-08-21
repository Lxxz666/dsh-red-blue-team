# 16 · wanter 架构设计手册（water + ant）

> wanter = **water**（水流）+ **ant**（蚂蚁）。融合 ACO 信息素动力学、势场法连续梯度、
> 水的侵蚀自组织，构成「连续流动 + 迹蒸发 + 动态改地形」的工作流编排动力学。
> 本手册是设计与实现的唯一权威文档：四法则的数学建模、与 dsh_python 的落层决策、
> 实验证据与扩展指引。

## 0. 四法则（用户原始思想 → 形式化）

| # | 用户思想 | 形式化 |
|---|---|---|
| ① | Agent 工作流 = 水流 | 每次 agent 尝试 = 一个「水滴」粒子，在势能地形上沿负梯度流动（Langevin 动力学） |
| ② | 路径留痕、指数蒸发、新流偏好旧径 | 水迹场 T(x,t)：访问处沉积、全局指数衰减；迹使地形下陷成「河道」，新流沿河道优先流动 |
| ③ | 全局势能地形，水向低处流，最低点=完成 | 标量场 Φ(x,t)；流动 = −∇Φ；目标点 = 势阱最深处；完成判据 = 到达目标势阱 |
| ④ | 局部洼地：缓慢降低周边地形，开新下坡通道 | 驻留（停滞）检测触发「侵蚀」：在被困点周边用高斯核挖低地形（可退火），开辟新通道继续向更低处探索 |

## 1. 数学建模

### 1.1 状态空间

- 连续坐标空间 X ⊆ ℝᵈ（d 默认 2）。
- 工作流状态（语义态）经 **Coordinator** 投影到坐标：默认用状态指纹的
  确定性哈希伪嵌入；可换 LLM embedding provider（能力缝）。
- 目标点 g ∈ X：任务完成语义对应坐标。

### 1.2 势能地形（法则 ③）

```
Φ(x, t) = Φ_goal(x) + Φ_base(x) + Φ_trace(x, t) + Φ_erosion(x, t)
```

| 项 | 公式 | 含义 |
|---|---|---|
| 目标势阱 | `Φ_goal(x) = Σᵢ ½·kᵢ·‖x − gᵢ‖²` | 谐波阱；多目标 = 子任务分解（每个子任务一个阱）；g 为全局最低势能点 = 任务完成 |
| 基础地形 | 用户定义（障碍/偏好） | 静态背景 |
| 迹刻蚀通道 | `Φ_trace(x,t) = −α·(K ⊛ T)(x) = −α·Σᵢ wᵢ·K(x − cᵢ)` | 水迹使地形下陷（负贡献）——走过的成功路径被「冲刷」成河道；**wᵢ<0（淤积）则抬高地形**，失败路径被泥沙堆积（反向刻蚀） |
| 侵蚀洼地 | `Φ_erosion(x,t) = −Σⱼ ηⱼ·K(x − cⱼ)` | 被困时挖出的低地（法则 ④） |

- 高斯核：`K(x) = exp(−‖x‖² / (2σ²))`；梯度：`∇K(x) = −(x/σ²)·K(x)`。
- 迹/侵蚀都是**负贡献**：它们物理上「降低地形」，即把走过的路径/被困的盆地
  挖低——这正是「水侵蚀出河道」的隐喻。

### 1.3 水迹动力学（法则 ②）

连续形式（信息素 PDE）：

```
∂T/∂t = β·ρ(x,t) − λ·T(x,t)
```

- **沉积**：水滴访问 x 处（工作流步骤成功）→ 事件沉积，权重 w = 奖励加权
  （成功 +w_success；失败 **淤积** −w_failure 抬高路径；用户反馈 up/down 亦可
  沉积/淤积——奖励连续化的三个来源）；
- **蒸发**：事件权重随时间指数衰减：`w(t) = w₀·e^(−λ(t−t₀))`，
  权重低于阈值 ε 的事件被遗忘（丢弃）；
- **路径偏好**：新水流在候选点上的选择概率随势能指数分布
  `P(v') ∝ exp(−Φ(v')/τ)`——旧路径因迹刻蚀而 Φ 更低 → 概率更高（= ACO 的信息素偏好）。

### 1.4 流动动力学（法则 ①+③）

连续（Langevin / 随机梯度流）：

```
dx = −∇Φ(x,t) dt + √(2D) dWₜ
```

- `−∇Φ dt`：向低处流动（确定性下坡）；
- `√(2D) dWₜ`：扩散探索噪声（温度 τ 与 D 等价：τ ∝ D）。

离散实现（水滴的一次「迈步」）：

- 纯梯度步：`x ← x − lr·∇Φ(x)`；
- 带噪步：`x ← x − lr·∇Φ(x) + √(2D·lr)·ξ`，ξ ~ 𝒩(0,1)；
- 候选选择（softmin Boltzmann）：`P(v') = exp(−Φ(v')/τ) / Σ exp(−Φ/τ)`。

**完成判据**：`‖x − g‖ < ε_complete` 或外部完成信号 → 水滴到达全局最低势能点。

### 1.5 洼地逃逸（法则 ④）

```
停滞检测: 窗口 W 步内位置漂移 < δ_x  →  判定「困于局部洼地」
          （水滴位置不再前进，等价于势能不再下降；带噪流动下的稳健实现）
侵蚀:     Φ_erosion ← Φ_erosion − Σ η·K(x − c_rim)
          c_rim 只在「通向下游」方向开挖：
            主通道 = 朝向最近目标的 rim 点（全深 η）
            拓宽   = 同向/垂直各轴 rim 点（半深 η/2）
            反向   = 洼地内侧 rim 点【不开挖】——对称环形会把陷阱挖得更深
          （实验教训：见 §5.2「偏置侵蚀对照」）
单点深度上限: |Φ_erosion 单点累计| ≤ D_max——河床有底：挖到上限后改向周边
          创建新 bump，通道拓宽而非无限加深
退火（可选）: η ← η·γ（γ<1），每次侵蚀后减小挖深，避免挖穿全局
```

- 侵蚀是**持久**的地形改造（与迹一样随时间保留，可选缓慢恢复）；
- 关键设计：侵蚀作用于**洼地周边（rim）而非洼底**——把 rim 挖低即「降低洼地
  周边地形高度」，开辟出通向下游的下坡通道（若挖洼底只会把坑挖得更深）；
- 连续被困 → 反复侵蚀 → rim 持续降低 → 最终出现低于外界的下坡通道 →
  水滴沿新通道流出（对应「缓慢降低洼地周边地形高度，开辟新下坡通道」）；
- 这与 ACO 的本质区别：**不是靠随机碰运气，而是主动改造地形**。

## 2. 四法则 → 代码映射表

| 数学对象 | dsh/wanter 模块 | 类/函数 |
|---|---|---|
| 势能场 Φ | `terrain.py` | `Terrain.phi(x)` / `grad_phi(x)` / `add_goal` / `nearest_goal` |
| 水迹场 T（含淤积） | `trace.py` | `TraceField.deposit(权重可负)/evaporate_to/density_at` |
| 流动动力学 | `flow.py` | `gradient_step` / `langevin_step` / `softmin_weights` |
| 洼地逃逸 | `erosion.py` | `StagnationDetector.stagnated` / `Eroder.erode`（目标方向主通道全深 + 同向半深拓宽 + 反向不开挖 + 单点深度上限） |
| 状态坐标投影 | `coordinator.py` | `HashCoordinator` / `MockEmbeddingCoordinator` / `HttpEmbeddingCoordinator` / `build_coordinator` |
| 引擎（编排+蒸发循环+持久化） | `engine.py` | `WanterEngine`（ctx.wanter；`embed`/`add_goal`/`completed(任一阱)`） |
| harness 集成 | `plugin.py` | `WanterPlugin`（feedback 沉积/淤积 + 失败淤积 + 坐标平滑）+ `ToolWanterGoalsPlugin`（wanter_goal_* 工具） |

## 3. 落层决策（在哪一层修改）

**原则：核心循环零修改。** wanter 完全作为插件挂在既有扩展点上：

| 法则 | 挂载点（事件） | 行为 |
|---|---|---|
| ① 水流 | agent 自身循环（每会话即一水滴）；水滴位置 = 会话坐标，随 turn 演化 | 无侵入 |
| ② 沉积/蒸发 | `tools/result`（成功→沉积）+ 引擎后台任务（蒸发 tick + storage 持久化） | 监听器 |
| ② 路径偏好 | 动态 `PromptContext`（order 15，注册于 `ctx.systemPrompt.context`）：每次组装把本会话水滴状态（坐标/势能/距目标）渲染进模型上下文——模型「看得见地形」，低势能旧路径（迹刻蚀通道）自然更受偏好 | 上下文注入 |
| ③ 势能 | 引擎共享于根 ctx（跨会话全局地形） | 服务 |
| ④ 驻留/侵蚀 | `agent/turn-stopping`（serial）：位置漂移停滞 → 侵蚀 + steer 探索性下一步 | 监听器 |

**为什么不改 agent-loop**：四法则都是「观察-决策-注入」，对应 dsh 的事件分类
（session 事件=事实、agent 事件=拦截、能力事件=策略）。wanter 是这套扩展哲学的
「终极测试用例」：一个全新的动力学架构，零核心改动即可挂上。

## 4. 实用性实验（测试计划）

| 实验 | 验证法则 | 断言 |
|---|---|---|
| 蒸发精确性 | ② | `T(t+Δ) = T(t)·e^(−λΔ)` 数值误差 < 1e-9；低于阈值事件被遗忘 |
| 梯度流达目标 | ③ | 无局部洼地时，纯梯度步在 N 步内 `‖x−g‖<ε` |
| **双势阱逃逸对照** | ④ | 构造「浅局部阱在 x≈1、深全局阱在 x≈3」地形：无侵蚀 → 水滴困于局部阱（x 不越过 2.5）；有侵蚀 → 有限次侵蚀后越过势垒到达全局阱。**这是 ④ 的直接证明** |
| 路径复用偏好 | ② | 对 A/B 两条等势路径，沉积 A 迹后，softmin 选择 A 的概率单调高于沉积前 |
| 引擎持久化 | — | storage 往返后地形/迹恢复一致 |
| 端到端（mock 循环） | ①+② | WanterPlugin 挂载后：turn 正常闭环、成功步骤产生沉积事件、停滞触发侵蚀并 steer |

## 5. 实现状态

### 5.1 文件清单

| 文件 | 内容 |
|---|---|
| `dsh/wanter/trace.py` | `TraceField`：事件沉积（权重可负=淤积）、惰性指数蒸发（`w(t)=w₀e^(−λ(t−t₀))`）、ε 遗忘（按绝对值）、密度/梯度（高斯核） |
| `dsh/wanter/terrain.py` | `Terrain`：Φ = 多目标势阱(Σ½kᵢ‖x−gᵢ‖²) + 山丘 + 迹刻蚀通道(−α·K⊛T) + 侵蚀洼地；phi/grad_phi/add_goal/nearest_goal/快照 |
| `dsh/wanter/flow.py` | `gradient_step`（纯梯度）、`langevin_step`（+√(2D·lr)·ξ）、`softmin_weights`（Boltzmann）、`descend` |
| `dsh/wanter/erosion.py` | `StagnationDetector`（位置漂移窗口）、`Eroder`（环形挖 rim ±r 每轴 + 最近目标方向 rim 点，退火 γ） |
| `dsh/wanter/coordinator.py` | `HashCoordinator` / `MockEmbeddingCoordinator` / `HttpEmbeddingCoordinator` / `build_coordinator`（状态摘要 → 坐标缝） |
| `dsh/wanter/engine.py` | `WanterEngine`（ctx.wanter）：编排 + 后台蒸发循环 + storage 持久化（domain "wanter"）+ embed/add_goal/completed(任一阱)/report_and_maybe_erode |
| `dsh/wanter/plugin.py` | `WanterPlugin`（沉积/淤积、feedback 回填、coordinator 平滑、停滞侵蚀+steer）+ `ToolWanterGoalsPlugin`（wanter_goal_* 工具）——零循环修改 |
| `dsh/wanter/viz.py` | `render_terrain_svg`（2D 热力图/1D 剖面/高维前两轴投影）+ `render_terrain_state`（面板 JSON）+ `render_calibration_chart`（校准对比 SVG） |
| `dsh/wanter/calibration.py` | `build_calibration_terrain` / `OracleEmbeddingProvider` / `run_matching_experiment` / `run_calibration`（语义坐标校准：短程高斯洼地 + oracle vs hash，见 §7.5/§8） |
| `examples/wanter_demo.py` | 双势阱 SVG 可视化（纯 Python，无依赖） |
| `examples/wanter_benchmark.py` | 确定性种子基准（20 种子 A/B 对照 + 学习曲线 + 性能）→ `wanter_metrics.json` |
| `examples/wanter_charts.py` | 四面板展示图 → `wanter_showcase.svg`（README/WANTER.md 引用） |
| `examples/wanter_embedding_calibration.py` | 语义坐标校准 → `wanter_embedding_metrics.json` + `wanter_embedding_chart.svg` |
| `tests/test_wanter.py` | 17 项物理/集成测试 |
| `tests/test_wanter_metrics.py` | 核心性质回归守护（安全余量断言，CI 防线） |
| `tests/test_wanter_calibration.py` | 校准回归（oracle 对齐 + 显著优于 hash） |

### 5.2 实验证据（17/17 通过）

| 实验 | 结果 |
|---|---|
| 蒸发精确性 | `w·e^(−λΔt)` 数值误差 < 1e-9；`e^−3≈0.05<ε=0.1` 事件被遗忘 ✓ |
| 梯度流达目标 | 500 步内 ‖x−g‖ < 0.05 ✓ |
| **双势阱对照（法则 ④ 核心证明）** | 无侵蚀：4000+ 步从未越过势垒（x<2.5，困于局部阱）；有侵蚀（目标方向主通道、退火 0.995）：越过势垒并落入全局阱（|x−3|<0.4），侵蚀次数 ≥1 ✓ |
| 路径复用偏好（法则 ②） | 等势 A/B 路径 P=0.5/0.5；A 侧沉积 12 单位水迹后 P(A) 上升 >0.2 ✓ |
| **淤积（反向刻蚀，深化 ①）** | A 侧负沉积 −3×2 → Φ(A) 上升（12.99→18.88）且 P(A) 下降 >0.2 ✓ |
| **多目标分解（深化 ③）** | 双阱叠加平衡点 = 加权质心（∇Φ=0 → x=11/3）；移除近目标后下坡至远目标；completed() 任一阱内即完成 ✓ |
| **Coordinator 缝（深化 ②）** | hash 确定性/可区分；mock 词哈希确定且范数受限；http 端点不可达回退 hash 不抛异常 ✓ |
| **奖励连续化（深化 ①）** | 真实 boot：message-feedback up→+2 沉积、down→淤积；同点 up+down 聚合为净 +0.5（抵消语义）✓；工具失败 → 淤积 ✓ |
| 停滞检测 | 静止 → 困；前进 → 不困 ✓ |
| 引擎持久化 | storage 往返后迹/侵蚀/计数一致 ✓ |
| 集成：沉积+地形可见 | 真实 boot + mock 循环：成功工具 → 迹沉积 ≥1；模型上下文含「wanter 地形状态」✓ |
| 集成：停滞 steer | 静止 12 轮 → 侵蚀 ≥1 次 + 收件箱收到 wanter 探索性 steer ✓ |
| **可视化** | `examples/wanter_demo.py` 纯 Python 生成 `wanter_double_well.svg`（地形剖面+水滴轨迹+侵蚀点，5.4KB），演示水滴从局部阱经侵蚀逃逸到目标 0.4 半径内 ✓ |
| **同点聚合（性能）** | 同一处两次沉积 → 1 事件权重相加（河道加深）；容量上限 3 时淘汰最弱两事件 ✓ |
| **时钟回拨保护（Bug）** | deposited 在未来时流逝钳位为 0（权重不爆炸）✓ |
| **偏置侵蚀（物理教训）** | 1D 下只在目标方向开挖（centers==[1.8]，无反向 0.2 点）；对称环形会把陷阱挖得更深（手册 §1.5 已记录）✓ |
| **单点深度上限** | 超限后改为周边新 bump（通道拓宽而非无限加深）✓ |
| **会话清理（防泄漏）** | agent/disposed 后位置/历史/feedback 去重集清空 ✓ |
| **脏写去抖（性能）** | 无变更时 `_persist` 不落盘；变更后落盘并复位 ✓ |

### 5.3 已知边界与打磨记录（如实标注）

- 状态坐标默认用会话 id 确定性哈希伪嵌入；Coordinator 缝可换（hash/mock/http）；
- 沉积权重来源：工具成败（±配置权重）+ 用户反馈（up/down）——奖励连续化已接入
  message-feedback；同点 up+down 聚合抵消（净权重 = 两者之和）；
- 蒸发/侵蚀 tick 与持久化为同一后台任务（1s 粒度），带 dirty 去抖；
- 双势阱实验参数（η=0.4、γ=0.995、r=0.8、lr=0.05、D=0.005）为演示参数，
  生产需按任务尺度调参（见 §6）。

**本轮打磨（Bug/性能修复及其物理含义）**：

| 修复 | 类型 | 物理含义 |
|---|---|---|
| `_weight_at` 流逝钳位 ≥0 | Bug | 时钟回拨不再导致 exp 爆炸（水迹不会「逆时间增生」） |
| 侵蚀只向目标方向开挖、反向不开挖 | Bug（实验发现） | 对称环形会把陷阱**内侧**也挖深，等于强化陷阱；正确做法是只开「通向下游」的渠 |
| 单点侵蚀深度上限 `erosion_max_depth` | 物理完善 | 河床有底：挖到极限后水改向周边拓宽，而非无限加深 |
| 同点沉积聚合 | 性能 | 多次冲刷同一处 = 河道更深（一个事件），查询 O(unique) |
| `trace_max_events` 容量上限 | 性能/内存 | 超限按 |有效权重| 最小者淘汰——最淡的水迹先干涸 |
| 会话状态随 agent/disposed 清理 | 防泄漏 | 水滴蒸发后不残留状态 |
| 引擎 dirty 持久化去抖 | 性能 | 无变化不落盘（蒸发 tick 不再每秒写存储） |
| 位置历史容量 `history_cap` | 内存 | feedback 只需最近消息的坐标 |

## 6. 调参与扩展指引

| 参数 | 含义 | 调大 → | 调小 → |
|---|---|---|---|
| λ（decay_rate） | 迹蒸发率 | 遗忘快（适应非稳态任务） | 记忆久（复用久经验） |
| β（deposit_beta） | 沉积强度 | 旧路径更强偏好 | 更弱 |
| α（alpha） | 迹刻蚀深度 | 河道更深 | 更浅 |
| τ / D | 温度/扩散 | 更随机（探索多） | 更确定（下坡快） |
| η / γ（erosion） | 挖深/退火 | 逃逸快但可能挖穿 | 逃逸慢但保守 |
| r（erosion_radius） | 侵蚀环半径 | 通道更宽 | 更窄 |
| w_success / w_failure | 成功沉积/失败淤积权重 | 路径偏好更极端 | 更温和 |
| w_up / w_down | 反馈沉积/淤积权重 | 人类信号更主导 | 更弱 |
| coordinator_blend | 语义坐标平滑系数 | 更贴 embedding | 更贴惯性 |
| trace_max_events | 迹事件容量上限 | 记忆更多（更慢） | 遗忘更快（更省） |
| erosion_max_depth | 单点最大累计挖深 | 通道更深 | 更早拓宽 |
| history_cap | 每会话位置历史容量 | 可回填更早反馈 | 更省内存 |

**已落地的深化方向**（§5.2 有对应实验）：

1. **奖励连续化**：工具成败 ±权重 + message-feedback up/down 回填沉积/淤积；
2. **LLM embedding coordinator**：hash（离线默认）/ mock（词哈希）/ http
   （OpenAI 兼容 /embeddings，失败回退）；
3. **失败淤积**：负沉积抬高地形（反向刻蚀），失败路径被系统性避开；
4. **多目标势阱 = 子任务分解**：`wanter_goal_add/list/complete` 工具 +
   completed(任一阱) + nearest_goal 侵蚀偏置。

**未来方向**：自动奖励模型（评分工/工具结果打分）、子目标自动生成（LLM 拆解
任务 → 批量 add_goal）、多维度真实 embedding 校准、跨会话任务的冷启动
（从 storage 恢复全局地形）。

> **第十批已落地**：迹的可视化面板（Web UI）——`dsh/wanter/viz.py`
> （`render_terrain_svg` 2D 热力图/1D 剖面 + `render_terrain_state` JSON）+
> Web 端点 `GET /api/wanter/terrain`（SVG）/ `GET /api/wanter/state`（JSON）+
> 静态页 `/static/wanter.html`（3s 自动刷新）。见手册 10 与
> `examples/wanter_panel_preview.svg`。

## 7. 量化指标（实测数据）

> 实验脚本：`examples/wanter_benchmark.py`（确定性种子网格，可复现；
> 产出 `examples/wanter_metrics.json`）。回归守护：`tests/test_wanter_metrics.py`。
> 实验环境：本机 CPython 3.11 / Windows；双势阱地形 = 局部阱@1（深2）+ 势垒@2（高1.5）
> + 全局阱@3（谐波目标阱）；水滴 Langevin lr=0.05、D=0.005；侵蚀 η=0.4、γ=0.995、r=0.8。

### 7.1 双势阱逃逸 A/B 对照（20 种子）

| 组 | 机制 | 逃逸率 | 平均逃逸步数 | 平均侵蚀次数 | 最终落点（均值±标准差） |
|---|---|---|---|---|---|
| 基线 A | 纯梯度下降 | **0%** | — | 0 | 1.22 ± 0.00（困于陷阱） |
| 基线 B | 梯度 + 噪声（ACO 式随机探索） | **0%** | — | 0 | 1.22 ± 0.04（困于陷阱） |
| **wanter** | 噪声 + 侵蚀（法则 ④） | **100%** | 386.5 | 9.8 | **2.62 ± 0.01**（落入目标盆地） |

**结论**：噪声本身几乎无法逃出局部阱（B=0%）；wanter 的「主动地形改造」
把逃逸率拉到 100%——这是法则 ④ 相对 ACO 随机探索的**量化优势**。

### 7.2 路径复用学习曲线（15 次连续水滴）

| 组 | 前 5 次均值（步数） | 后 5 次均值（步数） | 线性回归斜率 |
|---|---|---|---|
| 无沉积（对照） | 33.8 | 28.6 | −0.67（缓慢自然适应） |
| **有沉积（法则 ②）** | 242.8 | **4.4** | **−21.76（约 30 倍加速）** |

**结论**：水迹把「完成任务所需步数」从几百步压到个位数——「新水流优先沿旧
路径流动」的量化体现；初始几轮因河道刚形成略慢，随后显著加速。

### 7.3 性能与蒸发

| 指标 | 实测 |
|---|---|
| `density_at` 查询耗时（0 / 1000 / 5000 事件，200 查询均值） | 0.1 / 914 / 4729 µs（随事件数线性，聚合后可控） |
| 事件容量上限 | 500 次沉积在 max_events=100 时被钳制为 100 ✓ |
| 蒸发半衰期 | 理论 ln2/λ = 3.4657s，实测 3.4657s（误差 < 1e-4）✓ |

### 7.4 指标回归守护

`tests/test_wanter_metrics.py`（6 种子子集，约 2s）对核心性质加安全余量断言：
wanter 逃逸率 ≥ 0.85、噪声基线 ≤ 0.1、纯梯度 = 0；学习斜率（有沉积）比无沉积
低 5 以上；容量上限生效；半衰期实测与理论偏差 < 1%。**任何调参若破坏这些
性质，CI 会立即失败。**

### 7.5 语义坐标校准（第十一批，实测数据）

> 脚本：`examples/wanter_embedding_calibration.py` → `wanter_embedding_metrics.json`
> + `wanter_embedding_chart.svg`；回归：`tests/test_wanter_calibration.py`。

多目标语义匹配（4 任务 × 8 种子；目标 = 短程高斯洼地）：

| 坐标提供者 | 匹配率 | 平均步数 |
|---|---|---|
| hash 伪嵌入（Coordinator 默认） | **0%** | 282 |
| oracle 语义嵌入（模拟训练好的 embedding） | **100%** | **1** |

**结论**：坐标质量直接决定子任务路由——语义对齐的嵌入让任务直接落在自家
盆地（1 步即达）；哈希伪嵌入与目标布局无关，必然迷路。生产接入 =
Coordinator 缝（hash/mock/http，OpenAI 兼容 /embeddings，失败回退 hash）。

## 8. 可视化与校准模块（第十批/第十一批，函数级）

### 8.1 `dsh/wanter/viz.py`

| 函数 | 签名 | 说明 |
|---|---|---|
| `_palette(t: float) -> str` | 模块级 | t∈[0,1] 发散配色：蓝（高）→暗（中）→绿（低） |
| `render_terrain_svg(engine, size=520, grid=64, title=...) -> str` | 模块级 | 地形 SVG：dim==1 → 剖面曲线（`_render_1d`）；否则 2D 热力图（`_render_2d`：`_window` 取目标 ±3σ 窗口、逐格 `engine.phi` 归一化着色、目标绿环/侵蚀红点/迹紫点抽样 `[::7]`） |
| `_render_2d/_render_1d/_to_px/_window` | 模块级 | 内部渲染助手 |
| `render_terrain_state(engine) -> dict` | 模块级 | 面板 JSON 状态（dim/goals/sigma/侵蚀坑数/迹事件数/decay_rate/completed） |
| `render_calibration_chart(metrics) -> str` | 模块级 | 校准双栏图表（匹配率 % + 平均步数） |

消费方：Web 端点 `GET /api/wanter/terrain`（SVG）、`/api/wanter/state`（JSON）、
`/static/wanter.html`（3s 自动刷新）；`examples/wanter_panel_preview.svg`。

### 8.2 `dsh/wanter/calibration.py`（语义坐标校准，第十一批）

| 常量/类/函数 | 签名 | 说明 |
|---|---|---|
| `DEFAULT_GOALS / DEFAULT_LABELS / WELL_DEPTH` | 模块级 | 4 个目标（±5 轴距）、4 个任务标签、势阱深度 3.0 |
| `build_calibration_terrain(goals=None, sigma=1.0) -> Terrain` | 模块级 | **短程高斯洼地**（add_bump 负高度）——设计注：Terrain 目标模型为长程二次阱，多目标时远阱干扰制造人造中心鞍点（首版实验 0% 收敛） |
| `OracleEmbeddingProvider(goals=None, jitter=0.2)` | 类 | 模拟训练好的语义嵌入：`embed(label)` = 目标 + 标签哈希确定性抖动（复现性） |
| `run_matching_experiment(provider, goals, labels, seeds, step_cap=1200, lr=0.1, D=0.005, complete_radius=0.5) -> dict` | 模块级 | 种子 × 任务水滴 Langevin 下降；返回 matching（落入自己目标比率）/mean_steps/per_task |
| `run_calibration(seeds=(0..7)) -> dict` | 模块级 | hash（HashCoordinator）vs oracle 对照：实测 **hash 匹配 0% / oracle 100%（1 步）** |

消费方：`examples/wanter_embedding_calibration.py`（metrics JSON + SVG）、
`tests/test_wanter_calibration.py`（oracle 对齐 + oracle 显著优于 hash 的
安全余量断言）。

**设计注（陷阱记录）**：Terrain 的目标模型是**长程二次阱**
（0.5·strength·|x−goal|²），多目标时远阱干扰主导（φ≈10² 量级），会把人造
中心鞍点当成物理陷阱（首版实验 0% 收敛）。校准实验改用**短程高斯洼地**
（add_bump 负高度）——与真实语义嵌入的「局部盆地」直觉一致，相邻任务
不串扰。
