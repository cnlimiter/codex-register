const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const APP_JS_PATH = '/Users/zhoukailian/.config/superpowers/worktrees/codex-manager/repro-batch-monitor/static/js/app.js';

function createElementStub() {
  return {
    style: {},
    dataset: {},
    value: '',
    checked: false,
    disabled: false,
    innerHTML: '',
    textContent: '',
    className: '',
    appendChild() {},
    addEventListener() {},
    removeEventListener() {},
    querySelector() {
      return createElementStub();
    },
    querySelectorAll() {
      return [];
    },
    closest() {
      return null;
    },
  };
}

function createSandbox() {
  const elements = new Map();

  const sandbox = {
    console,
    setTimeout,
    clearTimeout,
    setInterval: () => 1,
    clearInterval: () => {},
    Event: class Event {
      constructor(type) {
        this.type = type;
      }
    },
    document: {
      getElementById(id) {
        if (!elements.has(id)) {
          elements.set(id, createElementStub());
        }
        return elements.get(id);
      },
      createElement() {
        return createElementStub();
      },
      addEventListener() {},
      querySelector() {
        return createElementStub();
      },
      querySelectorAll() {
        return [];
      },
    },
    sessionStorage: {
      getItem() {
        return null;
      },
      setItem() {},
      removeItem() {},
    },
    toast: {
      info() {},
      success() {},
      warning() {},
      error() {},
    },
    api: {
      get() {
        throw new Error('api.get should not be called in this test');
      },
      post() {
        throw new Error('api.post should not be called in this test');
      },
    },
    loadRecentAccounts() {},
    getServiceTypeText(type) {
      return {
        tempmail: '临时邮箱',
        outlook: 'Outlook',
      }[type] || type;
    },
    window: null,
    WebSocket: null,
  };

  sandbox.window = sandbox;
  sandbox.window.location = { protocol: 'http:', host: '127.0.0.1:8005' };

  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(APP_JS_PATH, 'utf8'), sandbox, { filename: 'app.js' });

  return { sandbox, elements };
}

test('single task websocket completion updates task info and resets buttons', () => {
  const { sandbox, elements } = createSandbox();

  vm.runInContext(
    `
      var __lastWs = null;
      startLogPolling = function() {
        throw new Error('startLogPolling should not be called for completed status');
      };
      loadRecentAccounts = function() {};
      currentTask = { task_uuid: 'task-1' };
      taskCompleted = false;
      taskFinalStatus = null;
      elements.startBtn.disabled = true;
      elements.cancelBtn.disabled = false;
      elements.taskStatusRow.style.display = 'grid';
      WebSocket = function(url) {
        this.url = url;
        this.readyState = 0;
        __lastWs = this;
      };
      WebSocket.OPEN = 1;
      WebSocket.CLOSED = 3;
      WebSocket.prototype.close = function() {
        this.readyState = WebSocket.CLOSED;
      };
      connectWebSocket('task-1');
      __lastWs.onmessage({
        data: JSON.stringify({
          type: 'status',
          status: 'completed',
          email: 'demo@example.com',
          email_service: 'tempmail',
        }),
      });
    `,
    sandbox,
  );

  assert.equal(elements.get('start-btn').disabled, false);
  assert.equal(elements.get('cancel-btn').disabled, true);
  assert.equal(elements.get('task-status').textContent, '已完成');
  assert.equal(elements.get('task-email').textContent, 'demo@example.com');
  assert.equal(elements.get('task-service').textContent, '临时邮箱');
});
