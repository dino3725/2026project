"""Prepare Nav2 without activation or a connection to the wheel command topic."""

from pathlib import Path

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, GroupAction
from launch.actions import IncludeLaunchDescription, OpaqueFunction
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import SetRemap
from launch_ros.substitutions import FindPackageShare


def prepare_nav2(context):
    """Require a real map and keep both velocity paths separate from hardware."""
    map_file = Path(LaunchConfiguration('map').perform(context)).expanduser()
    if not map_file.is_file() or map_file.suffix not in ('.yaml', '.yml'):
        raise RuntimeError('Provide an existing map YAML with map:=/absolute/path.yaml')
    source = PathJoinSubstitution([
        FindPackageShare('nav2_bringup'), 'launch', 'bringup_launch.py',
    ])
    params = PathJoinSubstitution([
        FindPackageShare('turtlebot3_manipulation_navigation2'),
        'param', 'turtlebot3.yaml',
    ])
    return [GroupAction([
        # Group remaps precede upstream node remaps in ROS Humble launch_ros.
        SetRemap(src='cmd_vel', dst='/navigation/cmd_vel_raw'),
        SetRemap(src='cmd_vel_smoothed', dst='/navigation/cmd_vel'),
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(source),
            launch_arguments={
                'map': str(map_file.resolve()), 'params_file': params,
                'use_sim_time': 'false', 'slam': 'false',
                'autostart': 'false', 'use_composition': 'False',
                'use_namespace': 'false', 'namespace': '',
                'use_respawn': 'false',
            }.items()),
    ])]


def generate_launch_description():
    """Expose only the map; activation and wheel routing require later integration."""
    return LaunchDescription([
        DeclareLaunchArgument('map', description='Existing saved map YAML'),
        OpaqueFunction(function=prepare_nav2),
    ])
