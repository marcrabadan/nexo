#include <stdio.h>
#include <stdint.h>

int main(void) {
    int64_t total = 0;
    for (int64_t i = 0; i < 50000000; i++) total += i % 7;
    printf("%lld\n", (long long)total);
    return 0;
}
