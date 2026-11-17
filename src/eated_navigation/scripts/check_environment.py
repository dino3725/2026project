#!/usr/bin/env python3
"""Report installed navigation prerequisites without starting ROS nodes."""

import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

from ament_index_python.packages import get_packages_with_prefixes


REQUIRED = (
    'navigation2', 'nav2_bringup', 'nav2_controller', 'nav2_planner',
    'nav2_bt_navigator', 'nav2_amcl', 'nav2_map_server', 'nav2_lifecycle_manager',
    'nav2_velocity_smoother', 'cartographer_ros',
    'turtlebot3_manipulation_cartographer',
    'turtlebot3_manipulation_navigation2', 'twist_mux',
)


def inspect_packages(packages):
    """Return explicit versions and missing entries; never infer readiness."""
    result = {}
    for name in REQUIRED:
        prefix = packages.get(name)
        item = {'installed': bool(prefix)}
        if prefix:
            manifest = Path(prefix) / 'share' / name / 'package.xml'
            item['prefix'] = prefix
            item['version'] = ET.parse(manifest).getroot().findtext('version')
        result[name] = item
    return result


def main():
    """Exit nonzero for missing dependencies, without accessing hardware."""
    packages = inspect_packages(get_packages_with_prefixes())
    complete = all(row['installed'] for row in packages.values())
    print(json.dumps({
        'dependencies_installed': complete, 'packages': packages,
        'live_hardware_verified': False, 'wheel_routing_integrated': False,
    }, indent=2))
    return 0 if complete else 1


if __name__ == '__main__':
    sys.exit(main())
