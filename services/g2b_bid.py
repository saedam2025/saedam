"""나라장터 입찰공고정보서비스(조달청 OpenAPI) 조회와 공고 해석.

- 용역 입찰공고를 공고명 낱말로 찾는다(``getBidPblancListInfoServcPPSSrch``).
  공고명 검색은 '그 글자가 그대로 들어 있는 공고'만 찾아 주므로(띄어쓰기까지 같아야 함)
  검색어의 첫 낱말로 넓게 찾은 뒤, 나머지 낱말·지역·기관은 여기서 다시 걸러 낸다.
- 기초금액은 공고번호로 따로 조회한다(``getBidPblancListInfoServcBsisAmount``).
- 공고 한 건을 공고번호로 다시 읽어(``getBidPblancListInfoServc``) 첨부파일 목록을 얻고,
  첨부파일은 나라장터(g2b.go.kr) 주소에서만 내려받는다.
- 낙찰 결과는 **낙찰정보서비스**(``ScsbidInfoService``)에서 공고번호로 찾는다: 최종 낙찰(``getScsbidListSttusServc``)이
  있으면 그것을, 아직 없으면 개찰 결과(``getOpengResultListInfoServc``)의 1순위·진행 상태(유찰 등)를 쓴다.
- 한 번에 조회할 수 있는 기간은 한 달이라 ``MAX_RANGE_DAYS``로 줄여 쓴다.

화면·DB와 무관한 순수 함수만 둔다(테스트에서는 ``urlopen``을 mock 처리한다).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlparse
from urllib.request import Request, urlopen

API_BASE = 'https://apis.data.go.kr/1230000/ad/BidPublicInfoService'
OP_SEARCH = 'getBidPblancListInfoServcPPSSrch'
OP_BASE_AMOUNT = 'getBidPblancListInfoServcBsisAmount'
OP_LIST = 'getBidPblancListInfoServc'
AWARD_API_BASE = 'https://apis.data.go.kr/1230000/as/ScsbidInfoService'
OP_AWARD = 'getScsbidListSttusServc'
OP_OPENING = 'getOpengResultListInfoServc'
STATE_AWARDED = '최종낙찰'
STATE_OPENED = '개찰완료'
PAGE_SIZE = 999
MAX_PAGES = 20
MAX_RANGE_DAYS = 28
REQUEST_TIMEOUT = 40
DOWNLOAD_TIMEOUT = 90
DOWNLOAD_CHUNK = 256 * 1024
ATTACHMENT_SLOTS = 10            # 공고 한 건에 실리는 첨부파일 칸 수(ntceSpecDocUrl1~10)
ATTACHMENT_HOST = 'g2b.go.kr'    # 이 도메인(하위 도메인 포함)의 https 주소만 내려받는다

KIND_CANCEL = '취소공고'
KIND_CHANGE = '변경공고'

# 기관 이름 앞에 붙는 시·도 이름 → 화면에 보여 줄 짧은 이름
PROVINCES = {
    '서울특별시': '서울', '부산광역시': '부산', '대구광역시': '대구', '인천광역시': '인천',
    '광주광역시': '광주', '대전광역시': '대전', '울산광역시': '울산', '세종특별자치시': '세종',
    '경기도': '경기', '강원특별자치도': '강원', '강원도': '강원',
    '충청북도': '충북', '충청남도': '충남',
    '전북특별자치도': '전북', '전라북도': '전북', '전라남도': '전남',
    '경상북도': '경북', '경상남도': '경남', '제주특별자치도': '제주',
}
_PROVINCE_NAMES = sorted(PROVINCES, key=len, reverse=True)
_OFFICE_SUFFIX = re.compile(r'교육(?:지원)?청$')
_SCHOOL_IN_TITLE = re.compile(r'([가-힣A-Za-z0-9]+(?:초등학교|중학교|고등학교|유치원|학교))')
_SCHOOL_SUFFIXES = ('학교', '유치원')

RESULT_MESSAGES = {
    '07': '조회 기간이 너무 깁니다(최대 1개월).',
    '08': '필수 조회 조건이 빠졌습니다.',
    '12': '나라장터 서비스 이용 신청이 되어 있지 않습니다(공공데이터포털에서 입찰공고정보·낙찰정보 서비스 활용신청 확인).',
    '20': '인증키로 이 서비스를 쓸 수 없습니다. 공공데이터포털에서 활용신청 상태를 확인해 주세요.',
    '22': '하루 조회 한도를 넘었습니다. 내일 다시 수집됩니다.',
    '30': '등록되지 않은 인증키입니다. 인증키를 다시 확인해 주세요.',
    '31': '사용 기간이 끝난 인증키입니다.',
}


class G2BError(RuntimeError):
    """나라장터 조회 실패(화면에 그대로 보여 줄 수 있는 안내 문구를 담는다)."""


class SearchPageLimit(G2BError):
    """검색 결과가 한 구간의 페이지 한도를 넘음(기간을 나눠 재조회 가능)."""


# ---------------------------------------------------------------------------
# 조회
# ---------------------------------------------------------------------------
def _error_from_text(raw):
    """JSON이 아닌 응답(인증 오류는 XML로 온다)에서 이유를 찾는다."""
    code = re.search(r'<returnReasonCode>\s*(\d+)\s*</returnReasonCode>', raw)
    if code and code.group(1) in RESULT_MESSAGES:
        return RESULT_MESSAGES[code.group(1)]
    message = re.search(r'<(?:returnAuthMsg|errMsg|resultMsg)>\s*([^<]+?)\s*</', raw)
    if message:
        return f'나라장터 조회 오류: {message.group(1)}'
    return '나라장터에서 알 수 없는 응답을 받았습니다. 잠시 후 다시 시도해 주세요.'


def _request(operation, service_key, params, base=API_BASE):
    """오퍼레이션 하나를 불러 ``body``(items·totalCount)를 돌려준다."""
    key = str(service_key or '').strip()
    if not key:
        raise G2BError('나라장터 인증키가 설정되지 않았습니다.')
    # 공공데이터포털의 예전 'Encoding 키'는 이미 %로 바뀌어 있어 한 번 풀어서 보낸다.
    if '%' in key:
        key = unquote(key)
    query = {'serviceKey': key, 'type': 'json'}
    query.update(params)
    url = f'{base}/{operation}?{urlencode(query)}'
    try:
        with urlopen(url, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read().decode('utf-8', 'replace')
    except HTTPError as error:
        # 주소에 인증키가 들어 있으므로 오류 문구에 주소를 싣지 않는다.
        if error.code in (401, 403):
            raise G2BError('나라장터 인증키를 확인해 주세요(인증 거절).') from None
        if error.code == 429:
            raise G2BError(RESULT_MESSAGES['22']) from None
        raise G2BError(f'나라장터 서버 오류입니다(HTTP {error.code}). 잠시 후 다시 시도해 주세요.') from None
    except (URLError, TimeoutError, OSError):
        raise G2BError('나라장터 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.') from None

    try:
        data = json.loads(raw)
    except ValueError:
        raise G2BError(_error_from_text(raw)) from None
    if not isinstance(data, dict):
        raise G2BError(_error_from_text(raw))
    response = data.get('response')
    if not isinstance(response, dict):
        # 오류 응답은 {"nkoneps.com.response.ResponseError": {"header": {...}}} 모양이다.
        response = next((value for value in data.values() if isinstance(value, dict)), {})
    header = response.get('header') or {}
    code = str(header.get('resultCode') or '')
    if code != '00':
        message = RESULT_MESSAGES.get(code) or f"나라장터 조회 오류: {header.get('resultMsg') or code or '알 수 없음'}"
        raise G2BError(message)
    body = response.get('body') or {}
    items = body.get('items') or []
    if isinstance(items, dict):  # 한 건일 때 {"item": {...}} 로 오는 예전 형식
        items = items.get('item') or []
        if isinstance(items, dict):
            items = [items]
    return {'items': [item for item in items if isinstance(item, dict)],
            'total': to_int(body.get('totalCount')) or 0}


def search_notices(service_key, term, begin, end):
    """공고명에 ``term``이 들어 있는 용역 입찰공고를 게시일시 기간으로 모두 가져온다."""
    return [item for page in iter_search_notices(service_key, term, begin, end) for item in page]


def iter_search_notices(service_key, term, begin, end):
    """조회 페이지를 차례로 전달한다. 일부만 받은 결과를 성공으로 숨기지 않는다."""
    if end - begin > timedelta(days=MAX_RANGE_DAYS):
        begin = end - timedelta(days=MAX_RANGE_DAYS)
    received = 0
    for page in range(1, MAX_PAGES + 1):
        body = _request(OP_SEARCH, service_key, {
            'pageNo': page,
            'numOfRows': PAGE_SIZE,
            'inqryDiv': 1,
            'inqryBgnDt': begin.strftime('%Y%m%d%H%M'),
            'inqryEndDt': end.strftime('%Y%m%d%H%M'),
            'bidNtceNm': term,
        })
        if body['total'] > MAX_PAGES * PAGE_SIZE:
            raise SearchPageLimit('나라장터 조회 페이지 한도를 넘었습니다. 검색 기간을 나눠 다시 조회합니다.')
        items = body['items']
        received += len(items)
        if items:
            yield items
        if not items or received >= body['total']:
            return
    raise SearchPageLimit(
        f'조회 결과 {body["total"]}건 중 {received}건만 받았습니다. '
        '조회 페이지 한도를 넘었습니다. 검색 기간을 줄여 다시 수집해 주세요.'
    )


def fetch_base_amount(service_key, bid_no):
    """공고번호의 기초금액. 아직 공개 전이면 None, 있으면 {'amount', 'opened_at'}."""
    body = _request(OP_BASE_AMOUNT, service_key, {
        'pageNo': 1, 'numOfRows': 20, 'inqryDiv': 2, 'bidNtceNo': bid_no,
    })
    best = None
    for item in body['items']:
        amount = to_int(item.get('bssamt'))
        if not amount:
            continue
        order = str(item.get('bidNtceOrd') or '')
        if best is None or order >= best['order']:
            best = {'order': order, 'amount': amount,
                    'opened_at': str(item.get('bssamtOpenDt') or '').strip()}
    if not best:
        return None
    return {'amount': best['amount'], 'opened_at': best['opened_at']}


def fetch_notice(service_key, bid_no):
    """공고번호로 공고를 다시 읽어 가장 최신 차수 한 줄을 돌려준다(없으면 None)."""
    body = _request(OP_LIST, service_key, {
        'pageNo': 1, 'numOfRows': 50, 'inqryDiv': 2, 'bidNtceNo': bid_no,
    })
    items = [item for item in body['items'] if str(item.get('bidNtceNo') or '') == bid_no]
    if not items:
        return None
    return max(items, key=lambda item: str(item.get('bidNtceOrd') or ''))


def _latest_round(items, bid_no):
    """같은 공고의 여러 줄(정정 차수·재입찰) 중 가장 마지막 것."""
    mine = [item for item in items if str(item.get('bidNtceNo') or '') == bid_no]
    if not mine:
        return None
    return max(mine, key=lambda item: (str(item.get('bidNtceOrd') or ''), str(item.get('rbidNo') or '')))


def fetch_award(service_key, bid_no):
    """공고번호의 낙찰 결과. 아직 개찰 전이면 None.

    {'state', 'winner_name', 'winner_amount', 'winner_rate', 'participants', 'opened_at', 'final_date'}
    - state '최종낙찰': 낙찰자가 확정됨.
    - state '개찰완료': 개찰은 했지만 최종 낙찰 전(업체는 개찰 1순위).
    - 그 밖(유찰·재입찰 등): 나라장터가 알려 준 진행 상태 그대로, 업체 없음.
    """
    query = {'pageNo': 1, 'numOfRows': 50, 'inqryDiv': 4, 'bidNtceNo': bid_no}
    award = _latest_round(_request(OP_AWARD, service_key, query, base=AWARD_API_BASE)['items'], bid_no)
    if award and str(award.get('bidwinnrNm') or '').strip():
        return {
            'state': STATE_AWARDED,
            'winner_name': str(award.get('bidwinnrNm')).strip(),
            'winner_amount': to_int(award.get('sucsfbidAmt')),
            'winner_rate': str(award.get('sucsfbidRate') or '').strip(),
            'participants': to_int(award.get('prtcptCnum')),
            'opened_at': str(award.get('rlOpengDt') or '').strip(),
            'final_date': str(award.get('fnlSucsfDate') or '').strip(),
        }
    opening = _latest_round(_request(OP_OPENING, service_key, query, base=AWARD_API_BASE)['items'], bid_no)
    if not opening:
        return None
    # 개찰 1순위: '업체명^사업자번호^대표자^투찰금액^투찰률'
    first = [part.strip() for part in str(opening.get('opengCorpInfo') or '').split('^')]
    first += [''] * (5 - len(first))
    state = str(opening.get('progrsDivCdNm') or '').strip() or STATE_OPENED
    opened = state == STATE_OPENED and bool(first[0])
    return {
        'state': state,
        'winner_name': first[0] if opened else '',
        'winner_amount': to_int(first[3]) if opened else None,
        'winner_rate': first[4] if opened else '',
        'participants': to_int(opening.get('prtcptCnum')),
        'opened_at': str(opening.get('inptDt') or opening.get('opengDt') or '').strip(),
        'final_date': '',
    }


def is_attachment_url(url):
    parsed = urlparse(str(url or ''))
    host = (parsed.hostname or '').lower()
    return parsed.scheme == 'https' and (host == ATTACHMENT_HOST or host.endswith('.' + ATTACHMENT_HOST))


def download_attachment(url, max_bytes):
    """첨부파일 하나를 내려받아 bytes로 돌려준다. 나라장터 주소가 아니거나 너무 크면 G2BError."""
    if not is_attachment_url(url):
        raise G2BError('나라장터 주소가 아닌 첨부파일은 받지 않습니다.')
    request = Request(url, headers={'User-Agent': 'Mozilla/5.0 (saedam-intranet)'})
    try:
        with urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
            # 다른 곳으로 넘겨 보내는 응답이면 받지 않는다.
            if not is_attachment_url(response.geturl()):
                raise G2BError('나라장터 주소가 아닌 첨부파일은 받지 않습니다.')
            chunks, size = [], 0
            while True:
                chunk = response.read(DOWNLOAD_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise G2BError('첨부파일이 너무 커서 받지 않았습니다.')
                chunks.append(chunk)
    except HTTPError as error:
        raise G2BError(f'첨부파일을 받지 못했습니다(HTTP {error.code}).') from None
    except (URLError, TimeoutError, OSError):
        raise G2BError('나라장터에서 첨부파일을 받지 못했습니다. 잠시 후 다시 시도해 주세요.') from None
    if not size:
        raise G2BError('첨부파일이 비어 있습니다.')
    return b''.join(chunks)


# ---------------------------------------------------------------------------
# 해석
# ---------------------------------------------------------------------------
def to_int(value):
    """'148610000', '1,234', 148610000.0 → 정수. 비었거나 숫자가 아니면 None."""
    text = str(value if value is not None else '').replace(',', '').strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def squash(text):
    """띄어쓰기를 없앤 글(공고마다 띄어쓰기가 달라 비교는 붙여서 한다)."""
    return re.sub(r'\s+', '', str(text or ''))


def clean_words(values, limit=20, max_length=30):
    """검색어·제외어 목록을 정리한다(빈 값·중복 제거, 낱말 사이 빈칸은 한 칸)."""
    if isinstance(values, str):
        values = re.split(r'[,\n]', values)
    result = []
    for value in values or []:
        word = re.sub(r'\s+', ' ', str(value or '')).strip()[:max_length]
        if word and word not in result:
            result.append(word)
    return result[:limit]


def keyword_match(title, keywords):
    """공고명에 맞는 첫 검색어. 검색어를 띄어 쓰면 그 낱말이 모두 들어 있어야 한다."""
    flat = squash(title)
    for keyword in keywords:
        words = keyword.split()
        if words and all(word in flat for word in words):
            return keyword
    return ''


def search_terms(keywords):
    """나라장터에 실제로 물어볼 낱말(검색어마다 첫 낱말, 중복 제거)."""
    terms = []
    for keyword in keywords:
        words = keyword.split()
        if words and words[0] not in terms:
            terms.append(words[0])
    return terms


def region_of(org_name):
    """'경기도교육청 경기도용인교육지원청 도현초등학교' → '경기 용인'."""
    province, district = '', ''
    tokens = str(org_name or '').split()
    for index, token in enumerate(tokens):
        full = next((name for name in _PROVINCE_NAMES if token.startswith(name)), '')
        if not full:
            continue
        province = PROVINCES[full]
        rest = _OFFICE_SUFFIX.sub('', token[len(full):])
        if rest:
            district = rest
        elif token == full and index + 1 < len(tokens) and not district:
            # '경기도 수원시'처럼 시·군이 따로 떨어져 있는 지자체
            following = tokens[index + 1]
            if not following.endswith(_SCHOOL_SUFFIXES):
                district = following
    return f'{province} {district}'.strip()


def school_of(org_name, title=''):
    """수요기관 이름의 마지막 낱말이 학교면 그 이름, 아니면 공고명에서 찾는다."""
    tokens = str(org_name or '').split()
    if tokens and tokens[-1].endswith(_SCHOOL_SUFFIXES):
        return tokens[-1]
    found = _SCHOOL_IN_TITLE.search(str(title or ''))
    return found.group(1) if found else ''


def attachments_of(item):
    """공고에 실린 첨부파일 [{'name', 'url'}] (나라장터 주소만)."""
    result = []
    for slot in range(1, ATTACHMENT_SLOTS + 1):
        url = str(item.get(f'ntceSpecDocUrl{slot}') or '').strip()
        if not is_attachment_url(url):
            continue
        name = str(item.get(f'ntceSpecFileNm{slot}') or '').strip() or f'첨부파일{slot}'
        result.append({'name': name, 'url': url})
    return result


def parse_notice(item):
    """API 한 줄을 우리 테이블 열 이름으로 바꾼다."""
    def text(key):
        return str(item.get(key) or '').strip()

    title = re.sub(r'\s+', ' ', text('bidNtceNm'))
    demand = text('dminsttNm') or text('ntceInsttNm')
    return {
        'bid_no': text('bidNtceNo'),
        'bid_ord': text('bidNtceOrd') or '000',
        'kind': text('ntceKindNm') or '등록공고',
        'title': title,
        'org_name': text('ntceInsttNm'),
        'demand_name': demand,
        'region': region_of(demand) or region_of(text('ntceInsttNm')),
        'school': school_of(demand, title),
        'notice_dt': text('bidNtceDt') or text('rgstDt'),
        'bid_begin': text('bidBeginDt'),
        'bid_close': text('bidClseDt'),
        'open_dt': text('opengDt'),
        'budget': to_int(item.get('asignBdgtAmt')),
        'est_price': to_int(item.get('presmptPrce')),
        'contract_method': text('cntrctCnclsMthdNm'),
        'award_method': text('sucsfbidMthdNm'),
        'official': text('ntceInsttOfclNm'),
        'tel': text('ntceInsttOfclTelNo'),
        'url': text('bidNtceDtlUrl') or text('bidNtceUrl'),
        'change_reason': text('chgNtceRsn'),
        'attachments': json.dumps(attachments_of(item), ensure_ascii=False),
    }


def passes_filters(notice, keywords, exclude_words, region, org_word, org_free_keywords=()):
    """수집 대상인지 본다. 맞으면 맞은 검색어, 아니면 ''.

    ``org_free_keywords``에 맞는 공고는 기관 조건(``org_word``)을 보지 않는다. 온동네 돌봄처럼
    학교가 아니라 교육지원청이 발주하는 사업을 놓치지 않기 위해서다(지역·제외어 조건은 그대로 본다).
    """
    org_free = keyword_match(notice['title'], org_free_keywords)
    keyword = org_free or keyword_match(notice['title'], keywords)
    if not keyword:
        return ''
    flat_title = squash(notice['title'])
    if any(squash(word) in flat_title for word in exclude_words if squash(word)):
        return ''
    org_text = squash(notice['org_name'] + notice['demand_name'])
    if squash(region) and squash(region) not in org_text:
        return ''
    if not org_free and squash(org_word) and squash(org_word) not in org_text + flat_title:
        return ''
    return keyword


def parse_datetime(value):
    """'2025-12-22 10:00:00' / '2025-12-22 10:00' → datetime, 아니면 None."""
    text = str(value or '').strip()
    for pattern, length in (('%Y-%m-%d %H:%M:%S', 19), ('%Y-%m-%d %H:%M', 16), ('%Y-%m-%d', 10)):
        try:
            return datetime.strptime(text[:length], pattern)
        except ValueError:
            continue
    return None
