import os
from glob import glob

from setuptools import find_packages, setup

package_name = 'voice_command_pkg'

setup(
    name=package_name,
    version='0.1.0',
    packages=find_packages(exclude=['test']),
    package_data={package_name: ['prompts/*.txt']},
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        (
            os.path.join('share', package_name, 'config'),
            glob(os.path.join('config', '*.yaml')),
        ),
        (
            os.path.join('share', package_name, 'launch'),
            glob(os.path.join('launch', '*.launch.py')),
        ),
    ],
    # faster-whisper and webrtcvad-wheels come from the project .venv
    # (see requirements.txt); they are not installed by colcon.
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='dino3725',
    maintainer_email='inufcsl@gmail.com',
    description='Laptop voice pipeline publishing EATED robot commands.',
    license='Apache-2.0',
    extras_require={
        'test': [
            'pytest',
        ],
    },
    entry_points={
        'console_scripts': [
            'voice_command_node = voice_command_pkg.voice_command_node:main',
            'vad_monitor = voice_command_pkg.audio_capture:main',
        ],
    },
)
