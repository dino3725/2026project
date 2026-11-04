#!/usr/bin/env python3
"""
Turn baseline CSV files into Markdown tables (no ROS needed).

    python3 tools/manipulator_eval/summarize_results.py \
        --vision results/raw/vision_2026-11-07.csv \
        --arm results/raw/arm_2026-11-07.csv

Paste the output into the results Markdown file.
"""

import argparse
import csv
from collections import defaultdict
from typing import Dict, List, Sequence

from baseline_math import axis_stats, PointError, summarize_errors


def read_rows(paths: Sequence[str]) -> List[Dict[str, str]]:
    """Read and concatenate CSV files."""
    rows = []
    for path in paths:
        with open(path, newline='', encoding='utf-8') as handle:
            rows.extend(csv.DictReader(handle))
    return rows


def errors_from(rows, prefix: str) -> List[PointError]:
    """Collect PointError values from ``<prefix>_x_mm`` style columns."""
    errors = []
    for row in rows:
        values = [row.get(f'{prefix}_{axis}_mm', '') for axis in 'xyz']
        if all(values):
            errors.append(PointError(*(float(v) for v in values)))
    return errors


def stats_table(groups: Dict[str, List[PointError]], title: str) -> str:
    """Render bias/spread per axis plus horizontal error per group."""
    lines = [
        f'#### {title}',
        '',
        '| 구분 | n | x 평균±표준편차 | y 평균±표준편차 | z 평균±표준편차 '
        '| 수평 RMS | 수평 최대 |',
        '|---|---|---|---|---|---|---|',
    ]
    for name, errors in groups.items():
        if not errors:
            continue
        s = summarize_errors(errors)
        horizontal_rms = axis_stats(
            [e.horizontal for e in errors]).rms
        lines.append(
            f'| {name} | {s["x"].n} '
            f'| {s["x"].mean:+.1f} ± {s["x"].std:.1f} '
            f'| {s["y"].mean:+.1f} ± {s["y"].std:.1f} '
            f'| {s["z"].mean:+.1f} ± {s["z"].std:.1f} '
            f'| {horizontal_rms:.1f} | {s["xy"].max_abs:.1f} |'
        )
    lines.append('')
    lines.append('단위 mm. 평균 = 계통 오차(보정 가능), '
                 '표준편차 = 우연 오차(보정으로 안 줄어듦).')
    return '\n'.join(lines)


def vision_section(rows) -> str:
    """Summarize vision trials per object and per mark."""
    by_object = defaultdict(list)
    by_mark = defaultdict(list)
    counts = defaultdict(lambda: [0, 0])
    for row in rows:
        counts[row['object']][1] += 1
        if row['result'] != 'ok':
            continue
        counts[row['object']][0] += 1
        error = errors_from([row], 'err')
        if error:
            by_object[row['object']].extend(error)
            by_mark[row['mark']].extend(error)
    all_errors = [e for errors in by_object.values() for e in errors]
    lines = ['### 비전 좌표 vs 실제 위치', '',
             '| 물체 | 인식 성공 / 시도 |', '|---|---|']
    for name, (ok, total) in sorted(counts.items()):
        lines.append(f'| {name} | {ok} / {total} |')
    lines.append('')
    groups = dict(sorted(by_object.items()))
    groups['전체'] = all_errors
    lines.append(stats_table(groups, '물체별 (비전 − 실제)'))
    lines.append('')
    lines.append(stats_table(dict(sorted(by_mark.items())),
                             '표식(위치)별 (비전 − 실제)'))
    return '\n'.join(lines)


def arm_section(rows) -> str:
    """Summarize arm-probe trials."""
    total = len(rows)
    reached = [row for row in rows if row['result'] == 'reached']
    plan_failed = sum(1 for row in rows if row['result'] == 'plan_failed')
    lines = ['### 팔 목표 vs 실제 도착 위치', '',
             f'- 시도 {total}, 도착 {len(reached)}, 계획 실패 {plan_failed}',
             '']
    groups = {
        'FK − 목표 (제어·허용 오차)': errors_from(reached, 'err_fk'),
        '실측 − 목표 (전체)': errors_from(reached, 'err_phys'),
    }
    mech = []
    for row in reached:
        values = [row.get(f'mech_{axis}_mm', '') for axis in 'xyz']
        if all(values):
            mech.append(PointError(*(float(v) for v in values)))
    groups['실측 − FK (기구·모델 오차)'] = mech
    lines.append(stats_table(groups, '오차 종류별'))
    lines.append('')
    lines.append(stats_table(
        {row['target_id']: errors_from([row], 'err_phys')
         for row in reached},
        '목표점별 (실측 − 목표)'))
    return '\n'.join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument('--vision', nargs='*', default=[])
    parser.add_argument('--arm', nargs='*', default=[])
    args = parser.parse_args()
    if not args.vision and not args.arm:
        parser.error('give --vision and/or --arm CSV files')
    sections = []
    if args.vision:
        sections.append(vision_section(read_rows(args.vision)))
    if args.arm:
        sections.append(arm_section(read_rows(args.arm)))
    print('\n\n'.join(sections))


if __name__ == '__main__':
    main()
