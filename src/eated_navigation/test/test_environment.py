"""Verify dependency reports and launch isolation without executing nodes."""

import importlib.util
import json
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock

from launch import LaunchContext
from launch.actions import IncludeLaunchDescription
from launch_ros.actions import Node, SetRemap
from PIL import Image
import pytest
import yaml


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


def test_keepout_uses_both_costmaps_and_preserves_robot_settings(tmp_path):
    """Keep the original robot profile and load the separate, aligned filter map."""
    module = load('launch/nav2_standby.launch.py')
    profile = tmp_path / 'nav.yaml'
    layers = {'filters': ['other'], 'plugins': ['inflation_layer'], 'inflation_layer': {
        'plugin': 'nav2_costmap_2d::InflationLayer', 'inflation_radius': 0.55}}
    areas = ('global_costmap', 'local_costmap')
    profile.write_text(yaml.safe_dump({a: {a: {'ros__parameters': layers}} for a in areas}))
    for stem in ('lab', 'lab_keepout'):
        Image.new('L', (8, 8)).save(tmp_path / (stem + '.pgm'))
        (tmp_path / (stem + '.yaml')).write_text(yaml.safe_dump({
            'image': stem + '.pgm', 'origin': [-1, -2, 0], 'resolution': 0.05}))
    physical = tmp_path / 'lab.yaml'
    mask = tmp_path / 'lab_keepout.yaml'
    context = LaunchContext()
    context.launch_configurations.update(map=str(physical), params_file=str(profile))
    group = module.prepare_nav2(context)[0]
    actions = group.get_sub_entities()
    assert sum(isinstance(action, Node) for action in actions) == 3
    include = next(a for a in actions if isinstance(a, IncludeLaunchDescription))
    args = dict(include.launch_arguments)
    assert args['map'] == str(physical)
    combined = yaml.safe_load(Path(args['params_file']).read_text())
    for area in areas:
        params = combined[area][area]['ros__parameters']
        assert params['plugins'] == layers['plugins']
        assert params['filters'] == ['other', 'keepout', 'keepout_inflation']
        assert params['keepout_inflation'] == layers['inflation_layer']
    Image.new('L', (7, 8)).save(mask.with_suffix('.pgm'))
    with pytest.raises(ValueError, match='dimensions'):
        module.prepare_nav2(context)


@pytest.mark.parametrize('publishers, subscribers, expected', [
    (['control_node'], ['diff_drive_controller'], 'exactly one'),
    (['twist_mux', 'other'], ['diff_drive_controller'], 'exactly one'),
    (['twist_mux'], [], 'disconnected'),
    (['twist_mux'], ['diff_drive_controller'], 'localization unavailable'),
])
def test_start_inspects_unremapped_wheel_endpoints(publishers, subscribers, expected):
    """Exercise the trigger without creating nodes or sending a goal."""
    trial = load('scripts/short_trial.py')
    node = Mock(spent=False, state=trial.TaskState.IDLE, _lock=RLock())
    node.fresh.return_value = True
    node.navigator.server_is_ready.return_value = True
    node.get_publishers_info_by_topic.return_value = [
        SimpleNamespace(node_name=name) for name in publishers]
    node.get_subscriptions_info_by_topic.return_value = [
        SimpleNamespace(node_name=name) for name in subscribers]
    node.tf_buffer.lookup_transform.side_effect = RuntimeError('localization unavailable')
    response = trial.Trial.start(node, None, trial.Trigger.Response())
    assert not response.success
    assert expected in response.message
    node.get_publishers_info_by_topic.assert_called_once_with('rt/cmd_vel', no_mangle=True)
    if publishers == ['twist_mux']:
        node.get_subscriptions_info_by_topic.assert_called_once_with('rt/cmd_vel', no_mangle=True)
    node._start_operation.assert_not_called()


def test_reviewed_route_validates_clearance_and_goal(tmp_path):
    """A corridor trial requires the verified clearance and a goal on the sampled path."""
    load_route = load('scripts/short_trial.py').read_reviewed_route
    file = tmp_path / 'corridor.json'
    route = dict(path=[[0., 0.], [.05, 0.]], goal=[.05, 0., 0.], clearance=.48)
    file.write_text(json.dumps(route))
    assert load_route(file)[2] == pytest.approx(.05)
    for change in ({'clearance': .2}, {'goal': [1., 0., 0.]}, {'path': [[0., 0.], [1., 0.]]}):
        file.write_text(json.dumps({**route, **change}))
        with pytest.raises(ValueError, match='exceeds trial limits'):
            load_route(file)
