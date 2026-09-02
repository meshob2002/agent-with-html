# 에이전트 프롬프트

OK Agent 플랫폼에 **4개 앱을 각각 등록**할 때 붙여넣는 시스템 프롬프트다.
각 앱의 `app_id` 를 앱 설정(또는 환경변수)에 넣으면 오케스트레이터가 이 규약대로 호출한다.

| 파일 | 에이전트 | 출력 계약(앱이 파싱하는 형식) |
|---|---|---|
| `1_router.md` | 라우팅 | `{"action":"analysis\|sql\|html\|finish","reason":...}` JSON 한 줄 |
| `2_analysis.md` | 분석 | 코드 필요 시 ` ```python ` 코드블록 1개, 완료 시 코드블록 없이 최종 요약 |
| `3_sql.md` | SQL 실행 | 결과 **CSV 다운로드 링크** 포함 |
| `4_html.md` | HTML 생성 | 완성된 standalone HTML 문서 |

> 이 프롬프트들은 앱 코드(`agents.py`)의 파싱 규칙과 짝을 이룬다. 형식을 바꾸면
> 파싱 쪽(`parse_router_decision`, `extract_code_blocks`, `extract_links`, `run_html`)도 함께 맞춰야 한다.
