# AME · 增材电子一体化软件



> 从三维结构与 PCB 设计，到可打印的双材料 G-code —— 并把"这块板到底打不打得出来"量化成 0–100 分。



华为 ICT 大赛创新赛 · 赛题一（基于华为云产品打造 AI 创新应用）参赛作品。



---



## 这个软件解决什么问题



传统 PCB 打样依赖蚀刻、曝光、电镀，周期长、门槛高；普通 3D 打印机只能打塑料，不懂电气连接。



本软件把"电气设计"和"增材制造"接起来，回答一个两边都不回答的问题：



> **这块板，按这台机器、这套材料，到底打不打得出来？**



不做原理图编辑、不做三维建模、不做多机调度。只做"从已有设计到可打印路径"这一层，

并把可打性量化成规则与分数。



---



## 核心能力（四域 + 智能体）



| 域 | 能力 | 关键实现 |

|---|---|---|

| **结构** | STEP 导入（AP203/214/242，mm/inch，含装配体展平）、几何诊断、面分析、布尔建议预览、剖切测量 | 自研 B-Rep 平面/柱面/圆锥/球面/环面解析，18 类诊断码，3 份真实 CAD 文件实测 |

| **走线** | 板文件导入（KiCad/网表）、器件放置、手动布线、A* 自动布线、跳线、连通性检查 | 8 邻域栅格寻路 + 转弯惩罚 + 禁布区避让 |

| **可打性** | 七条工艺规则、0–100 评分、问题定位、中文原因与建议、热力图 | R1 线宽 / R2 间距 / R3 坡度 / R4 可达性 / R5 遮挡 / R6 温度场 / R7 介电流道 |

| **切片** | 三层双材料道次编排、坝-槽-坑几何、G-code 导出（确定性可复现） | 7 道次 / 总厚 0.44 mm；同一设计两次导出哈希一致 |

| **打印** | 虚拟打印、暂停/恢复、故障注入、缺陷归因、断电续打 | 缺陷归因到 设计/工艺/设备 三类 |

| **智能体** | 设计 Agent（检查→改→复检）、审查、裁决、计划重放 | 60 → 94 分；重放时设计摘要与 G-code 哈希双一致 |



**AI 有两条路径，定位不同**：



| 路径 | 定位 | 说明 |

|---|---|---|

| 华为云 **MaaS** 大模型（`ai/maas.py`） | **主线** | 赛题要求"调用大模型"。把规则违规翻译成可执行建议、做候选语义重排。OpenAI 兼容接口，默认 DeepSeek-V3-32K |

| 本地 26 维线性排序器（`ai/local.py`） | **离线降级** | 未配置密钥/断网时兜底。**这不是神经网络**——是解一次线性方程得到的系数，详见 [AI证据链.md](AI证据链.md) §0 |



降级链：MaaS → 本地线性 → 规则先验，任一层失败都不影响主流程。



---



## 两种交付形态，同一套算法内核



```

              ┌───────────────────────────────┐

              │  四域算法内核（约 6500 行）    │

              │  geometry / mrc / slicing /   │

              │  circuit / printing / ai      │

              │  纯函数，无数据库、无界面依赖  │

              └────────────┬──────────────────┘

                           │

                ┌──────────┴──────────┐

                │  ledger 命令总线     │

                │  （唯一写入口）      │

                └──────────┬──────────┘

                 ┌─────────┴─────────┐

           ┌─────┴─────┐       ┌─────┴──────┐

           │ 桌面版     │       │ 云上 Demo  │

           │ Tkinter   │       │ FastAPI    │

           │ AME.exe   │       │ 浏览器画布  │

           └─────┬─────┘       └─────┬──────┘

           ┌─────┴─────┐       ┌─────┴──────┐

           │  SQLite   │       │ RDS PG +   │

           │ 本地文件   │       │ Redis      │

           └───────────┘       └────────────┘

```



**有测试断言"同一设计在两端算出的分数完全相同"**——证明上云是换壳而非另写一套。



云上画布除了四域闭环，还做了六项查看/编辑交互（`tests/test_web_features.py` 24 项锁定）。

其中五项桌面版早有对应入口，**版本历史面板是这边第一次做成可浏览的界面**

（桌面版只在顶栏显示「版本 N」，PRJ-004 的数据一直在账本里、没有列表视图）：



| 交互 | 后端 | 说明 |

|---|---|---|

| 逐层播放 | `GET /api/projects/{pid}/layer/{index}` | 滑块 + 上/下一道次 + 起停点标记；路径取自库中那份切片，不重算 |

| 问题定位 | `GET /api/projects/{pid}/state` | 点问题列表自动高亮对应走线/焊盘 |

| 热力图 | `GET /api/projects/{pid}/heatmap` | 开关切换叠加，按对象累计扣分 |

| 规则阈值编辑 | `GET/POST /api/projects/{pid}/rules[/{code}]` | 七条规则逐个改阈值/启停；改阈值使规则版本 +1，保存即重评分（仅切启停不改版本） |

| 版本历史 | `GET /api/projects/{pid}/versions` | 每版序号、原因、分数前后与变更项 |

| 剖切 Z 阈值 | `GET /api/projects/{pid}/state` | 三维视图只画 Z ≤ 阈值的面，判据与桌面版一致 |



`state` 接口的 `faces` 带 `loop` / `normal` / `centroid` 顶点数据（超过 4000 个面时只下发前 4000 个，

并在 `faces_note` 说明被截断）。



---



## 快速开始



### 方式一：桌面版（离线，零依赖）



```bash

pip install shapely numpy pillow

python -m ame                    # 启动界面

python -m ame --selftest         # 11 步无界面自检

```



或用打包版：解压 `dist/AME/`，双击 `AME.exe`。

