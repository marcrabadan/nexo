const counts = new Map();
for (let i = 1; i <= 1_000_000; i++) {
  const k = i % 1000;
  counts.set(k, (counts.get(k) ?? 0) + 1);
}
console.log(counts.size, counts.get(7));
