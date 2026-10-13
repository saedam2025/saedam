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

- **학교지원 > 강사 인성·직무적합성 검사** (`routes/personality_test.py` 화면·API, `services/personality_test_v2.py` 새 문항·채점·해석, `services/personality_test_v1.py` 기존 버전, `templates/personality/`)
  - 메뉴키 `school_personality`(학교지원 주메뉴 아래, 기본 레벨 4 — 응시자 개인정보가 있어 면접관리와 같게 제한. 메뉴 권한관리에서 조정).
    관리 화면 `/personality/`(검사 링크 만들기·복사·사용 중지·삭제 + 응시 결과 목록·검색), 결과지 `/personality/result/<id>`(응시자 정보·종합 의견·영역별 막대·면접 확인 질문·답안지, 인쇄/PDF 저장).
  - 응시 주소는 로그인 없이 여는 `/personality/t/<토큰>`(`app.py`의 `EXEMPT_ROUTES`·`PUBLIC_PATH_PREFIXES`, `menu_access`는 None). 응시자는 성명·생년월일·휴대폰·이메일 입력 + 개인정보 동의 → 40문항(10문항씩 4쪽) → 제출.
    같은 검사 링크에서 같은 이름+휴대폰은 한 번만 제출(다시 응시시키려면 결과를 삭제). 응시자 화면에는 점수·결과를 보여 주지 않고 관리자만 본다.
  - 테이블 `personality_links`(검사 링크, 토큰, `test_version`) / `personality_results`(개인정보·`answers` JSON {문항번호: 1~5}·`answer_sheet`·`scores`·`report` JSON·소요시간·`test_version`). 이미 응시자가 있는 링크는 삭제 불가(사용 중지만).
  - 새 검사 v2: 5점 척도 40문항 = 아동 존중·교육윤리 8, 책임감·성실성 7, 감정조절 7, 의사소통·협력 6, 문제해결 6, 직업윤리·안전의식 6. 역문항을 포함한다. 영역과 전체 점수는 응답 합계를 0~100으로 환산하며 표준화 점수가 아닌 면접 참고용이다. 같은 값 35개 이상·90초 미만 응답이면 '신뢰도 확인' 표시.
    기존 링크는 v1 문항을 유지하고 기존 결과지도 v1 문항으로 다시 볼 수 있다. **문항을 바꾸면 새 버전을 만들고 과거 버전 파일을 보존한다**. 이전 DB의 링크에는 v1이 자동 지정된다.
  - 시각은 한국시간 문자열로 저장한다. 테스트: `tests/test_personality_test.py`.

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
  - 3D 교실안내: [교실안내]에서 공유를 켠 학교만 연결되고, 자녀의 강의반 이름 또는 과목명(강의부서)이 배치도의 강좌와 맞으면(`_room_for_class`) `?to=<교실id>`로 바로 안내한다.
    `?to=` 링크는 PC에서 교실을 누른 것과 같이 그 교실의 층 평면도를 열고 교실 이름표(공유 화면은 빨간색 `--cg-select`)를 강조·확대하며(`viewer.js` `selectRoom`),
    이어서 가는 길을 그리되 화면은 교실에 맞춘다(`runRoute(..., {focus:'room'})`). 길 계산이 안 되는 배치도라도 교실은 항상 안내된다.
  - 학부모 링크페이지(`ih_link_pages`, `/school-link/…`)는 삭제했다. 운영 DB에 테이블이 남아 있어도 코드는 쓰지 않는다.
  - **학부모 화면 디자인 선택**: 학교(포털)마다 `ph_portals.design`(기본 `classic`, 예전 DB는 `_add_missing_columns`로 열 추가)에 저장하고,
    [학부모 안내] 탭의 [학부모 화면 디자인] 카드에서 고른다(POST `/parent-hub/portals/<id>/design`). 종류는 `parent_hub.DESIGNS`
    (`classic` 기본 / `neo` 네오: 남색·청록 글래스 + 더 진한 글자·상태 색, `static/css/parent_hub_neo.css`). 새 디자인은 `DESIGNS`에 한 줄 + 덮어쓰기 CSS
    (기본 CSS 다음에 불러오고 `body.ph-d-<키>` 아래에만 쓴다)를 만든다. 학교 링크 첫 화면과 학부모 홈에 적용되고(홈은 고른 자녀 학교의 포털 → 없으면 공개 중 첫 포털 → 기본),
    e리플렛 펼쳐보기는 어두운 화면이라 디자인과 무관하다. 고르기 전 확인은 GET `/parent-hub/portals/<id>/design-preview?design=&tab=`(직원 전용 예시 화면: 예시 자녀·출석, 실제 학생 정보·푸시 스크립트 없음).
  - 인쇄용 안내문: `/parent-hub/portals/<id>/print`(QR 포함 A4 한 장).
  - 화면 스타일: `templates/support_hub/_style.html`의 `.ih-*`(스마트 명세서 발송과 같은 전체 폭·가벼운 글씨: 400 / 500 / 600만).
  - 테스트: `tests/test_support_hub.py`(과목·학생 입력, 엑셀, 링크 발송·받는 사람 선택·알림톡·알림톡 설정, 입구 단일화, 메뉴 권한 이어받기), `tests/test_parent_hub.py`, `tests/test_instructor_hub.py`, 솔라피설정 화면·템플릿 칸은 `tests/test_solapi_settings.py`
    (직원 화면은 `base.html`을 빈 껍데기로 바꿔 끼워 본문만 확인, 문자는 `routes.solapi_settings.send_bulk_text`를 mock).

