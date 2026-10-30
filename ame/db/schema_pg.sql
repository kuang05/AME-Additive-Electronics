-- PostgreSQL 建表脚本（华为云 RDS for PostgreSQL 专用）。
--
-- 为什么不直接复用 schema.sql：那份是为 SQLite 写的，有三处 PG 不兼容——
--   1. SQLite 允许前向引用（表 A 的 FK 指向后建的表 B），PG 不允许；
--      schema.sql 里 session→version、pad→net、process_params→material_profile
--      都是前向引用。
--   2. SQLite 的 REAL 是 float8，PG 的 REAL 是 float4。几何量存 float4 会丢精度
--      （0.5785 变成 0.5785000324249268），进而让 G-code 哈希在两种后端上不一致，
--      破坏 SLC-008 与"云端桌面同分同哈希"的对外承诺。
--   3. schema.sql 里多处 CREATE 语句紧贴在一行 `--` 注释后面。按行累积到 `;`
--      再判断"是否以 -- 开头"的切分逻辑会把整条语句连同注释一起跳过。
--
-- 本文件由脚本从 schema.sql 抽取并按依赖拓扑排序生成。**列定义里不带行内注释**
-- （原生 DDL 保持纯净，避免注释内容干扰按 `;` 切分），类型 REAL→DOUBLE PRECISION，
-- 索引统一放在所有表之后。改动表结构时两处都要改，由
-- tests/test_pg_dialect.py 的 DDL 契约用例兜底。

-- project（依赖：无）
CREATE TABLE IF NOT EXISTS project (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1,
  head_commit_id TEXT,
  unit          TEXT NOT NULL DEFAULT 'mm',
  bed_x_mm      DOUBLE PRECISION NOT NULL DEFAULT 200.0,
  bed_y_mm      DOUBLE PRECISION NOT NULL DEFAULT 200.0,
  created_at    TEXT NOT NULL
);

-- version（依赖：project）
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

-- session（依赖：project, version）
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

-- plan（依赖：project）
CREATE TABLE IF NOT EXISTS plan (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES project(id),
  goal          TEXT NOT NULL,
  steps_json    TEXT NOT NULL,
  tool_versions TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','approved','running','done','rejected','stopped')),
  agent_role    TEXT NOT NULL CHECK (agent_role IN ('designer','reviewer','arbiter')),
  created_at    TEXT NOT NULL
);

-- structure（依赖：project）
CREATE TABLE IF NOT EXISTS structure (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES project(id),
  source_step   TEXT NOT NULL,
  step_hash     TEXT NOT NULL,
  unit          TEXT NOT NULL DEFAULT 'mm',
  tess_tol      DOUBLE PRECISION NOT NULL DEFAULT 0.02,
  feature_root  TEXT,
  bbox_json     TEXT NOT NULL DEFAULT '[0,0,0,0,0,0]',
  outline_json  TEXT NOT NULL DEFAULT '',
  holes_json    TEXT NOT NULL DEFAULT '[]',
  diag_json     TEXT NOT NULL DEFAULT '[]'
);

-- feature（依赖：structure）
CREATE TABLE IF NOT EXISTS feature (
  id            TEXT PRIMARY KEY,
  structure_id  TEXT NOT NULL REFERENCES structure(id),
  kind          TEXT NOT NULL CHECK (kind IN ('add','sub')),
  prim          TEXT NOT NULL CHECK (prim IN ('box','cylinder','prism')),
  dims_json     TEXT NOT NULL,
  transform_json TEXT NOT NULL DEFAULT '{"t":[0,0,0],"rz":0}',
  enabled       INTEGER NOT NULL DEFAULT 1,
  status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('accepted','pending','rejected')),
  note          TEXT NOT NULL DEFAULT ''
);

-- circuit（依赖：project）
CREATE TABLE IF NOT EXISTS circuit (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES project(id),
  netlist_source TEXT,
  netlist_hash  TEXT,
  format        TEXT NOT NULL CHECK (format IN ('kicad_pcb','netlist+xy','none')),
  imported_at   TEXT NOT NULL
);

