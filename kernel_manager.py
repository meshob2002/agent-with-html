"""
로컬 Jupyter 커널 세션 관리.

jupyter_client 로 커널 프로세스를 직접 띄우고, 실행 요청/결과 수집을 담당한다.
커널은 상태를 유지하므로 이전 호출에서 정의한 변수를 다음 호출에서 쓸 수 있다.
(jupyter_mcp.txt 의 JupyterKernelSession 과 동일한 인터페이스)
"""

import queue

from jupyter_client import KernelManager


# 커널이 새로 뜰 때마다 실행되는 초기화 코드.
# %matplotlib inline: plt.show() 시 그림을 display_data(PNG) 메시지로 내보내게 함
#                     (이게 없으면 Agg 백엔드라 "FigureCanvasAgg is non-interactive" 경고와 함께 그림 유실)
BOOTSTRAP_CODE = """
%matplotlib inline
try:
    import matplotlib
    matplotlib.rcParams['font.family'] = 'Malgun Gothic'   # 한글 깨짐 방지 (Windows)
    matplotlib.rcParams['axes.unicode_minus'] = False
except Exception:
    pass
"""


class JupyterKernelSession:
    def __init__(self, kernel_name: str = "python3"):
        self.kernel_name = kernel_name
        self.km: KernelManager | None = None
        self.kc = None
        self._needs_bootstrap = False

    # ------------------------------------------------------------------
    def start(self):
        """커널이 없거나 죽어있으면 새로 시작한다."""
        if self.is_alive():
            return
        self.km = KernelManager(kernel_name=self.kernel_name)
        self.km.start_kernel()
        self.kc = self.km.client()
        self.kc.start_channels()
        self.kc.wait_for_ready(timeout=60)
        self._needs_bootstrap = True

    def is_alive(self) -> bool:
        return self.km is not None and self.km.is_alive()

    def restart(self):
        """커널 재시작 (모든 변수/임포트 초기화)."""
        if self.km is not None and self.km.is_alive():
            if self.kc is not None:
                self.kc.stop_channels()
            self.km.restart_kernel(now=True)
            self.kc = self.km.client()
            self.kc.start_channels()
            self.kc.wait_for_ready(timeout=60)
            self._needs_bootstrap = True
        else:
            self.start()

    def shutdown(self):
        if self.kc is not None:
            self.kc.stop_channels()
            self.kc = None
        if self.km is not None and self.km.is_alive():
            self.km.shutdown_kernel(now=True)
        self.km = None

    # ------------------------------------------------------------------
    def execute(self, code: str, timeout: int = 60) -> dict:
        """
        코드를 실행하고 결과를 모아서 반환한다.

        Returns:
            {
                "status":  "ok" | "error" | "timeout",
                "stdout":  str,
                "stderr":  str,
                "result":  str | None,   # 마지막 표현식의 repr (노트북 Out[..])
                "images":  [base64 PNG, ...],
                "error":   str | None,   # traceback 텍스트
            }
        """
        self.start()

        # 새 커널이면 inline 백엔드 등 초기화 코드를 먼저 실행
        # (execute 재귀 호출 전에 플래그를 꺼서 무한 재귀 방지)
        if self._needs_bootstrap:
            self._needs_bootstrap = False
            self.execute(BOOTSTRAP_CODE, timeout=60)

        msg_id = self.kc.execute(code)
        out = {
            "status": "ok",
            "stdout": "",
            "stderr": "",
            "result": None,
            "images": [],
            "error": None,
        }

        while True:
            try:
                msg = self.kc.get_iopub_msg(timeout=timeout)
            except queue.Empty:
                out["status"] = "timeout"
                out["error"] = f"실행이 {timeout}초 안에 끝나지 않았습니다. (커널 인터럽트)"
                try:
                    self.km.interrupt_kernel()
                except Exception:
                    pass
                break

            # 다른 실행 요청의 메시지는 무시
            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                continue

            msg_type = msg["header"]["msg_type"]
            content = msg["content"]

            if msg_type == "stream":
                if content["name"] == "stdout":
                    out["stdout"] += content["text"]
                else:
                    out["stderr"] += content["text"]

            elif msg_type in ("execute_result", "display_data"):
                data = content.get("data", {})
                if "image/png" in data:
                    out["images"].append(data["image/png"])
                elif "text/plain" in data:
                    if msg_type == "execute_result":
                        out["result"] = data["text/plain"]
                    else:
                        out["stdout"] += data["text/plain"] + "\n"

            elif msg_type == "error":
                out["status"] = "error"
                out["error"] = "\n".join(content.get("traceback", []))

            elif msg_type == "status" and content.get("execution_state") == "idle":
                break

        return out
