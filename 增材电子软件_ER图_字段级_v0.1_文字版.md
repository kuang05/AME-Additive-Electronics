# 增材电子一体化软件 · 数据模型 ER 图（文字版）

> 本文件是 `增材电子软件_ER图_字段级_v0.1.png`（5200×3160）的**逐字转写**，供无视觉能力的 AI / 程序读取。
> 转写方式：将原图分区放大后逐块抄录，字段名、类型、注释均为图上原文；转写者补充的说明一律放在"〔注〕"标记内，与图上内容区分。

---

## 0. 图的元信息（图上原文）

- **标题**：增材电子一体化软件 · 数据模型 ER 图
- **副标题**：四域（结构 / 走线 / 切片 / 打印）+ 命令账本 · 字段级物理模型 · 依据 17 / 18 号文档 · v0.1 · 2026-10-25
- **右上角注记**：鸦爪记号： ─|| 1（双杠 = 1:1）  ─< N  · 红色虚线 = 回流 / 归因
- **页脚**：数据模型依据 18 号文档 §4 · 字段类型为逻辑类型（SQLite 映射见《数据模型说明》）· 2026-10-25

〔注〕全图共 **22 张表**，按底色分为 5 个分区（域）：
①工程账本（靛蓝，左列）、②结构域（蓝，顶行）、③走线域（绿）、④切片与材料（橙）、⑤打印与闭环（红底，内含紫色 MRC 两表）。

---

## 1. 读图图例（图左下角图例框，原文）

- **PK / FK / UK** ＝ 主键 / 外键 / 唯一
- **─┤ 1 端**；**─< N 端**；**鸦爪 = 一对多**
- **红色虚线 ＝ 回流 / 归因（虚线）**
- 补 9 表（18 号 §4 未显式建模）：Session · Pad · Path · Params · Material；PrintJob · Device · MrcRule · MrcIssue
- 紫 ＝ MRC 规则引擎 R1–R7；JSON → SQLite JSON1
- STEP / 证据 / 快照只存路径 + 哈希
- 命令总线是唯一写入口；人与 Agent 同源
- MrcIssue / Defect 的 commit_id 回指 Commit

---

## 2. 实体（22 张表，字段为图上原文）

### ① 工程账本（左列，靛蓝）

**Project**（工程 · 聚合根）

| 字段 | 键 | 类型 |
|---|---|---|
| id | PK | UUID |
| name | | TEXT |
| schema_version | | INT |
| head_commit_id | FK | UUID |
| unit | | TEXT |
| created_at | | DATETIME |

表尾注：·聚合根 ·当前版本指针

**Session**（会话 · 草稿）〔补表〕

| 字段 | 键 | 类型 |
|---|---|---|
| id | PK | UUID |
| project_id | FK | UUID |
| base_commit_id | FK | UUID |
| draft_ref | | PATH |
| mode | | ENUM |
| status | | ENUM |

表尾注：·CoW 草稿 ·内循环不落账

**Plan**（Agent 计划）

| 字段 | 键 | 类型 |
|---|---|---|
| id | PK | UUID |
| project_id | FK | UUID |
| goal | | TEXT |
| steps_json | | JSON |
| tool_versions | | JSON |
| status | | ENUM |
| agent_role | | ENUM |

表尾注：·外循环 ·可重放（版本不符拒绝）

**Commit**（提交 · 账本）

| 字段 | 键 | 类型 |
|---|---|---|
| id | PK | UUID |
| project_id | FK | UUID |
| parent_id | FK | UUID |
| seq | | INT |
| cmd_json | | JSON |
| diff_json | | JSON |
| reason | | TEXT |
| score_before | | INT |
| score_after | | INT |
| author | | ENUM |

表尾注：·命令落账 ·diff + 理由 + 分数

### ② 结构域（顶行，蓝）

**Structure**（结构 · STEP B-Rep）

| 字段 | 键 | 类型 |
|---|---|---|
| id | PK | UUID |
| project_id | FK | UUID |
| source_step | | PATH |
| step_hash | | HASH |
| unit | | TEXT |
| tess_tol | | REAL |
| feature_root | | UUID |

表尾注：·STEP B-Rep + 特征树

**Feature**（布尔特征）

| 字段 | 键 | 类型 |
|---|---|---|
| id | PK | UUID |
| structure_id | FK | UUID |
| kind | | ENUM |
| prim | | ENUM |
| dims_json | | JSON |
| transform_json | | JSON |
| enabled | | BOOL |
| note | | TEXT |

