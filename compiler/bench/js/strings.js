const parts = [];
for (let i = 1; i <= 300_000; i++) parts.push(`w${i % 500}`);
let total = 0;
for (const p of parts) total += p.length;
const line = "a b c d e f g h";
let n = 0;
for (let r = 0; r < 100_000; r++) n += line.split(" ").length;
console.log(total, n);