-- board_outline（依赖：project）
CREATE TABLE IF NOT EXISTS board_outline (
  project_id    TEXT PRIMARY KEY REFERENCES project(id),
  outline_json  TEXT NOT NULL,
  holes_json    TEXT NOT NULL DEFAULT '[]',
  source        TEXT NOT NULL DEFAULT 'board_file'
);

-- comp（依赖：circuit）
CREATE TABLE IF NOT EXISTS comp (
  id            TEXT PRIMARY KEY,
  circuit_id    TEXT NOT NULL REFERENCES circuit(id),
  ref           TEXT NOT NULL,
  footprint     TEXT NOT NULL DEFAULT '',
  x_mm          DOUBLE PRECISION NOT NULL, y_mm DOUBLE PRECISION NOT NULL,
  surface       TEXT NOT NULL DEFAULT 'top' CHECK (surface IN ('top','bottom')),
  rot_deg       DOUBLE PRECISION NOT NULL DEFAULT 0,
  height_mm     DOUBLE PRECISION NOT NULL DEFAULT 1.0,
  locked        INTEGER NOT NULL DEFAULT 0
);

-- net（依赖：circuit）
CREATE TABLE IF NOT EXISTS net (
  id            TEXT PRIMARY KEY,
  circuit_id    TEXT NOT NULL REFERENCES circuit(id),
  name          TEXT NOT NULL,
  klass         TEXT NOT NULL DEFAULT 'signal' CHECK (klass IN ('signal','power','nc')),
  pad_count     INTEGER NOT NULL DEFAULT 0
);

-- pad（依赖：comp, net）
CREATE TABLE IF NOT EXISTS pad (
  id            TEXT PRIMARY KEY,
  comp_id       TEXT NOT NULL REFERENCES comp(id),
  net_id        TEXT REFERENCES net(id),
  pad_no        TEXT NOT NULL,
  x_mm          DOUBLE PRECISION NOT NULL, y_mm DOUBLE PRECISION NOT NULL,
  w_mm          DOUBLE PRECISION NOT NULL DEFAULT 1.5,
  h_mm          DOUBLE PRECISION NOT NULL DEFAULT 1.0,
  shape         TEXT NOT NULL DEFAULT 'rect' CHECK (shape IN ('rect','circle','oval'))
);

-- route（依赖：circuit, net）
CREATE TABLE IF NOT EXISTS route (
  id            TEXT PRIMARY KEY,
  circuit_id    TEXT NOT NULL REFERENCES circuit(id),
  net_id        TEXT NOT NULL REFERENCES net(id),
  pts_json      TEXT NOT NULL,
  width_mm      DOUBLE PRECISION NOT NULL,
  surface       TEXT NOT NULL DEFAULT 'top' CHECK (surface IN ('top','bottom')),
  layer_index   INTEGER NOT NULL DEFAULT 0,
  strategy      TEXT NOT NULL DEFAULT 'manual' CHECK (strategy IN ('manual','auto','jumper','imported'))
);

-- keepout（依赖：project）
CREATE TABLE IF NOT EXISTS keepout (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES project(id),
  name          TEXT NOT NULL DEFAULT '',
  pts_json      TEXT NOT NULL,
  note          TEXT NOT NULL DEFAULT ''
);

-- jumper（依赖：net, pad）
CREATE TABLE IF NOT EXISTS jumper (
  id            TEXT PRIMARY KEY,
  net_id        TEXT NOT NULL REFERENCES net(id),
  from_pad_id   TEXT NOT NULL REFERENCES pad(id),
  to_pad_id     TEXT NOT NULL REFERENCES pad(id),
  body          TEXT NOT NULL DEFAULT '0R',
  reason        TEXT NOT NULL DEFAULT ''
);

-- material_profile（依赖：无）
CREATE TABLE IF NOT EXISTS material_profile (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  role          TEXT NOT NULL CHECK (role IN ('conductor','dielectric','protect')),
  recipe_json   TEXT NOT NULL DEFAULT '{}',
  batch         TEXT NOT NULL DEFAULT '',
  arrived_at    TEXT NOT NULL DEFAULT '',
  price_cny_ml  DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  enabled       INTEGER NOT NULL DEFAULT 1
);