表尾注：·一次布尔 ·可编辑节点

**说明框（虚线框，原文）——几何唯一入口：STEP（不做 STL）**

- 只吃 STEP（AP203 / 214 / 242），单位统一 mm；装配体展平
- ShapeFix / sewing 失败 → 返回诊断码，绝不静默继续
- B-Rep 布尔是唯一实现；失败回滚到上一可重建版本
- 每个布尔体 = 一个可撤销节点（Feature），不是一段脚本
- 三角化：OCCT BRepMesh，容差 0.02 mm；显示与切片同源
- 面判定输出：平面 / 曲面 / 柱面 + 法向 + 面积 + 边界 → MRC

### ③ 走线域（绿）

**Circuit**（电路 · 网表）

| 字段 | 键 | 类型 |
|---|---|---|
| id | PK | UUID |
| project_id | FK | UUID |
| netlist_source | | PATH |
| netlist_hash | | HASH |
| format | | ENUM |
| imported_at | | DATETIME |

表尾注：·网表导入 ·KiCad 先行

**Comp**（器件）

| 字段 | 键 | 类型 |
|---|---|---|
| id | PK | UUID |
| circuit_id | FK | UUID |
| ref | | TEXT |
| footprint | | TEXT |
| xy_mm | | REAL[2] |
| surface | | ENUM |
| rot_deg | | REAL |
| height_mm | | REAL |
| locked | | BOOL |

表尾注：·器件 ·位姿 / 面 / 锁定

**Pad**（焊盘（关联））〔补表〕

| 字段 | 键 | 类型 |
|---|---|---|
| id | PK | UUID |
| comp_id | FK | UUID |
| net_id | FK | UUID |
| pad_no | | TEXT |
| xy_mm | | REAL[2] |
| shape | | ENUM |

表尾注：·器件—网络关联（补）

**Net**（电气网络）

| 字段 | 键 | 类型 |
|---|---|---|
| id | PK | UUID |
| circuit_id | FK | UUID |
| name | | TEXT |
| klass | | ENUM |
| pad_count | | INT |

表尾注：·电气网络 ·连通性基准

**Route**（立体走线）

| 字段 | 键 | 类型 |
|---|---|---|
| id | PK | UUID |
| circuit_id | FK | UUID |
| net_id | FK | UUID |
| pts3d_json | | JSON |
| width_mm | | REAL |
| surface | | ENUM |
| layer_index | | INT |
| strategy | | ENUM |

表尾注：·立体走线 ·过棱 / 层间

**Jumper**（跳线）

| 字段 | 键 | 类型 |
|---|---|---|
| id | PK | UUID |
| net_id | FK | UUID |
| from_pad_id | FK | UUID |
| to_pad_id | FK | UUID |
| body | | TEXT |
| reason | | TEXT |

表尾注：·不可平面化时的 0Ω 跨接

### ④ 切片与材料（橙）

**SliceSet**（切片集）

| 字段 | 键 | 类型 |
|---|---|---|
| id | PK | UUID |
| project_id | FK | UUID |
| source_commit_id | FK | UUID |
| params_id | FK | UUID |
| layer_count | | INT |
| estimate_json | | JSON |
| created_at | | DATETIME |

表尾注：·一次切片 ·绑定源版本

**ProcessParams**（工艺参数）〔补表〕

| 字段 | 键 | 类型 |
|---|---|---|
| id | PK | UUID |
| material_a_id | FK | UUID |
| material_b_id | FK | UUID |
| layer_height_mm | | REAL |
| line_width_comp | | REAL |
| z_comp_mm | | REAL |
| speed_mm_s | | REAL |
| flow_factor | | REAL |
| oven_temp_c | | REAL |

表尾注：·工艺参数 ·双材料配方

**MaterialProfile**（材料配方）〔补表〕

| 字段 | 键 | 类型 |
|---|---|---|
| id | PK | UUID |
| name | | TEXT |
| role | | ENUM |
| recipe_json | | JSON |
| batch | | TEXT |
| enabled | | BOOL |

表尾注：·银浆 / 介电 / 保护配方库

**Layer**（层）

| 字段 | 键 | 类型 |
|---|---|---|
| id | PK | UUID |
| slice_set_id | FK | UUID |
| z_mm | | REAL |
| kind | | ENUM |
| material_id | FK | UUID |
| path_count | | INT |
| printable | | BOOL |

