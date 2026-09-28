"""멀티TTS 음성 효과: 어른 목소리를 아이 목소리처럼 바꾼다.

OpenAI 음성합성의 기본 목소리는 모두 어른이라 "아이처럼 말해 줘"라는 연기 지시만으로는
말투만 바뀌고 목소리 자체는 어른으로 들린다. 아이 목소리가 어른과 다른 핵심은 작은 몸,
즉 짧은 성도에서 나오는 높은 공명(포먼트)과 높은 음높이다.

여기서는 WSOLA로 말 길이를 늘린 뒤 그만큼 빠르게 다시 읽어(대역 제한 리샘플링)
말 빠르기는 그대로 두고 음높이와 포먼트를 함께 올린다. 성인 여성 목소리를 1.2~1.3배
올리면 초등학교 저학년 아이의 음높이·음색 범위에 들어온다.
"""

from __future__ import annotations

import numpy as np

SAMPLE_RATE = 24000
# 효과 계산 방식이 바뀌면 올린다. 미리듣기 캐시와 화면의 조각 재사용 판단에 쓰인다.
EFFECT_VERSION = 1

CHILD_TARGET_F0 = 300.0          # 7~8살 아이의 평소 말소리 음높이(Hz)
CHILD_SHIFT_RANGE = (1.18, 1.32)  # 약 +2.9 ~ +4.8반음. 더 올리면 만화 캐릭터처럼 들린다.
CHILD_DEFAULT_SHIFT = 1.25


def estimate_f0(samples, sample_rate: int = SAMPLE_RATE, fmin: float = 75.0, fmax: float = 600.0,
                threshold: float = 0.2, max_frames: int = 400) -> float | None:
    """YIN 방식으로 목소리의 대표 음높이(유성음 구간 중앙값, Hz)를 추정한다. 못 찾으면 None."""
    x = np.asarray(samples, dtype=np.float64)
    window = int(sample_rate * 0.03)
    tau_min = max(2, int(sample_rate / fmax))
    tau_max = int(sample_rate / fmin)
    length = window + tau_max
    hop = int(sample_rate * 0.02)
    if x.size < length + hop:
        return None

    # 말소리가 있는 구간만 골라(쉼·무음 제외) 최대 max_frames개만 본다.
    starts = np.arange(0, x.size - length, hop)
    power = np.concatenate(([0.0], np.cumsum(x * x)))
    energy = power[starts + window] - power[starts]
    loud_level = np.percentile(energy, 90)
    if loud_level <= 0:
        return None
    starts = starts[energy > 0.25 * loud_level]
    if starts.size < 5:
        return None
    if starts.size > max_frames:
        starts = starts[np.linspace(0, starts.size - 1, max_frames).astype(np.int64)]
    frames = x[starts[:, None] + np.arange(length)[None, :]]

    # 차이 함수 d(τ) = Σ(x_j - x_{j+τ})² 를 FFT 상관으로 한 번에 계산한다.
    head = frames[:, :window]
    nfft = 1 << int(np.ceil(np.log2(length + window)))
    corr = np.fft.irfft(np.conj(np.fft.rfft(head, nfft)) * np.fft.rfft(frames, nfft), nfft)[:, :tau_max + 1]
    squares = np.concatenate((np.zeros((frames.shape[0], 1)), np.cumsum(frames * frames, axis=1)), axis=1)
    lags = np.arange(tau_max + 1)
    diff = squares[:, [window]] + (squares[:, lags + window] - squares[:, lags]) - 2.0 * corr
    diff[:, 0] = 0.0
    # 누적 평균으로 나눈 차이(CMND): 1보다 충분히 작은 첫 골짜기가 한 주기다.
    cmnd = np.ones_like(diff)
    cmnd[:, 1:] = diff[:, 1:] * lags[1:] / np.maximum(np.cumsum(diff[:, 1:], axis=1), 1e-12)

    estimates = []
    for row in cmnd:
        below = np.flatnonzero(row[tau_min:tau_max] < threshold)
        if below.size == 0:
            continue  # 무성음(ㅅ·ㅎ 같은 소리)이나 잡음
        tau = tau_min + int(below[0])
        while tau + 1 < tau_max and row[tau + 1] < row[tau]:
            tau += 1
        left, middle, right = row[tau - 1], row[tau], row[tau + 1]
        bend = left - 2.0 * middle + right
        offset = 0.5 * (left - right) / bend if bend > 0 else 0.0
        estimates.append(sample_rate / (tau + offset))
    if len(estimates) < 5:
        return None
    return float(np.median(estimates))


