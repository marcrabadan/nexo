#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>

int main(void) {
    size_t len = 0, cap = 16;
    int64_t *xs = malloc(cap * sizeof *xs);
    for (int64_t i = 1; i <= 2000000; i++) {
        if (len == cap) { cap *= 2; xs = realloc(xs, cap * sizeof *xs); }
        xs[len++] = i * 2;
    }
    int64_t evens = 0;
    for (size_t j = 0; j < len; j++) if (xs[j] % 4 == 0) evens += xs[j] + 1;
    /* the comprehension builds a second list */
    size_t tlen = 0, tcap = 16;
    int64_t *thirds = malloc(tcap * sizeof *thirds);
    for (size_t j = 0; j < len; j++) {
        if (xs[j] % 3 != 0) continue;
        if (tlen == tcap) { tcap *= 2; thirds = realloc(thirds, tcap * sizeof *thirds); }
        thirds[tlen++] = xs[j];
    }
    printf("%lld\n%zu\n", (long long)evens, tlen);
    free(thirds);
    free(xs);
    return 0;
}
