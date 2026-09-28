/* [통합관리] > 멀티TTS 화면
 *
 * 긴 글과 대화는 조각(문장 묶음·대사 한 줄)마다 따로 음성을 만든 뒤 서버에서 한 파일로 잇는다.
 * 조각마다 "목소리 + 글 + 연기 지시 + 속도"로 서명을 매겨, 바뀐 조각만 다시 만든다.
 */
(() => {
  'use strict';

  const bootNode = document.getElementById('mttsBoot');
  if (!bootNode) return;
  const boot = JSON.parse(bootNode.textContent);
  const limits = boot.limits;
  const presetMap = new Map(boot.presets.map(preset => [preset.key, preset]));

  const CONCURRENCY = 3;
  const CHUNK_CHARS = Math.min(900, limits.segment_chars);
  const SENTENCE_GAP_MS = 250;
  const PAN = 0.4;
  const CHARS_PER_SECOND = 5.5;
  const DRAFT_KEY = 'multiTts.draft.v1';
  const TAB_KEY = 'multiTts.tab';

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  // ------------------------------------------------------------------
  // 공통 도우미
  // ------------------------------------------------------------------

  function h(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'dataset') Object.assign(node.dataset, value);
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else if (value === true) node.setAttribute(key, '');
      else node.setAttribute(key, value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  const icon = name => h('i', { class: `fa-solid ${name}`, 'aria-hidden': 'true' });
  const fmtNumber = value => Number(value || 0).toLocaleString('ko-KR');

  function fmtDuration(ms) {
    const total = Math.max(0, Math.round((ms || 0) / 1000));
    const minutes = Math.floor(total / 60);
    const seconds = total % 60;
    return minutes ? `${minutes}분 ${String(seconds).padStart(2, '0')}초` : `${seconds}초`;
  }

  function debounce(fn, wait) {
    let timer = 0;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), wait);
    };
  }

  function storageGet(key) {
    try { return localStorage.getItem(key); } catch { return null; }
  }

  function storageSet(key, value) {
    try { localStorage.setItem(key, value); } catch { /* 사생활 보호 모드 등에서는 저장하지 않는다 */ }
  }

  let toastTimer = 0;
  function toast(message, kind = 'info') {
    const node = $('#mttsToast');
    node.textContent = message;
    node.dataset.kind = kind;
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.hidden = true; }, kind === 'error' ? 6000 : 3500);
  }

  async function api(url, { method = 'POST', json, form } = {}) {
    const headers = { 'X-CSRF-Token': boot.csrf, 'X-Requested-With': 'XMLHttpRequest' };
    let body;
    if (json !== undefined) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(json);
    } else if (form) {
      body = form;
    }
    let response;
    try {
      response = await fetch(url, { method, headers, body, credentials: 'same-origin' });
    } catch {
      const error = new Error('서버에 연결하지 못했습니다. 네트워크를 확인해 주세요.');
      error.fatal = true;
      throw error;
    }
    let data = null;
    try { data = await response.json(); } catch { /* HTML 오류 화면 등 */ }
    if (!response.ok || !data || data.status !== 'ok') {
      const error = new Error((data && data.message) || `요청을 처리하지 못했습니다. (${response.status})`);
      error.status = response.status;
      error.data = data || {};
      // 키가 없거나 로그인이 풀린 경우는 나머지 조각도 모두 실패하므로 바로 멈춘다.
      error.fatal = [401, 403].includes(response.status) || error.data.code === 'no_api_key';
      throw error;
    }
    return data;
  }

  /** 작업을 최대 CONCURRENCY개씩 동시에 돌린다. 치명적 오류가 나면 남은 작업은 시작하지 않는다. */
  async function runPool(items, worker, onDone) {
    let next = 0;
    let stop = false;
    const errors = [];
    async function lane() {
      while (!stop && next < items.length) {
        const item = items[next++];
        try {
          await worker(item);
        } catch (error) {
          errors.push(error);
          if (error.fatal) stop = true;
        } finally {
          onDone();
        }
      }
    }
    await Promise.all(Array.from({ length: Math.min(CONCURRENCY, items.length) }, lane));
    return errors;
  }

  function autoGrow(textarea) {
    textarea.style.height = 'auto';
    textarea.style.height = `${textarea.scrollHeight + 2}px`;
  }

  function voiceLabel(spec) {
    if (!spec) return '';
    if (spec.preset === 'custom') return '직접 연출';
    return (presetMap.get(spec.preset) || {}).label || '';
  }

  function specError(spec) {
    if (spec.preset === 'custom' && !spec.instructions) {
      return '직접 연출할 목소리의 특징을 적어 주세요.';
    }
    return '';
  }

  /** 서버가 목소리 설정(지시문·효과)을 고치면 바뀌는 값. 서명에 넣어 예전 목소리 조각을 다시 쓰지 않는다. */
  function voiceRev(spec) {
    if (!spec || spec.preset === 'custom') return '';
    return (presetMap.get(spec.preset) || {}).rev || '';
  }

  // ------------------------------------------------------------------
  // 사용량 표시
  // ------------------------------------------------------------------

  const usage = boot.usage;
  function renderUsage() {
    const chip = $('#mttsUsageChip');
    const mine = usage.mine;
    chip.replaceChildren(icon('fa-chart-simple'), ` 이번 달 ${fmtNumber(mine.chars)}자 · ${mine.minutes.toFixed(1)}분`);
    if (usage.all) {
      chip.title = `이번 달 전체 직원: ${fmtNumber(usage.all.chars)}자 · ${usage.all.minutes.toFixed(1)}분`;
    }
  }
  function bumpUsage(chars, durationMs) {
    usage.mine.chars += chars;
    usage.mine.minutes += durationMs / 60000;
    if (usage.all) {
      usage.all.chars += chars;
      usage.all.minutes += durationMs / 60000;
    }
    renderUsage();
  }

  // ------------------------------------------------------------------
  // 재생(미리듣기·대사 듣기가 한 플레이어를 같이 쓴다)
  // ------------------------------------------------------------------

  const player = new Audio();
  let playingButton = null;
  let playingError = null;

  function setPlayState(button, state) {
    if (!button) return;
    button.dataset.state = state;
    const glyph = button.querySelector('i');
    if (glyph) {
      glyph.className = 'fa-solid ' + (
        state === 'loading' ? 'fa-spinner fa-spin' : state === 'playing' ? 'fa-stop' : 'fa-play'
      );
    }
  }

  function stopPlayback() {
    player.pause();
    setPlayState(playingButton, 'idle');
    playingButton = null;
    playingError = null;
  }

  function playUrl(url, button, onError) {
    stopPlayback();
    playingButton = button;
    playingError = onError || null;
    setPlayState(button, 'playing');
    player.src = url;
    player.play().catch(error => {
      // 파일 오류는 'error' 이벤트가 맡고, 다른 음성으로 바꿔 끊긴 경우(AbortError)는 그냥 둔다.
      if (error && error.name === 'NotAllowedError' && playingButton === button) {
        stopPlayback();
        toast('브라우저가 재생을 막았습니다. 한 번 더 눌러 주세요.', 'error');
      }
    });
  }

  function isPlaying(button) {
    return playingButton === button && !player.paused;
  }

  player.addEventListener('ended', stopPlayback);
  player.addEventListener('error', () => {
    const onError = playingError;
    stopPlayback();
    if (onError) onError();
    else toast('음성을 재생하지 못했습니다.', 'error');
  });

  const previewCache = new Map();
  async function previewVoice(spec, button) {
    if (isPlaying(button)) {
      stopPlayback();
      return;
    }
    const problem = specError(spec);
    if (problem) {
      toast(problem, 'error');
      return;
    }
    const cacheKey = JSON.stringify(spec);
    let url = previewCache.get(cacheKey);
    if (!url) {
      stopPlayback();
      setPlayState(button, 'loading');
      try {
        if (spec.preset === 'custom') {
          const data = await api(boot.urls.segments, { json: { voice: spec, text: boot.custom_sample, speed: 1 } });
          bumpUsage(boot.custom_sample.length, data.duration_ms);
          url = data.url;
        } else {
          url = (await api(boot.urls.preview, { json: { preset: spec.preset } })).url;
        }
        previewCache.set(cacheKey, url);
      } catch (error) {
        setPlayState(button, 'idle');
        toast(error.message, 'error');
        return;
      }
    }
    playUrl(url, button, () => {
      previewCache.delete(cacheKey);
      toast('미리듣기 음성이 만료되었습니다. 한 번 더 눌러 주세요.', 'error');
    });
  }

  function voiceSelect(selected) {
    const select = h('select');
    for (const group of boot.groups) {
      const optgroup = h('optgroup', { label: group.label });
      for (const preset of boot.presets.filter(item => item.group === group.key)) {
        optgroup.append(h('option', { value: preset.key, text: `${preset.label} · ${preset.description}` }));
      }
      select.append(optgroup);
    }
    select.append(h('optgroup', { label: '직접' }, h('option', { value: 'custom', text: '직접 연출 (특징을 글로 적기)' })));
    select.value = selected;
    return select;
  }

  function baseVoiceSelect(selected) {
    const select = h('select');
    for (const voice of boot.base_voices) select.append(h('option', { value: voice.key, text: voice.label }));
    select.value = selected || 'marin';
    return select;
  }

  // ------------------------------------------------------------------
  // 진행률 · 결과
  // ------------------------------------------------------------------

  function progressView(root) {
    const bar = $('.mtts-progress-bar span', root);
    const text = $('.mtts-progress-text', root);
    return {
      show() { root.hidden = false; this.set(0, '준비 중…'); },
      hide() { root.hidden = true; },
      set(ratio, message) {
        bar.style.width = `${Math.round(Math.min(1, Math.max(0, ratio)) * 100)}%`;
        text.textContent = message;
      },
    };
  }

  function renderResult(root, clip) {
    root.hidden = false;
    root.replaceChildren(
      h('div', { class: 'mtts-result-head' },
        icon('fa-circle-check'),
        h('div', {},
          h('strong', { text: clip.title }),
          h('span', { text: [clip.duration_label, clip.size_label, clip.summary, clip.stereo ? '입체 음향' : '']
            .filter(Boolean).join(' · ') }))),
      h('audio', { controls: true, preload: 'auto', src: clip.audio_url }),
      h('div', { class: 'mtts-result-actions' },
        h('a', { class: 'mtts-btn primary', href: clip.download_url }, icon('fa-download'), ' WAV 다운로드'),
        h('button', { type: 'button', class: 'mtts-btn', onclick: () => selectTab('library') },
          icon('fa-folder-open'), ' 저장한 음성 보기')),
    );
    root.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  let busy = false;
  function setBusy(value) {
    busy = value;
    for (const button of [$('#mttsSingleBuild'), $('#mttsDialogueBuild')]) button.disabled = value;
  }
  window.addEventListener('beforeunload', event => {
    if (busy) {
      event.preventDefault();
      event.returnValue = '';
    }
  });

  /** 합치기 요청. 서버가 "조각 만료"를 알리면 그 조각만 다시 만든 뒤 한 번 더 시도한다. */
  async function composeClip(buildPayload, regenerate) {
    for (let attempt = 0; ; attempt += 1) {
      try {
        return (await api(boot.urls.clips, { json: buildPayload() })).clip;
      } catch (error) {
        if (attempt === 0 && error.data && error.data.code === 'segment_expired') {
          await regenerate(new Set(error.data.missing || []));
          continue;
        }
        throw error;
      }
    }
  }

  // ------------------------------------------------------------------
  // 탭
  // ------------------------------------------------------------------

  const tabs = $$('.mtts-tabs [role="tab"]');
  function selectTab(name, focus = false) {
    for (const tab of tabs) {
      const active = tab.dataset.tab === name;
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
      document.getElementById(tab.getAttribute('aria-controls')).hidden = !active;
      if (active && focus) tab.focus();
    }
    storageSet(TAB_KEY, name);
    if (name === 'dialogue') $$('#mttsLines textarea').forEach(autoGrow);
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => selectTab(tab.dataset.tab));
    tab.addEventListener('keydown', event => {
      const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
      if (!step) return;
      event.preventDefault();
      selectTab(tabs[(index + step + tabs.length) % tabs.length].dataset.tab, true);
    });
  });

  // ------------------------------------------------------------------
  // 파일에서 글 불러오기
  // ------------------------------------------------------------------

  async function extractFile(file) {
    if (file.size > limits.upload_mb * 1024 * 1024) {
      throw new Error(`파일은 ${limits.upload_mb}MB까지 불러올 수 있습니다.`);
    }
    const form = new FormData();
    form.append('file', file);
    return api(boot.urls.extract, { form });
  }

  // ------------------------------------------------------------------
  // ① 텍스트 → 음성
  // ------------------------------------------------------------------

  const single = {
    preset: 'woman',
    cache: new Map(), // 서명 → 만들어 둔 조각
  };
  const singleText = $('#mttsSingleText');
  const singleSpeed = $('#mttsSingleSpeed');
  const singleGap = $('#mttsSingleGap');
  const customVoice = $('#mttsSingleCustomVoice');
  const customText = $('#mttsSingleCustomText');
  const singleProgress = progressView($('#mttsSingleProgress'));
  boot.base_voices.forEach(voice => customVoice.append(h('option', { value: voice.key, text: voice.label })));
  customVoice.value = 'marin';

  function singleSpec() {
    if (single.preset === 'custom') {
      return { preset: 'custom', voice: customVoice.value, instructions: customText.value.trim() };
    }
    return { preset: single.preset };
  }

  function renderVoiceGrid() {
    const grid = $('#mttsVoiceGrid');
    const card = (key, iconName, label, description, playable) => {
      const pick = h('button', {
        type: 'button', class: 'mtts-voice-pick', 'aria-pressed': String(single.preset === key),
        onclick: () => chooseSingleVoice(key),
      }, icon(iconName), h('span', { class: 'mtts-voice-name', text: label }),
      h('span', { class: 'mtts-voice-desc', text: description }));
      const play = playable ? h('button', {
        type: 'button', class: 'mtts-voice-play', 'aria-label': `${label} 목소리 미리듣기`, title: '미리듣기',
        onclick: event => previewVoice({ preset: key }, event.currentTarget),
      }, icon('fa-play')) : null;
      return h('div', { class: 'mtts-voice', dataset: { key } }, pick, play);
    };
    grid.replaceChildren(
      ...boot.groups.map(group => h('div', { class: 'mtts-voice-group' },
        h('h3', { text: group.label }),
        h('div', { class: 'mtts-voice-list' },
          boot.presets.filter(preset => preset.group === group.key)
            .map(preset => card(preset.key, preset.icon, preset.label, preset.description, true))))),
      h('div', { class: 'mtts-voice-group' },
        h('h3', { text: '직접' }),
        h('div', { class: 'mtts-voice-list' },
          card('custom', 'fa-sliders', '직접 연출', '원하는 특징을 글로 적어 만들기', false))),
    );
  }

  function chooseSingleVoice(key) {
    single.preset = key;
    $$('#mttsVoiceGrid .mtts-voice').forEach(node => {
      $('.mtts-voice-pick', node).setAttribute('aria-pressed', String(node.dataset.key === key));
    });
    $('#mttsSingleCustom').hidden = key !== 'custom';
    $('#mttsSingleVoiceName').textContent = `선택: ${voiceLabel(singleSpec())}`;
    if (key === 'custom') customText.focus();
    saveDraft();
  }

  /** 문단 → 문장 → 900자 이하 조각. 문단 끝 조각에는 문단 쉼이, 나머지에는 짧은 쉼이 붙는다. */
  function chunkText(text) {
    const paragraphs = text.replace(/\r\n?/g, '\n').split(/\n\s*\n/)
      .map(paragraph => paragraph.trim()).filter(Boolean);
    const chunks = [];
    for (const paragraph of paragraphs) {
      const sentences = paragraph.split(/(?<=[.!?。？！…~])\s+|\s*\n\s*/).map(s => s.trim()).filter(Boolean);
      let buffer = '';
      const flush = () => {
        if (buffer) chunks.push({ text: buffer, end: false });
        buffer = '';
      };
      for (let sentence of sentences) {
        while (sentence.length > CHUNK_CHARS) {
          let cut = sentence.lastIndexOf(' ', CHUNK_CHARS);
          if (cut < CHUNK_CHARS * 0.5) cut = CHUNK_CHARS;
          flush();
          chunks.push({ text: sentence.slice(0, cut).trim(), end: false });
          sentence = sentence.slice(cut).trim();
        }
        if (!sentence) continue;
        if (buffer && buffer.length + 1 + sentence.length > CHUNK_CHARS) flush();
        buffer = buffer ? `${buffer}\n${sentence}` : sentence;
      }
      flush();
      if (chunks.length) chunks[chunks.length - 1].end = true;
    }
    return chunks;
  }

  function updateSingleCount() {
    const length = singleText.value.length;
    $('#mttsSingleCount').textContent = `${fmtNumber(length)} / ${fmtNumber(limits.text_chars)}자`;
    const chars = singleText.value.replace(/\s+/g, '').length;
    $('#mttsSingleEstimate').textContent = chars
      ? `예상 길이 약 ${fmtDuration(chars / (CHARS_PER_SECOND * Number(singleSpeed.value)) * 1000)}`
      : '';
  }

  async function loadIntoSingle(file) {
    try {
      toast(`‘${file.name}’에서 글을 읽는 중…`);
      const data = await extractFile(file);
      if (singleText.value.trim() && !confirm('지금 입력된 글을 불러온 내용으로 바꿀까요?')) return;
      singleText.value = data.text;
      if (!$('#mttsSingleTitle').value.trim()) $('#mttsSingleTitle').value = file.name.replace(/\.[^.]+$/, '');
      updateSingleCount();
      saveDraft();
      toast(data.truncated
        ? `글이 길어 앞부분 ${fmtNumber(limits.text_chars)}자만 불러왔습니다. 나머지는 나눠서 만들어 주세요.`
        : `‘${data.filename}’에서 ${fmtNumber(data.chars)}자를 불러왔습니다.`, data.truncated ? 'error' : 'info');
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  async function buildSingle() {
    if (busy) return;
    if (!boot.api_ready) {
      toast('OpenAI API 키가 없어 음성을 만들 수 없습니다. [AI api설정]을 확인해 주세요.', 'error');
      return;
    }
    const chunks = chunkText(singleText.value);
    if (!chunks.length) {
      toast('음성으로 만들 글을 입력해 주세요.', 'error');
      singleText.focus();
      return;
    }
    const spec = singleSpec();
    const problem = specError(spec);
    if (problem) {
      toast(problem, 'error');
      customText.focus();
      return;
    }
    const speed = Number(singleSpeed.value);
    const gap = Number(singleGap.value);
    const jobs = chunks.map(chunk => ({
      ...chunk, sig: JSON.stringify({ v: spec, r: voiceRev(spec), t: chunk.text, s: speed }),
    }));
    const title = $('#mttsSingleTitle').value.trim()
      || singleText.value.trim().split('\n')[0].slice(0, 30) || '텍스트 낭독';

    const generate = async () => {
      const pending = jobs.filter(job => !single.cache.has(job.sig));
      const unique = [...new Map(pending.map(job => [job.sig, job])).values()];
      let done = jobs.length - pending.length;
      const report = () => singleProgress.set(done / jobs.length * 0.92,
        `음성 조각 ${done} / ${jobs.length} 만드는 중… (${voiceLabel(spec)})`);
      report();
      const errors = await runPool(unique, async job => {
        const data = await api(boot.urls.segments, { json: { voice: spec, text: job.text, speed } });
        single.cache.set(job.sig, data);
        bumpUsage(job.text.length, data.duration_ms);
      }, () => {
        done = jobs.filter(job => single.cache.has(job.sig)).length;
        report();
      });
      if (errors.length) throw errors[0];
    };

    stopPlayback();
    setBusy(true);
    $('#mttsSingleResult').hidden = true;
    singleProgress.show();
    try {
      await generate();
      singleProgress.set(0.95, '한 파일로 합치는 중…');
      const clip = await composeClip(() => ({
        mode: 'single',
        title,
        parts: jobs.map(job => ({
          segment_id: single.cache.get(job.sig).segment_id,
          gap_ms: job.end ? gap : SENTENCE_GAP_MS,
          pan: 0,
        })),
        script: { text: singleText.value, voice: spec, speed, gap },
      }), async missing => {
        for (const [sig, data] of single.cache) if (missing.has(data.segment_id)) single.cache.delete(sig);
        await generate();
      });
      singleProgress.hide();
      addClip(clip);
      renderResult($('#mttsSingleResult'), clip);
      toast('음성파일을 만들어 [저장한 음성]에 저장했습니다.');
    } catch (error) {
      singleProgress.hide();
      toast(error.message, 'error');
    } finally {
      setBusy(false);
    }
  }

  // 끌어다 놓기
  const singleDrop = $('#mttsSingleDrop');
  ['dragenter', 'dragover'].forEach(type => singleDrop.addEventListener(type, event => {
    if (![...event.dataTransfer.types].includes('Files')) return;
    event.preventDefault();
    singleDrop.classList.add('is-over');
  }));
  ['dragleave', 'drop'].forEach(type => singleDrop.addEventListener(type, () => singleDrop.classList.remove('is-over')));
  singleDrop.addEventListener('drop', event => {
    const file = event.dataTransfer.files[0];
    if (!file) return;
    event.preventDefault();
    loadIntoSingle(file);
  });
  $('#mttsSingleFile').addEventListener('change', event => {
    const file = event.target.files[0];
    event.target.value = '';
    if (file) loadIntoSingle(file);
  });
  $('#mttsSingleClear').addEventListener('click', () => {
    if (singleText.value.trim() && !confirm('입력한 글을 모두 지울까요?')) return;
    singleText.value = '';
    updateSingleCount();
    saveDraft();
    singleText.focus();
  });
  singleText.addEventListener('input', () => { updateSingleCount(); saveDraft(); });
  singleSpeed.addEventListener('input', () => {
    $('#mttsSingleSpeedOut').textContent = `${Number(singleSpeed.value).toFixed(2).replace(/0$/, '')}배`;
    updateSingleCount();
    saveDraft();
  });
  singleGap.addEventListener('input', () => {
    $('#mttsSingleGapOut').textContent = `${(Number(singleGap.value) / 1000).toFixed(1)}초`;
    saveDraft();
  });
  customText.addEventListener('input', () => saveDraft());
  customVoice.addEventListener('change', () => saveDraft());
  $('#mttsSingleCustomPlay').addEventListener('click', event => previewVoice(singleSpec(), event.currentTarget));
  $('#mttsSingleBuild').addEventListener('click', buildSingle);

  // ------------------------------------------------------------------
  // ② 대화 연출
  // ------------------------------------------------------------------

  const dialogue = {
    cast: {
      A: { name: 'A', preset: 'man', voice: 'cedar', instructions: '' },
      B: { name: 'B', preset: 'woman', voice: 'marin', instructions: '' },
    },
    lines: [],
  };
  let uidSeed = 0;
  const newLine = (speaker, text = '', direction = '') => ({
    uid: `l${++uidSeed}`, speaker, text, direction, seg: null, status: 'idle', error: '',
  });
  const linesRoot = $('#mttsLines');
  const dialogueSpeed = $('#mttsDialogueSpeed');
  const dialogueGap = $('#mttsDialogueGap');
  const dialogueStereo = $('#mttsDialogueStereo');
  const dialogueProgress = progressView($('#mttsDialogueProgress'));
  const otherSlot = slot => (slot === 'A' ? 'B' : 'A');

  function castSpec(slot) {
    const member = dialogue.cast[slot];
    if (member.preset === 'custom') {
      return { preset: 'custom', voice: member.voice, instructions: member.instructions.trim() };
    }
    return { preset: member.preset };
  }
  const castName = slot => dialogue.cast[slot].name.trim() || slot;

  function lineSig(line) {
    const spec = castSpec(line.speaker);
    return JSON.stringify({
      v: spec, r: voiceRev(spec), t: line.text.trim(), d: line.direction.trim(), s: Number(dialogueSpeed.value),
    });
  }
  const lineReady = line => Boolean(line.seg && line.seg.sig === lineSig(line));

  function renderCast() {
    const root = $('#mttsCast');
    root.replaceChildren(...['A', 'B'].map(slot => {
      const member = dialogue.cast[slot];
      const nameInput = h('input', { type: 'text', maxlength: limits.name_chars, value: member.name, placeholder: slot });
      const select = voiceSelect(member.preset);
      const base = baseVoiceSelect(member.voice);
      const traits = h('textarea', { rows: 2, maxlength: limits.custom_chars, placeholder: '예: 40대 무뚝뚝한 형사, 낮고 건조한 목소리' });
      traits.value = member.instructions;
      const custom = h('div', { class: 'mtts-cast-custom', hidden: member.preset !== 'custom' },
        h('label', {}, '기본 목소리', base), h('label', {}, '목소리 특징 · 연기 지시', traits));
      const play = h('button', {
        type: 'button', class: 'mtts-btn sm', title: '미리듣기',
        onclick: event => previewVoice(castSpec(slot), event.currentTarget),
      }, icon('fa-play'), ' 미리듣기');

      nameInput.addEventListener('input', () => {
        member.name = nameInput.value;
        $$(`[data-cast-name="${slot}"]`).forEach(node => { node.textContent = castName(slot); });
        saveDraft();
      });
      select.addEventListener('change', () => {
        member.preset = select.value;
        custom.hidden = member.preset !== 'custom';
        if (member.preset === 'custom') traits.focus();
        refreshLineStatuses();
        saveDraft();
      });
      const onCustom = debounce(() => { refreshLineStatuses(); saveDraft(); }, 300);
      base.addEventListener('change', () => { member.voice = base.value; onCustom(); });
      traits.addEventListener('input', () => { member.instructions = traits.value; onCustom(); });

      return h('div', { class: 'mtts-cast-card', dataset: { slot } },
        h('span', { class: 'mtts-cast-badge', text: slot }),
        h('div', { class: 'mtts-cast-fields' },
          h('label', {}, '이름', nameInput),
          h('label', {}, '목소리', select),
          custom),
        h('div', { class: 'mtts-cast-actions' }, play));
    }));
  }

  function renderLines() {
    linesRoot.replaceChildren(...dialogue.lines.map((line, index) => {
      const text = h('textarea', {
        class: 'mtts-line-text', rows: 1, maxlength: limits.segment_chars,
        placeholder: '대사를 입력하세요', 'aria-label': `${index + 1}번 대사`,
      });
      text.value = line.text;
      const direction = h('input', {
        type: 'text', class: 'mtts-line-direction', maxlength: limits.direction_chars, value: line.direction,
        placeholder: '연기 지시 (선택) · 예: 화내며, 속삭이듯, 울먹이며', 'aria-label': `${index + 1}번 연기 지시`,
      });
      const tool = (act, glyph, label, extra = '') => h('button', {
        type: 'button', class: `mtts-icon-btn ${extra}`.trim(), dataset: { act }, 'aria-label': label, title: label,
      }, icon(glyph));
      return h('li', { class: 'mtts-line', dataset: { uid: line.uid, speaker: line.speaker } },
        h('span', { class: 'mtts-line-no', text: index + 1 }),
        h('button', { type: 'button', class: 'mtts-speaker', dataset: { act: 'swap' }, title: '눌러서 화자 바꾸기' },
          h('b', { text: line.speaker }), h('span', { dataset: { castName: line.speaker }, text: castName(line.speaker) })),
        h('div', { class: 'mtts-line-body' }, text, direction),
        h('div', { class: 'mtts-line-tools' },
          h('span', { class: 'mtts-line-status' }),
          tool('play', 'fa-play', '이 대사 듣기'),
          tool('retake', 'fa-rotate-right', '이 대사 다시 만들기'),
          tool('up', 'fa-arrow-up', '위로'),
          tool('down', 'fa-arrow-down', '아래로'),
          tool('remove', 'fa-xmark', '대사 삭제', 'danger')));
    }));
    $('#mttsLinesEmpty').hidden = dialogue.lines.length > 0;
    $$('textarea', linesRoot).forEach(autoGrow);
    refreshLineStatuses();
  }

  const findLine = node => {
    const item = node.closest('.mtts-line');
    return item ? dialogue.lines.find(line => line.uid === item.dataset.uid) : null;
  };
  const lineNode = line => linesRoot.querySelector(`[data-uid="${line.uid}"]`);

  function updateLineStatus(line) {
    const node = lineNode(line);
    if (!node) return;
    const status = $('.mtts-line-status', node);
    const ready = lineReady(line);
    let state = 'idle';
    let label = line.text.trim() ? '대기' : '';
    if (line.status === 'busy') { state = 'busy'; label = '만드는 중'; }
    else if (line.status === 'error') { state = 'error'; label = '실패'; }
    else if (ready) { state = 'ready'; label = fmtDuration(line.seg.duration_ms); }
    else if (line.seg) { state = 'stale'; label = '고쳐짐'; }
    status.dataset.state = state;
    status.title = line.status === 'error' ? line.error : {
      ready: '만들어 둔 음성이 있습니다', stale: '내용이 바뀌어 다시 만들어야 합니다', busy: '', idle: '',
    }[state] || '';
    status.replaceChildren(
      state === 'busy' ? icon('fa-spinner fa-spin') : state === 'ready' ? icon('fa-circle-check')
        : state === 'error' ? icon('fa-circle-exclamation') : state === 'stale' ? icon('fa-pen') : '',
      label ? ` ${label}` : '');
    node.dataset.state = state;
  }
  const refreshLineStatuses = () => dialogue.lines.forEach(updateLineStatus);

  function addLine(speaker, afterIndex = dialogue.lines.length - 1, focus = true) {
    if (dialogue.lines.length >= limits.dialogue_lines) {
      toast(`대사는 최대 ${limits.dialogue_lines}줄까지 넣을 수 있습니다.`, 'error');
      return;
    }
    const line = newLine(speaker);
    dialogue.lines.splice(afterIndex + 1, 0, line);
    renderLines();
    saveDraft();
    if (focus) $('.mtts-line-text', lineNode(line)).focus();
  }

  async function generateLine(line) {
    const sig = lineSig(line);
    const spec = castSpec(line.speaker);
    const problem = specError(spec);
    if (problem) {
      const error = new Error(`${castName(line.speaker)}: ${problem}`);
      error.fatal = true;
      throw error;
    }
    line.status = 'busy';
    line.error = '';
    updateLineStatus(line);
    try {
      const data = await api(boot.urls.segments, {
        json: { voice: spec, text: line.text, direction: line.direction, speed: Number(dialogueSpeed.value) },
      });
      line.seg = { sig, ...data };
      line.status = 'idle';
      bumpUsage(line.text.trim().length, data.duration_ms);
    } catch (error) {
      line.status = 'error';
      line.error = error.message;
      throw error;
    } finally {
      updateLineStatus(line);
      saveDraft();
    }
  }

  async function playLine(line, button, force = false) {
    if (!force && isPlaying(button)) {
      stopPlayback();
      return;
    }
    if (!line.text.trim()) {
      toast('대사를 먼저 입력해 주세요.', 'error');
      return;
    }
    if (force || !lineReady(line)) {
      if (!boot.api_ready) {
        toast('OpenAI API 키가 없어 음성을 만들 수 없습니다.', 'error');
        return;
      }
      stopPlayback();
      try {
        await generateLine(line);
      } catch (error) {
        toast(error.message, 'error');
        return;
      }
    }
    // 목록이 다시 그려졌을 수 있으니 지금 화면의 버튼을 다시 찾는다.
    const current = lineNode(line);
    const playButton = current ? $('[data-act="play"]', current) : button;
    playUrl(line.seg.url, playButton, () => {
      line.seg = null;
      updateLineStatus(line);
      saveDraft();
      toast('만들어 둔 음성이 만료되었습니다. 듣기를 한 번 더 누르면 다시 만듭니다.', 'error');
    });
  }

  linesRoot.addEventListener('click', event => {
    const button = event.target.closest('[data-act]');
    if (!button) return;
    const line = findLine(button);
    if (!line) return;
    const index = dialogue.lines.indexOf(line);
    switch (button.dataset.act) {
      case 'swap':
        line.speaker = otherSlot(line.speaker);
        renderLines();
        break;
      case 'play':
        playLine(line, button);
        return;
      case 'retake':
        playLine(line, button, true);
        return;
      case 'up':
      case 'down': {
        const target = index + (button.dataset.act === 'up' ? -1 : 1);
        if (target < 0 || target >= dialogue.lines.length) return;
        [dialogue.lines[index], dialogue.lines[target]] = [dialogue.lines[target], dialogue.lines[index]];
        renderLines();
        $(`[data-act="${button.dataset.act}"]`, lineNode(line)).focus();
        break;
      }
      case 'remove':
        if (line.text.trim() && !confirm(`${index + 1}번 대사를 지울까요?`)) return;
        dialogue.lines.splice(index, 1);
        renderLines();
        break;
      default:
        return;
    }
    saveDraft();
  });

  linesRoot.addEventListener('input', event => {
    const line = findLine(event.target);
    if (!line) return;
    if (event.target.classList.contains('mtts-line-text')) {
      line.text = event.target.value;
      autoGrow(event.target);
    } else if (event.target.classList.contains('mtts-line-direction')) {
      line.direction = event.target.value;
    }
    if (line.status === 'error') line.status = 'idle';
    updateLineStatus(line);
    saveDraft();
  });

  linesRoot.addEventListener('keydown', event => {
    if (event.key !== 'Enter' || !(event.ctrlKey || event.metaKey)) return;
    const line = findLine(event.target);
    if (!line) return;
    event.preventDefault();
    addLine(otherSlot(line.speaker), dialogue.lines.indexOf(line));
  });

  $$('[data-add]').forEach(button => button.addEventListener('click', () => addLine(button.dataset.add)));
  $('#mttsScriptClear').addEventListener('click', () => {
    if (!dialogue.lines.length || !confirm('대본을 모두 지울까요?')) return;
    dialogue.lines = [];
    renderLines();
    saveDraft();
  });
  dialogueSpeed.addEventListener('input', () => {
    $('#mttsDialogueSpeedOut').textContent = `${Number(dialogueSpeed.value).toFixed(2).replace(/0$/, '')}배`;
    refreshLineStatuses();
    saveDraft();
  });
  dialogueGap.addEventListener('input', () => {
    $('#mttsDialogueGapOut').textContent = `${(Number(dialogueGap.value) / 1000).toFixed(1)}초`;
    saveDraft();
  });
  dialogueStereo.addEventListener('change', () => saveDraft());

  async function buildDialogue() {
    if (busy) return;
    if (!boot.api_ready) {
      toast('OpenAI API 키가 없어 음성을 만들 수 없습니다. [AI api설정]을 확인해 주세요.', 'error');
      return;
    }
    const lines = dialogue.lines.filter(line => line.text.trim());
    if (!lines.length) {
      toast('대사를 한 줄 이상 입력해 주세요.', 'error');
      return;
    }
    const totalChars = lines.reduce((sum, line) => sum + line.text.trim().length, 0);
    if (totalChars > limits.text_chars) {
      toast(`대사 전체가 ${fmtNumber(totalChars)}자입니다. 한 파일은 ${fmtNumber(limits.text_chars)}자까지 만들 수 있으니 나눠 주세요.`, 'error');
      return;
    }
    for (const slot of new Set(lines.map(line => line.speaker))) {
      const problem = specError(castSpec(slot));
      if (problem) {
        toast(`${castName(slot)}: ${problem}`, 'error');
        return;
      }
    }

    const generate = async () => {
      const pending = lines.filter(line => !lineReady(line));
      const report = () => {
        const done = lines.filter(lineReady).length;
        dialogueProgress.set(done / lines.length * 0.92, `대사 ${done} / ${lines.length} 만드는 중…`);
      };
      report();
      const errors = await runPool(pending, generateLine, report);
      if (errors.length) {
        const fatal = errors.find(error => error.fatal);
        if (fatal) throw fatal;
        throw new Error(`${errors.length}개 대사를 만들지 못했습니다. 빨간색으로 표시된 줄을 확인한 뒤 다시 눌러 주세요. (${errors[0].message})`);
      }
    };

    const gap = Number(dialogueGap.value);
    const stereo = dialogueStereo.checked;
    const title = $('#mttsDialogueTitle').value.trim() || `${castName('A')} · ${castName('B')} 대화`;

    stopPlayback();
    setBusy(true);
    $('#mttsDialogueResult').hidden = true;
    dialogueProgress.show();
    try {
      await generate();
      dialogueProgress.set(0.95, '대사를 이어 붙이는 중…');
      const clip = await composeClip(() => ({
        mode: 'dialogue',
        title,
        stereo,
        parts: lines.map(line => ({
          segment_id: line.seg.segment_id,
          gap_ms: gap,
          pan: stereo ? (line.speaker === 'A' ? -PAN : PAN) : 0,
        })),
        script: {
          cast: {
            A: { name: castName('A'), voice: castSpec('A') },
            B: { name: castName('B'), voice: castSpec('B') },
          },
          lines: dialogue.lines.map(({ speaker, text, direction }) => ({ speaker, text, direction })),
          speed: Number(dialogueSpeed.value),
          gap,
          stereo,
        },
      }), async missing => {
        for (const line of lines) {
          if (line.seg && missing.has(line.seg.segment_id)) line.seg = null;
        }
        refreshLineStatuses();
        await generate();
      });
      dialogueProgress.hide();
      addClip(clip);
      renderResult($('#mttsDialogueResult'), clip);
      toast('대화 음성파일을 만들어 [저장한 음성]에 저장했습니다.');
    } catch (error) {
      dialogueProgress.hide();
      toast(error.message, 'error');
    } finally {
      setBusy(false);
    }
  }
  $('#mttsDialogueBuild').addEventListener('click', buildDialogue);

  // ------------------------------------------------------------------
  // 대본 붙여넣기
  // ------------------------------------------------------------------

  /**
   * "이름: 대사", "[이름] 대사", "이름 : (연기 지시) 대사" 형식을 읽는다.
   * 이름이 A·B·등장인물 이름과 같으면 그 자리에, 처음 보는 이름은 나오는 순서대로 A·B에 배정한다.
   * 이름 없이 시작하는 줄은 바로 앞 대사에 이어 붙인다.
   */
  function parseScript(raw) {
    const slotOf = new Map();
    const rename = {};
    const known = slot => [slot.toLowerCase(), castName(slot).toLowerCase()];
    for (const slot of ['A', 'B']) known(slot).forEach(key => slotOf.set(key, slot));
    const claimed = new Set();
    let extraNames = 0;
    const lines = [];

    for (const rawRow of raw.replace(/\r\n?/g, '\n').split('\n')) {
      const row = rawRow.trim();
      if (!row) continue;
      const match = row.match(/^\[\s*([^\]]{1,20}?)\s*\]\s*[:：]?\s*(.*)$/) || row.match(/^([^:：()\[\]]{1,20}?)\s*[:：]\s*(.*)$/);
      if (!match) {
        if (lines.length) lines[lines.length - 1].text += `\n${row}`;
        else lines.push({ speaker: 'A', text: row, direction: '' });
        continue;
      }
      const name = match[1].trim();
      let body = match[2].trim();
      const key = name.toLowerCase();
      let slot = slotOf.get(key);
      if (!slot) {
        slot = ['A', 'B'].find(candidate => !claimed.has(candidate) && ['A', 'B'].includes(castName(candidate)))
          || ['A', 'B'].find(candidate => !claimed.has(candidate));
        if (slot) {
          if (['A', 'B'].includes(castName(slot))) rename[slot] = name;
        } else {
          slot = extraNames % 2 === 0 ? 'A' : 'B';
          extraNames += 1;
        }
        slotOf.set(key, slot);
      }
      claimed.add(slot);
      let direction = '';
      const acting = body.match(/^\(([^)]{1,60})\)\s*(.*)$/);
      if (acting) {
        direction = acting[1].trim();
        body = acting[2].trim();
      }
      if (body || direction) lines.push({ speaker: slot, text: body, direction });
    }
    return { lines: lines.filter(line => line.text.trim()), rename, extraNames };
  }

  const pasteDialog = $('#mttsPasteDialog');
  const pasteText = $('#mttsPasteText');
  const pasteStatus = $('#mttsPasteStatus');
  $('#mttsScriptPaste').addEventListener('click', () => {
    pasteStatus.textContent = '';
    pasteDialog.showModal();
    pasteText.focus();
  });
  $('#mttsPasteFile').addEventListener('change', async event => {
    const file = event.target.files[0];
    event.target.value = '';
    if (!file) return;
    pasteStatus.textContent = `‘${file.name}’에서 글을 읽는 중…`;
    try {
      const data = await extractFile(file);
      pasteText.value = data.text;
      pasteStatus.textContent = `‘${data.filename}’에서 ${fmtNumber(data.chars)}자를 불러왔습니다.`
        + (data.truncated ? ` (앞부분 ${fmtNumber(limits.text_chars)}자만)` : '');
    } catch (error) {
      pasteStatus.textContent = error.message;
    }
  });
  $('.mtts-dialog-body', pasteDialog).addEventListener('submit', event => {
    if (!event.submitter || event.submitter.value !== 'apply') return;
    const result = parseScript(pasteText.value);
    if (!result.lines.length) {
      event.preventDefault();
      pasteStatus.textContent = '읽을 수 있는 대사가 없습니다. “이름: 대사” 형식인지 확인해 주세요.';
      return;
    }
    const append = $('input[name="pasteMode"]:checked', pasteDialog).value === 'append';
    const room = limits.dialogue_lines - (append ? dialogue.lines.length : 0);
    const incoming = result.lines.slice(0, Math.max(0, room)).map(line => newLine(line.speaker, line.text, line.direction));
    for (const [slot, name] of Object.entries(result.rename)) dialogue.cast[slot].name = name.slice(0, limits.name_chars);
    dialogue.lines = append ? dialogue.lines.concat(incoming) : incoming;
    renderCast();
    renderLines();
    updateCastNames();
    saveDraft();
    pasteText.value = '';
    const notes = [`대사 ${incoming.length}줄을 넣었습니다.`];
    if (result.lines.length > incoming.length) notes.push(`최대 ${limits.dialogue_lines}줄까지만 넣었습니다.`);
    if (result.extraNames) notes.push('등장인물이 3명 이상이라 나머지는 A·B에 번갈아 배정했습니다.');
    toast(notes.join(' '), result.extraNames || result.lines.length > incoming.length ? 'error' : 'info');
  });

  function updateCastNames() {
    for (const slot of ['A', 'B']) $$(`[data-cast-name="${slot}"]`).forEach(node => { node.textContent = castName(slot); });
  }

  // ------------------------------------------------------------------
  // ③ 저장한 음성
  // ------------------------------------------------------------------

  const library = { clips: boot.clips, scope: 'mine' };

  function renderLibrary() {
    const root = $('#mttsLibrary');
    $('#mttsClipCount').textContent = library.scope === 'mine' ? library.clips.length : library.clips.filter(clip => clip.is_mine).length;
    const totalMs = library.clips.reduce((sum, clip) => sum + clip.duration_ms, 0);
    $('#mttsLibrarySummary').textContent = library.clips.length
      ? `${library.clips.length}개 · 모두 ${fmtDuration(totalMs)}`
      : '';
    if (!library.clips.length) {
      root.replaceChildren(h('div', { class: 'mtts-empty' },
        icon('fa-wave-square'),
        h('h3', { text: '아직 저장한 음성이 없습니다.' }),
        h('p', { text: '텍스트 → 음성 또는 대화 연출에서 음성을 만들면 여기에 자동으로 저장됩니다.' })));
      return;
    }
    root.replaceChildren(...library.clips.map(clip => h('article', { class: 'mtts-clip', dataset: { id: clip.id } },
      h('div', { class: `mtts-clip-icon ${clip.mode}` }, icon(clip.mode === 'dialogue' ? 'fa-comments' : 'fa-file-audio')),
      h('div', { class: 'mtts-clip-main' },
        h('h3', { text: clip.title }),
        h('div', { class: 'mtts-clip-meta' },
          h('span', { class: `mtts-badge ${clip.mode}`, text: clip.mode_label }),
          clip.stereo ? h('span', { class: 'mtts-badge', text: '입체' }) : null,
          h('span', { text: clip.summary }),
          h('span', { text: clip.duration_label }),
          h('span', { text: clip.size_label })),
        h('div', { class: 'mtts-clip-sub', text: `${clip.created_by} · ${clip.created_at}` }),
        h('audio', { controls: true, preload: 'none', src: clip.audio_url })),
      h('div', { class: 'mtts-clip-actions' },
        h('a', { class: 'mtts-btn sm primary', href: clip.download_url, title: 'WAV 파일 내려받기' }, icon('fa-download'), ' 다운로드'),
        h('button', { type: 'button', class: 'mtts-btn sm', dataset: { load: clip.id }, title: '대본을 불러와 고쳐 만들기' },
          icon('fa-pen-to-square'), ' 대본 불러오기'),
        h('button', { type: 'button', class: 'mtts-btn sm danger', dataset: { remove: clip.id }, title: '삭제' },
          icon('fa-trash'), ' 삭제')))));
  }

  function addClip(clip) {
    library.clips.unshift(clip);
    renderLibrary();
  }

  async function loadClipScript(clipId) {
    let data;
    try {
      data = await api(`${boot.urls.clips}/${clipId}`, { method: 'GET' });
    } catch (error) {
      toast(error.message, 'error');
      return;
    }
    const { clip, script } = data;
    if (clip.mode === 'dialogue') {
      if (dialogue.lines.some(line => line.text.trim()) && !confirm('지금 작성 중인 대본을 불러온 대본으로 바꿀까요?')) return;
      for (const slot of ['A', 'B']) {
        const member = (script.cast || {})[slot] || {};
        const voice = member.voice || {};
        Object.assign(dialogue.cast[slot], {
          name: member.name || slot,
          preset: voice.preset || dialogue.cast[slot].preset,
          voice: voice.voice || dialogue.cast[slot].voice,
          instructions: voice.instructions || '',
        });
      }
      dialogue.lines = (script.lines || []).slice(0, limits.dialogue_lines)
        .map(line => newLine(line.speaker === 'B' ? 'B' : 'A', line.text || '', line.direction || ''));
      setRange(dialogueSpeed, script.speed);
      setRange(dialogueGap, script.gap);
      dialogueStereo.checked = script.stereo !== false;
      $('#mttsDialogueTitle').value = clip.title;
      $('#mttsDialogueResult').hidden = true;
      renderCast();
      renderLines();
      updateCastNames();
      selectTab('dialogue');
    } else {
      if (singleText.value.trim() && !confirm('지금 입력된 글을 불러온 대본으로 바꿀까요?')) return;
      singleText.value = script.text || '';
      const voice = script.voice || {};
      if (voice.preset === 'custom') {
        customVoice.value = voice.voice || 'marin';
        customText.value = voice.instructions || '';
      }
      chooseSingleVoice(presetMap.has(voice.preset) || voice.preset === 'custom' ? voice.preset : 'woman');
      setRange(singleSpeed, script.speed);
      setRange(singleGap, script.gap);
      $('#mttsSingleTitle').value = clip.title;
      $('#mttsSingleResult').hidden = true;
      updateSingleCount();
      selectTab('single');
    }
    saveDraft();
    toast(`‘${clip.title}’ 대본을 불러왔습니다. 고친 뒤 다시 만들 수 있습니다.`);
  }

  function setRange(input, value) {
    if (value === undefined || value === null || value === '') return;
    input.value = value;
    input.dispatchEvent(new Event('input'));
  }

  $('#mttsLibrary').addEventListener('click', async event => {
    const load = event.target.closest('[data-load]');
    if (load) {
      loadClipScript(load.dataset.load);
      return;
    }
    const remove = event.target.closest('[data-remove]');
    if (!remove) return;
    const clip = library.clips.find(item => String(item.id) === remove.dataset.remove);
    if (!clip || !confirm(`‘${clip.title}’ 음성파일을 삭제할까요?\n삭제하면 되돌릴 수 없습니다.`)) return;
    remove.disabled = true;
    try {
      await api(`${boot.urls.clips}/${clip.id}/delete`);
      library.clips = library.clips.filter(item => item.id !== clip.id);
      renderLibrary();
      toast('삭제했습니다.');
    } catch (error) {
      remove.disabled = false;
      toast(error.message, 'error');
    }
  });

  $$('.mtts-segmented [data-scope]').forEach(button => button.addEventListener('click', async () => {
    const scope = button.dataset.scope;
    if (scope === library.scope) return;
    try {
      const data = await api(`${boot.urls.clips}?scope=${scope}`, { method: 'GET' });
      library.scope = scope;
      library.clips = data.clips;
      $$('.mtts-segmented [data-scope]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
      renderLibrary();
    } catch (error) {
      toast(error.message, 'error');
    }
  }));

  // ------------------------------------------------------------------
  // 작성 중인 내용 임시저장(이 브라우저에만)
  // ------------------------------------------------------------------

  const saveDraft = debounce(() => {
    storageSet(DRAFT_KEY, JSON.stringify({
      single: {
        text: singleText.value, preset: single.preset, customVoice: customVoice.value,
        customText: customText.value, speed: singleSpeed.value, gap: singleGap.value,
        title: $('#mttsSingleTitle').value,
      },
      dialogue: {
        cast: dialogue.cast,
        lines: dialogue.lines.map(({ speaker, text, direction, seg }) => ({ speaker, text, direction, seg })),
        speed: dialogueSpeed.value, gap: dialogueGap.value, stereo: dialogueStereo.checked,
        title: $('#mttsDialogueTitle').value,
      },
    }));
  }, 500);
  $('#mttsSingleTitle').addEventListener('input', () => saveDraft());
  $('#mttsDialogueTitle').addEventListener('input', () => saveDraft());

  function restoreDraft() {
    let draft = null;
    try { draft = JSON.parse(storageGet(DRAFT_KEY) || 'null'); } catch { draft = null; }
    if (!draft) {
      dialogue.lines = [newLine('A'), newLine('B')];
      return;
    }
    const s = draft.single || {};
    singleText.value = s.text || '';
    if (s.customVoice) customVoice.value = s.customVoice;
    customText.value = s.customText || '';
    if (presetMap.has(s.preset) || s.preset === 'custom') single.preset = s.preset;
    setRange(singleSpeed, s.speed);
    setRange(singleGap, s.gap);
    $('#mttsSingleTitle').value = s.title || '';

    const d = draft.dialogue || {};
    for (const slot of ['A', 'B']) {
      const member = (d.cast || {})[slot];
      if (member) Object.assign(dialogue.cast[slot], member);
    }
    dialogue.lines = (d.lines || []).slice(0, limits.dialogue_lines).map(line => {
      const restored = newLine(line.speaker === 'B' ? 'B' : 'A', line.text || '', line.direction || '');
      restored.seg = line.seg || null;
      return restored;
    });
    setRange(dialogueSpeed, d.speed);
    setRange(dialogueGap, d.gap);
    if (typeof d.stereo === 'boolean') dialogueStereo.checked = d.stereo;
    $('#mttsDialogueTitle').value = d.title || '';
  }

  // ------------------------------------------------------------------
  // 시작
  // ------------------------------------------------------------------

  restoreDraft();
  renderUsage();
  renderVoiceGrid();
  chooseSingleVoice(single.preset);
  updateSingleCount();
  renderCast();
  renderLines();
  updateCastNames();
  renderLibrary();
  const savedTab = storageGet(TAB_KEY);
  selectTab(['single', 'dialogue', 'library'].includes(savedTab) ? savedTab : 'single');
})();
