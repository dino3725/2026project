"""Create an exact-size A4 PDF for the pinned ChArUco board definition."""

import argparse
import json
from pathlib import Path

import cv2
import matplotlib
from matplotlib import pyplot as plt
from matplotlib.patches import Rectangle

from handeye import BOARD_SPEC, board

matplotlib.use('Agg')


def main():
    """Draw vector squares so printing does not depend on raster DPI settings."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    target = board()
    raster = target.draw((1750, 1250), marginSize=0, borderBits=1)
    # Every marker cell and square boundary falls on a 1 mm grid: 18/6 = 3 mm.
    fig = plt.figure(figsize=(210 / 25.4, 297 / 25.4))
    ax = fig.add_axes([0, 0, 1, 1], xlim=(0, 210), ylim=(297, 0))
    ax.axis('off')
    ax.text(17.5, 28, 'EATED camera mount calibration', fontsize=14)
    ax.text(17.5, 39, 'A4 | Print at 100% / Actual size | NO fit to page', fontsize=10)
    # Use integer pixel runs from the authoritative OpenCV board raster.
    # Physical scale is exactly 0.1 mm per pixel, without interpolation.
    for row in range(0, raster.shape[0]):
        if row and (raster[row] == raster[row - 1]).all():
            continue
        end = row + 1
        while end < len(raster) and (raster[end] == raster[row]).all():
            end += 1
        black = raster[row] == 0
        start = None
        for col in range(len(black) + 1):
            on = col < len(black) and black[col]
            if on and start is None:
                start = col
            elif not on and start is not None:
                ax.add_patch(Rectangle((17.5 + start / 10, 60 + row / 10),
                                       (col - start) / 10, (end - row) / 10,
                                       color='black', linewidth=0))
                start = None
    ax.text(17.5, 201, '7 x 5 squares | square: 25 mm | marker: 18 mm', fontsize=10)
    ax.text(17.5, 211, 'Pattern size: 175 x 125 mm | DICT_4X4_50 | OpenCV 4.5.4', fontsize=9)
    ax.plot([30, 130], [230, 230], color='black', linewidth=0.6)
    for x in [30, 130]:
        ax.plot([x, x], [227, 233], color='black', linewidth=0.6)
    ax.text(30, 242, 'This line must measure 100 mm after printing.', fontsize=10)
    ax.text(17.5, 261,
            'Attach flat to a rigid board. Avoid glossy tape over the pattern.', fontsize=9)
    ax.text(17.5, 272,
            'Keep the board and robot base fixed throughout hand-eye capture.', fontsize=9)
    fig.savefig(args.output / 'EATED_ChArUco_A4_25mm.pdf')
    plt.close(fig)
    cv2.imwrite(str(args.output / 'board.png'), raster)
    (args.output / 'board.json').write_text(json.dumps(BOARD_SPEC, indent=2) + '\n')


if __name__ == '__main__':
    main()
