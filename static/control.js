// control.js — turntable control panel
//
// The server decides what state the machine is in; this page only displays it.
// Until the server has said so, the page does not know, and every control stays off.

/**
 * The part of the Socket.IO client's socket that this page uses.
 * @typedef {object} SocketLike
 * @property {boolean} connected
 * @property {(event: string, handler: (...args: any[]) => void) => void} on
 * @property {(event: string, ...args: any[]) => void} emit
 */

(() => {
  'use strict';

  const MAX_DEGREES = 360;
  // While we are waiting for the server to answer `get_state`, ask again this often.
  const STATE_RETRY_MS = 3000;
  // If we sent a move and heard nothing back, give the controls back after this long.
  const PENDING_TIMEOUT_MS = 3000;
  const TOAST_MS = 5000;

  // What to show for each state. `detail` may be a function of the current move/message.
  const COPY = {
    unknown: {
      title: 'Checking machine',
      detail: () => 'Asking the turntable for its status.',
    },
    disconnected: {
      title: 'Connection lost',
      detail: () => 'Reconnecting. Controls stay off until the turntable answers.',
    },
    booting: {
      title: 'Starting up',
      detail: () => 'Wait for the green light before sending a move.',
    },
    ready: {
      title: 'Ready',
      detail: ({ lastMove }) => lastMove
        ? `Last move: ${formatMove(lastMove)}.`
        : 'Enter an angle and choose a direction.',
    },
    moving: {
      title: 'Moving',
      detail: ({ currentMove }) => currentMove
        ? formatMove(currentMove)
        : 'A move is in progress.',
    },
    estop: {
      title: 'E-stop engaged',
      detail: () => 'Release the e-stop button. The turntable restarts itself and will be ready in a few seconds.',
    },
    error: {
      title: 'System error',
      detail: ({ message, recoverable }) => (message ? `${message} ` : '') + (recoverable
        ? 'Press and release the e-stop to try again, or restart the turntable.'
        : 'Restart the turntable to recover.'),
    },
    shutdown: {
      title: 'Shutting down',
      detail: () => 'The turntable is powering off. Press the power button to start it again.',
    },
  };

  const $ = (id) => document.getElementById(id);
  const el = {
    status:   $('status'),
    title:    $('status-title'),
    detail:   $('status-detail'),
    form:     $('move-form'),
    controls: $('controls'),
    angle:    $('angle'),
    hint:     $('angle-hint'),
    go:       $('go'),
    presetsField: $('presets-field'),
    presets:  $('presets'),
    toast:    $('toast'),
  };

  let machineState = 'unknown';   // what the server last told us (or unknown / disconnected)
  let stateMessage = '';          // the `message` that came with an `error` state
  let stateRecoverable = false;   // whether the e-stop can clear that error
  let currentMove  = null;        // {degrees, direction} while a move is running
  let lastMove     = null;        // the most recent move that finished
  let pending      = false;       // we sent a move and the server hasn't answered yet
  let pendingTimer = null;
  let toastTimer   = null;

  // ---------- Helpers ----------

  function formatMove(move) {
    return `${move.degrees}° ${move.direction}`;
  }

  function validateAngle(value) {
    if (!Number.isFinite(value)) return 'Enter an angle in degrees.';
    if (value <= 0)              return 'The angle must be greater than 0.';
    if (value > MAX_DEGREES)     return `The angle can’t be more than ${MAX_DEGREES}°.`;
    return '';
  }

  function showHint(message) {
    el.hint.textContent = message;
    el.angle.setAttribute('aria-invalid', message ? 'true' : 'false');
  }

  function showToast(message) {
    el.toast.textContent = message;
    el.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(hideToast, TOAST_MS);
  }

  function hideToast() {
    el.toast.hidden = true;
    clearTimeout(toastTimer);
  }

  function clearPending() {
    pending = false;
    clearTimeout(pendingTimer);
  }

  // ---------- Rendering ----------

  function render() {
    // A state we don't recognize is treated as "unknown": better to lock up than to guess.
    const known = Object.prototype.hasOwnProperty.call(COPY, machineState);
    const shown = known ? machineState : 'unknown';
    const copy = COPY[shown];

    el.status.dataset.state = shown;
    el.title.textContent = copy.title;
    el.detail.textContent = copy.detail({ currentMove, lastMove, message: stateMessage, recoverable: stateRecoverable });

    // Only "ready" turns the controls on, and only if we aren't mid-send.
    el.controls.disabled = !(shown === 'ready' && !pending);
  }

  function setState(state, message, recoverable) {
    machineState = state;
    stateMessage = typeof message === 'string' ? message : '';
    stateRecoverable = recoverable === true;
    if (state !== 'moving') currentMove = null;
    render();
  }

  // ---------- Presets ----------

  function setupPresets() {
    const list = Array.isArray(window.TURNTABLE_PRESETS) ? window.TURNTABLE_PRESETS : [];
    for (const preset of list) {
      const degrees = Number(preset && preset.degrees);
      if (validateAngle(degrees)) continue;                        // skip bad entries quietly
      const direction = preset.direction === 'CW' || preset.direction === 'CCW' ? preset.direction : null;

      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'preset';
      button.textContent = preset.label || `${degrees}°` + (direction ? ` ${direction}` : '');
      button.addEventListener('click', () => {
        el.angle.value = String(degrees);
        showHint('');
        if (direction) el.form.elements.direction.value = direction;
      });
      el.presets.appendChild(button);
    }
    el.presetsField.hidden = el.presets.children.length === 0;
  }

  // ---------- Socket ----------

  // `io` is defined by socket.io.min.js, which control.html loads before this file.
  // noinspection JSUnresolvedReference
  const socket = /** @type {SocketLike} */ (io());

  socket.on('connect', () => {
    // Whatever we knew before is stale. Find out again, and only then trust the controls.
    clearPending();
    currentMove = null;
    setState('unknown');
    socket.emit('get_state');
  });

  socket.on('disconnect', () => {
    clearPending();
    setState('disconnected');
  });

  socket.on('connect_error', () => {
    if (!socket.connected) setState('disconnected');
  });

  socket.on('machine_status', (data) => {
    clearPending();
    setState(data && data.state, data && data.message, data && data.recoverable);
  });

  socket.on('move_started', (data) => {
    if (data && Number.isFinite(data.degrees) && data.direction) {
      currentMove = { degrees: data.degrees, direction: data.direction };
      render();
    }
  });

  socket.on('move_complete', () => {
    if (currentMove) lastMove = currentMove;
    currentMove = null;
    render();
  });

  // Sent only to the client whose command was refused.
  socket.on('move_error', (data) => {
    clearPending();
    render();
    showToast((data && data.message) || 'The move was not accepted.');
  });

  // If the first `get_state` was lost, ask again until the server answers.
  setInterval(() => {
    if (socket.connected && machineState === 'unknown') socket.emit('get_state');
  }, STATE_RETRY_MS);

  // ---------- Form ----------

  el.angle.addEventListener('input', () => showHint(''));
  el.toast.addEventListener('click', hideToast);

  el.form.addEventListener('submit', (event) => {
    event.preventDefault();
    if (machineState !== 'ready' || pending || !socket.connected) return;

    const degrees = el.angle.valueAsNumber;
    const problem = validateAngle(degrees);
    if (problem) {
      showHint(problem);
      el.angle.focus();
      return;
    }

    hideToast();
    pending = true;
    render();
    // Safety valve: if nothing comes back, don't leave the controls locked.
    pendingTimer = setTimeout(() => { pending = false; render(); }, PENDING_TIMEOUT_MS);

    socket.emit('move_command', {
      degrees,
      direction: el.form.elements.direction.value,
    });
  });

  setupPresets();
  render();
})();