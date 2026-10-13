"""강사 인성·직무적합성 검사 v2: 학교 현장 상황에 관한 40개 자기보고 문항.

이 점수는 내부 면접 참고용 환산점수이며 표준화 검사나 채용 합격 기준이 아니다.
문항·영역·채점을 바꿀 때는 버전을 올리고 과거 버전을 보존한다.
"""

from __future__ import annotations

from collections import Counter

TEST_VERSION = 'v2'
LIKERT_LABELS = {
    1: '전혀 그렇지 않다', 2: '그렇지 않다', 3: '보통이다',
    4: '그렇다', 5: '매우 그렇다',
}

SCALES = {
    'CH': ('아동 존중·교육윤리', '학생 존중, 공정한 대우'),
    'RS': ('책임감·성실성', '시간 준수, 수업 준비'),
    'EM': ('감정조절', '스트레스와 갈등 대처'),
    'CO': ('의사소통·협력', '학부모·학교·동료와 소통'),
    'PS': ('문제해결 능력', '돌발상황 대응'),
    'ET': ('직업윤리·안전의식', '비밀유지, 아동 안전'),
}
SCALE_ORDER = tuple(SCALES)

# (문항, 역채점 여부). 영역별 8·7·7·6·6·6문항.
_ITEM_BANK = {
    'CH': (
        ('나는 학생의 의견이 내 생각과 달라도 끝까지 듣는다.', False),
        ('나는 학생의 실수를 다른 학생들 앞에서 지적해도 괜찮다고 생각한다.', True),
        ('나는 수업에서 모든 학생에게 공평하게 참여 기회를 준다.', False),
        ('나는 학생의 배경이나 성적에 따라 기대 수준을 다르게 둔다.', True),
        ('나는 학생이 불편함을 표현하면 먼저 그 이유를 확인한다.', False),
        ('나는 학생의 개인적 특성을 존중하며 말과 행동을 조심한다.', False),
        ('나는 수업 규칙을 학생에게도 이해할 수 있게 설명한다.', False),
        ('나는 학생의 작은 성취도 알아보고 격려한다.', False),
    ),
    'RS': (
        ('나는 수업 시작 전에 필요한 자료와 준비물을 점검한다.', False),
        ('나는 수업 시간이나 제출 기한을 자주 놓치는 편이다.', True),
        ('나는 맡은 수업을 끝까지 책임지고 마무리한다.', False),
        ('나는 학생 출결이나 전달 사항을 정확하게 기록한다.', False),
        ('나는 준비가 부족해도 수업 중에 해결하면 된다고 생각한다.', True),
        ('나는 지각이나 결석이 예상되면 담당자에게 미리 알린다.', False),
        ('나는 수업 뒤에 부족했던 부분을 다음 수업에 반영한다.', False),
    ),
    'EM': (
        ('나는 예상치 못한 항의를 받아도 차분하게 대응한다.', False),
        ('나는 화가 나면 학생 앞에서도 감정을 숨기기 어렵다.', True),
        ('나는 수업이 계획대로 되지 않아도 침착하게 다시 정리한다.', False),
        ('나는 지적을 받으면 상대의 의도를 확인한 뒤 답한다.', False),
        ('나는 스트레스를 받으면 사소한 일에도 쉽게 짜증을 낸다.', True),
        ('나는 갈등이 생기면 감정이 가라앉은 뒤 해결 방법을 찾는다.', False),
        ('나는 어려운 상황에서도 학생에게 안정적인 태도를 보인다.', False),
    ),
    'CO': (
        ('나는 학생과 학부모에게 필요한 내용을 이해하기 쉽게 설명한다.', False),
        ('나는 학교 담당자의 요청을 정확히 확인하고 진행 상황을 공유한다.', False),
        ('나는 다른 사람의 의견이 다르면 대화를 피하는 편이다.', True),
        ('나는 문제가 생기면 관련된 사람에게 사실을 먼저 확인한다.', False),
        ('나는 동료 강사와 자료나 경험을 나누며 협력한다.', False),
        ('나는 상대의 말을 듣기보다 내 설명을 먼저 끝내려는 편이다.', True),
    ),
    'PS': (
        ('나는 수업 중 돌발상황이 생기면 학생의 안전부터 확인한다.', False),
        ('나는 준비한 활동이 어려워지면 대체 활동을 생각해 낸다.', False),
        ('나는 문제가 생겼을 때 원인을 살피기보다 누가 잘못했는지부터 따진다.', True),
        ('나는 반복되는 수업 문제를 기록하고 개선 방법을 찾는다.', False),
        ('나는 혼자 해결하기 어려운 일이면 적절한 담당자에게 도움을 요청한다.', False),
        ('나는 예상과 다른 상황이 오면 대응 방법을 바꾸기 어렵다.', True),
    ),
    'ET': (
        ('나는 업무 중 알게 된 학생과 가정의 정보를 필요한 범위에서만 다룬다.', False),
        ('나는 학생의 사진이나 개인 정보를 허락 없이 공유하지 않는다.', False),
        ('나는 수업 전후 교실과 준비물의 안전 상태를 확인한다.', False),
        ('나는 작은 안전사고라면 보고하지 않아도 된다고 생각한다.', True),
        ('나는 아동 보호와 학교의 안전 수칙을 숙지하고 지킨다.', False),
        ('나는 학생에게 위험 징후가 보이면 학교 절차에 따라 알린다.', False),
    ),
}

