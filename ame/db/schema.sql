-- 增材电子一体化软件 · 物理数据模型（依据 ER 图 v0.1 + 需求 §4.1 v1.1）
-- 22 张 ER 表 + tool_geometry（需求 1.1 变更#7 新增实体：工具几何）+ blob（内容寻址大文件索引）
-- 类型映射：UUID→TEXT, HASH→TEXT(sha256), JSON→TEXT(json1), ENUM→TEXT+CHECK, BOOL→INT, PATH→TEXT

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS project (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1,
  head_commit_id TEXT,
  unit          TEXT NOT NULL DEFAULT 'mm',
  bed_x_mm      REAL NOT NULL DEFAULT 200.0,
  bed_y_mm      REAL NOT NULL DEFAULT 200.0,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES project(id),
  base_commit_id TEXT REFERENCES version(id),
  draft_ref     TEXT,
  mode          TEXT NOT NULL DEFAULT 'edit' CHECK (mode IN ('edit','readonly')),
  status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed','crashed')),
  opened_at     TEXT NOT NULL,
  closed_at     TEXT
);

CREATE TABLE IF NOT EXISTS plan (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES project(id),
  goal          TEXT NOT NULL,
  steps_json    TEXT NOT NULL,      -- [{seq, action, args, precondition, expected}]
  tool_versions TEXT NOT NULL,      -- {"ame": "1.0.0", ...}
  status        TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','approved','running','done','rejected','stopped')),
  agent_role    TEXT NOT NULL CHECK (agent_role IN ('designer','reviewer','arbiter')),
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS version (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES project(id),
  parent_id     TEXT REFERENCES version(id),
  seq           INTEGER NOT NULL,
  cmd_json      TEXT NOT NULL,
  diff_json     TEXT NOT NULL,
  reason        TEXT NOT NULL,
  score_before  INTEGER,
  score_after   INTEGER,
  author        TEXT NOT NULL CHECK (author IN ('user','designer','reviewer','arbiter','system')),
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_version_project ON version(project_id, seq);

CREATE TABLE IF NOT EXISTS structure (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES project(id),
  source_step   TEXT NOT NULL,      -- 内容寻址路径 blobs/ab/cd/...
  step_hash     TEXT NOT NULL,
  unit          TEXT NOT NULL DEFAULT 'mm',
  tess_tol      REAL NOT NULL DEFAULT 0.02,
  feature_root  TEXT,
  bbox_json     TEXT NOT NULL DEFAULT '[0,0,0,0,0,0]',
  outline_json  TEXT NOT NULL DEFAULT '',   -- 手工/识别的板框（[[x,y],...]）
  holes_json    TEXT NOT NULL DEFAULT '[]', -- 板框内孔（[[[x,y],...],...]）
  diag_json     TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS feature (
  id            TEXT PRIMARY KEY,
  structure_id  TEXT NOT NULL REFERENCES structure(id),
  kind          TEXT NOT NULL CHECK (kind IN ('add','sub')),
  prim          TEXT NOT NULL CHECK (prim IN ('box','cylinder','prism')),
  dims_json     TEXT NOT NULL,      -- {x,y,z,r,h,n,...} 含原点与方向
  transform_json TEXT NOT NULL DEFAULT '{"t":[0,0,0],"rz":0}',
  enabled       INTEGER NOT NULL DEFAULT 1,
  status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('accepted','pending','rejected')),
  note          TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS circuit (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES project(id),
  netlist_source TEXT,
  netlist_hash  TEXT,
  format        TEXT NOT NULL CHECK (format IN ('kicad_pcb','netlist+xy','none')),
  imported_at   TEXT NOT NULL
);

-- 板文件自带的板框（CIR-001 / STR-007）：结构未提供板框时作为回退来源
CREATE TABLE IF NOT EXISTS board_outline (
  project_id    TEXT PRIMARY KEY REFERENCES project(id),
  outline_json  TEXT NOT NULL,
  holes_json    TEXT NOT NULL DEFAULT '[]',   -- 板框内孔，与 structure.holes_json 同格式
  source        TEXT NOT NULL DEFAULT 'board_file'
);

CREATE TABLE IF NOT EXISTS comp (
  id            TEXT PRIMARY KEY,
  circuit_id    TEXT NOT NULL REFERENCES circuit(id),
  ref           TEXT NOT NULL,
  footprint     TEXT NOT NULL DEFAULT '',
  x_mm          REAL NOT NULL, y_mm REAL NOT NULL,
  surface       TEXT NOT NULL DEFAULT 'top' CHECK (surface IN ('top','bottom')),
  rot_deg       REAL NOT NULL DEFAULT 0,
  height_mm     REAL NOT NULL DEFAULT 1.0,
  locked        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pad (
  id            TEXT PRIMARY KEY,
  comp_id       TEXT NOT NULL REFERENCES comp(id),
  net_id        TEXT REFERENCES net(id),
  pad_no        TEXT NOT NULL,
  x_mm          REAL NOT NULL, y_mm REAL NOT NULL,
  w_mm          REAL NOT NULL DEFAULT 1.5,
  h_mm          REAL NOT NULL DEFAULT 1.0,
  shape         TEXT NOT NULL DEFAULT 'rect' CHECK (shape IN ('rect','circle','oval'))
);
CREATE INDEX IF NOT EXISTS idx_pad_comp ON pad(comp_id);

CREATE TABLE IF NOT EXISTS net (
  id            TEXT PRIMARY KEY,
  circuit_id    TEXT NOT NULL REFERENCES circuit(id),
  name          TEXT NOT NULL,
  klass         TEXT NOT NULL DEFAULT 'signal' CHECK (klass IN ('signal','power','nc')),
  pad_count     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS route (
  id            TEXT PRIMARY KEY,
  circuit_id    TEXT NOT NULL REFERENCES circuit(id),
  net_id        TEXT NOT NULL REFERENCES net(id),
  pts_json      TEXT NOT NULL,      -- [[x,y,z],...] 三维（首版 z=层高）
  width_mm      REAL NOT NULL,
  surface       TEXT NOT NULL DEFAULT 'top' CHECK (surface IN ('top','bottom')),
  layer_index   INTEGER NOT NULL DEFAULT 0,
  strategy      TEXT NOT NULL DEFAULT 'manual' CHECK (strategy IN ('manual','auto','jumper','imported'))
);
CREATE INDEX IF NOT EXISTS idx_route_net ON route(net_id);

-- 禁布区（CIR-006）：自动布线必须避开的区域
CREATE TABLE IF NOT EXISTS keepout (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES project(id),
  name          TEXT NOT NULL DEFAULT '',
  pts_json      TEXT NOT NULL,      -- [[x,y],...] 多边形顶点
  note          TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS jumper (
  id            TEXT PRIMARY KEY,
  net_id        TEXT NOT NULL REFERENCES net(id),
  from_pad_id   TEXT NOT NULL REFERENCES pad(id),
  to_pad_id     TEXT NOT NULL REFERENCES pad(id),
  body          TEXT NOT NULL DEFAULT '0R',
  reason        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS process_params (
  id            TEXT PRIMARY KEY,
  material_a_id TEXT NOT NULL REFERENCES material_profile(id),
  material_b_id TEXT NOT NULL REFERENCES material_profile(id),
  layer_height_mm REAL NOT NULL,
  bottom_layer_mm REAL NOT NULL DEFAULT 0.08,   -- 下保护单道层厚（SLC-001 可设定）
  bottom_passes INTEGER NOT NULL DEFAULT 3,     -- 下保护道次数（SLC-002 典型 3）
  line_width_comp REAL NOT NULL DEFAULT 0.0,
  z_comp_mm     REAL NOT NULL DEFAULT 0.0,
  speed_mm_s    REAL NOT NULL,
  flow_factor   REAL NOT NULL DEFAULT 1.0,
  oven_temp_c   REAL NOT NULL DEFAULT 170.0,
  pregel_temp_c REAL NOT NULL DEFAULT 80.0,
  set_temp_c    REAL NOT NULL DEFAULT 100.0,
  cure_temp_c   REAL NOT NULL DEFAULT 170.0,
  cure_min      REAL NOT NULL DEFAULT 7.0,
  ramp_c_min    REAL NOT NULL DEFAULT 3.0,
  final_cure_min REAL NOT NULL DEFAULT 15.0,
  cool_to_c     REAL NOT NULL DEFAULT 60.0,
  silver_two_fills INTEGER NOT NULL DEFAULT 0,
  cure_mode     TEXT NOT NULL DEFAULT 'layer' CHECK (cure_mode IN ('pass','layer'))
);

CREATE TABLE IF NOT EXISTS material_profile (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  role          TEXT NOT NULL CHECK (role IN ('conductor','dielectric','protect')),
  recipe_json   TEXT NOT NULL DEFAULT '{}',
  batch         TEXT NOT NULL DEFAULT '',
  arrived_at    TEXT NOT NULL DEFAULT '',
  price_cny_ml  REAL NOT NULL DEFAULT 0.0,
  enabled       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS slice_set (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES project(id),
  source_commit_id TEXT NOT NULL REFERENCES version(id),
  params_id     TEXT NOT NULL REFERENCES process_params(id),
  layer_count   INTEGER NOT NULL,
  stale         INTEGER NOT NULL DEFAULT 0,
  estimate_json TEXT NOT NULL DEFAULT '{}',
  gcode_hash    TEXT,               -- 内容哈希（不含 ;gcode_sha256 行），SLC-008 一致性口径
  gcode_blob    TEXT,               -- G-code 文件内容寻址键
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS layer (
  id            TEXT PRIMARY KEY,
  slice_set_id  TEXT NOT NULL REFERENCES slice_set(id),
  seq           INTEGER NOT NULL,
  pass_no       INTEGER NOT NULL,   -- 道次号
  z_mm          REAL NOT NULL,      -- 该道次材料的顶面
  h_mm          REAL NOT NULL DEFAULT 0,  -- 该道次材料厚度（MRC-005 体积遮挡判定需要）
  kind          TEXT NOT NULL CHECK (kind IN ('bottom','circuit','top')),
  material_id   TEXT NOT NULL REFERENCES material_profile(id),
  path_count    INTEGER NOT NULL DEFAULT 0,
  printable     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_layer_set ON layer(slice_set_id, pass_no);

CREATE TABLE IF NOT EXISTS path (
  id            TEXT PRIMARY KEY,
  layer_id      TEXT NOT NULL REFERENCES layer(id),
  seq           INTEGER NOT NULL,
  role          TEXT NOT NULL CHECK (role IN ('outline','fill','dam','trench','pad_pit','pad_fill','travel')),
  width_mm      REAL NOT NULL DEFAULT 0.6,
  speed_mm_s    REAL NOT NULL DEFAULT 20.0,
  pts_json      TEXT NOT NULL       -- [[x,y],...]
);

CREATE TABLE IF NOT EXISTS device (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  protocol      TEXT NOT NULL CHECK (protocol IN ('virtual','serial','lan')),
  fw_version    TEXT NOT NULL DEFAULT 'v0',
  calib_json    TEXT NOT NULL DEFAULT '{"offset_a":[0,0],"offset_b":[0,0],"z_a":0,"z_b":0,"calibrated_at":null}',
  online        INTEGER NOT NULL DEFAULT 0,
  bed_x_mm      REAL NOT NULL DEFAULT 200.0,
  bed_y_mm      REAL NOT NULL DEFAULT 200.0
);

CREATE TABLE IF NOT EXISTS print_job (
  id            TEXT PRIMARY KEY,
  slice_set_id  TEXT NOT NULL REFERENCES slice_set(id),
  device_id     TEXT NOT NULL REFERENCES device(id),
  state         TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','precheck','printing','curing','paused','fault','done','cancelled')),
  layer_cursor  INTEGER NOT NULL DEFAULT 0,
  gcode_hash    TEXT NOT NULL,
  fault_plan_json TEXT NOT NULL DEFAULT '[]',
  safe_point_json TEXT,             -- 断电安全点 (道次, 已打印长度)，PRT-014
  skip_log_json   TEXT,             -- 跳层记录 [{pass_no,at_len_mm,reason}]，PRT-012
  started_at    TEXT,
  finished_at   TEXT
);

CREATE TABLE IF NOT EXISTS defect (
  id            TEXT PRIMARY KEY,
  job_id        TEXT NOT NULL REFERENCES print_job(id),
  layer_z_mm    REAL NOT NULL,
  pass_no       INTEGER NOT NULL DEFAULT 0,
  type          TEXT NOT NULL CHECK (type IN ('open_circuit','clog','overtemp','oven_undertemp','misalign','material_out')),
  severity      INTEGER NOT NULL DEFAULT 1,
  bbox_json     TEXT NOT NULL DEFAULT '[0,0,0,0]',
  evidence      TEXT NOT NULL DEFAULT '',
  status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','fixed','ignored')),
  attribution_commit_id TEXT REFERENCES version(id),
  note          TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS mrc_rule (
  code          TEXT PRIMARY KEY,   -- R1..R7
  name          TEXT NOT NULL,
  threshold_json TEXT NOT NULL,
  weight        INTEGER NOT NULL DEFAULT 5,
  rule_version  INTEGER NOT NULL DEFAULT 1,
  enabled       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS mrc_issue (
  id            TEXT PRIMARY KEY,
  rule_code     TEXT NOT NULL REFERENCES mrc_rule(code),
  target_type   TEXT NOT NULL CHECK (target_type IN ('route','pad','net','face','layer','trench','jumper')),
  target_id     TEXT NOT NULL,
  severity      INTEGER NOT NULL DEFAULT 1,
  penalty       INTEGER NOT NULL DEFAULT 5,
  reason        TEXT NOT NULL,
  suggestion    TEXT NOT NULL DEFAULT '',
  commit_id     TEXT REFERENCES version(id)
);

CREATE TABLE IF NOT EXISTS tool_geometry (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  nozzle_id_mm  REAL NOT NULL,      -- 针头内径
  tip_od_mm     REAL NOT NULL,      -- 针尖外径
  safe_gap_mm   REAL NOT NULL DEFAULT 0.5,
  head_envelope_mm REAL NOT NULL DEFAULT 20.0,
  min_corner_r_mm REAL NOT NULL DEFAULT 0.8,
  min_mesa_w_mm REAL NOT NULL DEFAULT 1.0,
  max_aspect    REAL NOT NULL DEFAULT 1.0
);

CREATE TABLE IF NOT EXISTS blob (
  sha256        TEXT PRIMARY KEY,
  size          INTEGER NOT NULL,
  relpath       TEXT NOT NULL       -- 数据目录内相对路径
);

-- ---- 高频查询索引（统一放在建表之后，避免引用未定义的表）----
CREATE INDEX IF NOT EXISTS idx_slice_latest ON slice_set(project_id, stale, created_at);
CREATE INDEX IF NOT EXISTS idx_path_layer ON path(layer_id, seq);
CREATE INDEX IF NOT EXISTS idx_issue_commit ON mrc_issue(commit_id, severity);
CREATE INDEX IF NOT EXISTS idx_pad_ref ON pad(comp_id, pad_no);
CREATE INDEX IF NOT EXISTS idx_route_circuit ON route(circuit_id);
CREATE INDEX IF NOT EXISTS idx_job_started ON print_job(started_at, id);
CREATE INDEX IF NOT EXISTS idx_comp_circuit ON comp(circuit_id);
CREATE INDEX IF NOT EXISTS idx_keepout_project ON keepout(project_id);
