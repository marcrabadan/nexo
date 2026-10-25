import java.util.HashMap;
import java.util.Map;

public class Maps {
    public static void main(String[] args) {
        Map<Long, Long> counts = new HashMap<>();
        for (long i = 1; i <= 1_000_000L; i++) counts.merge(i % 1000, 1L, Long::sum);
        System.out.println(counts.size() + " " + counts.get(7L));
    }
}
