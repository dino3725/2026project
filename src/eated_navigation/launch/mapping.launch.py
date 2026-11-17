"""Start mapping only; reuse the separately running calibrated hardware TF."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    """Use the ROBOTIS mapping configuration without launching hardware."""
    source = PathJoinSubstitution([
        FindPackageShare('turtlebot3_manipulation_cartographer'),
        'launch', 'cartographer.launch.py',
    ])
    return LaunchDescription([
        DeclareLaunchArgument('start_rviz', default_value='false'),
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(source),
            launch_arguments={
                'use_sim': 'false',
                'start_rviz': LaunchConfiguration('start_rviz'),
                'resolution': '0.05',
            }.items()),
    ])
