"""CIR-001：import_kicad_pcb 拆分重构的等价性测试（金标快照法）。

金标数据 GOLDEN 在拆分 ame/circuit/kicad.py **之前**从原实现（import_kicad_pcb
单体版，圈复杂度 61）一次性采集，覆盖：

- samples/data/board_10.kicad_pcb（10 器件 / 20 焊盘 / 12 网络 / 0 走线）；
- samples/data/board_T19A.kicad_pcb（8 器件 / 20 焊盘 / 12 网络 / 8 走线）；
- 内联合成板 syn_full：现代 (property "Reference") 与老式 (fp_text reference)
  位号、缺位号告警、旋转/圆形/roundrect 焊盘、segment 同网络串接与
  按线宽/面别拆分、未声明网络线段丢弃、非 Edge.Cuts 图形忽略；
- 内联合成板 syn_bare：无板框与无器件两条告警、纯 segment 走线。

断言：返回 dict 的键集合、器件/焊盘/网络/走线数量、板框顶点、以及完整
内容（顺序、坐标、rot、shape、告警文案）与拆分前完全一致。

运行：python -m pytest tests/test_kicad_split.py -q
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ame.circuit.kicad import import_kicad_pcb

SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "data"

SYN_FULL = r"""(kicad_pcb (version 20240108) (generator pcbnew)
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (gr_rect (start 0 0) (end 50 40) (layer "Edge.Cuts"))
  (gr_line (start 60 60) (end 70 70) (layer "F.SilkS"))
  (footprint "R_0603" (layer "F.Cu") (at 10 12 90)
    (property "Reference" "R1" (at 10 12 0) (layer "F.SilkS"))
    (pad "1" smd roundrect (at -0.8 0) (size 0.9 0.9) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at 0.8 0) (size 0.9 0.5) (layers "B.Cu") (net 2 "GND")))
  (footprint "R_legacy" (layer "B.Cu") (at 20 22 45)
    (fp_text reference "R7" (at 20 22) (layer "F.SilkS"))
    (pad "1" smd circle (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "VCC")))
  (footprint "X" (layer "F.Cu") (at 5 5)
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "VCC")))
  (segment (start 10 12) (end 15 12) (width 0.4) (layer "F.Cu") (net 1))
  (segment (start 15 12) (end 20 12) (width 0.4) (layer "F.Cu") (net 1))
  (segment (start 10 12) (end 15 12) (width 0.8) (layer "B.Cu") (net 1))
  (segment (start 30 30) (end 31 30) (width 0.4) (layer "F.Cu") (net 99))
  (segment (start 40 40) (end 41 40) (width 0.4) (layer "F.Cu"))
)"""

SYN_BARE = r"""(kicad_pcb (version 20171130)
  (segment (start 0 0) (end 1 1) (width 0.4) (layer "F.Cu") (net 1))
  (net 1 "N1"))"""

GOLDEN: dict[str, dict] = {
    'board_10': json.loads(r'''{"comps":[{"footprint":"Lib:R0603","height":1.5,"ref":"R1","rot":0.0,"surface":"top","x":15.0,"y":20.0},{"footprint":"Lib:R0603","height":1.5,"ref":"R2","rot":0.0,"surface":"top","x":32.0,"y":20.0},{"footprint":"Lib:R0603","height":1.5,"ref":"R3","rot":0.0,"surface":"top","x":49.0,"y":20.0},{"footprint":"Lib:R0603","height":1.5,"ref":"R4","rot":0.0,"surface":"top","x":66.0,"y":20.0},{"footprint":"Lib:R0603","height":1.5,"ref":"R5","rot":0.0,"surface":"top","x":83.0,"y":20.0},{"footprint":"Lib:R0603","height":1.5,"ref":"R6","rot":0.0,"surface":"top","x":15.0,"y":40.0},{"footprint":"Lib:R0603","height":1.5,"ref":"R7","rot":0.0,"surface":"top","x":32.0,"y":40.0},{"footprint":"Lib:R0603","height":1.5,"ref":"R8","rot":0.0,"surface":"top","x":49.0,"y":40.0},{"footprint":"Lib:R0603","height":1.5,"ref":"R9","rot":0.0,"surface":"top","x":66.0,"y":40.0},{"footprint":"Lib:R0603","height":1.5,"ref":"R10","rot":0.0,"surface":"top","x":83.0,"y":40.0}],"format":"kicad_pcb","nets":{"N1":{"klass":"signal"},"N10":{"klass":"signal"},"N11":{"klass":"signal"},"N12":{"klass":"signal"},"N2":{"klass":"signal"},"N3":{"klass":"signal"},"N4":{"klass":"signal"},"N5":{"klass":"signal"},"N6":{"klass":"signal"},"N7":{"klass":"signal"},"N8":{"klass":"signal"},"N9":{"klass":"signal"}},"outline":[[5.0,5.0],[95.0,5.0],[95.0,55.0],[5.0,55.0]],"pads":[{"h":1.0,"net":"N1","pad_no":"1","ref":"R1","rot":0.0,"shape":"rect","w":1.5,"x":12.2,"y":20.0},{"h":1.0,"net":"N2","pad_no":"2","ref":"R1","rot":0.0,"shape":"rect","w":1.5,"x":17.8,"y":20.0},{"h":1.0,"net":"N3","pad_no":"1","ref":"R2","rot":0.0,"shape":"rect","w":1.5,"x":29.2,"y":20.0},{"h":1.0,"net":"N4","pad_no":"2","ref":"R2","rot":0.0,"shape":"rect","w":1.5,"x":34.8,"y":20.0},{"h":1.0,"net":"N5","pad_no":"1","ref":"R3","rot":0.0,"shape":"rect","w":1.5,"x":46.2,"y":20.0},{"h":1.0,"net":"N6","pad_no":"2","ref":"R3","rot":0.0,"shape":"rect","w":1.5,"x":51.8,"y":20.0},{"h":1.0,"net":"N7","pad_no":"1","ref":"R4","rot":0.0,"shape":"rect","w":1.5,"x":63.2,"y":20.0},{"h":1.0,"net":"N8","pad_no":"2","ref":"R4","rot":0.0,"shape":"rect","w":1.5,"x":68.8,"y":20.0},{"h":1.0,"net":"N9","pad_no":"1","ref":"R5","rot":0.0,"shape":"rect","w":1.5,"x":80.2,"y":20.0},{"h":1.0,"net":"N10","pad_no":"2","ref":"R5","rot":0.0,"shape":"rect","w":1.5,"x":85.8,"y":20.0},{"h":1.0,"net":"N11","pad_no":"1","ref":"R6","rot":0.0,"shape":"rect","w":1.5,"x":12.2,"y":40.0},{"h":1.0,"net":"N12","pad_no":"2","ref":"R6","rot":0.0,"shape":"rect","w":1.5,"x":17.8,"y":40.0},{"h":1.0,"net":"N1","pad_no":"1","ref":"R7","rot":0.0,"shape":"rect","w":1.5,"x":29.2,"y":40.0},{"h":1.0,"net":"N2","pad_no":"2","ref":"R7","rot":0.0,"shape":"rect","w":1.5,"x":34.8,"y":40.0},{"h":1.0,"net":"N3","pad_no":"1","ref":"R8","rot":0.0,"shape":"rect","w":1.5,"x":46.2,"y":40.0},{"h":1.0,"net":"N4","pad_no":"2","ref":"R8","rot":0.0,"shape":"rect","w":1.5,"x":51.8,"y":40.0},{"h":1.0,"net":"N5","pad_no":"1","ref":"R9","rot":0.0,"shape":"rect","w":1.5,"x":63.2,"y":40.0},{"h":1.0,"net":"N6","pad_no":"2","ref":"R9","rot":0.0,"shape":"rect","w":1.5,"x":68.8,"y":40.0},{"h":1.0,"net":"N7","pad_no":"1","ref":"R10","rot":0.0,"shape":"rect","w":1.5,"x":80.2,"y":40.0},{"h":1.0,"net":"N8","pad_no":"2","ref":"R10","rot":0.0,"shape":"rect","w":1.5,"x":85.8,"y":40.0}],"routes":[],"warnings":[]}'''),
    'board_T19A': json.loads(r'''{"comps":[{"footprint":"Lib:QFN8","height":1.5,"ref":"U1","rot":0.0,"surface":"top","x":20.0,"y":15.0},{"footprint":"Lib:R0603","height":1.5,"ref":"U2","rot":0.0,"surface":"top","x":80.0,"y":15.0},{"footprint":"Lib:QFN8","height":1.5,"ref":"C3","rot":0.0,"surface":"top","x":80.0,"y":30.0},{"footprint":"Lib:R0603","height":1.5,"ref":"R1","rot":0.0,"surface":"top","x":20.0,"y":45.0},{"footprint":"Lib:R0603","height":1.5,"ref":"R2","rot":0.0,"surface":"top","x":45.0,"y":45.0},{"footprint":"Lib:R0603","height":1.5,"ref":"R3","rot":0.0,"surface":"top","x":70.0,"y":45.0},{"footprint":"Lib:R0603","height":1.5,"ref":"C1","rot":0.0,"surface":"top","x":20.0,"y":30.0},{"footprint":"Lib:R0603","height":1.5,"ref":"C2","rot":0.0,"surface":"top","x":50.0,"y":30.0}],"format":"kicad_pcb","nets":{"GND":{"klass":"signal"},"N1":{"klass":"signal"},"N2":{"klass":"signal"},"N3":{"klass":"signal"},"N4":{"klass":"signal"},"N5":{"klass":"signal"},"N6":{"klass":"signal"},"TP1":{"klass":"signal"},"TP2":{"klass":"signal"},"TP3":{"klass":"signal"},"TP4":{"klass":"signal"},"VCC":{"klass":"signal"}},"outline":[[5.0,5.0],[95.0,5.0],[95.0,55.0],[5.0,55.0]],"pads":[{"h":1.0,"net":"GND","pad_no":"1","ref":"U1","rot":0.0,"shape":"rect","w":1.5,"x":17.2,"y":15.0},{"h":1.0,"net":"VCC","pad_no":"2","ref":"U1","rot":0.0,"shape":"rect","w":1.5,"x":22.8,"y":15.0},{"h":1.0,"net":"TP1","pad_no":"3","ref":"U1","rot":0.0,"shape":"rect","w":1.5,"x":17.2,"y":13.0},{"h":1.0,"net":"TP2","pad_no":"4","ref":"U1","rot":0.0,"shape":"rect","w":1.5,"x":22.8,"y":13.0},{"h":1.0,"net":"VCC","pad_no":"1","ref":"U2","rot":0.0,"shape":"rect","w":1.5,"x":77.2,"y":15.0},{"h":1.0,"net":"N1","pad_no":"2","ref":"U2","rot":0.0,"shape":"rect","w":1.5,"x":82.8,"y":15.0},{"h":1.0,"net":"N6","pad_no":"1","ref":"C3","rot":0.0,"shape":"rect","w":1.5,"x":77.2,"y":30.0},{"h":1.0,"net":"GND","pad_no":"2","ref":"C3","rot":0.0,"shape":"rect","w":1.5,"x":82.8,"y":30.0},{"h":1.0,"net":"TP3","pad_no":"3","ref":"C3","rot":0.0,"shape":"rect","w":1.5,"x":77.2,"y":28.0},{"h":1.0,"net":"TP4","pad_no":"4","ref":"C3","rot":0.0,"shape":"rect","w":1.5,"x":82.8,"y":28.0},{"h":1.0,"net":"N1","pad_no":"1","ref":"R1","rot":0.0,"shape":"rect","w":1.5,"x":17.2,"y":45.0},{"h":1.0,"net":"N2","pad_no":"2","ref":"R1","rot":0.0,"shape":"rect","w":1.5,"x":22.8,"y":45.0},{"h":1.0,"net":"N2","pad_no":"1","ref":"R2","rot":0.0,"shape":"rect","w":1.5,"x":42.2,"y":45.0},{"h":1.0,"net":"N3","pad_no":"2","ref":"R2","rot":0.0,"shape":"rect","w":1.5,"x":47.8,"y":45.0},{"h":1.0,"net":"N3","pad_no":"1","ref":"R3","rot":0.0,"shape":"rect","w":1.5,"x":67.2,"y":45.0},{"h":1.0,"net":"N4","pad_no":"2","ref":"R3","rot":0.0,"shape":"rect","w":1.5,"x":72.8,"y":45.0},{"h":1.0,"net":"N4","pad_no":"1","ref":"C1","rot":0.0,"shape":"rect","w":1.5,"x":17.2,"y":30.0},{"h":1.0,"net":"N5","pad_no":"2","ref":"C1","rot":0.0,"shape":"rect","w":1.5,"x":22.8,"y":30.0},{"h":1.0,"net":"N5","pad_no":"1","ref":"C2","rot":0.0,"shape":"rect","w":1.5,"x":47.2,"y":30.0},{"h":1.0,"net":"N6","pad_no":"2","ref":"C2","rot":0.0,"shape":"rect","w":1.5,"x":52.8,"y":30.0}],"routes":[{"net":"VCC","pts":[[22.8,15.0,0.0],[77.2,15.0,0.0]],"surface":"top","width":0.3},{"net":"N1","pts":[[17.2,45.0,0.0],[13.0,41.0,0.0],[13.0,20.0,0.0],[88.0,20.0,0.0],[88.0,15.0,0.0],[82.8,15.0,0.0]],"surface":"top","width":0.3},{"net":"N2","pts":[[22.8,45.0,0.0],[22.8,42.0,0.0],[42.2,42.0,0.0],[42.2,45.0,0.0]],"surface":"top","width":0.3},{"net":"N3","pts":[[47.8,45.0,0.0],[47.8,42.0,0.0],[67.2,42.0,0.0],[67.2,45.0,0.0]],"surface":"top","width":0.3},{"net":"N4","pts":[[17.2,30.0,0.0],[15.0,33.0,0.0],[15.0,39.0,0.0],[72.8,39.0,0.0],[72.8,45.0,0.0]],"surface":"top","width":0.6},{"net":"N5","pts":[[22.8,30.0,0.0],[22.8,27.5,0.0],[47.2,27.5,0.0],[47.2,30.0,0.0]],"surface":"top","width":0.6},{"net":"N6","pts":[[52.8,30.0,0.0],[52.8,28.9,0.0],[77.2,28.9,0.0],[77.2,30.0,0.0]],"surface":"top","width":0.3},{"net":"GND","pts":[[17.2,15.0,0.0],[14.0,12.0,0.0],[10.0,12.0,0.0],[10.0,9.0,0.0],[90.0,9.0,0.0],[90.0,30.0,0.0],[82.8,30.0,0.0]],"surface":"top","width":0.6}],"warnings":[]}'''),
    'syn_bare': json.loads(r'''{"comps":[],"format":"kicad_pcb","nets":{"N1":{"klass":"signal"}},"outline":[],"pads":[],"routes":[{"net":"N1","pts":[[0.0,0.0,0.0],[1.0,1.0,0.0]],"surface":"top","width":0.4}],"warnings":["板文件无 Edge.Cuts 板框，请手工指定","板文件未解析到器件"]}'''),
    'syn_full': json.loads(r'''{"comps":[{"footprint":"R_0603","height":1.5,"ref":"R1","rot":90.0,"surface":"top","x":10.0,"y":12.0},{"footprint":"R_legacy","height":1.5,"ref":"R7","rot":45.0,"surface":"bottom","x":20.0,"y":22.0},{"footprint":"X","height":1.5,"ref":"COMP3","rot":0.0,"surface":"top","x":5.0,"y":5.0}],"format":"kicad_pcb","nets":{"GND":{"klass":"signal"},"VCC":{"klass":"signal"}},"outline":[[0.0,0.0],[50.0,0.0],[50.0,40.0],[0.0,40.0]],"pads":[{"h":0.9,"net":"VCC","pad_no":"1","ref":"R1","rot":90.0,"shape":"rect","w":0.9,"x":10.0,"y":11.2},{"h":0.5,"net":"GND","pad_no":"2","ref":"R1","rot":90.0,"shape":"rect","w":0.9,"x":10.0,"y":12.8},{"h":1.0,"net":"VCC","pad_no":"1","ref":"R7","rot":45.0,"shape":"circle","w":1.0,"x":20.0,"y":22.0},{"h":1.0,"net":"VCC","pad_no":"1","ref":"COMP3","rot":0.0,"shape":"rect","w":1.0,"x":5.0,"y":5.0}],"routes":[{"net":"VCC","pts":[[10.0,12.0,0.0],[15.0,12.0,0.0],[20.0,12.0,0.0]],"surface":"top","width":0.4},{"net":"VCC","pts":[[10.0,12.0,0.0],[15.0,12.0,0.0]],"surface":"bottom","width":0.8}],"warnings":["第 3 个器件未找到位号（Reference），临时命名为 COMP3；请在原理图/封装中补齐位号后重新导出"]}'''),
}


def _capture(text: str) -> dict:
    """BoardData → 可比较快照（元组经 JSON 规整为列表，字段与金标一致）。"""
    bd = import_kicad_pcb(text)
    cap = {
        "format": bd.format,
        "outline": [list(p) for p in bd.outline],
        "comps": bd.comps,
        "pads": bd.pads,
        "nets": bd.nets,
        "routes": bd.routes,
        "warnings": bd.warnings,
    }
    return json.loads(json.dumps(cap, ensure_ascii=False))


def _inputs() -> dict[str, str]:
    """等价性样本：真实样本（samples/data）+ 合成板。"""
    return {
        "board_10": (SAMPLES / "board_10.kicad_pcb").read_text("utf-8"),
        "board_T19A": (SAMPLES / "board_T19A.kicad_pcb").read_text("utf-8"),
        "syn_full": SYN_FULL,
        "syn_bare": SYN_BARE,
    }


@pytest.mark.parametrize("name", sorted(_inputs()))
def test_matches_pre_split_golden(name: str):
    """拆分前后：键集合 / 数量 / 板框顶点 / 完整内容全部一致。"""
    golden = GOLDEN[name]
    actual = _capture(_inputs()[name])
    # 返回 dict 的所有键一致
    assert sorted(actual) == sorted(golden)
    # 器件/焊盘/网络/走线数量完全一致
    for key in ("comps", "pads", "nets", "routes"):
        assert len(actual[key]) == len(golden[key]), key
    # 板框顶点完全一致
    assert actual["outline"] == golden["outline"]
    # 深度等价：每个器件/焊盘/走线/告警的顺序与字段值全部锁定
    assert actual == golden


def test_real_samples_exist():
    """金标对应的两个真实样本必须在位（防样本被移动后金标悄悄失效）。"""
    assert (SAMPLES / "board_10.kicad_pcb").is_file()
    assert (SAMPLES / "board_T19A.kicad_pcb").is_file()


def test_all_sample_pcbs_import_consistently():
    """samples/data 下所有 .kicad_pcb（含未来新增）都过结构性校验。"""
    for f in sorted(SAMPLES.rglob("*.kicad_pcb")):
        cap = _capture(f.read_text("utf-8"))
        assert sorted(cap) == ["comps", "format", "nets", "outline", "pads",
                               "routes", "warnings"], f.name
        # 板框：空，或四角包围盒（每个顶点是 (x, y)）
        assert all(len(p) == 2 for p in cap["outline"]), f.name
        refs = {c["ref"] for c in cap["comps"]}
        assert all(p["ref"] in refs for p in cap["pads"]), f.name
        # 焊盘网络与走线网络都必须登记在 nets 里（空网络名 → None）
        assert all(p["net"] is None or p["net"] in cap["nets"]
                   for p in cap["pads"]), f.name
        assert all(r["net"] in cap["nets"] for r in cap["routes"]), f.name


# ================================================================ NFR-002 畸形节点
class TestMalformedNodes:
    """畸形 KiCad 节点不得让导入崩溃（NFR-002）。

    真实触发场景：文件被截断、手工编辑出错、导出器 bug，
    都会产生 `(width)` `(net)` `(layer)` 这种**只有标签没有值**的节点。
    """

    @staticmethod
    def _board(extra: str) -> str:
        return f'''(kicad_pcb (version 20221018) (generator pcbnew)
  (net 0 "")
  (net 1 "N1")
  (footprint "R" (layer "F.Cu") (at 10 10)
    (fp_text reference "R1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "N1")))
  (segment (start 10 10) (end 20 20) (layer "F.Cu") {extra} (net 1))
)'''

    @pytest.mark.parametrize("node,label", [
        ("(width)", "width 无参数"),
        ("(net)", "net 无参数"),
        ("(layer)", "layer 无参数"),
    ])
    def test_empty_node_does_not_crash(self, node, label):
        r = import_kicad_pcb(self._board(node))
        # 该段走线因缺关键信息被丢弃是合理的，但**不能抛异常**
        assert isinstance(r.routes, list)
        assert r.comps and len(r.comps) == 1, "器件解析不应受走线段畸形影响"

    def test_truncated_file_does_not_crash(self):
        """文件截断到一半：不崩溃即可（能解析多少算多少）。"""
        full = self._board("(width 0.25)")
        for cut in (len(full) // 3, len(full) // 2, len(full) - 20):
            r = import_kicad_pcb(full[:cut])
            assert isinstance(r.comps, list)

    def test_normal_segment_still_parsed(self):
        """修复畸形容错后，正常 segment 必须仍然被正确解析（回归护栏）。"""
        r = import_kicad_pcb(self._board("(width 0.25)"))
        assert len(r.routes) == 1, f"正常走线应被解析，实际 {len(r.routes)} 条"
        assert r.routes[0]["width"] == pytest.approx(0.25)
