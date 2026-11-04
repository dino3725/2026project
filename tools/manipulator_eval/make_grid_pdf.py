#!/usr/bin/env python3
"""
Draw the printable base_link grid for measurements B and C (no ROS).

Four A4 portrait sheets tile x = -40..360 mm, y = -180..180 mm. Paper up is
robot forward (+x) and paper left is robot left (+y). Every sheet carries
10 mm lines, 50 mm bold lines, coordinate labels, the wheel placement boxes,
the vision marks (marks_vision.csv), the arm targets (targets_arm_*.csv),
registration marks on the joints and a 100 mm scale check bar.

    python3 tools/manipulator_eval/make_grid_pdf.py
    python3 tools/manipulator_eval/make_grid_pdf.py --png-preview /tmp/grid

Print at actual size (100 %, no "fit to page").
"""

import argparse
import csv
from dataclasses import dataclass
import os
from typing import Dict, List, Tuple

import matplotlib

matplotlib.use('Agg')
from matplotlib import font_manager  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402
import matplotlib.patches as patches  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, 'print', 'grid_a4_x4.pdf')

PAGE_W, PAGE_H = 210.0, 297.0
GRID_X = (-40.0, 360.0)        # robot forward, paper up
GRID_Y = (-180.0, 180.0)       # robot left, paper left
SPLIT_X = 160.0                # row joint
SPLIT_Y = 0.0                  # column joint (the y = 0 centre line)
BAND = 10.0                    # overlap band covered by the next sheet
LEFT = 10.0                    # paper margin left of every cell
BOTTOM = 72.0                  # paper margin below every cell

WHEEL_Y = 144.0                # URDF wheel_*_joint y
WHEEL_RADIUS = 33.0
WHEEL_WIDTH = 18.0
BODY_FRONT_X = 69.0            # URDF base collision box front face
BODY_HALF_Y = 133.0

REG_ON_ROW_JOINT = (150.0, 60.0, -60.0, -150.0)    # y values on x = 160
REG_ON_COL_JOINT = (-20.0, 110.0, 180.0, 340.0)    # x values on y = 0

THIN, BOLD, AXIS = 0.25, 0.9, 1.6   # line widths (pt)
GRID_GREY = '0.55'

FONT_CANDIDATES = (
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc',
)


@dataclass(frozen=True)
class Sheet:
    """One A4 sheet covering a rectangle of base_link x/y."""

    number: int
    name: str
    x_range: Tuple[float, float]
    y_range: Tuple[float, float]
    band_top: bool      # grid continues BAND mm past x_max (covered later)
    band_right: bool    # grid continues BAND mm past y_min (covered later)
    cut_bottom: bool    # trim along x_min, lay over the sheet below
    cut_left: bool      # trim along y_max, lay over the sheet to the left

    def to_paper(self, x: float, y: float) -> Tuple[float, float]:
        """Map base_link (x, y) mm to paper (u, v) mm."""
        return LEFT + (self.y_range[1] - y), BOTTOM + (x - self.x_range[0])

    @property
    def drawn_x(self) -> Tuple[float, float]:
        """Return the x span drawn, including the overlap band."""
        top = self.x_range[1] + (BAND if self.band_top else 0.0)
        return self.x_range[0], top

    @property
    def drawn_y(self) -> Tuple[float, float]:
        """Return the y span drawn, including the overlap band."""
        low = self.y_range[0] - (BAND if self.band_right else 0.0)
        return low, self.y_range[1]


SHEETS = (
    Sheet(1, '왼쪽 아래', (GRID_X[0], SPLIT_X), (SPLIT_Y, GRID_Y[1]),
          band_top=True, band_right=True, cut_bottom=False, cut_left=False),
    Sheet(2, '오른쪽 아래', (GRID_X[0], SPLIT_X), (GRID_Y[0], SPLIT_Y),
          band_top=True, band_right=False, cut_bottom=False, cut_left=True),
    Sheet(3, '왼쪽 위', (SPLIT_X, GRID_X[1]), (SPLIT_Y, GRID_Y[1]),
          band_top=False, band_right=True, cut_bottom=True, cut_left=False),
    Sheet(4, '오른쪽 위', (SPLIT_X, GRID_X[1]), (GRID_Y[0], SPLIT_Y),
          band_top=False, band_right=False, cut_bottom=True, cut_left=True),
)


