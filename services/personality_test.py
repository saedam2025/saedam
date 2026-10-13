"""인성검사 문항·채점·해석.

방과후 강사·직원 선발 때 참고하는 간이 인성검사다. 5점 척도 40문항으로,
다섯 가지 성격 영역(성실성·정서안정성·친화성·개방성·적극성)을 7문항씩 재고
응답 신뢰도(과장 응답) 문항 5개를 섞어 둔다.

※ 선별 참고용 도구이며 임상·법적 진단이 아니다. 문항을 바꾸면 TEST_VERSION을 올린다
  (저장된 답안은 문항 번호 기준이라, 버전이 다르면 예전 문항과 맞지 않기 때문이다).
"""

from __future__ import annotations

TEST_VERSION = 'v1'

LIKERT_LABELS = {
    1: '전혀 그렇지 않다',
    2: '그렇지 않다',
    3: '보통이다',
    4: '그렇다',
    5: '매우 그렇다',
}

# 영역 키 → (이름, 한 줄 설명)
SCALES = {
    'C': ('성실성', '책임감·계획성·꼼꼼함'),
    'S': ('정서안정성', '스트레스·감정 조절'),
    'A': ('친화성', '배려·협력·경청'),
    'O': ('개방성', '창의성·변화 수용'),
    'X': ('적극성', '대인 활력·자기표현'),
}
SCALE_ORDER = ('C', 'S', 'A', 'O', 'X')
VALIDITY_KEY = 'V'
ITEMS_PER_SCALE = 7
VALIDITY_ITEMS = 5

# (영역, 문항, 역문항 여부). 영역별로 7개씩, 신뢰도(V) 5개.
_ITEM_BANK = {
    'C': (
        ('나는 맡은 일은 끝까지 책임지고 마무리한다.', False),
        ('나는 준비물이나 서류를 빠뜨려 곤란해진 적이 자주 있다.', True),
        ('나는 약속 시간과 마감 기한을 잘 지킨다.', False),
        ('나는 일을 하기 전에 계획을 세우고 순서대로 처리한다.', False),
        ('나는 하기 싫은 일은 자꾸 뒤로 미루는 편이다.', True),
        ('나는 사소한 부분까지 꼼꼼하게 확인하는 편이다.', False),
        ('나는 규칙과 절차를 지키는 것이 중요하다고 생각한다.', False),
    ),
    'S': (
        ('나는 예상치 못한 문제가 생겨도 침착하게 대처한다.', False),
        ('나는 사소한 일에도 쉽게 불안해진다.', True),
        ('나는 기분이 상해도 곧 평정을 되찾는다.', False),
        ('나는 비판을 받으면 오랫동안 마음에 담아 둔다.', True),
        ('나는 여러 일이 한꺼번에 몰려도 크게 당황하지 않는다.', False),
        ('나는 화가 나면 감정을 조절하기 어렵다.', True),
        ('나는 대체로 마음이 편안하고 안정되어 있다.', False),
    ),
    'A': (
        ('나는 다른 사람의 이야기를 끝까지 귀 기울여 듣는다.', False),
        ('나는 나에게 손해가 된다면 남의 어려움까지 신경 쓰고 싶지 않다.', True),
        ('나는 의견이 달라도 상대방의 입장을 먼저 이해하려고 한다.', False),
        ('나는 주변 사람이 힘들어 보이면 먼저 도와주려고 한다.', False),
        ('나는 다른 사람의 실수를 날카롭게 따지는 편이다.', True),
        ('나는 동료와 협력해서 일할 때 보람을 느낀다.', False),
        ('나는 나이가 어린 사람이라도 존중하며 대하려고 노력한다.', False),
    ),
    'O': (
        ('나는 새로운 방법을 시도해 보는 것을 좋아한다.', False),
        ('나는 익숙한 방식이 아니면 불편해서 바꾸기 싫다.', True),
        ('나는 수업이나 업무에서 새로운 아이디어를 자주 떠올린다.', False),
        ('나는 하던 대로 하는 것이 가장 안전하다고 생각한다.', True),
        ('나는 새로운 것을 배우는 일이 즐겁다.', False),
        ('나는 계획이 갑자기 바뀌어도 유연하게 적응한다.', False),
        ('나는 나와 다른 생각이나 문화를 접하는 것에 흥미가 있다.', False),
    ),
    'X': (
        ('나는 처음 만난 사람과도 쉽게 대화를 시작한다.', False),
        ('나는 낯선 사람들 틈에 있으면 위축된다.', True),
        ('나는 여러 사람 앞에서 이야기하는 것이 어렵지 않다.', False),
        ('나는 사람들과 어울리기보다 혼자 있는 것이 훨씬 편하다.', True),
        ('나는 모임에서 분위기를 이끄는 편이다.', False),
        ('나는 활기차고 에너지가 넘친다는 말을 자주 듣는다.', False),
        ('나는 필요하면 먼저 나서서 의견을 말한다.', False),
    ),
    'V': (
        ('나는 살아오면서 거짓말을 단 한 번도 한 적이 없다.', False),
        ('나는 다른 사람에게 화가 나거나 짜증이 난 적이 한 번도 없다.', False),
        ('나는 약속을 어긴 적이 단 한 번도 없다.', False),
        ('나는 다른 사람을 험담해 본 적이 전혀 없다.', False),
        ('나는 어떤 상황에서도 실수를 한 적이 없다.', False),
    ),
}


