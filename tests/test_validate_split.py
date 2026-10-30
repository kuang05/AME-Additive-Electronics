"""validate_route 拆分等价性测试（CIR-010/011 校验清单重构）。

validate_route 原为约 102 行的单体函数（圈复杂度 34，ruff C901 口径），
已拆分为 _check_* 单项检查清单。本文件把拆分**之前**用原实现逐例采集的
(ok, reason) 金标准逐字固化：

- 每组用例断言 (ok, reason) 与拆分前完全一致，reason 一字不差；
- order_* 用例锁定检查顺序——先线宽后板框等顺序若被调整，
  同一条非法走线会报不同的错，对应用例会立即失败；
- 两个 TypeError 用例固化原实现对"数值型字符串"的异常行为
  （字符串能通过有限性检查、却在比较/运算处炸掉）。

金标准采集方式：对原实现逐例运行并记录 repr((ok, reason)) 后誊写。
"""
from __future__ import annotations

import pytest

from ame.circuit.connectivity import validate_route

SQUARE = [(0, 0), (100, 0), (100, 60), (0, 60)]
BOARD = [(5, 5), (95, 5), (95, 55), (5, 55)]
# U 形板：中间 40–60 处从 y=20 向上开缺口
U_SHAPE = [(0, 0), (100, 0), (100, 60), (60, 60), (60, 20),
           (40, 20), (40, 60), (0, 60)]
KEEPOUT = [{"pts": [[15, 15], [25, 15], [25, 25], [15, 25]], "name": "螺丝孔"}]
KEEPOUT_UNNAMED = [{"pts": [[15, 15], [25, 15], [25, 25], [15, 25]]}]
KEEPOUT_DEGENERATE = [{"pts": [[15, 15], [25, 15]], "name": "退化"}]
KEEPOUT_TOUCHING = [{"pts": [[10, 20.3], [30, 20.3], [30, 25], [10, 25]],
                     "name": "擦边"}]   # 导体带与禁区恰好共边（交集面积为 0）
KEEPOUT_CENTER = [{"pts": [[45, 45], [55, 45], [55, 55], [45, 55]],
                   "name": "中央"}]

