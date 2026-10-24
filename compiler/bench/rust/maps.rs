use std::collections::HashMap;
fn main() {
    let mut counts: HashMap<i64, i64> = HashMap::new();
    for i in 1..=1_000_000i64 {
        let k = i % 1000;
        *counts.entry(k).or_insert(0) += 1;
    }
    println!("{} {}", counts.len(), counts[&7]);
}
