"""
사내 Agent(LLM 앱) API 클라이언트.

[POST] {BASE_URL}/webapi/apps/{app_id}/run
  body = {"chat": {"message": ...}, "isStateful": true, "conversationId": ..., "stream": true}

응답 예시 (agent_api_call_sample.txt 참고):
  {"result": {"id": ..., "responses": [{"sender": "BOT"|"USER", "message": ..., ...}, ...],
              "conversation": {"id": ...}}}
"""

import re

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class AgentClient:
    def __init__(self, base_url: str, app_id: str, headers: dict, verify: bool = False):
        self.base_url = base_url.rstrip("/")
        self.app_id = app_id
        self.headers = headers
        self.verify = verify

    def run(self, query: str, conversation_id: str | None = None, timeout: int = 300) -> dict:
        """
        Agent 에 메시지를 보내고 (봇 응답 텍스트, conversation_id) 를 반환한다.

        Returns:
            {"message": str, "conversation_id": str | None, "raw": dict}

        Raises:
            requests.HTTPError, ValueError
        """
        url = f"{self.base_url}/webapi/apps/{self.app_id}/run"
        payload = {
            "chat": {"message": query},
            "isStateful": True,
            "conversationId": conversation_id,
            "stream": True,
        }

        response = requests.post(
            url, headers=self.headers, json=payload,
            verify=self.verify, timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()

        # 응답이 {"result": {...}} 로 감싸져 오거나, 바로 {...} 로 올 수 있음
        body = data.get("result", data)
        if isinstance(body, str):
            # result 가 그냥 텍스트인 경우
            return {"message": body, "conversation_id": conversation_id, "raw": data}

        new_conv_id = (body.get("conversation") or {}).get("id") or conversation_id

        bot_messages = [
            r.get("message", "")
            for r in body.get("responses", [])
            if r.get("sender") == "BOT" and r.get("message")
        ]
        if not bot_messages:
            raise ValueError(f"BOT 응답을 찾을 수 없습니다. 원본 응답: {data}")

        # 마지막 BOT 메시지가 이번 질의에 대한 실제 답변
        return {"message": bot_messages[-1], "conversation_id": new_conv_id, "raw": data}


# ----------------------------------------------------------------------
CODE_BLOCK_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)

DONE_MARKER = "[TASK_DONE]"


def extract_code_blocks(text: str) -> list[str]:
    """마크다운 응답에서 python 코드블록을 순서대로 추출한다."""
    return [m.strip() for m in CODE_BLOCK_RE.findall(text) if m.strip()]


def is_task_done(text: str) -> bool:
    return DONE_MARKER in text