def load_points() -> Tuple[Dict[str, Tuple[float, float]],
                           Dict[Tuple[float, float], List[str]]]:
    """Read vision marks and arm targets (grouped by x/y) from the CSVs."""
    vision = {}
    with open(os.path.join(HERE, 'marks_vision.csv'), encoding='utf-8') as f:
        for row in csv.DictReader(f):
            vision[row['id']] = (float(row['x_mm']), float(row['y_mm']))
    arm: Dict[Tuple[float, float], List[str]] = {}
    for name in ('targets_arm_high.csv', 'targets_arm_low.csv'):
        with open(os.path.join(HERE, name), encoding='utf-8') as f:
            for row in csv.DictReader(f):
                key = (float(row['x_mm']), float(row['y_mm']))
                ids = arm.setdefault(key, [])
                if row['id'] not in ids:
                    ids.append(row['id'])
    return vision, arm


def setup_font() -> str:
    """Register a CJK font and return its family name."""
    matplotlib.rcParams['pdf.fonttype'] = 3
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            font_manager.fontManager.addfont(path)
            return font_manager.FontProperties(fname=path).get_name()
    return 'DejaVu Sans'


def inside(sheet: Sheet, x: float, y: float, pad: float = 0.0) -> bool:
    """Return True if (x, y) lies on the drawn part of the sheet."""
    (x0, x1), (y0, y1) = sheet.drawn_x, sheet.drawn_y
    return x0 - pad <= x <= x1 + pad and y0 - pad <= y <= y1 + pad


def draw_grid(ax, sheet: Sheet) -> None:
    """Draw 10 mm / 50 mm lines and the two axes."""
    (x0, x1), (y0, y1) = sheet.drawn_x, sheet.drawn_y
    for x in range(int(x0), int(x1) + 1, 10):
        width = AXIS if x == 0 else BOLD if x % 50 == 0 else THIN
        color = 'black' if x % 50 == 0 else GRID_GREY
        (ua, va), (ub, vb) = sheet.to_paper(x, y0), sheet.to_paper(x, y1)
        ax.plot([ua, ub], [va, vb], lw=width, color=color,
                solid_capstyle='butt')
    for y in range(int(y0), int(y1) + 1, 10):
        width = AXIS if y == 0 else BOLD if y % 50 == 0 else THIN
        color = 'black' if y % 50 == 0 else GRID_GREY
        (ua, va), (ub, vb) = sheet.to_paper(x0, y), sheet.to_paper(x1, y)
        ax.plot([ua, ub], [va, vb], lw=width, color=color,
                solid_capstyle='butt')


def label(ax, u, v, text, size=5.5, **kwargs) -> None:
    """Write a small label with a white box so grid lines stay readable."""
    box = dict(boxstyle='square,pad=0.08', fc='white', ec='none')
    ax.text(u, v, text, fontsize=size, bbox=box, zorder=5, **kwargs)


