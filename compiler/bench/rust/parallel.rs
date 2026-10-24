fn work(n: i64) -> i64 {
    let mut t: i64 = 0;
    let mut i: i64 = 0;
    while i < 5_000_000 {
        t += (i * n) % 13;
        i += 1;
    }
    t
}
fn main() {
    let ns: Vec<i64> = (1..=16).collect();
    let sums: Vec<i64> = std::thread::scope(|s| {
        let handles: Vec<_> = ns.iter().map(|n| s.spawn(move || work(*n))).collect();
        handles.into_iter().map(|h| h.join().unwrap()).collect()
    });
    println!("{}", sums.iter().sum::<i64>());
}
