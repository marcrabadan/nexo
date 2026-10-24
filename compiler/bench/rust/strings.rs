fn main() {
    let mut parts: Vec<String> = Vec::new();
    for i in 1..=300_000i64 {
        parts.push(format!("w{}", i % 500));
    }
    let mut total: i64 = 0;
    for p in &parts {
        total += p.chars().count() as i64;
    }
    let line = String::from("a b c d e f g h");
    let mut n: i64 = 0;
    for _ in 0..100_000 {
        let words: Vec<String> = line.split(' ').map(String::from).collect();
        n += words.len() as i64;
    }
    println!("{} {}", total, n);
}
