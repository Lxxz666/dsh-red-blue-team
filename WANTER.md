# wanter —— 让 Agent 像水一样「记住走过的路，冲开堵住的洼地」

> **water + ant**：一条会侵蚀地形的虚拟水流。它不是又一个 ReAct 循环，而是
> 一个**零侵入的插件动力学层**，把每个 agent 会话变成一个在「经验势能地形」
> 上流动的水滴——成功的路被冲刷得更低（下次走得更快），困住它的洼地被
> 定向挖穿（逃出局部最优）。

```
纯梯度被困：0% 逃逸 · 加随机噪声：0% 逃逸 · wanter（噪声 + 地形改造）：100% 逃逸
路径复用：后 5 次平均 4.4 步（无沉积对照组 28.6 步）≈ 30 倍加速
```

---

## 1. 四法则：把「经验」建模成物理场

| 法则 | 数学 | 直觉 |
|---|---|---|
| ① 势能地形 | Φ = Φ_goal + Φ_base + Φ_trace + Φ_erosion | 目标=洼地、障碍=山丘、走过的路=河道、挖过的坑=地貌改造 |
| ② 水迹沉积/蒸发 | ∂T/∂t = βρ − λT | 成功之处沉积水迹（地形变低），随时间蒸发（遗忘）——**新水流优先沿旧路径** |
| ③ Langevin 流动 | dx = −∇Φ dt + √(2D) dW | 确定性下坡 + 温度噪声探索；候选选择 = softmin(Boltzmann) |
| ④ 洼地侵蚀 | 停滞检测 → 定向开挖 + 退火 | 被困 = 检测位置漂移停滞，**只朝目标方向**挖低周边（反向绝不挖——物理教训：对称开挖会强化陷阱），γ 退火防挖穿 |

**为什么有效**（一页纸）：
- 随机探索（ACO/ε-greedy）**不改变地形**——噪声在深阱里是无记忆的布朗运动；
- wanter 的洞察是**把探索预算投资在改造环境本身**：一次侵蚀永久降低陷阱
  高度，后续所有水滴共享这条「人工河道」；
- 沉积让路径复用变成物理惯性：`Φ_trace` 越低的路越「顺滑」，无需记忆表、
  无需回放、O(unique events) 查询。

## 2. 它如何运行在框架里（零循环侵入）

wanter 是 dsh_python 的**一个插件**（`base.yml` 三行：`wanter-engine` /
`wanter-plugin` / `tool-wanter-goals`），只挂接 5 个既有扩展点，**不修改
agent loop 一行代码**：

```
        ┌──────────────────── dsh_python 框架 ────────────────────┐
        │                                                          │
 agent loop ── tools/result ──────────► ② 成功沉积 / 失败淤积      │
      │        agent/turn-stopping ───► ④ 停滞检测 → 侵蚀 → steer  │
      │        message-feedback ──────► 奖励连续化（👍👎 回填）      │
      │        systemPrompt(context) ─► ③ 模型可见地形状态           │
      │        session/event ─────────► 位置历史（反馈回填）         │
      │                                                          │
      └──────► ① WanterEngine：Terrain + TraceField + Eroder      │
                （storage 持久化，跨会话共享地形）                  │
                └── Web 面板 /api/wanter/terrain（实时 SVG）       │
```

- **跨会话全局地形**：所有会话共享同一张 Φ（storage domain `"wanter"`，
  重启恢复）——一个 agent 挖开的河道，后来的 agent 直接受益（社会学习）；
- **多目标 = 子任务分解**：`wanter_goal_add/list/complete` 工具 + 侵蚀偏置
  `nearest_goal`；
- **失败淤积**：负沉积抬高失败路径的地形，被系统性避开（反向刻蚀）。

## 3. 量化指标（全部离线可复现）

> 脚本：`examples/wanter_benchmark.py`、`examples/wanter_embedding_calibration.py`
> （确定性种子；产出 `wanter_metrics.json` / `wanter_embedding_metrics.json`）；
> 回归守护：`tests/test_wanter_metrics.py`、`tests/test_wanter_calibration.py`。

![wanter 量化指标总览](examples/wanter_showcase.svg)

### 3.1 双势阱逃逸（20 种子，1D 陷阱@1 + 势垒@2 + 目标@3）