def _build_questions():
    """영역이 번갈아 나오도록 섞고, 신뢰도 문항은 앞쪽 다섯 묶음(5문항씩) 뒤에 하나씩 끼워 넣는다."""
    questions = []
    validity = iter(_ITEM_BANK[VALIDITY_KEY])
    for index in range(ITEMS_PER_SCALE):
        for scale in SCALE_ORDER:
            text, reverse = _ITEM_BANK[scale][index]
            questions.append({'scale': scale, 'text': text, 'reverse': reverse})
        item = next(validity, None)
        if item is not None:
            questions.append({'scale': VALIDITY_KEY, 'text': item[0], 'reverse': item[1]})
    for number, question in enumerate(questions, start=1):
        question['no'] = number
    return questions


QUESTIONS = _build_questions()
QUESTION_COUNT = len(QUESTIONS)
_QUESTION_BY_NO = {question['no']: question for question in QUESTIONS}

# 영역별 해석: 높음 / 보통 / 낮음 문장과, 낮게 나왔을 때 면접에서 확인할 질문.
INTERPRETATION = {
    'C': {
        'high': '책임감이 강하고 계획적이며 마감·약속을 잘 지킵니다. 수업 준비와 행정 서류 처리가 안정적일 것으로 기대됩니다.',
        'mid': '대체로 맡은 일을 해내지만 일정이 몰리면 빠뜨리는 부분이 생길 수 있어 점검 습관이 도움이 됩니다.',
        'low': '계획·마감 관리가 약할 수 있습니다. 준비물이나 서류 제출을 놓친 경험이 있는지 살펴볼 필요가 있습니다.',
        'ask': '맡은 일을 기한 안에 끝내지 못했던 경험과 그때 어떻게 수습했는지 말씀해 주세요.',
    },
    'S': {
        'high': '예상 밖의 상황에서도 침착하고 감정 기복이 적습니다. 학생·학부모 민원 상황에서도 안정적으로 대응할 가능성이 높습니다.',
        'mid': '평소에는 안정적이나 압박이 크거나 비판을 들을 때 일시적으로 흔들릴 수 있습니다.',
        'low': '스트레스나 비판에 예민하게 반응할 수 있습니다. 갈등·민원 상황에서의 대처 방식을 확인해 보세요.',
        'ask': '수업이나 업무 중 크게 당황했거나 화가 났던 일과 마음을 가라앉힌 방법을 말씀해 주세요.',
    },
    'A': {
        'high': '경청하고 배려하며 협력을 즐깁니다. 학생과의 라포 형성과 동료·학교 담당자와의 협업에 강점이 있습니다.',
        'mid': '기본적으로 협조적이나 이해관계가 부딪히면 자기 입장을 우선할 수 있습니다.',
        'low': '타인의 입장보다 자기 판단을 앞세우거나 실수에 엄격할 수 있습니다. 동료·학생과의 갈등 경험을 확인해 보세요.',
        'ask': '동료나 학생과 의견이 크게 달랐던 경험과 어떻게 해결했는지 말씀해 주세요.',
    },
    'O': {
        'high': '새로운 방법을 시도하고 변화에 유연합니다. 프로그램 개발이나 수업 방식 개선에 적극적일 것으로 기대됩니다.',
        'mid': '검증된 방식과 새로운 시도를 균형 있게 활용합니다.',
        'low': '익숙한 방식을 선호하고 변화에 부담을 느낄 수 있습니다. 새 교재·방식 도입 시 적응 경험을 확인해 보세요.',
        'ask': '새로운 수업 방식이나 업무 방법을 도입해 본 경험이 있다면 말씀해 주세요.',
    },
    'X': {
        'high': '사람들 앞에서 자신감 있게 표현하고 분위기를 이끕니다. 학생 지도와 학부모 상담·홍보 상황에 잘 어울립니다.',
        'mid': '상황에 따라 적극적으로 나서기도 하고 한발 물러서 있기도 합니다.',
        'low': '낯선 사람이나 많은 사람 앞에서 위축될 수 있습니다. 학부모 상담·공개수업 상황의 대처를 확인해 보세요.',
        'ask': '낯선 사람이나 여러 사람 앞에서 이야기해야 했던 경험과 어떻게 준비했는지 말씀해 주세요.',
    },
}

