# 새담인트라넷 — Claude 작업 안내

사단법인 새담청소년교육문화원의 방과후 업무 지원용 사내 인트라넷(Flask + SQLite).
운영 주소는 https://works.saedam.org 이며 Render에 배포한다.
UI 문구·주석·커밋 메시지·답변은 모두 **한국어**로 작성한다.

## 실행

- 로컬: `로컬서버실행.bat` 실행(웹푸시용 VAPID 키 준비 후 `python app.py`) → http://localhost:5000
  - 디버그 자동 재시작이 필요하면 `APP_DEBUG=1`.
- 운영(Render): `render.yaml` → `bash render-build.sh` 후
  `gunicorn --workers 1 --threads 100 --timeout 600 app:app`
  - Flask-SocketIO(메신저)를 쓰므로 **worker는 1개 유지**.
  - Persistent Disk `/mnt/data` + `DATA_DIR=/mnt/data`. 디스크가 없으면 일부러 실행을 멈춘다(`RENDER_DEPLOYMENT.md` 참고).

## 테스트

```
python -m unittest discover tests
python -m unittest tests.test_survey          # 한 파일만
```

- pytest가 아니라 unittest. 각 테스트는 모듈의 `get_db`를 임시 SQLite로 `patch`해서 돌린다.
- 기능을 고치면 해당 `tests/test_<모듈>.py`도 함께 고치거나 추가한다.
- 솔라피·메일·OpenAI 등 외부 API는 **실제로 호출하지 않는다.** `urlopen` 등을 mock 처리한다.

## 구조

- `app.py` — 블루프린트 등록(`url_prefix`), 시작 시 `init_*_schema()` 호출, 로그인·메뉴권한 검사(`before_request`).
- `routes/` — 메뉴별 블루프린트(파일 하나에 화면 + API).
  - `database.py`: `get_db()`(sqlite3, `row_factory=Row`, foreign_keys ON).
  - `storage.py`: 데이터 경로. `DATA_DIR` 미설정 시 로컬 DB는 **`data/saedam.db`**(루트의 `saedam.db`는 예전 파일).
  - `security.py`: 비밀번호 해시, 권한 데코레이터, `load_credential_secret()`(암호화 키).
  - `menu_access.py`: 메뉴 카탈로그와 레벨·부서별 접근 제한.
  - `admin_management.py`: 통합관리(시스템관리) 화면. 솔라피설정·AI설정 등.
  - `solapi_settings.py`: 솔라피 공용 모듈. 아래 "솔라피" 참고.
- `templates/` — Jinja. 대부분 `{% extends "base.html" %}` + 블록 `extra_css` / `content` / `scripts`. CSS·JS는 각 템플릿 안에 인라인.
- `static/`, `services/`(AI 도구·이력서·도면 등 보조 로직), `scripts/`(진단·생성용 일회성 스크립트), `terms/`(약관).
- `수정리스트.txt` — 사용자가 적어 둔 요청 목록.

## DB 스키마 변경 규칙

- 각 모듈의 `init_<모듈>_schema()`에서 `CREATE TABLE IF NOT EXISTS`로 만든다.
- 이미 운영 중인 테이블에 열을 추가할 때는 `PRAGMA table_info`로 확인 후 `ALTER TABLE ... ADD COLUMN` 하는
  `_add_missing_columns()` 패턴을 쓴다(예: `routes/survey.py`). 운영 DB를 직접 고치는 마이그레이션 스크립트는 만들지 않는다.
- 새 열을 읽을 때는 예전 행·예전 DB에서도 깨지지 않게 기본값을 준다.

## 권한·세션

- 세션 키: `emp_no`, `user_name`, `user_level`. `user_level <= 2` 또는 `user_name == 'admin'`이 관리자.
- 공개 화면(설문 응답, 계약 서명, 학부모 등록 등)은 로그인 없이 토큰 URL로 접근한다.

## 솔라피(문자·알림톡) — `routes/solapi_settings.py`