def wsola_stretch(samples, ratio: float, frame: int = 768, tolerance: int = 192) -> np.ndarray:
    """음높이는 그대로 두고 길이만 ratio배로 바꾼다(ratio > 1이면 길어진다).

    WSOLA: 출력에 반씩 겹쳐 쌓을 다음 조각을, 원래 위치 ±tolerance 안에서 앞 조각의
    자연스러운 이어짐과 파형이 가장 닮은 곳에서 가져와 음높이 주기가 어긋나지 않게 한다.
    """
    x = np.asarray(samples, dtype=np.float32)
    hop_out = frame // 2
    hop_in = hop_out / ratio
    n_out = int(round(x.size * ratio))
    frame_count = n_out // hop_out + 2
    pad_front = frame + tolerance
    pad_back = 3 * frame + 2 * tolerance
    padded = np.concatenate((np.zeros(pad_front, np.float32), x, np.zeros(pad_back, np.float32)))
    window = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(frame) / frame)).astype(np.float32)
    out = np.zeros(frame_count * hop_out + frame, np.float32)
    span = frame + 2 * tolerance
    nfft = 1 << int(np.ceil(np.log2(span)))
    candidates = 2 * tolerance + 1

    previous = pad_front
    for index in range(frame_count):
        ideal = pad_front + int(round(index * hop_in))
        if index == 0:
            position = ideal
        else:
            template = padded[previous + hop_out:previous + hop_out + frame]
            low = ideal - tolerance
            region = padded[low:low + span]
            scores = np.fft.irfft(np.fft.rfft(region, nfft) * np.conj(np.fft.rfft(template, nfft)), nfft)[:candidates]
            # 소리 크기에 끌려가지 않도록 후보 구간의 에너지로 나눈 상관을 쓴다.
            energy = np.concatenate(([0.0], np.cumsum(region.astype(np.float64) ** 2)))
            scores = scores / np.sqrt(energy[frame:frame + candidates] - energy[:candidates] + 1e-6)
            position = low + int(np.argmax(scores))
        start = index * hop_out
        out[start:start + frame] += window * padded[position:position + frame]
        previous = position
    return out[:n_out]


def _kaiser(u: np.ndarray, beta: float) -> np.ndarray:
    inside = np.clip(1.0 - u * u, 0.0, None)
    return np.where(np.abs(u) <= 1.0, np.i0(beta * np.sqrt(inside)) / np.i0(beta), 0.0)


def resample_by(samples, factor: float, half_taps: int = 16, phases: int = 512,
                block: int = 32768) -> np.ndarray:
    """factor배 빠르게 다시 읽는다: 길이는 1/factor, 음높이·포먼트는 factor배가 된다.

    카이저 창 sinc 보간표로 임의 배율을 처리하고, 빨라지며 나이퀴스트를 넘는 성분은
    미리 걸러 금속성 잡음(앨리어싱)이 생기지 않게 한다. 블록 단위라 메모리를 적게 쓴다.
    """
    x = np.asarray(samples, dtype=np.float32)
    n_out = int(x.size / factor)
    if n_out <= 0:
        return np.zeros(0, np.float32)
    cutoff = min(1.0, 1.0 / factor) * 0.92
    offsets = np.arange(2 * half_taps) - half_taps + 1
    distance = offsets[None, :] - (np.arange(phases + 1) / phases)[:, None]
    kernel = cutoff * np.sinc(cutoff * distance) * _kaiser(distance / half_taps, 8.0)
    kernel = (kernel / kernel.sum(axis=1, keepdims=True)).astype(np.float32)
    padded = np.pad(x, (half_taps, half_taps + 1))

    out = np.empty(n_out, np.float32)
    for start in range(0, n_out, block):
        stop = min(start + block, n_out)
        position = np.arange(start, stop, dtype=np.float64) * factor
        base = np.floor(position).astype(np.int64)
        phase = np.rint((position - base) * phases).astype(np.int64)
        taps = padded[(base + half_taps)[:, None] + offsets[None, :]]
        out[start:stop] = np.einsum("ij,ij->i", taps, kernel[phase])
    return out


def shift_voice(samples, factor: float) -> np.ndarray:
    """말 빠르기와 길이는 그대로 두고 음높이와 포먼트를 factor배로 바꾼다."""
    x = np.asarray(samples, dtype=np.float32)
    if abs(factor - 1.0) < 1e-3 or x.size < 256:
        return x.copy()
    shifted = resample_by(wsola_stretch(x, factor), factor)
    if shifted.size >= x.size:
        return shifted[:x.size]
    return np.pad(shifted, (0, x.size - shifted.size))


def child_shift_factor(samples) -> float:
    """목소리가 원래 높으면 덜, 낮으면 더 올려 아이 음높이에 맞춘다(범위 제한)."""
    f0 = estimate_f0(samples)
    if not f0:
        return CHILD_DEFAULT_SHIFT
    low, high = CHILD_SHIFT_RANGE
    return float(min(max(CHILD_TARGET_F0 / f0, low), high))


def childlike(samples) -> np.ndarray:
    """어른 목소리를 아이 목소리처럼 바꾼다."""
    return shift_voice(samples, child_shift_factor(samples))


EFFECTS = {"child": childlike}


def apply_effect(name: str, samples) -> np.ndarray:
    effect = EFFECTS.get(name or "")
    return effect(samples) if effect else np.asarray(samples, dtype=np.float32)
