/* 회의센터 · 실시간 회의진행 화면.

   - 왼쪽에서 안건을 고르면 가운데 메인에 안건 내용과 큰 기록 칸이 열린다.
   - 논의·결정 기록과 받아쓰기는 안건마다 따로 쌓이고 자동으로 서버에 저장된다.
   - 녹음은 지금 진행 중인 안건에 붙는다. 녹음 중에 안건을 바꾸면 그 자리에서
     한 회차를 끊어 올리고, 새 안건의 녹음으로 이어서 시작한다.
   - 안건의 자료·녹음·받아쓰기 파일은 아래 [첨부파일] 줄에 모이고,
     누르면 전체화면으로 크게 열린다.                                            */
(() => {
  const root = document.getElementById('mtLive');
  const dataNode = document.getElementById('mtStageData');
  if (!root || !dataNode) return;

  /* 상단 인트라넷 메뉴(z-index 1000)보다 위에 덮으려면 쌓임 맥락 밖으로 빼야 한다. */
  if (root.parentElement !== document.body) document.body.appendChild(root);

  let agendas = [];
  try {
    agendas = JSON.parse(dataNode.textContent || '[]');
  } catch (error) {
    agendas = [];
  }

  const meetingId = root.dataset.meetingId;
  const decisionUrlBase = (root.dataset.decisionUrl || '').replace(/\/0\/decision$/, '/');
  const decisionUrlOf = agenda => decisionUrlBase + agenda.id + '/decision';
  const transcriptUrlBase = (root.dataset.transcriptUrl || '').replace(/\/0\/transcript$/, '/');
  const recordingUrl = root.dataset.recordingUrl;
  const minutesUrl = root.dataset.minutesUrl;

  function transcriptUrlOf(agenda) {
    return transcriptUrlBase + agenda.id + '/transcript';
  }

  const listEl = document.getElementById('mtAgendaList');
  const stageNo = document.getElementById('mtStageNo');
  const stageTitle = document.getElementById('mtStageTitle');
  const stageSummary = document.getElementById('mtStageSummary');
  const stageOwner = document.getElementById('mtStageOwner');
  const agendaCount = document.getElementById('mtAgendaCount');
  const minutesText = document.getElementById('mtMinutesText');
  const decisionText = document.getElementById('mtDecisionText');
  const decisionStatus = document.getElementById('mtDecisionStatus');
  const decisionSave = document.getElementById('mtDecisionSave');
  const decisionState = document.getElementById('mtDecisionState');
  const transcript = document.getElementById('mtTranscript');
  const transcriptState = document.getElementById('mtTranscriptState');
  const interimBox = document.getElementById('mtInterim');
  const attachRow = document.getElementById('mtAttachRow');
  const attachCount = document.getElementById('mtAttachCount');
  const toast = document.getElementById('mtToast');
  const alertBox = document.getElementById('mtAlert');

  let current = null;      // 지금 진행 중인 안건
  let toastTimer = null;
  let sessionLost = false;

  function notify(message, duration) {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('show'), duration || 2200);
  }

  /* 화면 위쪽에 계속 남는 안내줄. 저장이 막힌 상태를 사용자가 놓치지 않게 한다. */
  function showAlert(html) {
    if (!alertBox) { notify(String(html).replace(/<[^>]+>/g, ''), 5000); return; }
    alertBox.innerHTML = html;
    alertBox.hidden = false;
  }

  function clearAlert() {
    if (!alertBox) return;
    alertBox.hidden = true;
    alertBox.innerHTML = '';
  }

  // ------------------------------------------------------- 서버 통신 공통부
  /* 로그인이 풀렸을 때를 일반 오류와 구분한다. 예전에는 서버가 로그인 화면을
     200으로 돌려주어, 실제로는 하나도 저장되지 않았는데도 화면에는 '저장됨'
     이라고 표시됐다. */
  class SessionError extends Error {
    constructor() {
      super('로그인이 풀려 저장하지 못했습니다.');
      this.name = 'SessionError';
    }
  }

  function markSessionLost() {
    if (sessionLost) return;
    sessionLost = true;
    showAlert(
      '<b>로그인이 풀려 저장되지 않고 있습니다.</b> '
      + '<a href="/" target="_blank" rel="noopener">새 창에서 다시 로그인</a>한 뒤 '
      + '이 화면으로 돌아와 [이 안건 기록 저장]을 눌러 주세요. '
      + '적어 두신 내용은 이 화면에 그대로 남아 있습니다.'
    );
  }

  function markSessionBack() {
    if (!sessionLost) return;
    sessionLost = false;
    clearAlert();
    notify('연결이 회복되어 다시 저장하고 있습니다.');
  }

  /* 모든 저장 요청은 이 함수를 통한다.
     - 스크립트 요청임을 알려(X-Requested-With) 서버가 401을 주도록 한다.
     - JSON이 아닌 응답(로그인 화면·오류 화면)은 성공으로 보지 않는다. */
  async function request(url, options) {
    const config = Object.assign({ credentials: 'same-origin', cache: 'no-store' }, options || {});
    config.headers = Object.assign({
      'X-Requested-With': 'XMLHttpRequest',
      Accept: 'application/json',
    }, config.headers || {});

    let response;
    try {
      response = await fetch(url, config);
    } catch (error) {
      throw new Error('서버에 연결하지 못했습니다. 인터넷 연결을 확인해 주세요.');
    }

    let data = null;
    if ((response.headers.get('Content-Type') || '').includes('application/json')) {
      data = await response.json().catch(() => null);
    }
    if (response.status === 401 || (data && data.code === 'login_required')) {
      markSessionLost();
      throw new SessionError();
    }
    if (!response.ok) {
      throw new Error((data && data.message) || `저장하지 못했습니다. (오류 ${response.status})`);
    }
    if (!data) {
      // 200이지만 JSON이 아니면 로그인 화면 등으로 넘어간 것이다.
      markSessionLost();
      throw new SessionError();
    }
    markSessionBack();
    return data;
  }

  function postJson(url, payload) {
    return request(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  }

  // ---------------------------------------------------------------- 안건 목록
  function renderList() {
    listEl.innerHTML = '';
    if (!agendas.length) {
      const empty = document.createElement('p');
      empty.className = 'mt-save-state';
      empty.style.padding = '10px';
      empty.textContent = '등록된 안건이 없습니다. [나가기]에서 안건을 먼저 등록해 주세요.';
      listEl.appendChild(empty);
      return;
    }
    agendas.forEach(agenda => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'mt-agenda-btn' + (current && current.id === agenda.id ? ' is-on' : '');
      button.dataset.agendaId = agenda.id;

      const no = document.createElement('b');
      no.textContent = agenda.no;

      const body = document.createElement('span');
      const title = document.createElement('span');
      title.textContent = agenda.title;
      body.appendChild(title);
      const sub = document.createElement('small');
      const extras = agendaRecordings(agenda).length + (agenda.transcript ? 1 : 0)
        + agenda.materials.length;
      sub.textContent = (agenda.owner ? agenda.owner : '담당 미지정')
        + (extras ? ' · 첨부 ' + extras + '개' : '');
      body.appendChild(sub);

      const dot = document.createElement('span');
      dot.className = 'mt-dot ' + (agenda.decision_status || 'pending');

      button.append(no, body, dot);
      button.addEventListener('click', () => select(agenda.id));
      listEl.appendChild(button);
    });
  }

  // ------------------------------------------------- 첨부파일 줄 · 전체화면 보기
  /* 첨부파일은 세 갈래다.
       1) 안건 자료 : 올린 파일 → 쪽 순서 (이미지·PDF는 쪽마다 그림으로 열린다)
       2) 이 안건 때 만든 녹음
       3) 이 안건의 받아쓰기(.txt)
     화면에는 타일로 늘어놓고, 전체화면 보기에서는 같은 순서로 넘겨 볼 수 있다. */
  const viewer = document.getElementById('mtViewer');
  const viewerStage = document.getElementById('mtViewerStage');
  const viewerTitle = document.getElementById('mtViewerTitle');
  const viewerDown = document.getElementById('mtViewerDown');
  const viewerPrev = document.getElementById('mtViewerPrev');
  const viewerNext = document.getElementById('mtViewerNext');
  const viewerClose = document.getElementById('mtViewerClose');

  let slides = [];        // 전체화면에서 넘겨 볼 장면 목록
  let slidePos = -1;

  function agendaRecordings(agenda) {
    return recordings.filter(item => item.agenda_id === agenda.id);
  }

  function makeTile(kind, iconClass, title, sub, thumbUrl) {
    const tile = document.createElement('button');
    tile.type = 'button';
    tile.className = 'mt-att is-' + kind;
    tile.title = title;
    if (thumbUrl) {
      const image = document.createElement('img');
      image.src = thumbUrl;
      image.alt = title;
      image.loading = 'lazy';
      tile.appendChild(image);
    } else {
      const icon = document.createElement('div');
      icon.className = 'mt-att-icon';
      icon.innerHTML = '<i class="' + iconClass + '"></i>';
      tile.appendChild(icon);
    }
    const cap = document.createElement('span');
    cap.className = 'mt-att-cap';
    cap.textContent = title;
    if (sub) {
      const small = document.createElement('small');
      small.textContent = sub;
      cap.appendChild(small);
    }
    tile.appendChild(cap);
    return tile;
  }

  function renderAttachments() {
    attachRow.innerHTML = '';
    slides = [];
    if (!current) {
      attachCount.textContent = '0';
      return;
    }
    let count = 0;

    // 1) 자료
    current.materials.forEach(material => {
      const pages = material.pages || [];
      count += 1;
      if (!pages.length) {
        const tile = makeTile('file', 'fa-solid fa-file-lines', material.name,
          '미리보기 없음 · 눌러서 열기');
        const start = slides.length;
        slides.push({
          kind: 'file', title: material.name, src: '',
          down: material.url + '?download=1',
        });
        tile.addEventListener('click', () => openViewer(start));
        attachRow.appendChild(tile);
        return;
      }
      const start = slides.length;
      pages.forEach(page => {
        slides.push({
          kind: 'image',
          title: pages.length > 1 ? `${material.name} · ${page.no}/${pages.length}쪽` : material.name,
          src: page.full,
          down: material.url + '?download=1',
        });
      });
      const tile = makeTile('image', 'fa-solid fa-image', material.name,
        pages.length > 1 ? pages.length + '쪽' : '1쪽', pages[0].thumb);
      tile.addEventListener('click', () => openViewer(start));
      attachRow.appendChild(tile);
    });

    // 2) 이 안건의 녹음
    agendaRecordings(current).forEach((item, index) => {
      count += 1;
      const name = '녹음 ' + (index + 1) + '회차';
      const tile = makeTile('audio', 'fa-solid fa-circle-play', name,
        item.length + ' · ' + item.size_kb + 'KB');
      const start = slides.length;
      slides.push({
        kind: 'audio', title: current.title + ' · ' + name,
        src: item.url, down: item.download_url,
      });
      tile.addEventListener('click', () => openViewer(start));
      attachRow.appendChild(tile);
    });

    // 올리지 못한 녹음은 눈에 보이게 두고 다시 올릴 수 있게 한다.
    recPending.filter(item => item.agendaId === current.id).forEach(item => {
      count += 1;
      const tile = makeTile('audio', 'fa-solid fa-rotate-right', '저장 못한 녹음',
        fmt(item.seconds) + ' · 눌러서 다시 올리기');
      tile.classList.add('is-pending');
      tile.addEventListener('click', () => flushPending(true));
      attachRow.appendChild(tile);
    });

    // 3) 받아쓰기 파일 (화면에 적힌 최신 내용을 그대로 보여 준다)
    if (transcript.value.trim() || current.transcript) {
      count += 1;
      const tile = makeTile('text', 'fa-solid fa-file-lines', '받아쓰기.txt',
        transcript.value.length + '자');
      const start = slides.length;
      slides.push({
        kind: 'text', title: current.title + ' · 받아쓰기',
        text: '', live: true, down: current.transcript_url + '?download=1',
      });
      tile.addEventListener('click', () => openViewer(start));
      attachRow.appendChild(tile);
    }

    attachCount.textContent = String(count);
    if (!count) {
      const none = document.createElement('div');
      none.className = 'mt-attach-empty';
      none.textContent = '이 안건에는 첨부된 자료·녹음·받아쓰기가 없습니다. '
        + '녹음이나 받아쓰기를 하면 여기에 파일로 붙습니다.';
      attachRow.appendChild(none);
    }
  }

  function openViewer(index) {
    if (!slides.length) return;
    slidePos = Math.max(0, Math.min(index, slides.length - 1));
    const slide = slides[slidePos];
    viewerStage.innerHTML = '';
    viewerTitle.textContent = slide.title + (slides.length > 1
      ? `  (${slidePos + 1}/${slides.length})` : '');
    viewerDown.href = slide.down || '#';
    viewerDown.hidden = !slide.down;

    if (slide.kind === 'image') {
      const image = document.createElement('img');
      image.src = slide.src;
      image.alt = slide.title;
      viewerStage.appendChild(image);
    } else if (slide.kind === 'audio') {
      const box = document.createElement('div');
      box.className = 'mt-viewer-audio';
      const label = document.createElement('div');
      label.textContent = slide.title;
      const audio = document.createElement('audio');
      audio.controls = true;
      audio.preload = 'metadata';
      audio.src = slide.src;
      box.append(label, audio);
      viewerStage.appendChild(box);
    } else if (slide.kind === 'text') {
      const pre = document.createElement('pre');
      pre.className = 'mt-viewer-text';
      pre.textContent = slide.live ? (transcript.value || '(받아쓰기 기록이 없습니다.)') : slide.text;
      viewerStage.appendChild(pre);
    } else {
      const box = document.createElement('div');
      box.className = 'mt-viewer-audio';
      box.textContent = '이 형식은 화면에서 미리 볼 수 없습니다. 위의 [내려받기]로 열어 주세요.';
      viewerStage.appendChild(box);
    }
    viewerPrev.disabled = slidePos === 0;
    viewerNext.disabled = slidePos === slides.length - 1;
    viewer.hidden = false;
  }

  function closeViewer() {
    viewer.hidden = true;
    viewerStage.innerHTML = '';   // 재생 중인 소리도 함께 멈춘다.
    slidePos = -1;
  }

  viewerPrev.addEventListener('click', () => openViewer(slidePos - 1));
  viewerNext.addEventListener('click', () => openViewer(slidePos + 1));
  viewerClose.addEventListener('click', closeViewer);
  document.addEventListener('keydown', event => {
    if (viewer.hidden) return;
    if (event.key === 'Escape') { event.preventDefault(); closeViewer(); }
    else if (event.key === 'ArrowLeft' && slidePos > 0) openViewer(slidePos - 1);
    else if (event.key === 'ArrowRight' && slidePos < slides.length - 1) openViewer(slidePos + 1);
  });

  function select(agendaId) {
    const agenda = agendas.find(item => item.id === agendaId);
    if (!agenda || (current && current.id === agenda.id)) return;
    // 보던 안건의 입력 내용을 잃지 않도록 옮기기 전에 저장한다.
    if (current && isDirty()) saveDecision(true);
    if (current && transcriptDirty()) saveTranscript(true);
    cancelPendingSaves();

    current = agenda;
    stageNo.textContent = '안건 ' + agenda.no;
    stageTitle.textContent = agenda.title;
    stageSummary.textContent = agenda.summary || '등록된 안건 설명이 없습니다.';
    stageOwner.textContent = agenda.owner ? '담당 : ' + agenda.owner : '';
    minutesText.value = agenda.minutes || '';
    decisionText.value = agenda.decision || '';
    decisionStatus.value = agenda.decision_status || 'pending';
    transcript.value = agenda.transcript || '';
    decisionState.textContent = '';
    transcriptState.textContent = '';
    closeViewer();
    renderAttachments();
    renderList();
    // 녹음 중이면 안건이 바뀐 이 시점에서 회차를 끊어 새 안건에 붙인다.
    rotateRecordingForAgenda();
  }

  // ------------------------------------------------------------ 결정 저장
  function isDirty() {
    if (!current) return false;
    return minutesText.value !== (current.minutes || '')
      || decisionText.value !== (current.decision || '')
      || decisionStatus.value !== (current.decision_status || 'pending');
  }

  async function saveDecision(quiet) {
    if (!current) return false;
    const agenda = current;
    const payload = {
      minutes: minutesText.value,
      decision: decisionText.value,
      decision_status: decisionStatus.value,
    };
    decisionState.textContent = '저장 중…';
    try {
      const data = await request(decisionUrlOf(agenda), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      agenda.minutes = payload.minutes;
      agenda.decision = payload.decision;
      agenda.decision_status = payload.decision_status;
      decisionState.textContent = '저장됨 ' + (data.saved_at || '');
      renderList();
      if (!quiet) notify('안건 기록을 저장했습니다.');
      return true;
    } catch (error) {
      decisionState.textContent = error.name === 'SessionError' ? '로그인 필요' : '저장 실패';
      // 저장에 실패하면 다시 시도한다. 회의 중 잠깐 끊긴 인터넷 때문에
      // 적어 둔 논의·결정이 사라지지 않게 하기 위함이다.
      scheduleDecisionRetry();
      if (!quiet || error.name !== 'SessionError') {
        notify(error.message || '저장 중 오류가 발생했습니다.', 2600);
      }
      return false;
    }
  }

  let decisionTimer = null;
  let decisionRetryTimer = null;
  function scheduleDecisionSave() {
    clearTimeout(decisionTimer);
    decisionState.textContent = '입력 중…';
    decisionTimer = setTimeout(() => saveDecision(true), 1600);
  }
  function scheduleDecisionRetry() {
    clearTimeout(decisionRetryTimer);
    decisionRetryTimer = setTimeout(() => { if (isDirty()) saveDecision(true); }, 8000);
  }
  [minutesText, decisionText].forEach(node => node.addEventListener('input', scheduleDecisionSave));
  decisionStatus.addEventListener('change', () => saveDecision(true));
  decisionSave.addEventListener('click', () => saveDecision(false));

  // ------------------------------------------------------ 회의 중 안건 추가
  const addBtn = document.getElementById('mtAddAgendaBtn');
  const addForm = document.getElementById('mtAddAgendaForm');
  const addTitle = document.getElementById('mtNewAgendaTitle');
  const addSummary = document.getElementById('mtNewAgendaSummary');
  const addCancel = document.getElementById('mtAddCancel');

  function toggleAddForm(open) {
    addForm.hidden = !open;
    addBtn.hidden = open;
    if (open) addTitle.focus();
    else { addTitle.value = ''; addSummary.value = ''; }
  }

  addBtn.addEventListener('click', () => toggleAddForm(true));
  addCancel.addEventListener('click', () => toggleAddForm(false));
  addForm.addEventListener('submit', async event => {
    event.preventDefault();
    const title = addTitle.value.trim();
    if (!title) { addTitle.focus(); return; }
    const submit = addForm.querySelector('button[type=submit]');
    submit.disabled = true;
    try {
      const data = await postJson(`/meeting/${meetingId}/live/agenda`, {
        title, summary: addSummary.value,
      });
      agendas.push(data.agenda);
      if (agendaCount) agendaCount.textContent = agendas.length;
      toggleAddForm(false);
      select(data.agenda.id);
      notify('안건을 추가했습니다.');
    } catch (error) {
      notify(error.message || '안건 추가 중 오류가 발생했습니다.', 2800);
    } finally {
      submit.disabled = false;
    }
  });

  // ------------------------------------------------------------ 받아쓰기 저장
  /* 받아쓰기는 안건마다 따로 쌓인다. 서버에는 마지막으로 받아 간 본문(base)을
     같이 보내, 그 사이 다른 참석자가 적은 내용이 있으면 서버가 두 기록을
     줄 단위로 합쳐 돌려준다(어느 쪽도 지워지지 않는다). */
  let transcriptTimer = null;
  let transcriptRetryTimer = null;

  function transcriptDirty() {
    return !!current && transcript.value !== (current.transcript || '');
  }

  function cancelPendingSaves() {
    clearTimeout(decisionTimer);
    clearTimeout(transcriptTimer);
  }

  async function saveTranscript(quiet) {
    const agenda = current;
    if (!agenda) return true;
    if (agenda.transcriptSaving) return false;
    const sending = transcript.value;
    if (sending === (agenda.transcript || '') && quiet) return true;
    agenda.transcriptSaving = true;
    transcriptState.textContent = '저장 중…';
    try {
      const data = await postJson(transcriptUrlOf(agenda), {
        transcript: sending,
        base: agenda.transcript || '',
      });
      if (data.merged && typeof data.transcript === 'string') {
        // 그 사이 다른 참석자가 적은 내용이 있어 서버가 두 기록을 합쳐 주었다.
        agenda.transcript = data.transcript;
        if (current === agenda) {
          const atBottom = transcript.scrollTop + transcript.clientHeight
            >= transcript.scrollHeight - 8;
          // 보내는 동안 내가 더 친 글자는 지우지 않고 뒤에 남긴다.
          const typedAfter = transcript.value.slice(sending.length);
          transcript.value = data.transcript + typedAfter;
          if (atBottom) transcript.scrollTop = transcript.scrollHeight;
        }
        notify('다른 참석자의 기록과 합쳤습니다.', 2400);
      } else {
        agenda.transcript = sending;
      }
      if (current === agenda) {
        transcriptState.textContent = '저장됨 ' + (data.saved_at || '') + ' · ' + (data.length || 0) + '자';
        renderAttachments();
      }
      clearTimeout(transcriptRetryTimer);
      renderList();
      if (!quiet) notify('받아쓰기를 저장했습니다.');
      return true;
    } catch (error) {
      if (current === agenda) {
        transcriptState.textContent = error.name === 'SessionError'
          ? '로그인 필요 · 저장 안 됨' : '저장 실패 · 다시 시도합니다';
      }
      clearTimeout(transcriptRetryTimer);
      transcriptRetryTimer = setTimeout(() => saveTranscript(true), 8000);
      if (!quiet) notify(error.message || '저장 중 오류가 발생했습니다.', 3000);
      return false;
    } finally {
      agenda.transcriptSaving = false;
    }
  }

  function scheduleTranscriptSave() {
    clearTimeout(transcriptTimer);
    transcriptState.textContent = '입력 중…';
    transcriptTimer = setTimeout(() => saveTranscript(true), 2000);
  }
  transcript.addEventListener('input', scheduleTranscriptSave);
  // 다른 칸으로 넘어갈 때는 기다리지 않고 바로 저장한다.
  transcript.addEventListener('blur', () => { if (transcriptDirty()) saveTranscript(true); });

  /* 화면을 닫거나 탭을 옮길 때 마지막 몇 초의 기록이 사라지지 않도록,
     응답을 기다리지 않는 sendBeacon으로 한 번 더 보낸다. */
  function flushBeacon() {
    if (!current || !navigator.sendBeacon) return;
    try {
      if (transcriptDirty()) {
        const body = new Blob([JSON.stringify({
          transcript: transcript.value, base: current.transcript || '',
        })], { type: 'application/json' });
        if (navigator.sendBeacon(transcriptUrlOf(current), body)) current.transcript = transcript.value;
      }
      if (isDirty()) {
        const body = new Blob([JSON.stringify({
          minutes: minutesText.value, decision: decisionText.value,
          decision_status: decisionStatus.value,
        })], { type: 'application/json' });
        if (navigator.sendBeacon(decisionUrlOf(current), body)) {
          current.minutes = minutesText.value;
          current.decision = decisionText.value;
          current.decision_status = decisionStatus.value;
        }
      }
    } catch (error) { /* 못 보내면 떠나기 전 경고창이 뜬다. */ }
  }

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flushBeacon();
    else {
      if (transcriptDirty()) saveTranscript(true);
      if (isDirty()) saveDecision(true);
    }
  });
  window.addEventListener('pagehide', flushBeacon);

  function appendTranscript(text) {
    const line = String(text || '').trim();
    if (!line || !current) return;
    const prefix = transcript.value && !transcript.value.endsWith('\n') ? '\n' : '';
    const stamp = new Date().toTimeString().slice(0, 5);
    transcript.value += `${prefix}[${stamp}] ${line}\n`;
    transcript.scrollTop = transcript.scrollHeight;
    scheduleTranscriptSave();
  }

  // ------------------------------------------------------------ 음성 인식
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const sttBtn = document.getElementById('mtSttBtn');
  const sttLabel = document.getElementById('mtSttLabel');
  const sttHint = document.getElementById('mtSttHint');
  let recognition = null;
  let sttOn = false;
  let sttRestartTimer = null;
  let sttQuickEnds = 0;       // 아무 말도 못 받고 곧바로 끝난 횟수(연속)
  let sttLastStart = 0;

  if (!SpeechRecognition) {
    sttBtn.disabled = true;
    sttLabel.textContent = '받아쓰기 미지원';
  }

  function showInterim(text) {
    if (!interimBox) return;
    interimBox.textContent = text ? '듣는 중 : ' + text : '';
    interimBox.hidden = !text;
  }

  function sttFinish(message) {
    sttOn = false;
    clearTimeout(sttRestartTimer);
    if (recognition) {
      recognition.onend = null;
      try { recognition.stop(); } catch (error) { /* 무시 */ }
      recognition = null;
    }
    showInterim('');
    sttLabel.textContent = '받아쓰기 시작';
    sttBtn.classList.remove('is-on', 'rec');
    if (sttHint) sttHint.textContent = message;
  }

  function createRecognition() {
    const instance = new SpeechRecognition();
    instance.lang = 'ko-KR';
    instance.continuous = true;
    instance.interimResults = true;     // 말하는 도중에도 인식 중인 글자를 보여 준다.
    instance.maxAlternatives = 1;

    instance.onstart = () => { sttLastStart = Date.now(); };
    instance.onresult = event => {
      let interim = '';
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        const result = event.results[index];
        if (result.isFinal) {
          sttQuickEnds = 0;
          appendTranscript(result[0].transcript);
        } else {
          interim += result[0].transcript;
        }
      }
      showInterim(interim);
    };
    instance.onerror = event => {
      const code = event.error;
      if (code === 'no-speech' || code === 'aborted') return;   // 조용했을 뿐이다. 계속 듣는다.
      if (code === 'not-allowed' || code === 'service-not-allowed') {
        notify('마이크 사용이 차단되어 받아쓰기를 할 수 없습니다. 주소창의 자물쇠에서 마이크를 허용해 주세요.', 4500);
        sttFinish('마이크가 차단되었습니다. 허용한 뒤 [받아쓰기 시작]을 다시 눌러 주세요.');
      } else if (code === 'audio-capture') {
        notify('마이크를 찾지 못했습니다. 연결 상태를 확인해 주세요.', 4000);
        sttFinish('마이크를 찾지 못했습니다.');
      } else if (code === 'network') {
        // 받아쓰기는 브라우저가 음성을 인터넷으로 보내 글자로 바꾼다.
        notify('받아쓰기 서버에 연결하지 못했습니다. 인터넷 연결을 확인해 주세요. 자동으로 다시 시도합니다.', 4000);
      } else if (code === 'language-not-supported') {
        notify('이 브라우저는 한국어 받아쓰기를 지원하지 않습니다.', 4000);
        sttFinish('한국어 받아쓰기를 지원하지 않는 브라우저입니다.');
      }
    };
    // 조용한 구간이 길면 브라우저가 인식을 멈추므로 켜 둔 동안에는 다시 시작한다.
    // 곧바로 계속 끝나는 경우(연결 문제 등)는 간격을 늘려 무한 반복을 막는다.
    instance.onend = () => {
      showInterim('');
      if (!sttOn) return;
      if (Date.now() - sttLastStart < 1500) sttQuickEnds += 1;
      const delay = Math.min(300 + sttQuickEnds * 700, 4000);
      clearTimeout(sttRestartTimer);
      sttRestartTimer = setTimeout(() => {
        if (!sttOn) return;
        try {
          recognition = createRecognition();
          recognition.start();
        } catch (error) {
          sttFinish('받아쓰기가 중단되었습니다. [받아쓰기 시작]을 다시 눌러 주세요.');
        }
      }, delay);
    };
    return instance;
  }

  function startStt() {
    if (!SpeechRecognition || sttOn) return;
    if (!current) {
      notify('먼저 왼쪽에서 안건을 선택해 주세요. 받아쓰기는 선택한 안건에 쌓입니다.', 3200);
      return;
    }
    if (!window.isSecureContext) {
      notify('보안 연결(https)에서만 받아쓰기를 할 수 있습니다.', 4200);
      return;
    }
    sttQuickEnds = 0;
    sttOn = true;
    try {
      recognition = createRecognition();
      recognition.start();
    } catch (error) {
      sttOn = false;
      recognition = null;
      notify('받아쓰기를 시작하지 못했습니다.', 2600);
      return;
    }
    sttLabel.textContent = '받아쓰기 중지';
    sttBtn.classList.add('rec', 'is-on');
    if (sttHint) sttHint.textContent = '받아쓰기 중입니다. 말한 내용이 지금 선택한 안건에 자동으로 적힙니다.';
  }

  function stopStt() {
    sttFinish('받아쓰기를 멈췄습니다. 내용은 그대로 저장됩니다.');
    saveTranscript(true);
  }

  sttBtn.addEventListener('click', () => (sttOn ? stopStt() : startStt()));

  // ------------------------------------------------------------ 녹음
  /* 회의는 길다. 한 번에 다 담아 두었다가 끝날 때 통째로 올리면
     - 브라우저가 소리 전체를 메모리에 물고 있어야 하고,
     - 중간에 창이 닫히거나 업로드가 한 번 실패하면 회의 전체가 사라진다.
     그래서 5분마다, 그리고 안건이 바뀔 때마다 조각(회차)으로 끊어 그때그때
     서버에 올린다. 각 회차는 녹음을 시작한 때의 안건에 붙는다. */
  const REC_SEGMENT_MS = 5 * 60 * 1000;   // 5분마다 한 회차로 끊어 올린다.
  const REC_CHUNK_MS = 2000;              // 2초마다 한 덩어리씩 받아 둔다.

  const recBtn = document.getElementById('mtRecBtn');
  const recLabel = document.getElementById('mtRecLabel');
  const recDot = document.getElementById('mtRecDot');
  const recText = document.getElementById('mtRecText');

  let recorder = null;         // 지금 돌아가는 MediaRecorder
  let recSegmentAgenda = 0;    // 지금 조각이 붙을 안건
  let recStream = null;        // 마이크 입력
  let recStartedAt = 0;        // 이번 조각을 시작한 시각
  let recTotalSeconds = 0;     // 이번 녹음에서 지금까지 담은 시간
  let recTimer = null;         // 화면의 경과시간 표시
  let recRotateTimer = null;   // 조각 나누기 예약
  let recWanted = false;       // 사용자가 [녹음 시작]을 누른 상태인지
  let recUploading = 0;        // 올리는 중인 조각 수
  let recordings = [];
  const recPending = [];       // 올리지 못해 기다리는 조각들

  try {
    recordings = JSON.parse(document.getElementById('mtRecData').textContent || '[]');
  } catch (error) {
    recordings = [];
  }

  /* 브라우저마다 만들 수 있는 형식이 다르다. 크롬·엣지는 webm, 사파리와
     아이폰은 mp4만 된다. 지원하는 형식을 골라 두고 서버에도 알려 준다. */
  function pickMimeType() {
    const candidates = [
      'audio/webm;codecs=opus', 'audio/webm',
      'audio/ogg;codecs=opus', 'audio/ogg',
      'audio/mp4;codecs=mp4a.40.2', 'audio/mp4',
    ];
    if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) return '';
    return candidates.find(type => MediaRecorder.isTypeSupported(type)) || '';
  }

  function extensionFor(mimeType) {
    const base = String(mimeType || '').split(';')[0].toLowerCase();
    if (base.includes('ogg')) return 'ogg';
    if (base.includes('mp4')) return 'm4a';
    if (base.includes('mpeg')) return 'mp3';
    if (base.includes('wav')) return 'wav';
    return 'webm';
  }

  function fmt(seconds) {
    const value = Math.max(0, Math.floor(seconds));
    const mm = String(Math.floor(value / 60)).padStart(2, '0');
    const ss = String(value % 60).padStart(2, '0');
    return mm + ':' + ss;
  }

  function recStateText() {
    if (recWanted) {
      const live = recTotalSeconds + (recStartedAt ? (Date.now() - recStartedAt) / 1000 : 0);
      return '녹음 중 ' + fmt(live)
        + (recUploading ? ' · 저장 중 ' + recUploading + '개' : '')
        + (recPending.length ? ' · 대기 ' + recPending.length + '개' : '');
    }
    if (recUploading) return '녹음 저장 중… (' + recUploading + '개)';
    if (recPending.length) return '저장 못한 녹음 ' + recPending.length + '개';
    if (recordings.length) return '녹음 ' + recordings.length + '회차 저장됨';
    return '녹음 대기';
  }

  function paintRecState() {
    if (recText) recText.textContent = recStateText();
  }

  async function uploadSegment(blob, seconds, mimeType, agendaId) {
    const extension = extensionFor(mimeType);
    const form = new FormData();
    form.append('recording', blob, 'meeting_' + meetingId + '_' + Date.now() + '.' + extension);
    form.append('seconds', String(Math.round(seconds)));
    form.append('mime', mimeType || blob.type || '');
    form.append('agenda_id', String(agendaId || 0));
    const data = await request(recordingUrl, { method: 'POST', body: form });
    recordings = data.recordings || recordings;
    return data;
  }

  /* 조각 하나를 올린다. 실패하면 버리지 않고 대기 목록에 담아 두었다가
     [다시 올리기]를 누르거나 다음 조각을 저장할 때 함께 다시 시도한다. */
  async function saveSegment(blob, seconds, mimeType, agendaId) {
    if (!blob || !blob.size) return;
    recUploading += 1;
    paintRecState();
    try {
      const data = await uploadSegment(blob, seconds, mimeType, agendaId);
      renderAttachments();
      renderList();
      notify('녹음 ' + (data.count || recordings.length) + '회차를 안건 첨부파일로 저장했습니다.');
      flushPending(false);
    } catch (error) {
      recPending.push({
        blob, seconds, mimeType, agendaId, extension: extensionFor(mimeType),
      });
      renderAttachments();
      if (error.name !== 'SessionError') {
        notify(error.message || '녹음을 저장하지 못했습니다. 대기 목록에 담아 두었습니다.', 4000);
      }
    } finally {
      recUploading -= 1;
      paintRecState();
    }
  }

  async function flushPending(announce) {
    if (!recPending.length) return;
    const queue = recPending.splice(0, recPending.length);
    renderAttachments();
    for (const item of queue) {
      recUploading += 1;
      paintRecState();
      try {
        await uploadSegment(item.blob, item.seconds, item.mimeType, item.agendaId);
      } catch (error) {
        recPending.push(item);
        if (announce && error.name !== 'SessionError') {
          notify(error.message || '아직 저장하지 못했습니다.', 3200);
        }
      } finally {
        recUploading -= 1;
      }
    }
    renderAttachments();
    renderList();
    paintRecState();
    if (announce && !recPending.length) notify('밀린 녹음을 모두 저장했습니다.');
  }

  /* MediaRecorder 한 개(=한 회차)를 시작한다. onstop에서 쓰는 값은 모두
     지역 변수로 붙잡아 둔다. 예전에는 정지 직후 공용 recorder 변수를 비워
     버려서 onstop이 돌 때 오류가 났고, 그 때문에 녹음이 한 번도 서버로
     올라가지 못했다. */
  function startSegment() {
    if (!recStream) return;
    const wanted = pickMimeType();
    let instance;
    try {
      instance = wanted
        ? new MediaRecorder(recStream, { mimeType: wanted, audioBitsPerSecond: 96000 })
        : new MediaRecorder(recStream);
    } catch (error) {
      notify('이 브라우저에서 녹음을 시작하지 못했습니다.', 3200);
      stopRecording();
      return;
    }
    const actualMime = instance.mimeType || wanted || 'audio/webm';
    const startedAt = Date.now();
    const agendaId = current ? current.id : 0;   // 이 조각이 붙을 안건
    const chunks = [];

    instance.ondataavailable = event => {
      if (event.data && event.data.size) chunks.push(event.data);
    };
    instance.onerror = () => {
      notify('녹음 중 오류가 발생했습니다. [녹음 시작]을 다시 눌러 주세요.', 3600);
    };
    instance.onstop = () => {
      const seconds = (Date.now() - startedAt) / 1000;
      recTotalSeconds += seconds;
      const blob = new Blob(chunks, { type: actualMime });
      chunks.length = 0;
      if (blob.size) saveSegment(blob, seconds, actualMime, agendaId);
      // 사용자가 아직 녹음 중이면 곧바로 다음 회차를 이어서 시작한다.
      if (recWanted && recStream) startSegment();
      else stopStream();
    };

    recorder = instance;
    recSegmentAgenda = agendaId;
    recStartedAt = startedAt;
    instance.start(REC_CHUNK_MS);

    clearTimeout(recRotateTimer);
    recRotateTimer = setTimeout(() => {
      if (recorder === instance && instance.state === 'recording') instance.stop();
    }, REC_SEGMENT_MS);
  }

  // 녹음 중에 안건을 바꾸면, 지금까지의 소리는 이전 안건 회차로 끊어 올리고 새로 시작한다.
  function rotateRecordingForAgenda() {
    if (!recWanted || !recorder || recorder.state !== 'recording') return;
    const agendaId = current ? current.id : 0;
    if (agendaId === recSegmentAgenda) return;
    try { recorder.stop(); } catch (error) { /* onstop이 이어서 처리 */ }
  }

  function stopStream() {
    if (recStream) {
      recStream.getTracks().forEach(track => track.stop());
      recStream = null;
    }
  }

  let recStarting = false;

  async function startRecording() {
    if (recStarting) return;          // 마이크 허용을 기다리는 동안의 두 번 누름 방지
    if (!window.isSecureContext) {
      notify('보안 연결(https)에서만 녹음할 수 있습니다. 주소가 https로 시작하는지 확인해 주세요.', 4200);
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) {
      notify('이 브라우저에서는 녹음을 지원하지 않습니다. 크롬이나 엣지에서 열어 주세요.', 3600);
      return;
    }
    if (!current) {
      notify('먼저 왼쪽에서 안건을 선택해 주세요. 녹음은 선택한 안건에 첨부됩니다.', 3200);
      return;
    }
    recStarting = true;
    try {
      recStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      });
    } catch (error) {
      const denied = error && (error.name === 'NotAllowedError' || error.name === 'SecurityError');
      notify(denied
        ? '마이크 사용을 허용해야 녹음할 수 있습니다. 주소창의 자물쇠에서 마이크를 허용해 주세요.'
        : '마이크를 찾지 못했습니다. 연결 상태를 확인해 주세요.', 4000);
      recStream = null;
      return;
    } finally {
      recStarting = false;
    }

    recWanted = true;
    recTotalSeconds = 0;
    startSegment();
    if (!recorder) { recWanted = false; stopStream(); paintRecState(); return; }

    recDot.classList.add('is-on');
    recBtn.classList.add('is-on');
    recLabel.textContent = '녹음 정지';
    clearInterval(recTimer);
    recTimer = setInterval(paintRecState, 1000);
    paintRecState();
  }

  function stopRecording() {
    recWanted = false;
    clearTimeout(recRotateTimer);
    clearInterval(recTimer);
    recStartedAt = 0;
    // 남은 소리는 onstop이 모아 서버로 올린다. 마이크 정리도 거기서 한다.
    if (recorder && recorder.state !== 'inactive') {
      try { recorder.stop(); } catch (error) { stopStream(); }
    } else {
      stopStream();
    }
    recorder = null;
    recDot.classList.remove('is-on');
    recBtn.classList.remove('is-on');
    recLabel.textContent = '녹음 시작';
    paintRecState();
  }

  function isRecording() {
    return recWanted;
  }

  recBtn.addEventListener('click', () => (isRecording() ? stopRecording() : startRecording()));

  // ------------------------------------------------------------ AI 회의록
  const minutesBtn = document.getElementById('mtMinutesBtn');
  if (minutesBtn) {
    minutesBtn.addEventListener('click', async () => {
      if (!confirm('지금까지 기록한 안건별 논의·결정과 받아쓰기 내용으로 AI 회의록을 만들까요?\n실행항목은 메인화면 달력에도 자동으로 등록됩니다.')) return;
      if (sttOn) stopStt();
      if (isRecording()) stopRecording();
      // 안건 기록과 받아쓰기를 먼저 확실히 저장한 뒤에 회의록을 만든다.
      if (isDirty() && !(await saveDecision(true))) {
        notify('안건 기록을 저장하지 못해 회의록 작성을 멈췄습니다.', 4000);
        return;
      }
      if (!(await saveTranscript(true))) {
        notify('받아쓰기를 저장하지 못해 회의록 작성을 멈췄습니다.', 4000);
        return;
      }

      minutesBtn.disabled = true;
      const original = minutesBtn.innerHTML;
      minutesBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i><span>회의록 작성 중…</span>';
      notify('AI가 회의록을 작성하고 있습니다. 잠시만 기다려 주세요.', 6000);
      try {
        const data = await postJson(minutesUrl, {});
        notify(`회의록을 만들었습니다. 실행항목 ${data.tasks_added || 0}건을 달력에 등록했습니다.`, 2600);
        setTimeout(() => { window.location.href = data.redirect; }, 900);
      } catch (error) {
        minutesBtn.disabled = false;
        minutesBtn.innerHTML = original;
        notify(error.message || '회의록 작성 중 오류가 발생했습니다.', 5000);
      }
    });
  }

  // ------------------------------------------------------------ 전체화면
  const fullBtn = document.getElementById('mtFullBtn');

  function nativeFullscreen() {
    return document.fullscreenElement || document.webkitFullscreenElement || null;
  }

  fullBtn.addEventListener('click', () => {
    if (nativeFullscreen()) {
      const exit = document.exitFullscreen || document.webkitExitFullscreen;
      if (exit) { try { exit.call(document); } catch (error) { /* 이미 나감 */ } }
      return;
    }
    const request = root.requestFullscreen || root.webkitRequestFullscreen;
    if (!request) return;
    try { request.call(root, { navigationUI: 'hide' }); } catch (error) { /* 지원 안 함 */ }
  });
  ['fullscreenchange', 'webkitfullscreenchange'].forEach(type => {
    document.addEventListener(type, () => {
      fullBtn.innerHTML = nativeFullscreen()
        ? '<i class="fa-solid fa-compress"></i>'
        : '<i class="fa-solid fa-expand"></i>';
    });
  });

  // 모바일 주소창이 접히고 펴질 때마다 실제 보이는 높이에 맞춘다.
  if (window.visualViewport) {
    const syncViewport = () => root.style.setProperty('--mt-vh', window.visualViewport.height + 'px');
    window.visualViewport.addEventListener('resize', syncViewport);
    syncViewport();
  }

  // 저장하지 않은 기록이 있으면 화면을 떠나기 전에 알린다.
  // 받아쓰기·올리지 못한 녹음·녹음 중 상태까지 모두 확인한다.
  window.addEventListener('beforeunload', event => {
    if (!isDirty() && !transcriptDirty() && !recPending.length
        && !isRecording() && !recUploading) return;
    event.preventDefault();
    event.returnValue = '';
  });

  // 인터넷이 다시 붙으면 밀려 있던 저장을 스스로 이어서 끝낸다.
  window.addEventListener('online', () => {
    if (transcriptDirty()) saveTranscript(true);
    if (isDirty()) saveDecision(true);
    flushPending(true);
  });
  window.addEventListener('offline', () => {
    showAlert('<b>인터넷 연결이 끊겼습니다.</b> 적으신 내용은 화면에 그대로 남아 있고, '
      + '연결이 돌아오면 자동으로 저장됩니다.');
  });

  renderList();
  renderAttachments();
  paintRecState();
  if (agendas.length) select(agendas[0].id);
})();
