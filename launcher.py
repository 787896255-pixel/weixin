"""便携版启动入口：运行时和依赖随包提供。"""
from pathlib import Path
import os
import sys
import traceback

try:
    from app import main
    main()
except Exception:
    folder=Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'WeChatBrain'
    folder.mkdir(parents=True,exist_ok=True)
    log=folder/'startup-error.log'
    log.write_text(traceback.format_exc(),encoding='utf-8')
    if os.name=='nt':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None,'软件启动失败，错误信息已保存到：\n'+str(log),'京客导出助手',0x10)
    else:raise
