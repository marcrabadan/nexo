import java.util.ArrayList;
import java.util.List;

public class Strings {
    public static void main(String[] args) {
        List<String> parts = new ArrayList<>();
        for (int i = 1; i <= 300_000; i++) parts.add("w" + (i % 500));
        long total = 0;
        for (String p : parts) total += p.length();
        String line = "a b c d e f g h";
        long n = 0;
        for (int r = 0; r < 100_000; r++) n += line.split(" ").length;
        System.out.println(total + " " + n);
    }
}