首次启动 → 工程 → 打开示例工程（一键看到完整链路）。



### 方式二：云上 Demo（浏览器）



```bash

pip install -r requirements-cloud.txt

python -m uvicorn ame.web.app:app --port 8000

# 打开 http://127.0.0.1:8000

```



或用 Docker：



```bash

docker compose up --build        # 含 PostgreSQL + Redis

```



### 方式三：接华为云 RDS + Redis



```bash

export AME_PG_DSN="postgresql://user:pass@<rds-host>:5432/ame"

export AME_REDIS_URL="redis://<redis-host>:6379/0"

python -m uvicorn ame.web.app:app --host 0.0.0.0 --port 8000

curl http://127.0.0.1:8000/api/health   # storage 应为 postgresql

```



详见 [云上部署与比赛材料.md](云上部署与比赛材料.md)。



---



## 复现验证（组委会按材料核对时用这个）



```bash

# 1. 全量测试（不含性能基准，约 8 分钟，离线）

python -m pytest -q -m "not perf"



# 2. 质量门禁（10 项客观指标）

python check_quality.py --full



# 3. 桌面版自检（11 步，无需显示环境）

python -m ame --selftest



# 4. 云端接口自检

python -m uvicorn ame.web.app:app --port 8000 &

curl -X POST http://127.0.0.1:8000/api/projects/sample

```



**预期结果**：886 项测试通过、2 项跳过（本机无 PostgreSQL，`psycopg` 未安装）；

门禁 9/10（`ame/geometry/stepio.py` 1384 行超出 1200 行阈值，其余 9 项通过）；

自检 11 步全过；示例工程 60 分 / 12 条问题；切片 7 道次 / 0.44 mm。



---



## 项目结构



```

ame/

├── ai/            收益排序（features / local / cloud / service / train）

├── agent/         设计·审查·裁决 Agent 与计划重放

├── circuit/       connectivity(判定) + routing(寻路) + kicad(导入)

├── db/            store(读写) + pg_store(PG) + storage(协议) + migrations + cache

├── geometry/      vec3 → types → syntax → assembly → stepio（严格单向依赖）

├── ledger/        errors + commands(写) + workspace(状态) + services

├── mrc/           engine（7 条规则，每条一个函数）

├── printing/      virtual（虚拟打印机与缺陷注入）

├── slicing/       raster(路径) + slicer(道次) + gcode(导出)

├── ui/            theme + app + _shell + _canvas + domains/*（7 个域）

└── web/           app(FastAPI) + static/index.html（浏览器画布）

```



读代码建议顺序：`ledger/commands.py` 的模块 docstring（写入口不变量）

→ `workspace.mrc_state()`（数据如何组装）→ 各域实现。



---



## 工程数据



| 指标 | 数值 |

|---|---|

| 代码规模 | `ame` 15,849 行 / 59 个模块 |

| 测试 | 710 项通过 + 2 项跳过（22 个测试文件） |

| 静态检查 | `ruff check ame/` 零违规 |

| 业务层类型注解 | 返回 76.6% / 参数 68.8% |

| 最大模块 | 1384 行（`geometry/stepio.py`；重构前为 3502 行的 `ui/app.py`） |

| 真实 CAD 验证 | 3 份（PRO/ENGINEER、Open CASCADE、SolidWorks，共约 800 KB） |



---



## 已知边界（主动说明）



| 边界 | 说明 |

|---|---|

| STEP 曲面 | 平面/柱面/圆锥（含圆台）/球面/环面可解析（面积用解析公式，三角网格只用于 `loop` 渲染，不参与面积）；B 样条/NURBS 等自由曲面仍不支持，标 `G-SURFACE-UNSUPPORTED`，面积取"包围盒与三角扇的较小值"保守低估。环面与球冠侧别不可判定时标 `G-SURFACE-APPROX` |

| 布尔运算 | 只做预览不执行（需求 V1 范围） |

| 真机联调 | 仅虚拟打印机，串口/网络协议已预留 |

| 多层过孔 | 首版不支持双层过孔与连续曲面切片 |

| 云上多用户 | 当前单用户演示；多租户需换 Redis 会话 + 每请求校验 |

| MindSpore/昇腾 | **未使用，也不宣称**。赛题一要求的是华为云产品（CodeArts/RDS/Redis），这三项均已接入 |

| PostgreSQL 实测 | 代码与方言转换已完成；本机（Python 3.13）无 psycopg wheel，真实 PG 集成需在有 RDS 时执行 |



---



## 文档索引



| 文档 | 内容 |

|---|---|

| [云上部署与比赛材料.md](云上部署与比赛材料.md) | 赛题一对应关系、部署步骤、CodeArts 留证、评委问答准备 |

| [赛题一改造方案.md](赛题一改造方案.md) | 云原生化技术裁决与施工记录 |

| [验收报告.md](验收报告.md) | 验收结论、测试分布、逐项实测记录 |

| [AI证据链.md](AI证据链.md) | 先说明「这里的 AI 是什么」、拟合数据、LOSO 交叉验证、降级链测试 |

| [重构记录.md](重构记录.md) | 架构拆分（模块边界、搬运方法、未做的取舍） |

| [质量提升记录.md](质量提升记录.md) | 缺陷清单、覆盖率、静态检查整改 |

| [用户操作说明.md](用户操作说明.md) | 五分钟演示路径、概念速查、快捷键 |

| [增材电子一体化软件_需求规格说明书.md](增材电子一体化软件_需求规格说明书.md) | 需求基线（唯一验收依据） |



---



## 许可



本项目采用 **Apache License 2.0**，详见 [LICENSE](LICENSE)。



允许商业使用、修改、分发，需保留版权声明与许可文本。

