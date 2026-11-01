(() => {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const scene = $('#char-scene');
  const username = $('#username');
  const password = $('#password');
  const note = $('#login-note');
  const submit = $('#login-button');
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const tooltip = $('#role-tooltip');
  const roleText = $('#role-text');
  let focusMode = 'none';
  let pointer = { x: window.innerWidth * 0.5, y: window.innerHeight * 0.5 };
  let running = true;
  let frameId = 0;
  let blinkTimer = 0;
  let roleTimer = 0;
  let toastTimer = 0;
  let eyeSequence = 0;

  const characters = $$('.character', scene).map((element) => ({
    element,
    eyes: $$('.eyeball', element).map((eye) => ({ element: eye, x: 0, y: 0, id: eyeSequence++ })),
    factor: Number(element.dataset.tilt),
    rotation: 0,
  }));

  function safeNext() {
    const value = new URLSearchParams(window.location.search).get('next') || '/workspace';
    return value.startsWith('/') && !value.startsWith('//') ? value : '/workspace';
  }

  function toast(message) {
    const element = $('#toast');
    element.textContent = message;
    element.classList.add('show');
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => element.classList.remove('show'), 3200);
  }

  function syncPrivacy() {
    const privacy = focusMode === 'password' && password.type === 'password';
    scene.classList.toggle('privacy', privacy);
    if (privacy) scene.classList.remove('blink');
  }

  function targetPoint() {
    if (focusMode !== 'none') {
      const input = focusMode === 'username' ? username : password;
      const bounds = input.getBoundingClientRect();
      return { x: bounds.left + bounds.width * 0.57, y: bounds.top + bounds.height * 0.5 };
    }
    return pointer;
  }

  function animate() {
    frameId = 0;
    if (!running || document.hidden || reducedMotion.matches) return;
    const target = targetPoint();
    const privacy = focusMode === 'password' && password.type === 'password';
    for (const character of characters) {
      const bounds = character.element.getBoundingClientRect();
      const centerX = bounds.left + bounds.width / 2;
      const centerY = bounds.top + bounds.height * 0.28;
      const dx = target.x - centerX;
      const dy = target.y - centerY;
      const length = Math.hypot(dx, dy) || 1;
      const wanted = privacy ? -5.5 * character.factor : Math.max(-9, Math.min(9, dx / length * 6 * character.factor));
      character.rotation += (wanted - character.rotation) * 0.16;
      character.element.style.setProperty('--head-rot', `${character.rotation.toFixed(2)}deg`);
      for (const eye of character.eyes) {
        const rect = eye.element.getBoundingClientRect();
        const eyeX = target.x - (rect.left + rect.width / 2);
        const eyeY = target.y - (rect.top + rect.height / 2);
        const distance = Math.hypot(eyeX, eyeY) || 1;
        const maxX = eye.element.clientWidth < 13 ? 1.8 : 3.2;
        const maxY = eye.element.clientHeight < 13 ? 1.5 : 2.7;
        const desiredX = privacy ? 0 : eyeX / distance * maxX;
        const desiredY = privacy ? 0 : eyeY / distance * maxY;
        eye.x += (desiredX - eye.x) * 0.16;
        eye.y += (desiredY - eye.y) * 0.16;
        eye.element.style.setProperty('--p-x', `${eye.x.toFixed(2)}px`);
        eye.element.style.setProperty('--p-y', `${eye.y.toFixed(2)}px`);
      }
    }
    frameId = window.requestAnimationFrame(animate);
  }

  function startAnimation() {
    if (!frameId && running && !document.hidden && !reducedMotion.matches) {
      frameId = window.requestAnimationFrame(animate);
    }
  }

  function stopAnimation() {
    window.cancelAnimationFrame(frameId);
    frameId = 0;
  }

  function blinkOnce() {
    if ((focusMode === 'password' && password.type === 'password') || reducedMotion.matches) return;
    scene.classList.add('blink');
    window.setTimeout(() => scene.classList.remove('blink'), 240);
  }

  function scheduleBlink() {
    window.clearTimeout(blinkTimer);
    if (document.hidden || reducedMotion.matches || !running) return;
    blinkTimer = window.setTimeout(() => {
      blinkOnce();
      scheduleBlink();
    }, 3000 + Math.random() * 2400);
  }

  function showRole(character) {
    window.clearTimeout(roleTimer);
    roleText.textContent = character.dataset.role;
    tooltip.style.setProperty('--role-color', character.dataset.roleColor);
    tooltip.classList.add('visible');
    roleTimer = window.setTimeout(() => tooltip.classList.remove('visible'), 2200);
  }

  window.addEventListener('pointermove', (event) => {
    if (event.pointerType !== 'touch') pointer = { x: event.clientX, y: event.clientY };
  }, { passive: true });
  document.documentElement.addEventListener('mouseleave', () => {
    pointer = { x: window.innerWidth * 0.5, y: window.innerHeight * 0.45 };
  });

  [username, password].forEach((input) => {
    input.addEventListener('focus', () => {
      focusMode = input === username ? 'username' : 'password';
      syncPrivacy();
    });
    input.addEventListener('blur', () => {
      window.setTimeout(() => {
        if (![username, password].includes(document.activeElement)) {
          focusMode = 'none';
          syncPrivacy();
        }
      }, 30);
    });
  });

  $('#reveal-password').addEventListener('click', () => {
    const reveal = password.type === 'password';
    password.type = reveal ? 'text' : 'password';
    const toggle = $('#reveal-password');
    toggle.setAttribute('aria-label', reveal ? '隐藏密码' : '显示密码');
    toggle.setAttribute('aria-pressed', String(reveal));
    password.focus();
    syncPrivacy();
  });

  for (const character of $$('.character', scene)) {
    character.addEventListener('pointerenter', (event) => {
      if (event.pointerType !== 'touch') showRole(character);
    });
    character.addEventListener('click', () => showRole(character));
    character.addEventListener('focus', () => showRole(character));
    character.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        showRole(character);
      }
    });
  }

  $('#forgot').addEventListener('click', () => toast('请联系平台管理员重置密码。'));

  $('#login-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    note.className = 'auth-note';
    if (!username.value.trim()) {
      note.textContent = '请输入账户名';
      username.focus();
      return;
    }
    if (!password.value) {
      note.textContent = '请输入密码';
      password.focus();
      return;
    }
    submit.disabled = true;
    submit.setAttribute('aria-busy', 'true');
    submit.textContent = '正在验证…';
    note.textContent = '正在安全验证账号，请稍候';
    try {
      const response = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ username: username.value.trim(), password: password.value }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = payload.detail;
        throw new Error(typeof detail === 'string' ? detail : detail?.message || '登录失败，请稍后重试');
      }
      note.className = 'auth-note success';
      note.textContent = '验证成功，正在进入工作台';
      window.location.assign(safeNext());
    } catch (error) {
      note.className = 'auth-note error';
      note.textContent = error.message || '登录失败，请检查网络后重试';
      submit.disabled = false;
      submit.removeAttribute('aria-busy');
      submit.textContent = '登录';
    }
  });

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      stopAnimation();
      window.clearTimeout(blinkTimer);
    } else {
      startAnimation();
      scheduleBlink();
    }
  });
  reducedMotion.addEventListener('change', () => {
    if (reducedMotion.matches) stopAnimation();
    startAnimation();
    scheduleBlink();
  });
  window.addEventListener('pagehide', () => {
    running = false;
    stopAnimation();
    window.clearTimeout(blinkTimer);
    window.clearTimeout(roleTimer);
  });
  window.addEventListener('pageshow', (event) => {
    if (event.persisted) {
      running = true;
      startAnimation();
      scheduleBlink();
    }
  });

  syncPrivacy();
  startAnimation();
  scheduleBlink();
})();
