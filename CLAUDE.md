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
  - 학부모 안내 링크 알림톡은 `parent_pf_id` / `parent_template_id`(환경변수 `SOLAPI_PARENT_PF_ID`·`SOLAPI_PARENT_TEMPLATE_ID`)를 쓴다.
    기본값은 코드 상수 `PARENT_KAKAO_PF_ID`(설문조사와 같은 채널)·`PARENT_KAKAO_TEMPLATE_ID`(2026-10 승인 템플릿)이고, 저장값 → 환경변수 → 상수 순으로 쓴다.
    채널 ID가 비면 설문조사 채널을 대신 쓴다(`parent_channel_id`). 솔라피 키·발신번호와 템플릿 ID가 있어야 `parent_kakao_configured`가 켜진다.
    `kakao_template_problem()`(미승인 템플릿 점검)과 `kakao_template_preview()`(알림톡 모습 글 미리보기)는 공용 도우미다.
  - 화면을 그릴 때는 `get_settings(conn)`처럼 연결을 넘겨 읽는다(테스트에서 실제 DB를 건드리지 않는다).
  - **알림톡 템플릿은 기능마다 한 칸씩** `KAKAO_TEMPLATE_SLOTS`(인증전자계약·설문조사·학부모 안내 링크)에 등록하고, 통합관리 > 솔라피설정의 노란 배경
    [카카오 알림톡 템플릿] 구역에 카드로 나온다(칸 정의 → `_template_slots_for_view()`가 설정 여부까지 계산). 새 기능이 알림톡을 쓰면
    칸 한 줄 + `…_pf_id`/`…_template_id` 저장·환경변수 항목을 더한다. 화면에서 [+ 템플릿 추가]로 적어 둔 **추가 템플릿**(이름·채널·템플릿 ID·메모, 최대 30개)은
    `admin_settings`의 `extra_templates`에 저장되고(`clean_extra_templates`가 검사, 채널을 비우면 설문조사 채널), 코드에서는 `get_template_ref('이름')`으로 불러 쓴다.
    `save_settings(extra_templates=None)`이면 기존 목록 유지, 목록(빈 목록 포함)이면 그대로 교체. 화면의 [승인 상태 확인]은
    GET `/admin/solapi-settings/template-check?template_id=`(관리자 전용, 솔라피 조회만)이다.
  - **학부모 안내 알림톡의 변수 이름은 솔라피설정에서 바꿀 수 있다**: 학부모 안내 카드의 [템플릿 변수 이름]에 학교 이름·안내 주소 변수를 템플릿에 쓴 이름대로 적는다
    (`parent_var_school`·`parent_var_url` 저장, 기본 `학교명`·`url`, `#{ }`를 붙여 적어도 이름만 저장, 비우면 기본 이름, 둘이 같은 이름이면 거절).
    변수 인식 순서는 ① 직접 정한 이름과 정확히 같음 → ② 이름 속 단어로 짐작(`학교`·`기관`·`school` → 학교 이름 / `url`·`link`·`링크`·`주소`·`바로가기` → 안내 주소)
    (`solapi.link_variable_source`). 그래서 `#{school_name}`처럼 단어가 들어 있으면 설정 없이도 되고, `#{sname}`처럼 단어가 없으면 설정해야 한다.
    [승인 상태 확인](`mode=link`)은 입력칸의 이름으로 연결 결과·채울 수 없는 변수·템플릿에 없는 항목을 보여 준다(`match_link_variables`).
    인증전자계약(`#{이름}`·`#{url}` 고정)과 설문조사(설문 화면에서 변수별 직접 입력)는 이 설정을 쓰지 않는다.
- 알림톡 대체문자는 LMS 전환 시 제목이 비면 1010 오류가 나므로 `replacements`에 제목을 넣어 보낸다.
- 접수 성공(2000/4000)은 최종 수신 완료가 아니다.

## 메뉴별 메모

- **학교관리 > 설문조사** (`routes/survey.py`, `templates/survey/`)
  - 발송방법: 카카오 알림톡(기본) / 문자(LMS). 설문마다 `surveys.send_method`에 기억한다.
  - 알림톡 템플릿의 `#{변수}`는 이름으로 자동 매핑하고(`KAKAO_VARIABLE_RULES`), 못 채우는 변수는 `surveys.kakao_variables`에 직접 입력한다.
  - **자동 마감**: 진행중(`open`) 설문은 응답 종료일(`ends_on`)이 지나면 한국시간 0시부터 '마감'으로 본다(`_effective_status`, 서버가 UTC라 `KST` 기준 `_today()`).
    DB의 `status`는 바꾸지 않고 화면·필터·응답·발송에서 계산해 쓴다 → 종료일을 늘리면 저절로 다시 진행중이 되고, 직접 마감(`closed`)한 설문은 그대로다.
    종료일이 지난 설문은 `api_status`로 진행중 전환과 `api_send` 발송이 거절된다(작성중 설문도 발송 불가). 목록 응답의 `auto_closed`가 자동 마감 표시용이다.
  - 발송 탭 화면: 1 수신자 등록 → 2 발송방법 선택(템플릿·변수·대체문자는 [세부설정]을 눌러야 펼침) → 3 발송 미리보기 → 4 발송.

