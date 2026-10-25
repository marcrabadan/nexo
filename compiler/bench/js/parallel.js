// 16 tasks on worker threads, the results in task order.
const { Worker, isMainThread, parentPort, workerData } = require("worker_threads");

function work(n) {
  let t = 0;
  for (let i = 0; i < 5_000_000; i++) t += (i * n) % 13;
  return t;
}

if (isMainThread) {
  const tasks = [];
  for (let n = 1; n <= 16; n++) {
    tasks.push(new Promise((resolve, reject) => {
      const w = new Worker(__filename, { workerData: n });
      w.on("message", resolve);
      w.on("error", reject);
    }));
  }
  Promise.all(tasks).then((sums) => console.log(sums.reduce((a, b) => a + b, 0)));
} else {
  parentPort.postMessage(work(workerData));
}
