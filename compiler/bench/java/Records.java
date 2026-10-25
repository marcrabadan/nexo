public class Records {
    record P(long x, long y) {
        long norm() {
            return x * x + y * y;
        }
    }

    public static void main(String[] args) {
        long acc = 0;
        for (long i = 1; i <= 2_000_000L; i++) {
            P p = new P(i, i + 1);
            acc += p.norm() % 10;
        }
        System.out.println(acc);
    }
}
