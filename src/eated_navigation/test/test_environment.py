"""Verify dependency reports and launch isolation without executing nodes."""

import importlib.util
from pathlib import Path

from launch import LaunchContext
from launch.actions import IncludeLaunchDescription
from launch_ros.actions import Node, SetRemap
import pytest


ROOT = Path(__file__).resolve().parents[1]


def load(relative):
    """Import a source file without starting its command entry point."""
    spec = importlib.util.spec_from_file_location('under_test', ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_packages_are_not_reported_ready():
    """An empty ROS package index must report every dependency as missing."""
    module = load('scripts/check_environment.py')
    result = module.inspect_packages({})
    assert len(result) == len(module.REQUIRED)
    assert all(not row['installed'] for row in result.values())


def test_installed_version_is_read_from_manifest(tmp_path):
    """Installed versions come from the selected ROS prefix, not apt guesses."""
    module = load('scripts/check_environment.py')
    directory = tmp_path / 'share' / 'nav2_bringup'
    directory.mkdir(parents=True)
    (directory / 'package.xml').write_text('<package><version>1.1.20</version></package>')
    result = module.inspect_packages({'nav2_bringup': str(tmp_path)})
    assert result['nav2_bringup']['version'] == '1.1.20'
    assert not result['twist_mux']['installed']


def test_nav2_requires_existing_map(tmp_path):
    """Never silently load a vendor simulation map in the real workspace."""
    module = load('launch/nav2_standby.launch.py')
    context = LaunchContext()
    context.launch_configurations['map'] = str(tmp_path / 'absent.yaml')
    with pytest.raises(RuntimeError, match='existing map'):
        module.prepare_nav2(context)


def test_nav2_is_inactive_and_velocity_remaps_precede_upstream(tmp_path):
    """Use actual launch_ros expansion; no action or node is executed."""
    module = load('launch/nav2_standby.launch.py')
    map_file = tmp_path / 'map.yaml'
    map_file.write_text('image: map.pgm\n')
    context = LaunchContext()
    context.launch_configurations['map'] = str(map_file)
    group = module.prepare_nav2(context)[0]
    actions = group.get_sub_entities()
    include = next(a for a in actions if isinstance(a, IncludeLaunchDescription))
    args = dict(include.launch_arguments)
    assert args['autostart'] == 'false'
    assert args['use_composition'] == 'False'
    for action in actions:
        if isinstance(action, SetRemap):
            action.execute(context)
    node = Node(package='nav2_velocity_smoother', executable='velocity_smoother',
                remappings=[('cmd_vel', 'cmd_vel_nav'),
                            ('cmd_vel_smoothed', 'cmd_vel')])
    node._perform_substitutions(context)
    assert node.expanded_remapping_rules[:2] == [
        ('cmd_vel', '/navigation/cmd_vel_raw'),
        ('cmd_vel_smoothed', '/navigation/cmd_vel'),
    ]
