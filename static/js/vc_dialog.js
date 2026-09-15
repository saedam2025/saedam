/*!
 * 인증전자계약 공통 알림창.
 * 브라우저 기본 alert / confirm / prompt 를 대신하는 그래픽 모달이며,
 * 모두 Promise 를 돌려주므로 호출부에서 await 로 결과를 받는다.
 *   await vcAlert('저장했습니다.')
 *   if (!await vcConfirm('삭제할까요?')) return;
 *   const value = await vcPrompt('열 개수', '3');   // 취소하면 null
 */
(function (global) {
  'use strict';
  if (global.vcAlert) return;

  var STYLE_ID = 'vc-dialog-style';
  var CSS = [
    '.vcd-backdrop{position:fixed;inset:0;z-index:99999;display:flex;align-items:center;justify-content:center;',
    'padding:20px;background:rgba(12,20,33,.55);-webkit-backdrop-filter:blur(3px);backdrop-filter:blur(3px);',
    'opacity:0;transition:opacity .16s ease;font-family:"Pretendard","Noto Sans KR","Malgun Gothic",sans-serif}',
    '.vcd-backdrop.vcd-show{opacity:1}',
    '.vcd-backdrop.vcd-closing{pointer-events:none}',
    '.vcd-card{width:100%;max-width:404px;background:var(--vcd-card,#fff);color:var(--vcd-text,#1f2937);',
    'border-radius:16px;box-shadow:0 18px 50px rgba(9,18,32,.32);overflow:hidden;',
    'transform:translateY(14px) scale(.97);transition:transform .18s cubic-bezier(.2,.8,.3,1)}',
    '.vcd-backdrop.vcd-show .vcd-card{transform:none}',
    '.vcd-accent{height:5px;background:#1f3a5f}',
    '.vcd-body{padding:26px 26px 6px;text-align:center}',
    '.vcd-icon{width:52px;height:52px;margin:0 auto 14px;border-radius:50%;display:flex;align-items:center;',
    'justify-content:center;font-size:25px;line-height:1}',
    '.vcd-title{margin:0 0 8px;font-size:16.5px;font-weight:800;letter-spacing:-.01em}',
    '.vcd-message{margin:0;font-size:14px;line-height:1.72;color:var(--vcd-muted,#55637a);white-space:pre-wrap;word-break:keep-all}',
    '.vcd-input{width:100%;margin-top:16px;padding:11px 13px;font-size:14px;box-sizing:border-box;',
    'border:1px solid var(--vcd-border,#d4dce7);border-radius:9px;background:var(--vcd-input,#fff);',
    'color:inherit;outline:none;transition:border-color .12s,box-shadow .12s}',
    '.vcd-input:focus{border-color:#1f3a5f;box-shadow:0 0 0 3px rgba(31,58,95,.14)}',
    '.vcd-hint{margin:7px 0 0;font-size:12px;color:var(--vcd-muted,#7c8aa0);text-align:left}',
    '.vcd-foot{display:flex;gap:9px;padding:20px 26px 24px}',
    '.vcd-btn{flex:1;height:44px;border:0;border-radius:10px;font-size:14px;font-weight:700;cursor:pointer;',
    'font-family:inherit;transition:filter .12s,background .12s}',
    '.vcd-btn:hover{filter:brightness(1.06)}',
    '.vcd-btn:focus-visible{outline:2px solid #1f3a5f;outline-offset:2px}',
    '.vcd-btn.vcd-primary{background:#1f3a5f;color:#fff}',
    '.vcd-btn.vcd-ghost{background:var(--vcd-ghost,#eef1f6);color:var(--vcd-ghost-text,#44526b)}',
    '.vcd-foot.vcd-stack{flex-direction:column;gap:8px}',
    '.vcd-foot.vcd-stack .vcd-btn{flex:none;width:100%}',
    '@media(max-width:480px){.vcd-body{padding:22px 20px 4px}.vcd-foot{padding:18px 20px 20px}}',
    '@media(prefers-color-scheme:dark){.vcd-backdrop{--vcd-card:#1b2433;--vcd-text:#e8edf5;--vcd-muted:#9fb0c6;',
    '--vcd-border:#3a4759;--vcd-input:#131b27;--vcd-ghost:#2b3849;--vcd-ghost-text:#cdd8e6}}',
    '@media(prefers-reduced-motion:reduce){.vcd-backdrop,.vcd-card{transition:none}}'
  ].join('');

  var TONES = {
    info:    { accent: '#1f3a5f', bg: '#eaf0f8', color: '#1f3a5f', icon: 'ℹ', title: '알림' },
    success: { accent: '#137a5f', bg: '#e6f5ef', color: '#137a5f', icon: '✓', title: '완료' },
    warning: { accent: '#b07000', bg: '#fdf2df', color: '#8a5700', icon: '!', title: '확인' },
    error:   { accent: '#b3322f', bg: '#fdecec', color: '#a32b28', icon: '!', title: '오류' },
    ask:     { accent: '#1f3a5f', bg: '#eaf0f8', color: '#1f3a5f', icon: '?', title: '확인' }
  };

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var tag = document.createElement('style');
    tag.id = STYLE_ID;
    tag.textContent = CSS;
    document.head.appendChild(tag);
  }

  // 오류·완료처럼 뻔한 문구는 아이콘을 알아서 골라 준다.
  // '완료되지 않았습니다' 처럼 부정문이 성공으로 읽히지 않도록 부정 표현을 먼저 본다.
  function guessTone(text) {
    var value = String(text || '');
    if (/실패|오류|에러|불가/.test(value)) return 'error';
    if (/않|아직|없습니다|없음|올바르지|초과|잘못/.test(value)) return 'warning';
    if (/완료|저장|성공|등록되었|발송했|삭제했|변경되었/.test(value)) return 'success';
    return 'info';
  }

  function open(options) {
    ensureStyle();
    var tone = TONES[options.tone] || TONES.info;
    var previous = document.activeElement;

    var backdrop = document.createElement('div');
    backdrop.className = 'vcd-backdrop';
    backdrop.setAttribute('role', 'dialog');
    backdrop.setAttribute('aria-modal', 'true');

    var card = document.createElement('div');
    card.className = 'vcd-card';
    backdrop.appendChild(card);

    var accent = document.createElement('div');
    accent.className = 'vcd-accent';
    accent.style.background = tone.accent;
    card.appendChild(accent);

    var body = document.createElement('div');
    body.className = 'vcd-body';
    card.appendChild(body);

    var icon = document.createElement('div');
    icon.className = 'vcd-icon';
    icon.style.background = tone.bg;
    icon.style.color = tone.color;
    icon.textContent = tone.icon;
    body.appendChild(icon);

    var title = document.createElement('h3');
    title.className = 'vcd-title';
    title.textContent = options.title || tone.title;
    body.appendChild(title);

    var message = document.createElement('p');
    message.className = 'vcd-message';
    message.textContent = options.message == null ? '' : String(options.message);
    body.appendChild(message);

    var input = null;
    if (options.type === 'prompt') {
      input = document.createElement('input');
      input.className = 'vcd-input';
      input.type = 'text';
      input.value = options.defaultValue == null ? '' : String(options.defaultValue);
      if (options.placeholder) input.placeholder = options.placeholder;
      body.appendChild(input);
    }

    var foot = document.createElement('div');
    foot.className = 'vcd-foot';
    card.appendChild(foot);

    var settled = false;
    var resolveOuter;
    var promise = new Promise(function (resolve) { resolveOuter = resolve; });

    function close(result) {
      if (settled) return;
      settled = true;
      document.removeEventListener('keydown', onKey, true);
      backdrop.classList.remove('vcd-show');
      backdrop.classList.add('vcd-closing');
      backdrop.setAttribute('aria-hidden', 'true');
      setTimeout(function () {
        if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
        if (previous && typeof previous.focus === 'function') {
          try { previous.focus({ preventScroll: true }); } catch (error) { previous.focus(); }
        }
      }, 160);
      resolveOuter(result);
    }

    function confirmValue() {
      if (options.type === 'prompt') return input.value;
      return options.type === 'alert' ? true : true;
    }

    if (options.type !== 'alert' && options.type !== 'choice') {
      var cancel = document.createElement('button');
      cancel.type = 'button';
      cancel.className = 'vcd-btn vcd-ghost';
      cancel.textContent = options.cancelText || '취소';
      cancel.addEventListener('click', function () {
        close(options.type === 'prompt' ? null : false);
      });
      foot.appendChild(cancel);
    }

    var ok = null;
    if (options.type === 'choice') {
      foot.className = 'vcd-foot vcd-stack';
      (options.choices || []).forEach(function (choice, index) {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'vcd-btn ' + (index === 0 ? 'vcd-primary' : 'vcd-ghost');
        button.textContent = choice.label;
        button.addEventListener('click', function () { close(choice.value); });
        foot.appendChild(button);
        if (index === 0) ok = button;
      });
      var giveUp = document.createElement('button');
      giveUp.type = 'button';
      giveUp.className = 'vcd-btn vcd-ghost';
      giveUp.textContent = options.cancelText || '취소';
      giveUp.addEventListener('click', function () { close(null); });
      foot.appendChild(giveUp);
    } else {
      ok = document.createElement('button');
      ok.type = 'button';
      ok.className = 'vcd-btn vcd-primary';
      ok.textContent = options.confirmText || '확인';
      if (options.danger) ok.style.background = '#a32b28';
      ok.addEventListener('click', function () { close(confirmValue()); });
      foot.appendChild(ok);
    }

    function onKey(event) {
      if (event.key === 'Escape') {
        event.preventDefault();
        close(options.type === 'alert' ? true : options.type === 'confirm' ? false : null);
      } else if (event.key === 'Enter' && (options.type !== 'prompt' || event.target === input)) {
        event.preventDefault();
        if (ok) ok.click(); else close(confirmValue());
      }
    }
    document.addEventListener('keydown', onKey, true);
    backdrop.addEventListener('mousedown', function (event) {
      // 알림창은 바깥을 잘못 눌러 닫히지 않도록 확인 버튼으로만 닫는다.
      if (event.target !== backdrop || options.type === 'alert') return;
      close(options.type === 'alert' ? true : options.type === 'confirm' ? false : null);
    });

    document.body.appendChild(backdrop);
    requestAnimationFrame(function () {
      backdrop.classList.add('vcd-show');
      if (input) { input.focus(); input.select(); } else { ok.focus(); }
    });

    return promise;
  }

  global.vcAlert = function (message, options) {
    options = options || {};
    return open({
      type: 'alert',
      message: message,
      title: options.title,
      tone: options.tone || guessTone(message),
      confirmText: options.confirmText || '확인'
    });
  };

  global.vcConfirm = function (message, options) {
    options = options || {};
    return open({
      type: 'confirm',
      message: message,
      title: options.title,
      tone: options.tone || (options.danger ? 'warning' : 'ask'),
      confirmText: options.confirmText || '확인',
      cancelText: options.cancelText || '취소',
      danger: options.danger
    });
  };

  /**
   * 여러 갈래 중 하나를 고르게 한다. 고른 항목의 value 를, 취소하면 null 을 돌려준다.
   *   await vcChoice('범위 선택', [{label:'전체', value:'all'}, {label:'1~2쪽', value:'1-2'}])
   */
  global.vcChoice = function (message, choices, options) {
    options = options || {};
    return open({
      type: 'choice',
      message: message,
      choices: choices || [],
      title: options.title,
      tone: options.tone || 'ask',
      cancelText: options.cancelText || '취소'
    });
  };

  global.vcPrompt = function (message, defaultValue, options) {
    options = options || {};
    return open({
      type: 'prompt',
      message: message,
      defaultValue: defaultValue,
      placeholder: options.placeholder,
      title: options.title,
      tone: options.tone || 'ask',
      confirmText: options.confirmText || '확인',
      cancelText: options.cancelText || '취소'
    });
  };
})(window);
