"""Review fixed image regions after the one-pose live test; no fitting of calibration."""
from pathlib import Path
import json,sys,hashlib
import cv2,numpy as np
from scipy.spatial.transform import Rotation
sys.path.insert(0,str(Path.cwd()/'tools/vision_eval'))
from rgbd_refinement import fit_plane
root=Path(__file__).parent
capture=json.loads((root/'geometry_ready/capture.json').read_text())
regions={'table_far':[490,90,630,200],'table_left':[250,470,305,600],
         'lid_left':[460,340,570,440],'lid_front':[470,600,950,640],
         'lid_right':[990,400,1040,500]}
rows=[]
for frame in capture['frames']:
 depth=cv2.imread(str(root/'geometry_ready'/f"{frame['index']:03d}_depth.png"),-1)
 k=np.array(frame['intrinsics']['k']).reshape(3,3)
 tf=frame['transforms_to_base_link_at_depth_stamp'][frame['optical_frame']]
 rotation=Rotation.from_quat(tf['quaternion']).as_matrix();translation=np.array(tf['translation'])
 points={};coverage={}
 for name,(x0,y0,x1,y1) in regions.items():
  v,u=np.mgrid[y0:y1:3,x0:x1:3];z=depth[v,u].ravel()*.001
  keep=(z>.2)&(z<1.2)
  coverage[name]=float(keep.mean())
  if keep.mean()<.8:continue
  points[name]=np.c_[(u.ravel()-k[0,2])*z/k[0,0],(v.ravel()-k[1,2])*z/k[1,1],z][keep]
 table=np.concatenate([points[n] for n in points if n.startswith('table')]);lid=np.concatenate([points[n] for n in points if n.startswith('lid')])
 row={'index':frame['index'],'valid_depth_fraction':coverage,'regions':{},
      'relative_lid_height_median_mm':None,'table_plane_rms_mm':None,
      'table_normal_tilt_in_base_deg':None}
 try:
  plane=fit_plane(table);normal=plane['normal'];offset=plane['offset_m']
  if (rotation@normal)[2]<0:normal=-normal;offset=-offset
  row['relative_lid_height_median_mm']=float(np.median((lid@normal+offset)*1000))
  row['table_plane_rms_mm']=plane['inlier_rms_mm']
  row['table_normal_tilt_in_base_deg']=float(np.rad2deg(np.arccos(np.clip((rotation@normal)[2],-1,1))))
 except ValueError as exc:
  row['table_plane_error']=str(exc)
 for name,p in points.items():
  world=p@rotation.T+translation
  h=world[:,2]*1000+10
  row['regions'][name]={'support_height_median_mm':float(np.median(h)),
                        'support_height_p05_p95_mm':np.percentile(h,[5,95]).tolist()}
 rows.append(row)
summary={}
for name in regions:
 values=[r['regions'][name]['support_height_median_mm'] for r in rows if name in r['regions']]
 summary[name]={'valid_frames':len(values),'mean':float(np.mean(values)) if values else None,'min':float(min(values)) if values else None,'max':float(max(values)) if values else None}
relative=[r['relative_lid_height_median_mm'] for r in rows if r['relative_lid_height_median_mm'] is not None]
summary['relative_lid_height_mm']={'valid_frames':len(relative),'mean':float(np.mean(relative)) if relative else None,'min':float(min(relative)) if relative else None,'max':float(max(relative)) if relative else None}
result={'status':'one_pose_live_audit','regions_xyxy':regions,'frames':rows,'summary':summary,
'expected_approximate_heights_mm':{'table':0,'lid':80},'camera_calibration_refitted':False,
'notes':['Manually reviewed surface regions; common support plane confirmed by user.',
'Regions with less than 80% usable depth excluded and coverage recorded; masks not changed.',
'No new ruler measurement or independent coke footprint reference supplied.',
'Plane-relative lid height is invariant to a rigid camera transform; not proof of calibration.'],
'analysis_source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
(root/'geometry_summary.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(summary,indent=2))
