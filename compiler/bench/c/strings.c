#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

int main(void) {
    size_t n = 300000;
    char **parts = malloc(n * sizeof *parts);
    for (size_t i = 1; i <= n; i++) {
        char buf[16];
        int len = snprintf(buf, sizeof buf, "w%zu", i % 500);
        parts[i - 1] = memcpy(malloc(len + 1), buf, len + 1);
    }
    int64_t total = 0;
    for (size_t i = 0; i < n; i++) total += strlen(parts[i]);
    const char *line = "a b c d e f g h";
    int64_t words = 0;
    for (int r = 0; r < 100000; r++) {
        /* split: a copy of every piece, as the other languages make */
        const char *start = line;
        for (const char *c = line;; c++) {
            if (*c == ' ' || *c == '\0') {
                size_t len = c - start;
                char *piece = malloc(len + 1);
                memcpy(piece, start, len);
                piece[len] = '\0';
                words++;
                free(piece);
                if (*c == '\0') break;
                start = c + 1;
            }
        }
    }
    printf("%lld %lld\n", (long long)total, (long long)words);
    for (size_t i = 0; i < n; i++) free(parts[i]);
    free(parts);
    return 0;
}
