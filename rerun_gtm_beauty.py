import make_shorts as ms
import make_folder_videos as mfv
# 加重美颜:磨皮更强 + 略多提亮
ms.CONFIG["beauty_filter"] = "bilateral=sigmaS=6:sigmaR=0.14,eq=brightness=0.035:saturation=1.07"
mfv.OUTPUT_DIR.mkdir(exist_ok=True)
mfv.WORK_DIR.mkdir(exist_ok=True)
mfv.process_folder("GTM")
