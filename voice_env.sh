#!/usr/bin/env bash
# Laptop environment for voice_command_pkg. Use with `source`, not `bash`:
#   source ~/2026project/voice_env.sh
#   ros2 launch voice_command_pkg voice_command.launch.py
#
# ROS console scripts run with /usr/bin/python3. Like eated_pane.sh on the
# Jetson, add the project .venv site-packages (faster-whisper, webrtcvad) to
# PYTHONPATH instead of installing ROS into the venv.

EATED_WORKSPACE="${EATED_WORKSPACE:-$HOME/2026project}"

source /opt/ros/humble/setup.bash
if [[ -f "$EATED_WORKSPACE/install/setup.bash" ]]; then
    source "$EATED_WORKSPACE/install/setup.bash"
else
    echo "[voice_env] $EATED_WORKSPACE/install/setup.bash 없음: 먼저 colcon build 하세요." >&2
fi
if [[ -d "$EATED_WORKSPACE/.venv/lib/python3.10/site-packages" ]]; then
    export PYTHONPATH="$EATED_WORKSPACE/.venv/lib/python3.10/site-packages${PYTHONPATH:+:$PYTHONPATH}"
fi
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-37}"
