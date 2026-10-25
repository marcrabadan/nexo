const xs = [];
for (let i = 1; i <= 2_000_000; i++) xs.push(i * 2);
const evens = xs.filter((x) => x % 4 === 0).map((x) => x + 1).reduce((a, b) => a + b, 0);
console.log(evens);
console.log(xs.filter((x) => x % 3 === 0).length);