- **업무지원 > 입찰공고관리** (`routes/bid_notice.py` 화면·수집·메일·자동 실행, `services/g2b_bid.py` 나라장터 조회·해석, `templates/bid_notice/bid_notice.html`)
  - 메뉴키 `bid_notice_main`(업무지원 아래, 기본 레벨 5), 화면 `/bid-notice/`. 설정 저장·메일 발송 API는 관리자(레벨 2 이하)만 쓴다.
  - 조달청 **나라장터 입찰공고정보서비스**(`https://apis.data.go.kr/1230000/ad/BidPublicInfoService`)를 쓴다:
    용역 공고 검색 `getBidPblancListInfoServcPPSSrch`(공고명 `bidNtceNm`, 게시일시 기간 `inqryDiv=1`, **한 번에 최대 1개월** → 28일로 줄여 조회),
    기초금액 `getBidPblancListInfoServcBsisAmount`(`inqryDiv=2` + 공고번호). 정정이력 오퍼레이션은 공고번호로 조회하면 비어 와서 쓰지 않는다.
  - 인증키는 화면의 [수집·메일 설정]에 Fernet 암호화로 저장(`admin_settings`의 `bid_notice_settings`)하고, 없으면 환경변수 `G2B_SERVICE_KEY`를 쓴다. **소스에 키를 적지 않는다.**
  - **검색 방식**: 나라장터 공고명 검색은 글자가 그대로(띄어쓰기까지) 들어 있어야 찾으므로, 검색어의 **첫 낱말**로 넓게 조회한 뒤(`search_terms`)
    나머지 낱말(띄어쓰기 무시, 모두 포함)·제외어·지역·기관 낱말을 코드에서 거른다(`passes_filters`). 기본값: 검색어 `방과후`·`늘봄`·`맞춤형`·`온동네 돌봄`,
    제외어 `급식`·`통학차량`·`폐기물`, 지역 `경기도`, 기관 `초등학교`(비우면 제한 없음). 학교마다 공고명이 달라("방과후학교 프로그램 운영 용역") 긴 문구는 대부분을 놓친다.
    **기관 조건 예외**: `org_free_keywords`(기본 `온동네 돌봄`)에 맞는 공고는 기관 낱말을 보지 않는다 — 온동네 돌봄·교육센터처럼 학교가 아니라 교육지원청이 발주하는 사업을 놓치지 않기 위해서다(지역·제외어는 그대로 적용, 나라장터 조회 낱말에도 더한다).
  - 테이블: `bid_notices`(공고번호 1건 = 1행, 가장 최신 차수 내용) / `bid_notice_events`(신규 `new`·정정 `changed`·취소 `cancelled`, `mailed_at`이 비면 미발송) /
    `bid_collect_runs`(수집 기록, 자동 회차는 `slot_key`='YYYY-MM-DD HH:MM'). 차수(`bid_ord` 000→001…)가 올라가면 정정·취소로 보고 달라진 항목을 `summary`에 적는다.
    이미 쌓아 둔 공고는 조건과 상관없이 끝까지 따라간다. 지역·학교 이름은 수요기관 이름에서 뽑는다(`region_of`·`school_of`).
  - **자동 실행**: `start_bid_scheduler()`가 서버 안 백그라운드 스레드로 1분마다 확인해 한국시간 09:00·15:00 회차를 한 번씩 돌린다(worker 1개 전제, 실패하면 20분 간격으로 3번까지).
    **운영 서버(Render)에서만 켜지고** 로컬은 `BID_SCHEDULER=1`일 때만 켠다(`BID_SCHEDULER=0`이면 운영에서도 끔). 서버가 회차 시각에 꺼져 있었으면 켜진 뒤 그날 마지막 회차를 한 번 돌린다.
  - 수집 범위: 처음은 최근 28일(이때 찾은 공고는 메일 대상에서 뺀다), 그 뒤는 지난 수집 시각 − 24시간부터. 기초금액은 한 번에 60건까지 확인하고 없으면 배정예산을 보여 준다.
  - **메일**: 자동 수집에서 새 소식이 있을 때만 받는 주소(최대 5곳)로 보낸다. 보내는 계정은 인트라넷 공용 메일(`contacts._mail_credentials`: `MAIL_USERNAME`/`MAIL_PASSWORD` → `mail_settings.json`).
    [지금 수집]은 메일을 보내지 않고, [테스트 메일]·[안 보낸 소식 지금 보내기]는 확인 창에서 동의했을 때만 보낸다(`confirm`).
  - **기간 수집**: 목록의 [게시일] 기간은 쌓아 둔 공고를 거르는 조건이고, [이 기간 공고를 나라장터에서 찾아오기](POST `/bid-notice/api/collect-range`)는 그 기간을
    나라장터에서 직접 찾아 더한다(28일씩 잘라 조회, 한 번에 최대 366일, `trigger_type='range'`). 지난 공고라 메일 대상에서 빼고, 다음 자동 수집이 이어 볼 시각에도 영향을 주지 않는다.
  - **화면 구성**: 위쪽 버튼으로 화면을 완전히 나눈다 — [공고 목록]([목록에서 찾기] 한 줄 상자(상태 탭 · 게시일 · 검색어 · 검색/조건 지우기/개수, `.findbar`) + 표) / [나라장터에서 가져오기](지금 수집 · 기간을 정해 가져오기) /
    [수집·메일 설정](관리자만) / [수집 기록]. 목록의 게시일 조건과 가져오기의 기간 입력칸은 서로 다른 칸이다. 메뉴 상자는 본문 전체 폭을 쓴다(스마트 공문발송과 같은 폭, `.content-container`의 1700px·좌우 45px 그대로). 요약 숫자(진행 중·오늘 새 공고·정정/취소·누적·마지막/다음 수집)는 **브라우저 오른쪽 벽에 붙은 [현황 요약] 색인 탭**(`.bn-dock`, `position:fixed`, 전자계약의 `.vc-dock`과 같은 방식)에 있다 — 탭을 누르면 판이 왼쪽으로 펼쳐지고 다시 누르거나 바깥·Esc를 누르면 접힌다(펼침 상태는 기억하지 않음). 화면 머리글의 설명 문구는 두지 않는다. 판 안의 줄에는 이 화면의 `.sub`(display:block)와 겹치지 않게 `.minor`를 쓴다. 표의 [관리] 버튼(첨부 받기·이력)은 한 줄, 오른쪽 정렬이다. 처음 열면 [전체] 탭이다. 공고 목록 표는 알림 메일과 같은 글꼴(맑은 고딕 13px, 파란 링크)을 쓴다.
    표의 열 순서: 번호(보이는 순서의 역순) · 공고명(한 줄, 넘치면 … / 전체 제목은 툴팁) · 지역 · 학교 · 입찰 마감(아래에 작게 개찰일) · 기초금액 · 결과(낙찰 업체 이름, 누르면 낙찰금액 등 상세) · 관리 · 진행 상태.
    상태 탭은 전체·진행 중·마감·취소·**3일내 마감**(`status=soon`: 마감 전이고 마감일이 오늘부터 `SOON_DAYS`=3일 안, 마감이 가까운 순)이다. [지역] 머리글도 선택 상자다(불러온 목록 안의 지역별, `fillRegionFilter`). [입찰 마감]·[결과] 머리글을 누르면 화면에서 정렬한다(오름 → 내림 → 원래 순서, 빈 값은 항상 맨 뒤, `SORT`·`renderNotices`). 공고번호·게시일·담당자 줄은 표에 보이지 않는다. 목록은 화면에서 쪽을 나눈다([조건 지우기] 옆 30·60·100개씩 선택, 브라우저에 기억 / 표 아래 쪽 번호). 서버는 조건에 맞는 공고를 `LIST_LIMIT`(500)건까지 한 번에 준다. [진행 상태] 머리글은 선택 상자라 고른 상태의 공고만 보여 준다(`work_status` 조건). 진행 상태가 `입찰제외`인 공고는 행 전체 배경을 회색으로 보여 준다(`tr.excluded`, 상태를 바꾸면 바로 반영). [결과] 머리글도 선택 상자다(결과 있음·없음·유찰 등·낙찰 업체별, 불러온 목록 안에서 화면이 거른다 — `fillResultFilter`·`resultMatches`). 옆의 화살표를 누르면 업체 이름순 정렬. 금액 칸은 금액만 보여 준다(기초금액, 없으면 배정예산 — 구분은 툴팁).
  - **진행 상태**: 공고마다 `bid_notices.work_status`(`WORK_STATUSES`: 입찰예정·입찰제외·서류접수완료·가격투찰완료·설명회완료·개찰완료, 비면 '상태선택')를 고른다.
    마감·취소 같은 공고 자체의 상태(탭)와는 별개이고, 정정 공고가 나와도 유지된다.
  - **선정 결과는 수집할 때 자동 조회**(버튼 없음): `collect_notices`가 끝에 `refresh_results`를 불러, 취소가 아니고 개찰 예정 시각(`open_dt`)이 지났으며 아직 `최종낙찰`이 아닌 공고를
    `g2b.fetch_award`로 확인한다 — **낙찰정보서비스**(`https://apis.data.go.kr/1230000/as/ScsbidInfoService`, 입찰공고와 **같은 인증키**, `inqryDiv=4` + 공고번호).
    최종 낙찰(`getScsbidListSttusServc`)이 있으면 `result_state='최종낙찰'`, 없으면 개찰 결과(`getOpengResultListInfoServc`)의 1순위(`opengCorpInfo` = 업체^사업자번호^대표^금액^투찰률)를
    `'개찰완료'`로, 유찰 등은 진행 상태 그대로 저장한다. 한 번에 `RESULT_LIMIT`(40)건(기간 수집은 120건), 오래 확인하지 않은 것부터. 최종 낙찰이 나올 때까지 다시 확인하되
    개찰 뒤 `RESULT_RECHECK_DAYS`(60일)가 지나면 한 번만 본다(`result_checked_at`). 결과가 아직 없으면 예전 값을 지우지 않는다. 조회 실패는 수집을 실패로 만들지 않는다.
    저장 열: `winner_name`·`winner_amount`·`winner_rate`·`result_participants`·`result_opened_at`·`result_final_date`. 직접 입력 화면·[결과 검색] 버튼은 없앴다.
  - **첨부파일은 서버에 보관하지 않는다**: 수집할 때 공고의 첨부 목록(`ntceSpecDocUrl1~10`)만 `bid_notices.attachments`(JSON)에 적어 두고, [첨부 받기](GET `/bid-notice/api/notices/<id>/files`)를
    누르는 순간 나라장터에서 받아 압축파일 하나로 묶어 바로 내려 준다(`build_attachment_zip`, 임시 파일, 파일당 100MB·합계 300MB, **g2b.go.kr https 주소만**).
    못 받은 파일 수는 응답 머리글 `X-Bid-File-Failed`로 알린다. 예전에 쌓은 공고(`attachments`가 NULL)는 공고번호로 목록을 다시 읽는다(`g2b.fetch_notice`).
    **여러 공고 한꺼번에**: 표 맨 앞 체크박스(머리글 체크박스 = 이 쪽 전체, 선택은 걸러진 전체 목록 안에서 유지)로 고른 뒤, 화면 전환 버튼 줄 맨 오른쪽의 선택 막대(`#bulkBar`, 공고 목록 화면에서만 보임)에서 [선택한 공고 첨부 받기](POST `/bid-notice/api/files/bundle` `{ids:[…]}`, 한 번에 `MAX_BUNDLE_NOTICES`=30건·전체 600MB까지)를 누르면 `build_bundle_zip`이 공고마다 폴더(`학교명_공고번호/파일`)로 묶은 압축파일 하나를 내려 준다. 첨부 없는 공고는 건너뛰고 건수는 `X-Bid-Notice-Count`·`X-Bid-Notice-Skipped`·`X-Bid-File-Count`·`X-Bid-File-Failed` 머리글로 알린다.
    예전 버전이 압축파일을 두던 `DATA_DIR/bid_notice_files` 폴더는 시작할 때 지운다(`_remove_legacy_files`). 새 열은 `EXTRA_NOTICE_COLUMNS` + `_add_missing_columns`로 붙인다.
  - 테스트: `tests/test_bid_notice.py`(나라장터는 `g2b.urlopen`·`g2b.search_notices`·`g2b.fetch_notice`·`g2b.download_attachment`·`g2b.fetch_award`를 mock, 메일은 `bid_notice._send_mail`을 mock,
    예전 첨부 폴더는 `bid_notice.LEGACY_FILES_DIR`을 임시 폴더로 patch).

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
