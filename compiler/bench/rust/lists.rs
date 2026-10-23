fn main() {
    let mut xs: Vec<i64> = Vec::new();
    for i in 1..=2_000_000i64 {
        xs.push(i * 2);
    }
    let evens: i64 = xs.iter().filter(|x| *x % 4 == 0).map(|x| x + 1).sum();
    println!("{}", evens);
    let threes: Vec<i64> = xs.iter().filter(|x| *x % 3 == 0).cloned().collect();
    println!("{}", threes.len());
}
