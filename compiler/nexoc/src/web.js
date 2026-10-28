// The page side of `nexo build --target web` (D175): the WASI functions a
// compiled Nexo program calls, for a browser (or Node). Output goes to
// `write(text, fd)`, input() reads `stdin` and then asks with prompt();
// files and the network do not exist here, so opening a file fails as a
// missing file and every other call answers ENOSYS.
const NexoWeb = (() => {
  const ESUCCESS = 0, EBADF = 8, ENOENT = 44, ENOSYS = 52, ESPIPE = 70;

  class Exit {
    constructor(code) { this.code = code; }
  }

  async function run(wasm, options = {}) {
    const args = options.args || ["app"];
    const write = options.write || ((text) => console.log(text));
    let input = new TextEncoder().encode(options.stdin || "");
    const ask = options.ask || (typeof prompt === "function" ? () => prompt("input") : () => null);
    const decoders = { 1: new TextDecoder(), 2: new TextDecoder() };
    let memory;
    const view = () => new DataView(memory.buffer);
    const bytes = () => new Uint8Array(memory.buffer);
    const strings = (list, ptrs, buf) => {
      const enc = new TextEncoder();
      let at = buf;
      list.forEach((s, i) => {
        const b = enc.encode(s + "\0");
        view().setUint32(ptrs + 4 * i, at, true);
        bytes().set(b, at);
        at += b.length;
      });
      return ESUCCESS;
    };
    const sizes = (list, count, size) => {
      view().setUint32(count, list.length, true);
      view().setUint32(size, list.reduce((n, s) => n + new TextEncoder().encode(s).length + 1, 0), true);
      return ESUCCESS;
    };
    const calls = {
      args_get: (ptrs, buf) => strings(args, ptrs, buf),
      args_sizes_get: (count, size) => sizes(args, count, size),
      environ_get: (ptrs, buf) => strings([], ptrs, buf),
      environ_sizes_get: (count, size) => sizes([], count, size),
      clock_res_get: (id, out) => { view().setBigUint64(out, 1000n, true); return ESUCCESS; },
      clock_time_get: (id, precision, out) => {
        const ns = id === 0
          ? BigInt(Date.now()) * 1000000n
          : BigInt(Math.round((typeof performance !== "undefined" ? performance.now() : Date.now()) * 1e6));
        view().setBigUint64(out, ns, true);
        return ESUCCESS;
      },
      fd_write: (fd, iovs, n, written) => {
        if (fd !== 1 && fd !== 2) return EBADF;
        let total = 0;
        for (let i = 0; i < n; i++) {
          const ptr = view().getUint32(iovs + 8 * i, true);
          const len = view().getUint32(iovs + 8 * i + 4, true);
          write(decoders[fd].decode(bytes().slice(ptr, ptr + len), { stream: true }), fd);
          total += len;
        }
        view().setUint32(written, total, true);
        return ESUCCESS;
      },
      fd_read: (fd, iovs, n, read) => {
        if (fd !== 0) return EBADF;
        if (input.length === 0) {
          const line = ask();
          if (line !== null && line !== undefined) input = new TextEncoder().encode(line + "\n");
        }
        let total = 0;
        for (let i = 0; i < n && input.length > 0; i++) {
          const ptr = view().getUint32(iovs + 8 * i, true);
          const len = Math.min(view().getUint32(iovs + 8 * i + 4, true), input.length);
          bytes().set(input.subarray(0, len), ptr);
          input = input.subarray(len);
          total += len;
        }
        view().setUint32(read, total, true);
        return ESUCCESS;
      },
      fd_fdstat_get: (fd, out) => {
        if (fd > 2) return EBADF;
        bytes().fill(0, out, out + 24);
        view().setUint8(out, 2, true); // a character device
        return ESUCCESS;
      },
      fd_fdstat_set_flags: () => ESUCCESS,
      fd_filestat_get: (fd, out) => {
        if (fd > 2) return EBADF;
        bytes().fill(0, out, out + 64);
        view().setUint8(out + 16, 2, true);
        return ESUCCESS;
      },
      fd_close: (fd) => (fd <= 2 ? ESUCCESS : EBADF),
      fd_seek: (fd) => (fd <= 2 ? ESPIPE : EBADF),
      fd_sync: () => ESUCCESS,
      fd_prestat_get: () => EBADF,
      fd_prestat_dir_name: () => EBADF,
      path_open: () => ENOENT,
      path_filestat_get: () => ENOENT,
      random_get: (buf, len) => {
        const out = bytes().subarray(buf, buf + len);
        for (let at = 0; at < len; at += 65536) crypto.getRandomValues(out.subarray(at, Math.min(len, at + 65536)));
        return ESUCCESS;
      },
      poll_oneoff: (subs, events, n, count) => {
        // no blocking in a page: every subscription is ready at once
        for (let i = 0; i < n; i++) {
          bytes().fill(0, events + 32 * i, events + 32 * i + 32);
          view().setBigUint64(events + 32 * i, view().getBigUint64(subs + 48 * i, true), true);
          view().setUint8(events + 32 * i + 10, view().getUint8(subs + 48 * i + 8), true);
        }
        view().setUint32(count, n, true);
        return ESUCCESS;
      },
      sched_yield: () => ESUCCESS,
      proc_exit: (code) => { throw new Exit(code); },
    };
    const wasi = new Proxy(calls, { get: (t, name) => t[name] || (() => ENOSYS) });
    const module = wasm instanceof WebAssembly.Module ? wasm : await WebAssembly.compile(wasm);
    const instance = await WebAssembly.instantiate(module, { wasi_snapshot_preview1: wasi });
    memory = instance.exports.memory;
    try {
      instance.exports._start();
      return 0;
    } catch (e) {
      if (e instanceof Exit) return e.code;
      throw e;
    } finally {
      for (const fd of [1, 2]) {
        const rest = decoders[fd].decode();
        if (rest) write(rest, fd);
      }
    }
  }

  return { run };
})();

if (typeof module !== "undefined") module.exports = NexoWeb;