| 组 | 机制 | 逃逸率 | 平均步数 | 侵蚀次数 | 落点 |
|---|---|---|---|---|---|
| A | 纯梯度下降 | **0%** | — | 0 | 1.22±0.00（困死） |
| B | 梯度 + 噪声（ACO 式） | **0%** | — | 0 | 1.22±0.04（困死） |
| **wanter** | 噪声 + **侵蚀** | **100%** | 386.5 | 9.8 | **2.62±0.01**（目标盆地） |

### 3.2 路径复用（15 次连续水滴）

| 组 | 前 5 次均值 | 后 5 次均值 | 斜率 |
|---|---|---|---|
| 无沉积 | 33.8 步 | 28.6 步 | −0.67 |
| **有沉积** | 242.8 步 | **4.4 步** | **−21.76（≈30×）** |

### 3.3 语义坐标校准（多目标匹配，4 任务 × 8 种子）

| 坐标提供者 | 匹配率 | 平均步数 |
|---|---|---|
| hash 伪嵌入 | 0% | 282 |
| **oracle 语义嵌入** | **100%** | **1** |

> 结论：坐标质量直接决定「子任务路由」——语义对齐的嵌入让每个任务直接
> 落在自己的盆地（1 步即达）；哈希伪嵌入与目标布局无关，必然迷路。
> 生产接入点 = Coordinator 缝（hash/mock/http，OpenAI 兼容 `/embeddings`，
> 失败回退 hash）。

### 3.4 性能与守恒律

- `density_at` 查询：0.2 / 1476 / 7036 µs（0/1k/5k 事件，随规模线性）；
- 容量上限：500 次沉积在 `max_events=100` 时钳制为 100（最淡水迹先干涸）；
- 半衰期 ln2/λ：理论 3.4657s，实测 3.4657s（误差 <1e-4）——蒸发守恒律成立。

## 4. 可视化

- **实时地形面板**：Web UI 内嵌 `/static/wanter.html`（3 秒自动刷新）——
  势能热力图 + 目标/侵蚀/迹标记，`GET /api/wanter/terrain` 返回 SVG；
- **静态展示图**：`examples/wanter_showcase.svg`（本文顶部）、
  `examples/wanter_double_well.svg`（1D 逃逸轨迹）、
  `examples/wanter_embedding_chart.svg`（校准对比）。

## 5. 代码导航

| 组件 | 文件 | 职责 |
|---|---|---|
| 引擎 | `dsh/wanter/engine.py` | Terrain + TraceField + Eroder + 持久化/恢复 |
| 势能地形 | `dsh/wanter/terrain.py` | Φ_goal/Φ_base/Φ_trace/Φ_erosion + 多目标 |
| 水迹场 | `dsh/wanter/trace.py` | 沉积/蒸发/密度/梯度（聚合 + 容量上限） |
| 流动 | `dsh/wanter/flow.py` | 梯度/Langevin/softmin(Boltzmann) |
| 侵蚀 | `dsh/wanter/erosion.py` | 停滞检测 + 定向偏置开挖 + 退火 |
| 坐标缝 | `dsh/wanter/coordinator.py` | hash/mock/http 嵌入提供者 |
| 插件 | `dsh/wanter/plugin.py` | 5 个挂载点 + 反馈回填 + steer（零循环侵入） |
| 校准 | `dsh/wanter/calibration.py` | 多目标语义匹配实验（oracle 对照） |
| 可视化 | `dsh/wanter/viz.py` | SVG 地形/图表渲染 |

## 6. 与现有方法的定位对比

| | ACO/随机探索 | 经验回放（RL） | 记忆检索（RAG） | **wanter** |
|---|---|---|---|---|
| 状态表示 | 信息素表 | 价值网络 | 向量库 | **连续势能场**（无离散表） |
| 记忆更新 | 蒸发+沉积 | 梯度更新 | 插入/检索 | **PDE 蒸发 + 沉积**（物理守恒） |
| 逃出局部最优 | 弱（无环境改造） | 依赖探索策略 | 不适用 | **定向侵蚀地形**（永久改造） |
| 路径复用 | 信息素偏好 | 策略内化 | 检索后重算 | **Φ_trace 梯度惯性**（零重算） |
| 跨会话共享 | 需全局表 | 需分布式训练 | 共享库 | **storage 域即全局地形** |

wanter 不是替代而是**补层**：它不与任何推理循环/记忆方案冲突，只要求
「成功/失败信号 + 每 turn 一个挂载点」。