FOLLOW_UPS = {
    'CH': '학생을 공정하게 대하기 어려웠던 상황과 어떻게 해결했는지 말씀해 주세요.',
    'RS': '수업 준비나 일정에 차질이 생겼을 때 어떻게 대처했는지 말씀해 주세요.',
    'EM': '학생이나 학부모와 갈등이 있었을 때 감정을 어떻게 다뤘는지 말씀해 주세요.',
    'CO': '학교 담당자 또는 학부모와 의견이 달랐던 경험을 말씀해 주세요.',
    'PS': '수업 중 예상치 못한 문제가 생겼을 때 대응한 경험을 말씀해 주세요.',
    'ET': '학생 안전이나 개인정보 보호를 위해 실제로 취한 조치를 말씀해 주세요.',
}


def _build_questions():
    questions = []
    for index in range(8):
        for scale in SCALE_ORDER:
            if index < len(_ITEM_BANK[scale]):
                prompt, reverse = _ITEM_BANK[scale][index]
                questions.append({'no': len(questions) + 1, 'scale': scale,
                                  'text': prompt, 'reverse': reverse})
    return questions


QUESTIONS = _build_questions()
QUESTION_COUNT = len(QUESTIONS)
_BY_NO = {question['no']: question for question in QUESTIONS}
DISCLAIMER = (
    '이 결과는 응시자의 자기보고를 토대로 한 내부 면접 참고 자료입니다. '
    '표준화 검사 점수나 진단 결과가 아니며 채용·배치 결정의 단독 근거로 사용하지 마세요.'
)


def question_for_page():
    return [{'no': q['no'], 'text': q['text']} for q in QUESTIONS]


def validate_answers(raw):
    if not isinstance(raw, dict):
        raise ValueError('답안 형식을 확인해 주세요.')
    cleaned = {}
    for question in QUESTIONS:
        no = question['no']
        value = raw.get(str(no), raw.get(no))
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f'{no}번 문항에 답해 주세요.')
        if number not in LIKERT_LABELS:
            raise ValueError(f'{no}번 문항의 응답이 올바르지 않습니다.')
        cleaned[no] = number
    return cleaned


def score_answers(answers):
    sums = {key: 0 for key in SCALE_ORDER}
    for question in QUESTIONS:
        value = int(answers[question['no']])
        sums[question['scale']] += 6 - value if question['reverse'] else value
    scales = {}
    for key in SCALE_ORDER:
        count = len(_ITEM_BANK[key])
        percent = round((sums[key] - count) / (count * 4) * 100)
        band = 'high' if percent >= 70 else 'low' if percent < 40 else 'mid'
        scales[key] = {'raw': sums[key], 'percent': percent, 'band': band}
    total = round((sum(sums.values()) - QUESTION_COUNT) / (QUESTION_COUNT * 4) * 100)
    return {'scales': scales, 'total': total, 'validity_sum': 0}


def reliability_flags(answers, validity_sum=0, elapsed_sec=None):
    flags = []
    if answers and max(Counter(answers.values()).values()) >= 35:
        flags.append('거의 모든 문항을 같은 값으로 응답했습니다. 면접에서 확인해 주세요.')
    if elapsed_sec is not None and 0 < int(elapsed_sec) < 90:
        flags.append(f'응답 시간이 {int(elapsed_sec)}초로 매우 짧습니다. 면접에서 확인해 주세요.')
    return flags


def build_report(answers, elapsed_sec=None):
    scored = score_answers(answers)
    scales = []
    for key in SCALE_ORDER:
        name, short = SCALES[key]
        info = scored['scales'][key]
        scales.append({'key': key, 'name': name, 'short': short, **info,
                       'band_label': {'high': '높음', 'mid': '보통', 'low': '낮음'}[info['band']],
                       'comment': f'{short}에 관한 자기보고 점수입니다.'})
    ranked = sorted(scales, key=lambda item: item['percent'])
    follow_ups = [{'name': item['name'], 'question': FOLLOW_UPS[item['key']]}
                  for item in ranked[:2] if item['percent'] < 70]
    low = [item['name'] for item in ranked if item['band'] == 'low']
    summary = f"전체 자기보고 환산점수는 {scored['total']}점입니다."
    if low:
        summary += f" {'·'.join(low)} 영역은 상대적으로 낮아 실제 경험을 면접에서 확인해 보세요."
    else:
        summary += ' 영역별 점수와 면접 경험을 함께 확인해 보세요.'
    flags = reliability_flags(answers, elapsed_sec=elapsed_sec)
    return {'scales': scales, 'total': scored['total'], 'summary': summary,
            'follow_ups': follow_ups, 'validity_sum': 0, 'flags': flags,
            'reliable': not flags, 'disclaimer': DISCLAIMER}


def answer_sheet(answers):
    rows = []
    for question in QUESTIONS:
        value = int(answers.get(question['no'], 0) or 0)
        rows.append({'no': question['no'], 'text': question['text'],
                     'scale_name': SCALES[question['scale']][0], 'reverse': question['reverse'],
                     'value': value, 'label': LIKERT_LABELS.get(value, '무응답')})
    return rows
