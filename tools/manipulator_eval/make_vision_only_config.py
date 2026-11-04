#!/usr/bin/env python3
"""
Write measurement-only parameter copies (no ROS).

1. control.yaml with ``pick_enabled: false``. With this copy, an A press
   or fetch command runs only RECOGNITION -> /find_object -> HOME. The arm
   still moves between the fixed poses, but it never reaches for the
   object.
2. With ``--vision-confidence``: vision2.yaml with a lower YOLO
   ``confidence_threshold``. MEASUREMENT ONLY - results taken with it must
   be labelled with the threshold; never use it for normal operation.

    python3 tools/manipulator_eval/make_vision_only_config.py
    python3 tools/manipulator_eval/make_vision_only_config.py \
        --vision-confidence 0.3
"""

import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, '..', '..', 'src')
GENERATED = os.path.join(HERE, 'generated')

CONTROL_SOURCE = os.path.join(SRC, 'control_pkg', 'config', 'control.yaml')
CONTROL_TARGET = os.path.join(GENERATED, 'control_vision_only.yaml')
PICK_PATTERN = re.compile(r'^(\s*pick_enabled:\s*)true\b', re.MULTILINE)

VISION_SOURCE = os.path.join(SRC, 'vision_pkg', 'config', 'vision2.yaml')
CONF_PATTERN = re.compile(
    r'^(\s*confidence_threshold:\s*)([0-9.]+)\s*$', re.MULTILINE)


def convert(source: str, pattern, replacement: str, header: str) -> str:
    """Return the converted text, or raise if the key is not unique."""
    with open(source, encoding='utf-8') as handle:
        text = handle.read()
    converted, count = pattern.subn(replacement, text)
    if count != 1:
        raise ValueError(
            f'expected exactly one match in {os.path.abspath(source)}')
    return f'# GENERATED: {header}. Do not commit.\n' + converted


def write(path: str, text: str) -> None:
    """Write a generated file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(text)
    print(f'Wrote {path}')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument('--vision-confidence', type=float, default=None,
                        help='also write vision2.yaml with this threshold')
    args = parser.parse_args()

    try:
        write(CONTROL_TARGET, convert(
            CONTROL_SOURCE, PICK_PATTERN, r'\1false',
            'vision baseline, pick_enabled false'))
    except ValueError as exc:
        print(f'pick_enabled: true not found: {exc}; nothing written.')
        return 1
    print('Pane 7 (control) for vision-only trials:')
    print('  ros2 launch control_pkg control.launch.py '
          f'config_file:={CONTROL_TARGET}')

    if args.vision_confidence is not None:
        value = args.vision_confidence
        if not 0.05 <= value < 0.5:
            print('--vision-confidence must be in [0.05, 0.5)')
            return 1
        target = os.path.join(
            GENERATED, f'vision2_conf{int(round(value * 100)):02d}.yaml')
        try:
            write(target, convert(
                VISION_SOURCE, CONF_PATTERN, rf'\g<1>{value:.2f}',
                f'MEASUREMENT ONLY, confidence_threshold {value:.2f}'))
        except ValueError as exc:
            print(f'confidence_threshold not found: {exc}')
            return 1
        print('Pane 4 (vision) for this measurement only:')
        print('  ros2 launch vision_pkg vision2.launch.py '
              'model_path:=/home/fcsl/2026project/best.pt '
              f'config_file:={target}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
