import java.util.ArrayList;
import java.util.List;

public class Lists {
    public static void main(String[] args) {
        List<Long> xs = new ArrayList<>();
        for (long i = 1; i <= 2_000_000L; i++) xs.add(i * 2);
        long evens = xs.stream().filter(x -> x % 4 == 0).mapToLong(x -> x + 1).sum();
        List<Long> thirds = new ArrayList<>();
        for (long x : xs) if (x % 3 == 0) thirds.add(x);
        System.out.println(evens);
        System.out.println(thirds.size());
    }
}
