"""Training-only intrinsic sensitivity, without camera/FK configuration changes."""
import sys,json,cv2,numpy as np
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tools/vision_eval'))
from handeye import board
root=Path(__file__).parent
paths=sorted(root.glob('train_*/observation.json'))
records=[json.loads(p.read_text()) for p in paths]
target=board();obj=[target.chessboardCorners[np.array(r['corner_ids'])].astype(np.float32) for r in records];img=[np.array(r['corners_px'],dtype=np.float32).reshape(-1,1,2) for r in records]
k=np.array(records[0]['color_info']['k']).reshape(3,3);dist=np.array(records[0]['color_info']['d']);size=(records[0]['color_info']['width'],records[0]['color_info']['height'])
flags=cv2.CALIB_USE_INTRINSIC_GUESS|cv2.CALIB_FIX_PRINCIPAL_POINT|cv2.CALIB_ZERO_TANGENT_DIST|cv2.CALIB_FIX_K1|cv2.CALIB_FIX_K2|cv2.CALIB_FIX_K3
out={'status':'diagnostic_only_not_an_intrinsic_calibration','factory_k':k.tolist(),'factory_per_view_pnp_rms_px':float(np.sqrt(np.mean([r['reprojection_rms_px']**2 for r in records]))),'caveat':'Limited board-normal diversity and board image coverage; optimized focal values are not accepted calibration. No held-out data used.'}
for label,extra in [('fixed_aspect_single_focal',cv2.CALIB_FIX_ASPECT_RATIO),('two_focals_fixed_center',0)]:
 result=cv2.calibrateCameraExtended(obj,img,size,k.copy(),dist.copy(),flags=flags|extra,criteria=(cv2.TERM_CRITERIA_EPS+cv2.TERM_CRITERIA_COUNT,100,1e-9))
 rms,estimated,coefficients,rvecs,tvecs,std_intr,std_ext,view_errors=result
 out[label]={'rms_px':float(rms),'k':estimated.tolist(),'d':coefficients.tolist(),'internal_parameter_std':std_intr.reshape(-1).tolist(),'per_view_rms_px':view_errors.reshape(-1).tolist()}
print(json.dumps(out,indent=2))
with (root/'intrinsics_diagnostic.json').open('x') as f:json.dump(out,f,indent=2);f.write('\n')
