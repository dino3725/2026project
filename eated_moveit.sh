#!/usr/bin/env bash
# Keep MoveIt and hardware on the same reviewed arm mounting model.
set -eo pipefail
source /opt/ros/humble/setup.bash
source /home/fcsl/2026project/install/setup.bash
set -u
readonly EATED_MODEL_DIR=/home/fcsl/2026project/log/measured_model_20261111
python3 /home/fcsl/2026project/tools/vision_eval/ensure_calibrated_model.py \
    --directory "$EATED_MODEL_DIR"
exec ros2 launch "$EATED_MODEL_DIR/move_group.launch.py" use_sim:=false "$@"
