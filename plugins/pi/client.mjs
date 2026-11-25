// One bounded JSON Lines channel per Pi session. Each request owns its response.
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export class CoreClient {
  #child;
  #pending = new Map();
  #nextId = 0;
  #closed = false;
  #finished;

  constructor({ db, python = process.env.SASE_PYTHON || 'python3', timeoutMs = 200 }) {
    if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 60_000) {
      throw new RangeError('Invalid SaSe timeout');
    }
    this.timeoutMs = timeoutMs;
    const dbPath = db === ':memory:' ? db : resolve(db);
    const interpreter = python.includes('/') ? resolve(python) : python;
    const child = spawn(interpreter, ['-m', 'sase', 'serve', '--db', dbPath], {
      cwd: fileURLToPath(new URL('../../', import.meta.url)),
      stdio: ['pipe', 'pipe', 'ignore'],
    });
    this.#child = child;
    const lines = createInterface({ input: child.stdout });
    lines.on('line', line => {
      let reply;
      try { reply = JSON.parse(line); }
      catch { return; }
      const pending = this.#pending.get(reply?.id);
      if (!pending) return; // A response to an expired request.
      this.#pending.delete(reply.id);
      clearTimeout(pending.timer);
      if (reply.ok === true) pending.resolve(reply.result);
      else pending.reject(new Error('SaSe core rejected request'));
    });
    const disconnect = () => {
      this.#closed = true;
      for (const { timer, reject } of this.#pending.values()) {
        clearTimeout(timer);
        reject(new Error('SaSe core unavailable'));
      }
      this.#pending.clear();
    };
    child.on('error', disconnect);
    child.stdin.on('error', disconnect);
    this.#finished = new Promise(resolve => child.once('close', () => {
      lines.close();
      disconnect();
      resolve();
    }));
  }

  request(req) {
    if (this.#closed || !this.#child.stdin.writable || this.#child.stdin.writableNeedDrain ||
        this.#pending.size >= 256) {
      return Promise.reject(new Error('SaSe core unavailable'));
    }
    const id = ++this.#nextId;
    return new Promise((resolve, reject) => {
      const line = JSON.stringify({ ...req, v: 1, id }) + '\n';
      const timer = setTimeout(() => {
        this.#pending.delete(id);
        reject(new Error('SaSe deadline exceeded'));
      }, this.timeoutMs);
      this.#pending.set(id, { timer, resolve, reject });
      this.#child.stdin.write(line, error => {
        if (!error || !this.#pending.delete(id)) return;
        clearTimeout(timer);
        reject(new Error('SaSe core unavailable'));
      });
    });
  }

  async close() {
    this.#closed = true;
    // EOF drains queued evidence before exit. A stuck child has a bounded grace period.
    this.#child.stdin.end();
    const timer = setTimeout(() => this.#child.kill('SIGKILL'), this.timeoutMs);
    try { await this.#finished; }
    finally { clearTimeout(timer); }
  }
}
