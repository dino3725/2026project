"""Prepare Nav2 without activation or a connection to the wheel command topic."""

import atexit
from pathlib import Path
from tempfile import NamedTemporaryFile

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, GroupAction
from launch.actions import IncludeLaunchDescription, OpaqueFunction
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node, SetRemap
from launch_ros.substitutions import FindPackageShare
from PIL import Image
import yaml


def keepout_configuration(map_file, mask_file, params_file):
    """Load the aligned binary mask and add filters to the selected robot profile."""
    geometry = yaml.safe_load(map_file.read_text())
    mask = yaml.safe_load(mask_file.read_text())
    if any(mask[key] != geometry[key] for key in ('resolution', 'origin')):
        raise ValueError('Keepout mask resolution and origin must match the map')
    with Image.open(map_file.parent / geometry['image']) as image, Image.open(
            mask_file.parent / mask['image']) as mask_image:
        if image.size != mask_image.size:
            raise ValueError('Keepout mask dimensions must match the map')

    params = yaml.safe_load(Path(params_file).read_text())
    for name in ('global_costmap', 'local_costmap'):
        layer = params[name][name]['ros__parameters']
        keepout_filters = ['keepout', 'keepout_inflation']
        earlier = [item for item in layer.get('filters', []) if item not in keepout_filters]
        layer['filters'] = earlier + keepout_filters
        layer['keepout'] = {
            'plugin': 'nav2_costmap_2d::KeepoutFilter',
            'filter_info_topic': '/keepout_filter_info',
        }
        # The regular inflation plugin runs before filters and cannot see the mask.
        layer['keepout_inflation'] = dict(layer['inflation_layer'])
    with NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as output:
        yaml.safe_dump(params, output)
    atexit.register(Path(output.name).unlink, missing_ok=True)
    nodes = [
        Node(package='nav2_map_server', executable='map_server', name='keepout_mask_server',
             parameters=[{'yaml_filename': str(mask_file.resolve()), 'topic_name': '/keepout_mask',
                          'frame_id': 'map'}], output='screen'),
        Node(package='nav2_map_server', executable='costmap_filter_info_server',
             name='keepout_filter_info_server', parameters=[{
                 'type': 0, 'filter_info_topic': '/keepout_filter_info',
                 'mask_topic': '/keepout_mask', 'base': 0.0, 'multiplier': 1.0}],
             output='screen'),
        Node(package='nav2_lifecycle_manager', executable='lifecycle_manager',
             name='lifecycle_manager_keepout', parameters=[{
                 'autostart': True, 'node_names': ['keepout_mask_server',
                                                   'keepout_filter_info_server']}],
             output='screen'),
    ]
    return output.name, nodes


def prepare_nav2(context):
    """Require a real map and keep both velocity paths separate from hardware."""
    map_file = Path(LaunchConfiguration('map').perform(context)).expanduser()
    if not map_file.is_file() or map_file.suffix not in ('.yaml', '.yml'):
        raise RuntimeError('Provide an existing map YAML with map:=/absolute/path.yaml')
    source = PathJoinSubstitution([
        FindPackageShare('nav2_bringup'), 'launch', 'bringup_launch.py',
    ])
    params = LaunchConfiguration('params_file', default=PathJoinSubstitution([
        FindPackageShare('turtlebot3_manipulation_navigation2'), 'param', 'turtlebot3.yaml',
    ]))
    mask_arg = LaunchConfiguration('keepout_mask', default='auto').perform(context)
    automatic = mask_arg == 'auto'
    mask = (map_file.with_name(map_file.stem + '_keepout.yaml') if automatic
            else Path(mask_arg).expanduser())
    servers = []
    if mask.is_file() or not automatic:
        params, servers = keepout_configuration(map_file, mask, params.perform(context))
    return [GroupAction([
        # Group remaps precede upstream node remaps in ROS Humble launch_ros.
        SetRemap(src='cmd_vel', dst='/navigation/cmd_vel_raw'),
        SetRemap(src='cmd_vel_smoothed', dst='/navigation/cmd_vel'),
        *servers,
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(source),
            launch_arguments={
                'map': str(map_file.resolve()), 'params_file': params,
                'use_sim_time': 'false', 'slam': 'False',
                'autostart': 'false', 'use_composition': 'False',
                'use_namespace': 'false', 'namespace': '',
                'use_respawn': 'false',
            }.items()),
    ])]


def generate_launch_description():
    """Expose only the map; activation and wheel routing require later integration."""
    return LaunchDescription([
        DeclareLaunchArgument('map', description='Existing saved map YAML'),
        DeclareLaunchArgument('keepout_mask', default_value='auto',
                              description='Mask YAML or auto for <map stem>_keepout.yaml'),
        DeclareLaunchArgument('params_file', default_value=PathJoinSubstitution([
            FindPackageShare('turtlebot3_manipulation_navigation2'), 'param', 'turtlebot3.yaml',
        ])),
        OpaqueFunction(function=prepare_nav2),
    ])
