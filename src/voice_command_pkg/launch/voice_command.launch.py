"""Launch the laptop voice command node."""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description() -> LaunchDescription:
    """Build the voice command launch description."""
    default_config = os.path.join(
        get_package_share_directory('voice_command_pkg'),
        'config',
        'voice_command.yaml',
    )
    config_argument = DeclareLaunchArgument(
        'config_file',
        default_value=default_config,
        description='Voice command node parameter YAML file',
    )
    voice_node = Node(
        package='voice_command_pkg',
        executable='voice_command_node',
        name='voice_command_node',
        output='screen',
        emulate_tty=True,
        parameters=[LaunchConfiguration('config_file')],
    )
    return LaunchDescription([config_argument, voice_node])
