#!/usr/bin/env bash
# Restore ROS library paths after sg switches to the serial-device group.
set -eo pipefail

source /opt/ros/humble/setup.bash
source /home/fcsl/2026project/install/setup.bash
set -u
export LDS_MODEL=LDS-02

readonly EATED_MODEL_DIR=/home/fcsl/2026project/log/measured_model_20261111
python3 /home/fcsl/2026project/tools/vision_eval/ensure_calibrated_model.py \
    --directory "$EATED_MODEL_DIR"

exec ros2 launch "$EATED_MODEL_DIR/hardware.launch.py" \
    use_fake_hardware:=false start_rviz:=false "$@"