- **학교지원 > 학부모·강사 통합지원** (`routes/support_hub.py` 화면, `routes/instructor_hub.py`·`routes/parent_hub.py` 기능, `templates/support_hub/`, `templates/parent_hub/`, `static/css/parent_hub.css`)
  - 예전 강사통합지원·학부모통합지원을 **메뉴 하나로 합쳤다**. 메뉴키 `school_support_hub`(레벨 7), 화면은 `/support-hub/?tab=instructors|roster|review|portal&school=<학교명>`.
    예전 `/instructor-hub`·`/parent-hub` 화면 주소는 이 화면으로 넘어가고(`TAB_ALIASES`), 권한은 예전 두 메뉴 중 더 엄격한 쪽을 이어받는다(`ensure_menu_access_schema`).
    위쪽 학교 선택 상자는 [학교업무공간]의 `schools`이고 네 탭이 같은 학교를 쓴다(마지막 학교는 세션 `ih_admin_school`).
  - **학부모가 들어오는 입구는 하나**다. 다른 메뉴(학생출석관리·학부모알림전송·강사 포털)는 학부모용 QR·등록 링크를 더 이상 만들지 않는다.
    예전 주소는 모두 통합지원으로 보낸다: `/parent/register/<개인토큰>` → `/parent/home/<개인토큰>`, 과목 학부모 QR(`/student-attendance/p/<키>`) → 학교 링크(포털이 없으면 예전 양식).
  - **과목·수강생 입력은 [과목·학생] 탭 한 곳**이다(`support_hub.py`: `save_class`·`save_student`·`bulk_students`·`import_roster`).
    과목 만들기의 **과목명은 선택 상자**(이 학교 강사의 강의부서 목록, `_subject_options`)이고 `parent_classes.department`에 저장되며,
    **강의반**(예: 로봇과학A)은 직접 입력해 `class_name`에 저장한다. 둘 다 필수. 강사 자동 연결은 과목명(=department) 기준이다.
    과목(요일·시간·지각 기준·QR 방식·알림)·강사 연결(강의부서 자동 / 직접 선택)·학생(한 명 / 붙여넣기 최대 300줄 / 학교 전체 엑셀 최대 1000줄) 모두 여기서 하고,
    실제 저장은 `student_attendance.save_class_data`·`register_students`·`parse_roster_text`를 같이 쓴다. 붙여넣기·엑셀은 **한 줄이라도 틀리면 아무도 등록하지 않는다**(줄 번호 안내).
    [학생출석관리]는 출석 운영(실시간·교실 QR·월별)만, [학부모알림전송]은 알림 발송·기록만 남기고 입력 화면은 없앴다(통합지원으로 가는 안내만 둔다).
  - 강사 등록·관리 탭: 강사는 **본사 담당자가 직접 등록**한다(한 명 / 붙여넣기 / 엑셀 `.xlsx`, 최대 300줄). 등록 = `register_instructor()`: 이름·휴대폰(010…)·강의부서 필수.
    같은 휴대폰번호 계정이 있으면 새로 만들지 않고 학교·강의부서만 더한다. 새 계정은 접속 링크(`ih_accounts.token`)와 6자리 비밀번호(암호화 `pw_enc`)가 무작위로 생긴다.
    엑셀 머리글(이름·휴대폰·강의부서·이메일·학교)은 순서와 무관하게 찾는다. [학교업무공간]에 없는 학교는 등록할 수 없다.
    강사별 관리: 문자 발송(`send_bulk_text`, 확인 창에서 동의했을 때만) · 학교 전체 일괄 문자 · 비번/링크 재발급 · 사용 중지 · 이름·번호 수정 · 강의부서 변경 · 이 학교에서 빼기 · (배정 없는 계정만) 삭제 · 접속정보 엑셀.
  - 승인관리 탭: 강사 한 명이 한 줄(강사 | 프로필 | 홍보 리플렛), 승인 대기 줄은 강조, 반려 사유는 입력 창.
  - 학부모 포털 탭: 학교당 포털 1개(`ph_portals`), 공개 주소(로그인 없음, `app.py`의 `PUBLIC_PATH_PREFIXES`)는 학교 링크 `/parent/hub/<포털토큰>`,
    보호자 개인 화면 `/parent/home/<개인토큰>?tab=home|attendance|teachers|leaflet|guide|notices`. 개인토큰 = 보호자 초대 토큰(`parent_invites.token`)이라 푸시 구독·알림 내역이 한 사람 기준으로 이어진다.
  - **학부모 링크 발송**(`parent_hub.admin_send_link`, POST `/parent-hub/portals/<id>/send`): [학부모 안내] 탭의 ①대상 ②발송 방법 ③내용 순서 화면이다.
    - ① 대상(전체 / 알림 미등록 / 아직 안 받은 사람) 옆 인원 숫자를 누르면 **받는 사람 명단**이 펼쳐지고 체크로 한 명씩 빼거나 넣는다(이름·번호 검색, 보이는 사람 전체 선택/해제).
      서버는 `custom=1`일 때 체크된 `guardian_ids`만 보내되 **대상 안에 있는 보호자만 인정**한다(다른 학교·번호가 틀린 보호자는 골라도 제외). `custom`이 없으면 대상 전체(예전 방식).
    - ② 발송 방법: **문자**(솔라피 SMS/LMS, 안내 문구 직접 입력) 또는 **카카오 알림톡**. 알림톡은 통합관리 > 솔라피설정의 [학부모 안내 알림톡 템플릿 ID]가 있어야 고를 수 있다.
    - 알림톡 변수는 **학교 이름과 안내 주소 두 가지만** 지원한다(기본 이름 `#{학교명}`·`#{url}`, 템플릿에 쓴 이름이 다르면 솔라피설정에서 변수 이름을 맞춘다. 아래 솔라피 항목 참고, `kakao_link_values()`).
      버튼 주소가 `https://#{url}`이면 https://를 뺀 주소를 넣는다. 다른 변수가 있거나 미승인 템플릿이면 보내지 않고 이유를 알려 준다. 못 받은 사람은 문자로 대체발송(체크, 기본 켬)하며 문구는 안내 문구 칸을 쓴다.
    - 알림톡 미리보기(템플릿·변수표)는 GET `/parent-hub/portals/<id>/kakao-preview?link_kind=personal|school`이 솔라피 템플릿을 **조회만** 해서 보여 준다(발송 안 함).
    - 링크 종류는 개인 링크(기본) / 학교 공통 링크. 보호자 한 명당 한 통, 번호가 틀린 보호자는 건너뛴다. 접수 성공한 사람만 `parent_invites.sms_sent_at/sms_send_count`와
      `ph_portals.last_sent_at/last_sent_count/last_sent_method`(text|kakao)에 기록한다(알림톡 성공 = 접수 기준). 공개 중이 아닌 포털은 보낼 수 없다. 확인 창에서 동의했을 때만 보낸다.
  - 보호자 확인: 학교 링크에서 `자녀 이름 + 보호자 휴대폰번호`가 그 학교 명단과 맞으면 개인 화면으로 이동하고 쿠키(`saedam_parent_hub`)로 기억.
    실패는 `student_attendance`의 과목×IP 제한(20회/10분)을 같이 쓴다. 포털이 없는 학교의 보호자도 개인 화면에서 출석·알림은 본다.
  - 출석률: 과목에서 한 명이라도 기록된 날 중 **자녀의 첫 기록일 이후**만 수업 횟수로 세고, 지각은 출석으로 계산한다(`attendance_report`).
  - 푸시알림을 누르면 개인 화면이 열린다(`parent_notifications._notification_url`: 출결 알림→출석 탭, 그 밖→알림 탭). 알림 목록 시각은 한국시간(KST)으로 보여 준다.
  - 학교 e리플렛은 **가상으로 합친다**: 본사 앞쪽(`ph_leaflet_pages`) → 강사 홍보(승인된 `ih_promos`→`ebooks`, 강의부서·이름순) → 본사 뒤쪽.
    쪽 그림은 포털 토큰 아래 `/parent/hub/<토큰>/leaflet/hq|eb/<쪽id>`로만 열린다(그 학교 리플렛에 실린 쪽만). 강사는 강사 포털의 홍보페이지에서 올린다.
  - 3D 교실안내: [교실안내]에서 공유를 켠 학교만 연결되고, 자녀 강좌명이 배치도의 강좌와 맞으면 `?to=<교실id>`로 바로 안내한다.
  - 학부모 링크페이지(`ih_link_pages`, `/school-link/…`)는 삭제했다. 운영 DB에 테이블이 남아 있어도 코드는 쓰지 않는다.
  - 인쇄용 안내문: `/parent-hub/portals/<id>/print`(QR 포함 A4 한 장).
  - 화면 스타일: `templates/support_hub/_style.html`의 `.ih-*`(스마트 명세서 발송과 같은 전체 폭·가벼운 글씨: 400 / 500 / 600만).
  - 테스트: `tests/test_support_hub.py`(과목·학생 입력, 엑셀, 링크 발송·받는 사람 선택·알림톡·알림톡 설정, 입구 단일화, 메뉴 권한 이어받기), `tests/test_parent_hub.py`, `tests/test_instructor_hub.py`, 솔라피설정 화면·템플릿 칸은 `tests/test_solapi_settings.py`
    (직원 화면은 `base.html`을 빈 껍데기로 바꿔 끼워 본문만 확인, 문자는 `routes.solapi_settings.send_bulk_text`를 mock).

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