def multiples_of_50(low: float, high: float) -> List[int]:
    """Return multiples of 50 strictly inside (low, high), plus 0 if inside."""
    start = int(-(-low // 50) * 50)
    values = [v for v in range(start, int(high) + 1, 50) if low < v < high]
    if low <= 0 <= high and 0 not in values:
        values.append(0)
    return values


def draw_labels(ax, sheet: Sheet) -> None:
    """Label every 50 mm line near both edges of the cell."""
    (x0, x1), (y0, y1) = sheet.x_range, sheet.y_range
    for x in multiples_of_50(x0, x1):
        for y_edge, ha in ((y1 - 2, 'left'), (y0 + 12, 'right')):
            u, v = sheet.to_paper(x, y_edge)
            label(ax, u, v + 1.0, f'x {x:+d}' if x else 'x 0',
                  ha=ha, va='bottom')
    for y in multiples_of_50(y0, y1):
        for x_edge, va in ((x0 + 2, 'bottom'), (x1 - 2, 'top')):
            u, v = sheet.to_paper(x_edge, y)
            label(ax, u + 1.0, v, f'y {y:+d}' if y else 'y 0',
                  ha='left', va=va, rotation=90)


def draw_axes_notes(ax, sheet: Sheet) -> None:
    """Name the x = 0 axle line and the y = 0 centre line."""
    if sheet.x_range[0] <= 0 <= sheet.x_range[1]:
        y_text = 75.0 if sheet.y_range[1] > 0 else -75.0
        u, v = sheet.to_paper(0, y_text)
        label(ax, u, v + 1.2, 'x = 0  바퀴 축 선 (base_link 원점 통과)',
              size=6.5, ha='center', va='bottom', weight='bold')
    x_text = 105.0 if sheet.x_range[0] < 0 else 320.0
    u, v = sheet.to_paper(x_text, 0)
    side = 9.0 if sheet.y_range[1] > 0 else -9.0
    label(ax, u - side, v, 'y = 0  중심선 (로봇 정면 방향)', size=6.5,
          ha='right' if side > 0 else 'left', va='center', rotation=90,
          weight='bold')


def draw_robot(ax, sheet: Sheet) -> None:
    """Draw wheel placement boxes and the body front edge."""
    for side, y_wheel, name in ((1, WHEEL_Y, '왼쪽'), (-1, -WHEEL_Y, '오른쪽')):
        if not inside(sheet, 0, y_wheel):
            continue
        u0, v0 = sheet.to_paper(-WHEEL_RADIUS, y_wheel + WHEEL_WIDTH / 2)
        rect = patches.Rectangle(
            (u0, v0), WHEEL_WIDTH, 2 * WHEEL_RADIUS, fill=True,
            fc='0.88', ec='black', lw=1.4, ls='--', zorder=3)
        ax.add_patch(rect)
        u, v = sheet.to_paper(0, y_wheel)
        ax.plot([u - 6, u + 6], [v, v], color='black', lw=1.4, zorder=4)
        ax.plot([u, u], [v - 6, v + 6], color='black', lw=1.4, zorder=4)
        text_u = u + (WHEEL_WIDTH / 2 + 2 if side > 0 else
                      -WHEEL_WIDTH / 2 - 2)
        label(ax, text_u, v + 18,
              f'{name} 바퀴\n(0, {y_wheel:+.0f})\n'
              '허브 중심을\n십자 위에', size=6, weight='bold',
              ha='left' if side > 0 else 'right', va='center')
    y_lo = max(-BODY_HALF_Y, sheet.y_range[0])
    y_hi = min(BODY_HALF_Y, sheet.y_range[1])
    if sheet.x_range[0] <= BODY_FRONT_X <= sheet.x_range[1] and y_lo < y_hi:
        (ua, va), (ub, vb) = (sheet.to_paper(BODY_FRONT_X, y_lo),
                              sheet.to_paper(BODY_FRONT_X, y_hi))
        ax.plot([ua, ub], [va, vb], color='black', lw=0.8, ls=(0, (4, 2)),
                zorder=3)
        u, v = sheet.to_paper(BODY_FRONT_X, (y_lo + y_hi) / 2)
        label(ax, u, v + 1.2, '로봇 몸체 앞면 참고선 (URDF x≈69)', size=5.5,
              ha='center', va='bottom')


def on_cell(sheet: Sheet, x: float, y: float) -> bool:
    """Return True if (x, y) lies in the sheet's own cell, edges included."""
    return (sheet.x_range[0] <= x <= sheet.x_range[1]
            and sheet.y_range[0] <= y <= sheet.y_range[1])


def draw_points(ax, sheet: Sheet, vision, arm) -> None:
    """
    Draw vision marks (circle) and arm targets (square).

    A point on a joint line is drawn on both sheets so that each half stays
    visible after the overlap; labels go on the side that is never covered
    (paper left, +y).
    """
    for mark_id, (x, y) in vision.items():
        if not on_cell(sheet, x, y):
            continue
        u, v = sheet.to_paper(x, y)
        ax.add_patch(patches.Circle((u, v), 4.0, fill=False, ec='black',
                                    lw=1.3, zorder=6))
        ax.plot([u - 7, u + 7], [v, v], color='black', lw=0.9, zorder=6)
        ax.plot([u, u], [v - 7, v + 7], color='black', lw=0.9, zorder=6)
        if sheet.cut_left and y == sheet.y_range[1]:
            continue  # the neighbour sheet carries the label
        if y == SPLIT_Y:
            label(ax, u - 4.5, v + 4.5, mark_id, size=8, weight='bold',
                  ha='right', va='bottom')
        else:
            label(ax, u + 4.5, v + 4.5, mark_id, size=8, weight='bold',
                  ha='left', va='bottom')
    for (x, y), ids in arm.items():
        if not on_cell(sheet, x, y):
            continue
        u, v = sheet.to_paper(x, y)
        ax.add_patch(patches.Rectangle((u - 3, v - 3), 6, 6, fill=False,
                                       ec='black', lw=1.1, zorder=6))
        ax.plot([u - 5, u + 5], [v, v], color='black', lw=0.6, zorder=6)
        ax.plot([u, u], [v - 5, v + 5], color='black', lw=0.6, zorder=6)
        if sheet.cut_left and y == sheet.y_range[1]:
            continue
        label(ax, u - 4, v - 4, ' '.join(ids), size=6.5, ha='right',
              va='top', style='italic')


def draw_registration(ax, sheet: Sheet) -> None:
    """Draw alignment targets centred on the two joint lines."""
    points = []
    if sheet.y_range[0] <= SPLIT_Y <= sheet.y_range[1] or sheet.band_right:
        points += [(x, SPLIT_Y) for x in REG_ON_COL_JOINT
                   if sheet.x_range[0] <= x <= sheet.x_range[1]]
    if sheet.band_top or sheet.cut_bottom:
        points += [(SPLIT_X, y) for y in REG_ON_ROW_JOINT
                   if sheet.y_range[0] <= y <= sheet.y_range[1]]
    for x, y in points:
        u, v = sheet.to_paper(x, y)
        ax.add_patch(patches.Circle((u, v), 3.0, fill=False, ec='black',
                                    lw=0.9, zorder=7))
        ax.add_patch(patches.Circle((u, v), 0.6, fc='black', zorder=7))
        ax.plot([u - 5, u + 5], [v, v], color='black', lw=0.5, zorder=7)
        ax.plot([u, u], [v - 5, v + 5], color='black', lw=0.5, zorder=7)


def draw_band_and_cuts(ax, sheet: Sheet) -> None:
    """Shade overlap bands and mark cut lines."""
    if sheet.band_top:
        (u0, v0) = sheet.to_paper(sheet.x_range[1], sheet.drawn_y[1])
        width = sheet.drawn_y[1] - sheet.drawn_y[0]
        ax.add_patch(patches.Rectangle((u0, v0), width, BAND, fc='0.8',
                                       alpha=0.45, ec='none', zorder=1))
        label(ax, u0 + width / 2, v0 + BAND / 2,
              f'겹침 영역 — {sheet.number + 2}번 장이 덮습니다', size=6,
              ha='center', va='center')
    if sheet.band_right:
        u0, v0 = sheet.to_paper(sheet.drawn_x[0], SPLIT_Y)
        height = sheet.drawn_x[1] - sheet.drawn_x[0]
        ax.add_patch(patches.Rectangle((u0, v0), BAND, height, fc='0.8',
                                       alpha=0.45, ec='none', zorder=1))
        # Placed between marks: no target sits near x = 40 or x = 325.
        x_text = 40.0 if sheet.x_range[0] < 0 else 325.0
        _, v_text = sheet.to_paper(x_text, SPLIT_Y)
        label(ax, u0 + BAND / 2 + 1.5, v_text,
              f'겹침: {sheet.number + 1}번이 덮음', size=5.5,
              ha='center', va='center', rotation=90)
    if sheet.cut_bottom:
        (ua, va) = sheet.to_paper(SPLIT_X, sheet.y_range[1])
        (ub, _) = sheet.to_paper(SPLIT_X, sheet.y_range[0])
        ax.plot([ua - 6, ub + 6], [va, va], color='black', lw=0.7,
                ls=(0, (3, 2)), zorder=8)
        ax.text((ua + ub) / 2, va - 6.5,
                '- - 자르는 선: 이 선(x = 160)을 따라 자르고 아래 장의 회색 겹침 영역에 '
                '덮으세요', fontsize=6.5, ha='center', va='top')
    if sheet.cut_left:
        ua, va = sheet.to_paper(sheet.x_range[0], SPLIT_Y)
        _, vb = sheet.to_paper(sheet.x_range[1], SPLIT_Y)
        ax.plot([ua, ua], [va, vb + 6], color='black', lw=0.7,
                ls=(0, (3, 2)), zorder=8)
        # Short note between marks, >= 6 mm from the paper edge.
        x_note = 40.0 if sheet.x_range[0] < 0 else 322.0
        _, v_note = sheet.to_paper(x_note, SPLIT_Y)
        ax.text(ua - 1.5, v_note, '자르는 선 (y = 0)', fontsize=5.5,
                ha='right', va='center', rotation=90)


def draw_header(ax, sheet: Sheet) -> None:
    """Title, layout map, direction arrows, scale bar and instructions."""
    top = BOTTOM - 14.0 if sheet.cut_bottom else BOTTOM - 4.0
    ax.text(LEFT, top, f'EATED 측정 격자 — {sheet.number}/4 장 ({sheet.name})',
            fontsize=10, weight='bold', va='top')
    ax.text(LEFT, top - 6.5,
            'base_link 기준, 단위 mm.  가는 선 10 mm · 굵은 선 50 mm · '
            '○ 비전 표식(V) · □ 팔 목표(H/L) · ⊕ 정렬 표시',
            fontsize=6.5, va='top')

    # 100 mm scale bar.
    v_bar = 22.0
    ax.plot([LEFT, LEFT + 100], [v_bar, v_bar], color='black', lw=1.6,
            solid_capstyle='butt')
    for tick in range(0, 101, 10):
        height = 4.0 if tick % 50 == 0 else 2.2
        ax.plot([LEFT + tick] * 2, [v_bar, v_bar + height], color='black',
                lw=0.6)
    ax.text(LEFT + 50, v_bar - 2.0,
            '배율 확인: 이 선이 정확히 100 mm (±1 mm)여야 합니다',
            fontsize=6.5, ha='center', va='top')
    ax.text(LEFT, 12.0,
            '인쇄: "실제 크기 / 100 %", "페이지에 맞춤" 끔. 세로 배율도 굵은 선 '
            '두 칸(100 mm)을 자로 확인.',
            fontsize=6, va='top')
    ax.text(LEFT, 7.5,
            '붙이기: 1·2를 나란히(2가 1의 오른쪽 겹침을 덮음) → 3·4도 같이 → '
            '위 줄을 아래 줄에 덮음. 격자선과 ⊕를 맞춰 테이프.',
            fontsize=6, va='top')

    # Layout map with robot-forward arrow.
    map_u, map_v, cell = 150.0, 26.0, 11.0
    for other in SHEETS:
        col = 0 if other.y_range[1] > 0 else 1
        row = 0 if other.x_range[0] < SPLIT_X else 1
        rect = patches.Rectangle(
            (map_u + col * cell, map_v + row * cell), cell, cell,
            fc='0.25' if other.number == sheet.number else 'white',
            ec='black', lw=0.7)
        ax.add_patch(rect)
        ax.text(map_u + col * cell + cell / 2, map_v + row * cell + cell / 2,
                str(other.number), fontsize=7, ha='center', va='center',
                color='white' if other.number == sheet.number else 'black')
    ax.text(map_u + cell, map_v - 1.5, '로봇 (뒤쪽)', fontsize=6,
            ha='center', va='top')
    ax.annotate('', xy=(map_u + 2 * cell + 9, map_v + 2 * cell),
                xytext=(map_u + 2 * cell + 9, map_v),
                arrowprops=dict(arrowstyle='->', lw=1.0))
    ax.text(map_u + 2 * cell + 11, map_v + cell, '+x\n정면',
            fontsize=6, va='center')
    ax.annotate('', xy=(map_u - 9, map_v + cell),
                xytext=(map_u - 1, map_v + cell),
                arrowprops=dict(arrowstyle='->', lw=1.0))
    ax.text(map_u - 9, map_v + cell + 2, '+y 왼쪽', fontsize=6,
            ha='left', va='bottom')


def draw_sheet(sheet: Sheet, vision, arm, family: str):
    """Return a matplotlib figure for one sheet (1 data unit = 1 mm)."""
    fig = plt.figure(figsize=(PAGE_W / 25.4, PAGE_H / 25.4))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, PAGE_W)
    ax.set_ylim(0, PAGE_H)
    ax.set_aspect('equal')
    ax.axis('off')
    matplotlib.rcParams['font.family'] = family
    draw_band_and_cuts(ax, sheet)
    draw_grid(ax, sheet)
    draw_labels(ax, sheet)
    draw_axes_notes(ax, sheet)
    draw_robot(ax, sheet)
    draw_registration(ax, sheet)
    draw_points(ax, sheet, vision, arm)
    draw_header(ax, sheet)
    u, v = sheet.to_paper(sheet.x_range[0] + 25, sheet.y_range[1] - 5)
    ax.text(u, v, str(sheet.number), fontsize=26, color='0.7',
            ha='left', va='center', weight='bold', zorder=2)
    return fig


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument('--out', default=DEFAULT_OUT)
    parser.add_argument('--png-preview', default='',
                        help='also write <prefix>_<n>.png previews')
    args = parser.parse_args()

    family = setup_font()
    vision, arm = load_points()
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with PdfPages(args.out) as pdf:
        for sheet in SHEETS:
            fig = draw_sheet(sheet, vision, arm, family)
            pdf.savefig(fig)
            if args.png_preview:
                fig.savefig(f'{args.png_preview}_{sheet.number}.png', dpi=110)
            plt.close(fig)
    print(f'Wrote {args.out} ({len(SHEETS)} A4 pages)')


if __name__ == '__main__':
    main()
