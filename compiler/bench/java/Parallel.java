import java.util.stream.LongStream;

public class Parallel {
    static long work(long n) {
        long t = 0;
        for (long i = 0; i < 5_000_000L; i++) t += (i * n) % 13;
        return t;
    }

    public static void main(String[] args) throws Exception {
        long[] sums = new long[16];
        Thread[] threads = new Thread[16];
        for (int n = 1; n <= 16; n++) {
            final int k = n;
            threads[n - 1] = new Thread(() -> sums[k - 1] = work(k));
            threads[n - 1].start();
        }
        long total = 0;
        for (int n = 0; n < 16; n++) {
            threads[n].join();
            total += sums[n];
        }
        System.out.println(total);
    }
}
