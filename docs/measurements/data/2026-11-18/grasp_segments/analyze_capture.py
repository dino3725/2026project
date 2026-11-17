import json,sys
from pathlib import Path
import cv2,numpy as np
from ultralytics import YOLO
sys.path.insert(0,'tools/vision_eval')
from analyze_grasp_observations import analyze
p=Path('log/grasp_segments_20261118'); root=p/'capture'
cap=json.loads((root/'capture.json').read_text()); model=YOLO('best.pt');rows=[]
for f in cap['frames']:
 i=f['index'];rgb=cv2.imread(str(root/f'{i:03d}_rgb.png'));depth=cv2.imread(str(root/f'{i:03d}_depth.png'),-1)
 pred=model.predict(rgb,conf=.5,verbose=False)[0]; boxes=[]
 for b in pred.boxes:
  name=model.names[int(b.cls[0].item())]
  if name!='coke':continue
  v=b.xyxy[0].cpu().tolist();boxes.append(dict(class_name=name,confidence=float(b.conf[0].item()),**dict(zip(['x1','y1','x2','y2'],v))))
 detection=max(boxes,key=lambda b:b['confidence']) if boxes else None
 k=f['intrinsics']['k'];tf=f['transforms_to_base_link_at_depth_stamp'][f['optical_frame']]
 meta=dict(detection=detection,intrinsics_fx_fy_cx_cy=[k[0],k[4],k[2],k[5]],depth_encoding='16UC1',depth_scale=.001,camera_to_base=dict(translation=tf['translation'],quaternion_xyzw=tf['quaternion']),rgb_stamp_ns=f['rgb_stamp_ns'],depth_stamp_ns=f['depth_stamp_ns'])
 row=analyze(meta,depth,.070,.134,.0265);row['capture_index']=i;rows.append(row)
 print(i, row['axis'],flush=True)
(p/'geometry.json').write_text(json.dumps(dict(frames=rows,source=str(root/'capture.json')),indent=2)+'\n')
