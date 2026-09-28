"""Native Windows service wrapper (pywin32). Install: scripts\\install_service.ps1"""
import os
import sys
import threading

import servicemanager  # type: ignore
import win32event  # type: ignore
import win32service  # type: ignore
import win32serviceutil  # type: ignore


class DiacritizerService(win32serviceutil.ServiceFramework):
    _svc_name_ = "ArabicDiacritizer"
    _svc_display_name_ = "Arabic Diacritization API"
    _svc_description_ = "LLM-backed Arabic diacritization HTTP API (FastAPI/uvicorn)."

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.server = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        if self.server:
            self.server.should_exit = True
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        import uvicorn

        os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        cfg = uvicorn.Config("diacritizer.api:app", host=os.environ.get("DIAC_HOST", "0.0.0.0"),
                             port=int(os.environ.get("DIAC_PORT", "8080")), log_config=None, workers=1)
        self.server = uvicorn.Server(cfg)
        servicemanager.LogInfoMsg("ArabicDiacritizer starting")
        t = threading.Thread(target=self.server.run, daemon=True)
        t.start()
        win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)
        t.join(timeout=15)


if __name__ == "__main__":
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(DiacritizerService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(DiacritizerService)
