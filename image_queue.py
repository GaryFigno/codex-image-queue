from pathlib import Path
import runpy
runpy.run_path(str(Path(__file__).parent/'skills/codex-image-queue/scripts/image_queue.py'),run_name='__main__')