- 설정은 통합관리 > 솔라피설정에 Fernet 암호화로 저장(`admin_settings` 테이블). **저장값이 환경변수보다 우선**한다.
- 인증전자계약: `send_alimtalk()`(pf_id/template_id, `#{이름}`, `#{url}`=https:// 제외 주소), 본인인증 `send_sms_otp()`.
- 설문조사: `send_bulk_text()`(SMS/LMS 자동 전환, 90바이트 EUC-KR 기준), `send_bulk_alimtalk()`,
  `fetch_kakao_template()`(`GET /kakao/v2/templates/{id}`, 10분 캐시).
  - 설문 알림톡은 전자계약과 별도인 `survey_pf_id` / `survey_template_id`를 쓴다. 기본값은 코드 상수
    `SURVEY_KAKAO_PF_ID`, `SURVEY_KAKAO_TEMPLATE_ID`.
- 알림톡 대체문자는 LMS 전환 시 제목이 비면 1010 오류가 나므로 `replacements`에 제목을 넣어 보낸다.
- 접수 성공(2000/4000)은 최종 수신 완료가 아니다.

## 메뉴별 메모

- **학교관리 > 설문조사** (`routes/survey.py`, `templates/survey/`)
  - 발송방법: 카카오 알림톡(기본) / 문자(LMS). 설문마다 `surveys.send_method`에 기억한다.
  - 알림톡 템플릿의 `#{변수}`는 이름으로 자동 매핑하고(`KAKAO_VARIABLE_RULES`), 못 채우는 변수는 `surveys.kakao_variables`에 직접 입력한다.
  - 발송 탭 화면: 1 수신자 등록 → 2 발송방법 선택(템플릿·변수·대체문자는 [세부설정]을 눌러야 펼침) → 3 발송 미리보기 → 4 발송.

- **사내결재 > 강사전자계약 / 임직원전자계약** (`routes/verified_contract.py`, `templates/verified_contract/`)
  - 예전 인증전자계약관리를 메뉴 2개로 분리: `verified_contract_instructor` / `verified_contract_employee`.
    관리 주소는 `/verified-contract/<instructor|employee>/admin…`, 계약자 서명 주소(`/verified-contract/sign/<토큰>`)는 그대로.
  - 어느 계약구분이 어느 메뉴인지는 `data/verified_contract/category_kinds.json`(없으면 코드의 `DEFAULT_EMPLOYEE_CATEGORIES` 기준).
    `_categories()`는 현재 메뉴의 구분만 돌려주고, 번호(id)로 다른 메뉴 계약을 건드리면 `before_request`가 막는다.
  - 회사·발송계정은 두 메뉴가 공유한다. 예전 `/verified-contract/admin`은 접근 가능한 첫 메뉴로 이동.
  - DB도 분리: `instructor_/employee_verified_contracts` + `…_verified_contract_events`(`verified_contract_repository.py`).
    예전 `verified_contracts`는 시작 시 `split_verified_contracts()`가 id 그대로 1회 복사(계약구분→메뉴 판정은 `category_kind_of_type()`)하고
    `admin_settings`의 `verified_contract_split_v1`로 완료 표시 후 백업용으로만 남긴다. 공개 서명 링크는 `_load_by_token()`이 두 테이블을 모두 찾는다.
    kind 없이 호출하면(스크립트·테스트) 예전 테이블을 쓴다.
  - 테스트: `tests/test_verified_contract_split.py`.

- **사내결재 > 증명서 발급관리** (`routes/document.py`, `templates/certificate/`)
  - 메뉴 3개로 완전 분리: 강사증명(`document_instructor`) / 임직원증명(`document_employee`) / 우수강사인증서(`document_excellent`).
    URL은 `/document/<instructor|employee|excellent>/admin·settings·generate·pdf·delete·edit`, 코드는 `CERT_KINDS`로 종류별 설정을 모은다.
  - 신청 테이블도 분리: `instructor_/employee_/excellent_certificate_requests`(+ `excellent_certificate_request_groups`).
    예전 `certificate_requests`는 1회 복사 후 백업용으로만 남긴다(`split_certificate_requests`).
  - 회사·작업그룹·발송계정(`certificate_companies/workgroups`, `ai_mail_senders`)은 세 메뉴가 공유하고, 작업그룹의 `allow_*` 열로 메뉴별 신청 링크를 켜고 끈다.
  - 공개 신청 주소는 그대로: `/document/apply`(강사), `/apply2`(임직원), `/apply-excellent`(우수강사). 예전 `/document/admin`은 접근 가능한 첫 메뉴로 이동.
  - 테스트: `tests/test_certificate_split.py` (발급번호 파일·엑셀 이관은 반드시 patch — 실제 `data/`를 건드린다).

## 작업 규칙

- 기존 파일을 크게 고치기 전에 원본을 `-== 백업/<작업명>_수정전_YYYYMMDD/`에 **같은 상대경로**로 복사해 둔다.
- `saedam.db`, `data/`, `mail_settings.json`, 보안키 폴더는 수정·삭제하지 않는다(실데이터·비밀값).
- 외부 발송(문자·알림톡·메일)을 실제로 일으키는 명령은 사용자 확인 없이 실행하지 않는다.
- 화면 문구는 사용자(사무직원·강사·학부모)가 이해하기 쉬운 말로 쓴다. 화면은 단계가 분명하게 나뉘도록 구성하고, 자주 안 쓰는 설정은 접어 둔다.
- 변경 후에는 관련 unittest를 돌리고, 가능하면 로컬 서버에서 화면을 직접 확인한다.