# 백분위 환산 점수(0~100) 기준
HIGH_CUTOFF = 70
LOW_CUTOFF = 40
BAND_LABELS = {'high': '높음', 'mid': '보통', 'low': '낮음'}

# 응답 신뢰도 기준
VALIDITY_HIGH_SUM = 21          # 신뢰도 문항 합계(5~25)가 이 이상이면 과장 응답 의심
SAME_ANSWER_LIMIT = 35          # 한 가지 값으로 이만큼 이상 찍으면 성의 없는 응답 의심
FAST_SECONDS = 90               # 이보다 빨리 끝내면 성의 없는 응답 의심(문항당 2초 남짓)

DISCLAIMER = (
    '이 결과는 응답자의 자기보고에 바탕을 둔 선별 참고 자료입니다. 임상적·법적 진단이 아니며, '
    '채용·배치 결정의 단독 근거로 쓰지 말고 면접과 경력 확인을 함께 살펴 주세요.'
)


def question_for_page():
    """응시 화면에 내려 줄 문항(정답 방향·영역은 숨긴다)."""
    return [{'no': question['no'], 'text': question['text']} for question in QUESTIONS]


def validate_answers(raw):
    """화면에서 온 답안을 {번호: 1~5}로 정리한다. 빠지거나 범위를 벗어나면 ValueError."""
    if not isinstance(raw, dict):
        raise ValueError('답안 형식을 확인해 주세요.')
    cleaned = {}
    for question in QUESTIONS:
        value = raw.get(str(question['no']), raw.get(question['no']))
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{question['no']}번 문항에 답해 주세요.")
        if number not in LIKERT_LABELS:
            raise ValueError(f"{question['no']}번 문항의 응답이 올바르지 않습니다.")
        cleaned[question['no']] = number
    return cleaned


def _scored(question, value):
    return 6 - value if question['reverse'] else value


