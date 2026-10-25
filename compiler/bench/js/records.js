class P {
  constructor(x, y) {
    this.x = x;
    this.y = y;
  }

  norm() {
    return this.x * this.x + this.y * this.y;
  }
}

let acc = 0;
for (let i = 1; i <= 2_000_000; i++) {
  const p = new P(i, i + 1);
  acc += p.norm() % 10;
}
console.log(acc);
