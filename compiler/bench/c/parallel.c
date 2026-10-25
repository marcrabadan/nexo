#include <pthread.h>
#include <stdio.h>
#include <stdint.h>

static int64_t results[16];

static void *work(void *arg) {
    int64_t n = (int64_t)(intptr_t)arg, t = 0;
    for (int64_t i = 0; i < 5000000; i++) t += (i * n) % 13;
    results[n - 1] = t;
    return NULL;
}

int main(void) {
    pthread_t threads[16];
    for (int64_t n = 1; n <= 16; n++) pthread_create(&threads[n - 1], NULL, work, (void *)(intptr_t)n);
    int64_t total = 0;
    for (int n = 0; n < 16; n++) { pthread_join(threads[n], NULL); total += results[n]; }
    printf("%lld\n", (long long)total);
    return 0;
}