def score_answers(answers):
    """영역별 점수와 신뢰도 점검 결과를 계산한다.

    answers: {문항번호: 1~5}. 반환의 scales[키]는 합계(raw)·0~100 환산(percent)·구간(band).
    """
    sums = {key: 0 for key in SCALE_ORDER}
    validity_sum = 0
    for question in QUESTIONS:
        value = int(answers[question['no']])
        if question['scale'] == VALIDITY_KEY:
            validity_sum += value
        else:
            sums[question['scale']] += _scored(question, value)

    scales = {}
    low_raw = ITEMS_PER_SCALE
    span = ITEMS_PER_SCALE * 4
    for key in SCALE_ORDER:
        percent = round((sums[key] - low_raw) / span * 100)
        if percent >= HIGH_CUTOFF:
            band = 'high'
        elif percent < LOW_CUTOFF:
            band = 'low'
        else:
            band = 'mid'
        scales[key] = {'raw': sums[key], 'percent': percent, 'band': band}
    return {'scales': scales, 'validity_sum': validity_sum}


def reliability_flags(answers, validity_sum, elapsed_sec=None):
    """응답을 얼마나 믿고 볼 수 있는지 점검한다. 의심 사유 목록(빈 목록이면 이상 없음)."""
    flags = []
    if validity_sum >= VALIDITY_HIGH_SUM:
        flags.append('자신을 지나치게 좋게 보이려는 응답 경향(과장 응답)이 있습니다.')
    counts = {}
    for value in answers.values():
        counts[value] = counts.get(value, 0) + 1
    if counts and max(counts.values()) >= SAME_ANSWER_LIMIT:
        flags.append('거의 모든 문항을 같은 값으로 응답했습니다(성의 없는 응답 의심).')
    if elapsed_sec is not None and 0 < int(elapsed_sec) < FAST_SECONDS:
        flags.append(f'응답 시간이 {int(elapsed_sec)}초로 매우 짧습니다(성의 없는 응답 의심).')
    return flags


def build_report(answers, elapsed_sec=None):
    """결과지 화면에 그대로 쓸 수 있는 해석 묶음을 만든다."""
    scored = score_answers(answers)
    scales = []
    for key in SCALE_ORDER:
        name, short = SCALES[key]
        info = scored['scales'][key]
        text = INTERPRETATION[key]
        scales.append({
            'key': key, 'name': name, 'short': short,
            'raw': info['raw'], 'percent': info['percent'], 'band': info['band'],
            'band_label': BAND_LABELS[info['band']],
            'comment': text[info['band']],
        })
    ranked = sorted(scales, key=lambda item: (-item['percent'], SCALE_ORDER.index(item['key'])))
    strengths = [item['name'] for item in ranked[:2] if item['band'] != 'low']
    weak = [item for item in reversed(ranked) if item['band'] == 'low'][:2]
    if not weak:
        weak = [item for item in reversed(ranked) if item['band'] == 'mid'][:1]
    follow_ups = [
        {'name': item['name'], 'question': INTERPRETATION[item['key']]['ask']} for item in weak
    ]

    if strengths:
        summary = f"상대적으로 {'·'.join(strengths)} 영역이 두드러집니다."
    else:
        summary = '뚜렷하게 높은 영역이 없고 전반적으로 보통 이하입니다.'
    if weak and weak[0]['band'] == 'low':
        summary += f" {'·'.join(item['name'] for item in weak)} 영역은 낮게 나와 면접에서 확인해 보시길 권합니다."

    flags = reliability_flags(answers, scored['validity_sum'], elapsed_sec)
    return {
        'scales': scales,
        'summary': summary,
        'follow_ups': follow_ups,
        'validity_sum': scored['validity_sum'],
        'flags': flags,
        'reliable': not flags,
        'disclaimer': DISCLAIMER,
    }


def answer_sheet(answers):
    """답안지 표: 문항 순서대로 응답 값과 글자 라벨, 영역 이름."""
    rows = []
    for question in QUESTIONS:
        value = int(answers.get(question['no'], 0) or 0)
        scale = question['scale']
        rows.append({
            'no': question['no'],
            'text': question['text'],
            'scale_name': '응답신뢰도' if scale == VALIDITY_KEY else SCALES[scale][0],
            'reverse': question['reverse'],
            'value': value,
            'label': LIKERT_LABELS.get(value, '무응답'),
        })
    return rows
