struct P {
    x: i64,
    y: i64,
}
impl P {
    fn norm(&self) -> i64 {
        self.x * self.x + self.y * self.y
    }
}
fn main() {
    let mut acc: i64 = 0;
    for i in 1..=2_000_000i64 {
        // records are shared values in Nexo: compare with a heap value
        // (black_box keeps the allocation, which rustc would otherwise remove)
        let p = std::hint::black_box(Box::new(P { x: i, y: i + 1 }));
        acc += p.norm() % 10;
    }
    println!("{}", acc);
}