表尾注：·层 ·材料 ·可打性

**Path**（路径段）〔补表〕

| 字段 | 键 | 类型 |
|---|---|---|
| id | PK | UUID |
| layer_id | FK | UUID |
| seq | | INT |
| role | | ENUM |
| width_mm | | REAL |
| speed_mm_s | | REAL |
| pts_json | | JSON |

表尾注：·路径段 ·G-code 最小单元

### ⑤ 打印与闭环（红；紫色两表属 MRC 规则引擎）

**PrintJob**（打印作业）〔补表〕

| 字段 | 键 | 类型 |
|---|---|---|
| id | PK | UUID |
| slice_set_id | FK | UUID |
| device_id | FK | UUID |
| state | | ENUM |
| layer_cursor | | INT |
| gcode_hash | | HASH |
| started_at | | DATETIME |

表尾注：·一次打印 ·可复现（哈希）

**Device**（设备）〔补表〕

| 字段 | 键 | 类型 |
|---|---|---|
| id | PK | UUID |
| name | | TEXT |
| protocol | | ENUM |
| fw_version | | TEXT |
| calib_json | | JSON |
| online | | BOOL |

表尾注：·真机 / 虚拟打印机

**Defect**（缺陷）

| 字段 | 键 | 类型 |
|---|---|---|
| id | PK | UUID |
| job_id | FK | UUID |
| layer_z_mm | | REAL |
| type | | ENUM |
| severity | | INT |
| bbox_json | | JSON |
| evidence | | PATH |
| status | | ENUM |
| attribution_commit_id | FK | UUID |

表尾注：·缺陷 ·归因回设计 / 工艺

**MrcRule**（MRC 规则）〔紫色＝MRC 规则引擎〕

| 字段 | 键 | 类型 |
|---|---|---|
| code | PK | TEXT |
| name | | TEXT |
| threshold_json | | JSON |
| weight | | INT |
| rule_version | | INT |
| enabled | | BOOL |

表尾注：·七条规则 ·版本化

**MrcIssue**（MRC 问题）〔紫色＝MRC 规则引擎〕

| 字段 | 键 | 类型 |
|---|---|---|
| id | PK | UUID |
| rule_code | FK | TEXT |
| target_type | | ENUM |
| target_id | | UUID |
| severity | | INT |
| penalty | | INT |
| reason | | TEXT |
| suggestion | | TEXT |
| commit_id | FK | UUID |

表尾注：·违规 + 扣分 + 可执行建议

〔注〕MrcIssue.target_id 无 FK 徽标，为多态引用（由 target_type 指明目标表）。

---

## 3. 连线与关系（按图上连线逐一核对）

记号约定（图例原文）：单杠 ─┤ ＝ 1 端；双杠 ─|| ＝ 恰好一（1:1）；鸦爪 ─< ＝ N 端；红色虚线 ＝ 回流 / 归因。

### 3.1 Project 主干（左列经竖向走廊向右分出）

Project 是聚合根，一条竖向主干线从 Project—Structure 连线上的分支点向下引出，依次分支：

| # | 关系（按图上记号） | 实现外键 | 备注 |
|---|---|---|---|
| 1 | Project 1 ─|| 1 Structure（双杠＝1:1） | Structure.project_id | |
| 2 | Project 1 ─< N Session | Session.project_id | |
| 3 | Project 1 ─|| 1 Circuit（双杠＝1:1） | Circuit.project_id | |
| 4 | Project 1 ─< N Plan | Plan.project_id | |
| 5 | Project 1 ─< N SliceSet | SliceSet.project_id | 从 SliceSet 左缘进入 |
| 6 | Project 1 ─< N Commit | Commit.project_id | 从 Commit 右缘进入 |

〔注〕Project.head_commit_id（FK → Commit.id，"当前版本指针"）在图上**未画独立连线**，以表尾注表达。

### 3.2 结构域内部

| # | 关系 | 实现外键 |
|---|---|---|
| 7 | Structure 1 ─< N Feature | Feature.structure_id |

### 3.3 走线域内部

