(function () {
  'use strict';

  // ── Inject HTML ──────────────────────────────────────────────────────────
  var html =
    '<button class="ai-tutor-fab" id="aiTutorFab" title="AI 學習助教">&#x1F393;</button>' +
    '<div class="ai-tutor-panel" id="aiTutorPanel">' +
      '<div class="ai-tutor-header">' +
        '<span class="ai-tutor-header-title">AI 學習助教</span>' +
        '<div class="ai-tutor-header-actions">' +
          '<button class="ai-tutor-header-btn" id="aiTutorNew" title="開啟新對話">&#x1F5D8;</button>' +
          '<button class="ai-tutor-header-btn" id="aiTutorClose" title="關閉">&times;</button>' +
        '</div>' +
      '</div>' +
      '<div class="ai-tutor-messages" id="aiTutorMessages"></div>' +
      '<div class="ai-tutor-input-area">' +
        '<textarea class="ai-tutor-input" id="aiTutorInput" placeholder="輸入問題..." rows="1"></textarea>' +
        '<button class="ai-tutor-send" id="aiTutorSend" title="送出">&#x27A4;</button>' +
      '</div>' +
    '</div>';

  var wrapper = document.createElement('div');
  wrapper.innerHTML = html;
  while (wrapper.firstChild) {
    document.body.appendChild(wrapper.firstChild);
  }

  // ── Elements ─────────────────────────────────────────────────────────────
  var fab = document.getElementById('aiTutorFab');
  var panel = document.getElementById('aiTutorPanel');
  var messagesEl = document.getElementById('aiTutorMessages');
  var input = document.getElementById('aiTutorInput');
  var sendBtn = document.getElementById('aiTutorSend');
  var closeBtn = document.getElementById('aiTutorClose');
  var newBtn = document.getElementById('aiTutorNew');

  var sending = false;

  // ── Toggle panel ─────────────────────────────────────────────────────────
  fab.addEventListener('click', function () {
    var isOpen = panel.classList.toggle('open');
    if (isOpen) {
      loadHistory();
      input.focus();
    }
  });

  closeBtn.addEventListener('click', function () {
    panel.classList.remove('open');
  });

  // ── New conversation ─────────────────────────────────────────────────────
  newBtn.addEventListener('click', function () {
    fetch('/api/tutor/new', { method: 'POST' })
      .then(function () {
        messagesEl.innerHTML = '';
        addMessage('assistant', '你好！有什麼問題想問嗎？');
      });
  });

  // ── Send message ─────────────────────────────────────────────────────────
  sendBtn.addEventListener('click', sendMessage);
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  // Auto-resize textarea
  input.addEventListener('input', function () {
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 80) + 'px';
  });

  function sendMessage() {
    var text = input.value.trim();
    if (!text || sending) return;

    sending = true;
    sendBtn.disabled = true;
    input.value = '';
    input.style.height = 'auto';

    addMessage('user', text);
    var loadingEl = addMessage('loading', '思考中...');

    var pageContext = detectPageContext();

    fetch('/api/tutor/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, page_context: pageContext }),
    })
      .then(function (resp) {
        if (!resp.ok) throw new Error('API error');
        return resp.json();
      })
      .then(function (data) {
        messagesEl.removeChild(loadingEl);
        addMessage('assistant', data.answer, data.sources);
      })
      .catch(function () {
        messagesEl.removeChild(loadingEl);
        addMessage('assistant', '抱歉，目前無法回答您的問題，請稍後再試。');
      })
      .finally(function () {
        sending = false;
        sendBtn.disabled = false;
        input.focus();
      });
  }

  // ── Render messages ──────────────────────────────────────────────────────
  function addMessage(role, content, sources) {
    var div = document.createElement('div');
    div.className = 'ai-tutor-msg ' + role;
    div.innerHTML = formatContent(content);

    if (sources && sources.length > 0) {
      var srcDiv = document.createElement('div');
      srcDiv.className = 'ai-tutor-sources';
      srcDiv.textContent = '參考：' + sources.map(function (s) { return s.title; }).join('、');
      div.appendChild(srcDiv);
    }

    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return div;
  }

  function formatContent(text) {
    // Simple markdown: bold, line breaks, lists
    return text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/^\d+\.\s+(.+)$/gm, '<li>$1</li>')
      .replace(/^[-*]\s+(.+)$/gm, '<li>$1</li>')
      .replace(/\n/g, '<br>');
  }

  // ── Load history ─────────────────────────────────────────────────────────
  function loadHistory() {
    fetch('/api/tutor/history')
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        messagesEl.innerHTML = '';
        if (!data.messages || data.messages.length === 0) {
          addMessage('assistant', '你好！我是 EAGLE 學習助教，有什麼問題想問嗎？');
          return;
        }
        data.messages.forEach(function (m) {
          addMessage(m.role, m.content);
        });
      });
  }

  // ── Detect page context ──────────────────────────────────────────────────
  function detectPageContext() {
    var path = window.location.pathname;
    var match = path.match(/\/task\/(\d+)/);
    if (match) return 'task' + match[1];
    if (path.indexOf('/journal') !== -1) return 'journal';
    if (path.indexOf('/dashboard') !== -1) return 'dashboard';
    if (path.indexOf('/manual') !== -1) return 'manual';
    return '';
  }
})();