-- process_params（依赖：material_profile）
CREATE TABLE IF NOT EXISTS process_params (
  id            TEXT PRIMARY KEY,
  material_a_id TEXT NOT NULL REFERENCES material_profile(id),
  material_b_id TEXT NOT NULL REFERENCES material_profile(id),
  layer_height_mm DOUBLE PRECISION NOT NULL,
  bottom_layer_mm DOUBLE PRECISION NOT NULL DEFAULT 0.08,
  bottom_passes INTEGER NOT NULL DEFAULT 3,
  line_width_comp DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  z_comp_mm     DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  speed_mm_s    DOUBLE PRECISION NOT NULL,
  flow_factor   DOUBLE PRECISION NOT NULL DEFAULT 1.0,
  oven_temp_c   DOUBLE PRECISION NOT NULL DEFAULT 170.0,
  pregel_temp_c DOUBLE PRECISION NOT NULL DEFAULT 80.0,
  set_temp_c    DOUBLE PRECISION NOT NULL DEFAULT 100.0,
  cure_temp_c   DOUBLE PRECISION NOT NULL DEFAULT 170.0,
  cure_min      DOUBLE PRECISION NOT NULL DEFAULT 7.0,
  ramp_c_min    DOUBLE PRECISION NOT NULL DEFAULT 3.0,
  final_cure_min DOUBLE PRECISION NOT NULL DEFAULT 15.0,
  cool_to_c     DOUBLE PRECISION NOT NULL DEFAULT 60.0,
  silver_two_fills INTEGER NOT NULL DEFAULT 0,
  cure_mode     TEXT NOT NULL DEFAULT 'layer' CHECK (cure_mode IN ('pass','layer'))
);

-- slice_set（依赖：process_params, project, version）
CREATE TABLE IF NOT EXISTS slice_set (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES project(id),
  source_commit_id TEXT NOT NULL REFERENCES version(id),
  params_id     TEXT NOT NULL REFERENCES process_params(id),
  layer_count   INTEGER NOT NULL,
  stale         INTEGER NOT NULL DEFAULT 0,
  estimate_json TEXT NOT NULL DEFAULT '{}',
  gcode_hash    TEXT,
  gcode_blob    TEXT,
  created_at    TEXT NOT NULL
);

-- layer（依赖：material_profile, slice_set）
CREATE TABLE IF NOT EXISTS layer (
  id            TEXT PRIMARY KEY,
  slice_set_id  TEXT NOT NULL REFERENCES slice_set(id),
  seq           INTEGER NOT NULL,
  pass_no       INTEGER NOT NULL,
  z_mm          DOUBLE PRECISION NOT NULL,
  h_mm          DOUBLE PRECISION NOT NULL DEFAULT 0,
  kind          TEXT NOT NULL CHECK (kind IN ('bottom','circuit','top')),
  material_id   TEXT NOT NULL REFERENCES material_profile(id),
  path_count    INTEGER NOT NULL DEFAULT 0,
  printable     INTEGER NOT NULL DEFAULT 1
);

-- path（依赖：layer）
CREATE TABLE IF NOT EXISTS path (
  id            TEXT PRIMARY KEY,
  layer_id      TEXT NOT NULL REFERENCES layer(id),
  seq           INTEGER NOT NULL,
  role          TEXT NOT NULL CHECK (role IN ('outline','fill','dam','trench','pad_pit','pad_fill','travel')),
  width_mm      DOUBLE PRECISION NOT NULL DEFAULT 0.6,
  speed_mm_s    DOUBLE PRECISION NOT NULL DEFAULT 20.0,
  pts_json      TEXT NOT NULL
);

-- device（依赖：无）
CREATE TABLE IF NOT EXISTS device (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  protocol      TEXT NOT NULL CHECK (protocol IN ('virtual','serial','lan')),
  fw_version    TEXT NOT NULL DEFAULT 'v0',
  calib_json    TEXT NOT NULL DEFAULT '{"offset_a":[0,0],"offset_b":[0,0],"z_a":0,"z_b":0,"calibrated_at":null}',
  online        INTEGER NOT NULL DEFAULT 0,
  bed_x_mm      DOUBLE PRECISION NOT NULL DEFAULT 200.0,
  bed_y_mm      DOUBLE PRECISION NOT NULL DEFAULT 200.0
);

