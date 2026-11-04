#!/usr/bin/env python3
"""
Write a copy of control.yaml with ``pick_enabled: false`` (no ROS).

With this copy, an A press or fetch command runs only
RECOGNITION -> /find_object -> HOME. The arm still moves between the fixed
poses, but it never reaches for the object.

    python3 tools/manipulator_eval/make_vision_only_config.py
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, '..', '..', 'src', 'control_pkg', 'config',
                      'control.yaml')
TARGET = os.path.join(HERE, 'generated', 'control_vision_only.yaml')
PATTERN = re.compile(r'^(\s*pick_enabled:\s*)true\b', re.MULTILINE)


def main() -> int:
    with open(SOURCE, encoding='utf-8') as handle:
        text = handle.read()
    converted, count = PATTERN.subn(r'\1false', text)
    if count != 1:
        print('pick_enabled: true was not found exactly once in '
              f'{os.path.abspath(SOURCE)}; nothing written.')
        return 1
    os.makedirs(os.path.dirname(TARGET), exist_ok=True)
    with open(TARGET, 'w', encoding='utf-8') as handle:
        handle.write('# GENERATED for the vision baseline. Do not commit.\n')
        handle.write(converted)
    print(f'Wrote {TARGET}')
    print('Pane 7 command for vision-only trials:')
    print(f'  ros2 launch control_pkg control.launch.py config_file:={TARGET}')
    print('Check that the startup log says "pick_enabled=false".')
    return 0


if __name__ == '__main__':
    sys.exit(main())
