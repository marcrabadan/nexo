#include <stdio.h>
#include <stdint.h>

typedef struct { int64_t x, y; } P;

static int64_t norm(P p) { return p.x * p.x + p.y * p.y; }

int main(void) {
    int64_t acc = 0;
    for (int64_t i = 1; i <= 2000000; i++) {
        P p = { i, i + 1 };
        acc += norm(p) % 10;
    }
    printf("%lld\n", (long long)acc);
    return 0;
}