-- print_job（依赖：device, slice_set）
CREATE TABLE IF NOT EXISTS print_job (
  id            TEXT PRIMARY KEY,
  slice_set_id  TEXT NOT NULL REFERENCES slice_set(id),
  device_id     TEXT NOT NULL REFERENCES device(id),
  state         TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','precheck','printing','curing','paused','fault','done','cancelled')),
  layer_cursor  INTEGER NOT NULL DEFAULT 0,
  gcode_hash    TEXT NOT NULL,
  fault_plan_json TEXT NOT NULL DEFAULT '[]',
  safe_point_json TEXT,
  skip_log_json   TEXT,
  started_at    TEXT,
  finished_at   TEXT
);

-- defect（依赖：print_job, version）
CREATE TABLE IF NOT EXISTS defect (
  id            TEXT PRIMARY KEY,
  job_id        TEXT NOT NULL REFERENCES print_job(id),
  layer_z_mm    DOUBLE PRECISION NOT NULL,
  pass_no       INTEGER NOT NULL DEFAULT 0,
  type          TEXT NOT NULL CHECK (type IN ('open_circuit','clog','overtemp','oven_undertemp','misalign','material_out')),
  severity      INTEGER NOT NULL DEFAULT 1,
  bbox_json     TEXT NOT NULL DEFAULT '[0,0,0,0]',
  evidence      TEXT NOT NULL DEFAULT '',
  status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','fixed','ignored')),
  attribution_commit_id TEXT REFERENCES version(id),
  note          TEXT NOT NULL DEFAULT ''
);

-- mrc_rule（依赖：无）
CREATE TABLE IF NOT EXISTS mrc_rule (
  code          TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  threshold_json TEXT NOT NULL,
  weight        INTEGER NOT NULL DEFAULT 5,
  rule_version  INTEGER NOT NULL DEFAULT 1,
  enabled       INTEGER NOT NULL DEFAULT 1
);

-- mrc_issue（依赖：mrc_rule, version）
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

-- tool_geometry（依赖：无）
CREATE TABLE IF NOT EXISTS tool_geometry (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  nozzle_id_mm  DOUBLE PRECISION NOT NULL,
  tip_od_mm     DOUBLE PRECISION NOT NULL,
  safe_gap_mm   DOUBLE PRECISION NOT NULL DEFAULT 0.5,
  head_envelope_mm DOUBLE PRECISION NOT NULL DEFAULT 20.0,
  min_corner_r_mm DOUBLE PRECISION NOT NULL DEFAULT 0.8,
  min_mesa_w_mm DOUBLE PRECISION NOT NULL DEFAULT 1.0,
  max_aspect    DOUBLE PRECISION NOT NULL DEFAULT 1.0
);

-- blob（依赖：无）
CREATE TABLE IF NOT EXISTS blob (
  sha256        TEXT PRIMARY KEY,
  size          INTEGER NOT NULL,
  relpath       TEXT NOT NULL
);

-- ---- 高频查询索引（全部表建完之后）----

CREATE INDEX IF NOT EXISTS idx_version_project ON version(project_id, seq);

CREATE INDEX IF NOT EXISTS idx_pad_comp ON pad(comp_id);

CREATE INDEX IF NOT EXISTS idx_route_net ON route(net_id);

CREATE INDEX IF NOT EXISTS idx_layer_set ON layer(slice_set_id, pass_no);

CREATE INDEX IF NOT EXISTS idx_slice_latest ON slice_set(project_id, stale, created_at);

CREATE INDEX IF NOT EXISTS idx_path_layer ON path(layer_id, seq);

CREATE INDEX IF NOT EXISTS idx_issue_commit ON mrc_issue(commit_id, severity);

CREATE INDEX IF NOT EXISTS idx_pad_ref ON pad(comp_id, pad_no);

CREATE INDEX IF NOT EXISTS idx_route_circuit ON route(circuit_id);

CREATE INDEX IF NOT EXISTS idx_job_started ON print_job(started_at, id);

CREATE INDEX IF NOT EXISTS idx_comp_circuit ON comp(circuit_id);

CREATE INDEX IF NOT EXISTS idx_keepout_project ON keepout(project_id);