# (pts, width, outline, keepouts, 期望 ok, 期望 reason——拆分前逐字采集)
CASES = [
    # ---- 合法走线 --------------------------------------------------------
    pytest.param([[22.8, 15], [77.2, 15]], 0.6, BOARD, None,
                 True, "", id="legal_two_points"),
    pytest.param([[10, 10], [50, 10], [50, 40], [80, 40]], 0.6, SQUARE, None,
                 True, "", id="legal_multi_segment"),
    pytest.param([[10, 10], [50, 50]], 1, SQUARE, None,
                 True, "", id="legal_int_width"),
    pytest.param([[10, 10], [40, 10], [40, 40], [70, 40]], 0.6, SQUARE, None,
                 True, "", id="legal_adjacent_shared_point_zigzag"),
    pytest.param([[10, 10], [50, 50], [50, 20], [10, 10]], 0.6, SQUARE, None,
                 True, "", id="legal_closed_loop"),
    pytest.param([[10, 10], [10, 50]], 0.6, U_SHAPE, None,
                 True, "", id="legal_route_in_concave_board"),
    pytest.param([[10, 10], [50, 50]], 0.6, [], None,
                 True, "", id="legal_empty_outline"),
    pytest.param([[10, 10], [50, 50]], 0.6, [(0, 0), (1, 1)], None,
                 True, "", id="two_point_outline_skipped"),
    # ---- 边界值 ----------------------------------------------------------
    pytest.param([[10, 10], [50, 50]], 0.5, SQUARE, None,
                 True, "", id="boundary_width_exactly_0_5"),
    pytest.param([[10, 5], [50, 5]], 0.6, BOARD, None,
                 True, "", id="boundary_point_on_outline_edge"),
    pytest.param([[5, 5], [50, 30]], 0.6, BOARD, None,
                 True, "", id="boundary_point_on_outline_corner"),
    pytest.param([[5, 55], [50, 5]], 0.6, BOARD, None,
                 True, "", id="legal_touch_outline_vertex"),
    pytest.param([[10, 20], [30, 20]], 0.6, SQUARE, KEEPOUT_TOUCHING,
                 True, "", id="legal_keepout_touch_edge"),
    pytest.param([[10, 20], [30, 20]], 0.6, SQUARE, KEEPOUT_DEGENERATE,
                 True, "", id="legal_keepout_degenerate"),
    pytest.param([[70, 20], [90, 20]], 0.6, SQUARE, KEEPOUT,
                 True, "", id="legal_keepout_far"),
    # ---- 线宽非法（CIR-010）----------------------------------------------
    pytest.param([[10, 10], [50, 50]], 0.3, SQUARE, None,
                 False, "线宽 0.30 mm 低于工艺下限 0.5 mm", id="illegal_width_0_3"),
    pytest.param([[10, 10], [50, 50]], -0.8, SQUARE, None,
                 False, "线宽 -0.80 mm 低于工艺下限 0.5 mm", id="illegal_width_negative"),
    pytest.param([[10, 10], [50, 50]], float("nan"), SQUARE, None,
                 False, "线宽必须是有限数值，收到 nan", id="illegal_width_nan"),
    pytest.param([[10, 10], [50, 50]], float("inf"), SQUARE, None,
                 False, "线宽必须是有限数值，收到 inf", id="illegal_width_inf"),
    pytest.param([[10, 10], [50, 50]], "abc", SQUARE, None,
                 False, "线宽必须是数值，收到 'abc'", id="illegal_width_string_alpha"),
    pytest.param([[10, 10], [50, 50]], None, SQUARE, None,
                 False, "线宽必须是数值，收到 None", id="illegal_width_none"),
    # ---- 点数 / 坐标非法 --------------------------------------------------
    pytest.param([[22.8, 15]], 0.6, BOARD, None,
                 False, "走线至少需要两个点", id="illegal_single_point"),
    pytest.param([], 0.6, BOARD, None,
                 False, "走线至少需要两个点", id="illegal_empty_pts"),
    pytest.param([[10, 10], [float("nan"), 20]], 0.6, SQUARE, None,
                 False, "第 2 个点的 X 坐标不是有限数值：nan", id="illegal_nan_coord"),
    pytest.param([[10, float("inf")], [50, 20]], 0.6, SQUARE, None,
                 False, "第 1 个点的 Y 坐标不是有限数值：inf", id="illegal_inf_coord"),
    pytest.param([[10, 10], ["x", 20]], 0.6, SQUARE, None,
                 False, "第 2 个点的 X 坐标不是数值：'x'", id="illegal_str_coord"),
    pytest.param([[10, 10], [20]], 0.6, SQUARE, None,
                 False, "第 2 个点坐标不完整", id="illegal_incomplete_point"),
    # ---- 零长度 / 重复点 --------------------------------------------------
    pytest.param([[22.8, 15], [22.8, 15]], 0.6, BOARD, None,
                 False, "第 1 段为零长度", id="illegal_zero_length_dup_pair"),
    pytest.param([[10, 10], [20, 20], [20, 20], [30, 30]], 0.6, SQUARE, None,
                 False, "第 2 段为零长度", id="illegal_zero_length_mid"),
    pytest.param([[10, 10], [50, 50], [10, 10]], 0.6, SQUARE, None,
                 False, "第 1 个点与第 3 个点重复 (10.0,10.0)",
                 id="illegal_duplicate_nonadjacent"),
    pytest.param([[10, 10], [50, 50], [80, 80], [50, 50]], 0.6, SQUARE, None,
                 False, "第 2 个点与第 4 个点重复 (50.0,50.0)",
                 id="illegal_duplicate_interior"),
    # ---- 板框（CIR-011）---------------------------------------------------
    pytest.param([[22.8, 15], [99, 30]], 0.6, BOARD, None,
                 False, "第 2 个点 (99.0,30.0) 超出板框",
                 id="illegal_point_out_of_outline"),
    pytest.param([[30, 50], [70, 50]], 0.6, U_SHAPE, None,
                 False, "第 1 段穿出板框",
                 id="illegal_segment_through_concave_gap"),
    # ---- 自交 -------------------------------------------------------------
    pytest.param([[10, 10], [50, 50], [50, 10], [10, 50]], 0.6, SQUARE, None,
                 False, "走线自交于第 1 段与第 3 段", id="illegal_self_intersect"),
    pytest.param([[10, 10], [50, 10], [30, 10], [30, 30]], 0.6, SQUARE, None,
                 False, "走线自交于第 1 段与第 3 段", id="t_junction_touch"),
    # ---- 禁布区（CIR-006）-------------------------------------------------
    pytest.param([[10, 20], [30, 20]], 0.6, SQUARE, KEEPOUT,
                 False, "走线进入禁布区 螺丝孔（CIR-006：禁区不可布线）",
                 id="illegal_keepout_hit"),
    pytest.param([[10, 20], [30, 20]], 0.6, SQUARE, KEEPOUT_UNNAMED,
                 False, "走线进入禁布区 未命名（CIR-006：禁区不可布线）",
                 id="illegal_keepout_unnamed"),
    # ---- 检查顺序（顺序一变，同一条非法走线就会报不同的错）----------------
    pytest.param([[10, 10], [99, 30]], 0.3, BOARD, None,
                 False, "线宽 0.30 mm 低于工艺下限 0.5 mm",
                 id="order_width_before_outline"),
    pytest.param([[22.8, 15]], 0.3, BOARD, None,
                 False, "线宽 0.30 mm 低于工艺下限 0.5 mm",
                 id="order_width_before_single_point"),
    pytest.param([[10, 10], [float("nan"), 20]], 0.6, BOARD, None,
                 False, "第 2 个点的 X 坐标不是有限数值：nan",
                 id="order_coord_before_outline"),
    pytest.param([[22.8, 15]], 0.6, BOARD, None,
                 False, "走线至少需要两个点",
                 id="order_pointcount_before_zero_length"),
    pytest.param([[10, 10], [10, 10], [50, 50]], 0.6, SQUARE, None,
                 False, "第 1 段为零长度",
                 id="order_zero_length_before_duplicate"),
    pytest.param([[10, 10], [50, 50], [50, 10], [110, 50]], 0.6, BOARD, None,
                 False, "第 4 个点 (110.0,50.0) 超出板框",
                 id="order_outline_before_self_intersect"),
    pytest.param([[10, 20], [99, 20]], 0.6, BOARD, KEEPOUT,
                 False, "第 2 个点 (99.0,20.0) 超出板框",
                 id="order_outline_before_keepout"),
    pytest.param([[10, 10], [50, 50], [50, 10], [10, 50]], 0.6, SQUARE,
                 KEEPOUT_CENTER,
                 False, "走线进入禁布区 中央（CIR-006：禁区不可布线）",
                 id="order_keepout_before_self_intersect"),
]


@pytest.mark.parametrize("pts,width,outline,keepouts,exp_ok,exp_reason", CASES)
def test_matches_pre_split_golden(pts, width, outline, keepouts,
                                  exp_ok, exp_reason):
    """(ok, reason) 必须与拆分前逐字一致（含检查顺序决定的报错优先级）。"""
    ok, reason = validate_route(pts, width, outline, keepouts=keepouts)
    assert (ok, reason) == (exp_ok, exp_reason)


@pytest.mark.parametrize("pts,width,outline", [
    pytest.param([[10, 10], [50, 50]], "0.3", SQUARE,
                 id="numeric_string_width"),
    pytest.param([[10, 10], ["50", 20]], 0.6, SQUARE,
                 id="numeric_string_coord"),
], )
def test_type_error_behavior_preserved(pts, width, outline):
    """数值型字符串能通过有限性检查（float() 可转），随后在比较/运算处
    抛 TypeError——原实现如此，拆分后必须保持同一行为。"""
    with pytest.raises(TypeError):
        validate_route(pts, width, outline)
