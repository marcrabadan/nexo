public class Loop {
    public static void main(String[] args) {
        long total = 0;
        for (long i = 0; i < 50_000_000L; i++) total += i % 7;
        System.out.println(total);
    }
}
