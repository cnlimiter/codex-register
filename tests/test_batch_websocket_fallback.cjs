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
      getElementById() {
        return createElementStub();
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
    window: null,
    WebSocket: null,
  };

  sandbox.window = sandbox;
  sandbox.window.location = { protocol: 'http:', host: '127.0.0.1:8003' };

  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(APP_JS_PATH, 'utf8'), sandbox, { filename: 'app.js' });

  return sandbox;
}

async function runFallback(mode) {
  const sandbox = createSandbox();

  vm.runInContext(
    `
      var __calls = [];
      currentBatch = { batch_id: 'test-batch' };
      isOutlookBatchMode = ${mode === 'outlook' ? 'true' : 'false'};
      batchCompleted = false;
      batchFinalStatus = null;
      startOutlookBatchPolling = function(batchId) { __calls.push(['outlook', batchId]); };
      startBatchPolling = function(batchId) { __calls.push(['batch', batchId]); };
      WebSocket = function(url) {
        this.url = url;
        this.readyState = 0;
        setTimeout(() => {
          if (this.onerror) {
            this.onerror({ type: 'error' });
          }
        }, 0);
      };
      WebSocket.OPEN = 1;
      connectBatchWebSocket('test-batch');
    `,
    sandbox,
  );

  await new Promise((resolve) => setTimeout(resolve, 20));
  return JSON.parse(vm.runInContext('JSON.stringify(__calls)', sandbox));
}

test('normal batch websocket fallback uses standard batch polling', async () => {
  const calls = await runFallback('batch');
  assert.deepEqual(calls, [['batch', 'test-batch']]);
});

test('outlook batch websocket fallback uses outlook polling', async () => {
  const calls = await runFallback('outlook');
  assert.deepEqual(calls, [['outlook', 'test-batch']]);
});