| # | 关系 | 实现外键 | 备注 |
|---|---|---|---|
| 8 | Circuit 1 ─< N Comp | Comp.circuit_id | |
| 9 | Comp 1 ─< N Pad | Pad.comp_id | |
| 10 | Net 1 ─< N Pad | Pad.net_id | 鸦爪在 Pad 端 |
| 11 | Circuit 1 ─< N Net | Net.circuit_id | |
| 12 | Circuit 1 ─< N Route | Route.circuit_id | 经顶部绕行，从 Route 顶缘进入 |
| 13 | Net 1 ─< N Route | Route.net_id | |
| 14 | Net 1 ─< N Jumper | Jumper.net_id | 经底部绕行，从 Jumper 底缘进入 |

### 3.4 切片与材料域（含与走线域、打印域的跨域连线）

| # | 关系（按图上记号） | 实现外键 | 备注 |
|---|---|---|---|
| 15 | Circuit 1 ─< N SliceSet | （无对应外键列） | 连线标注"**切片输入**"，属数据流连线 |
| 16 | SliceSet 1 ─< N ProcessParams | SliceSet.params_id | ⚠ 见 3.6 注 A |
| 17 | ProcessParams 1 ─< N MaterialProfile | ProcessParams.material_a_id / material_b_id | ⚠ 见 3.6 注 A |
| 18 | MaterialProfile 1 ─< N Layer | Layer.material_id | 从 Layer 左缘进入 |
| 19 | SliceSet 1 ─< N Layer | Layer.slice_set_id | 连线标注"**分层 1:N**"，从 Layer 底缘进入 |
| 20 | Layer 1 ─< N Path | Path.layer_id | |

### 3.5 打印与闭环域

| # | 关系 | 实现外键 | 备注 |
|---|---|---|---|
| 21 | SliceSet 1 ─< N PrintJob | PrintJob.slice_set_id | 从 PrintJob 顶缘进入 |
| 22 | Device 1 ─< N PrintJob | PrintJob.device_id | |
| 23 | PrintJob 1 ─< N Defect | Defect.job_id | 经底部绕行，从 Defect 底缘进入 |
| 24 | Defect ──> Commit（**红色虚线**） | Defect.attribution_commit_id → Commit.id | 连线标注"**缺陷归因 · 回流**"；箭头指向 Commit 右缘 score_before / score_after 行附近 |
| 25 | MrcRule 1 ─< N MrcIssue | MrcIssue.rule_code → MrcRule.code | |

〔注〕Commit.parent_id（自引用父版本）与 MrcIssue.commit_id → Commit.id 在图上**未画连线**；后者由图例文字说明："MrcIssue / Defect 的 commit_id 回指 Commit"。

### 3.6 转写注记（图与外键方向不一致处）

- **注 A**：第 16、17 条连线，图上鸦爪画在 ProcessParams 端 / MaterialProfile 端（即按连线读作"SliceSet 1—N ProcessParams""ProcessParams 1—N MaterialProfile"）；但外键列方向相反（params_id 在 SliceSet 上，material_a_id / material_b_id 在 ProcessParams 上，按外键应读作 N:1 指向被引用表）。**做连接（JOIN）语义时以外键列为准**，连线记号方向照原图如实记录。
- 其余所有连线的鸦爪方向与外键方向一致。

---

## 4. 与需求文档（《需求规格说明书》§4.1）的对应

Project=工程，Session=会话，Commit=版本，Plan=计划，Structure=结构，Feature=特征，Circuit=电路，Comp=器件，Pad=焊盘，Net=网络，Route=走线，Jumper=跳线，SliceSet=切片，ProcessParams=工艺参数，MaterialProfile=材料，Layer=层/道次，Path=路径，PrintJob=打印作业，Device=设备，Defect=缺陷，MrcRule/MrcIssue=规则/问题。
其中 Session、Pad、Path、ProcessParams(Params)、MaterialProfile(Material)、PrintJob、Device、MrcRule、MrcIssue 共 9 表为图上标注的"补 9 表（18 号 §4 未显式建模）"。

---

## 5. 转写可靠性说明

- 全部 22 张表的字段名、键徽标（PK/FK）、类型、表尾注释，以及标题、副标题、图例框、STEP 说明框、连线标签（切片输入 / 分层 1:N / 缺陷归因 · 回流）、页脚，均为将原图放大 2–5 倍逐块抄录核对的结果。
- 连线经接头处放大逐一核对端点与记号方向；仅 3.6 注 A 所列两处存在"鸦爪方向与外键方向相反"的现象，已如实标注。
- 图中未发现 UK（唯一）徽标；若需唯一约束信息，需查阅《数据模型说明》。
