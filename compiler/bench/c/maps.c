/* A small open-addressing hash map from int64 to int64, as C has none. */
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>

typedef struct { int64_t key, value; int used; } Slot;
typedef struct { Slot *slots; size_t cap, len; } Map;

static size_t hash(int64_t k, size_t cap) { return (size_t)((uint64_t)k * 11400714819323198485ull) & (cap - 1); }

static Slot *find(Map *m, int64_t k) {
    size_t h = hash(k, m->cap);
    while (m->slots[h].used && m->slots[h].key != k) h = (h + 1) & (m->cap - 1);
    return &m->slots[h];
}

static void grow(Map *m) {
    Map bigger = { calloc(m->cap * 2, sizeof(Slot)), m->cap * 2, 0 };
    for (size_t i = 0; i < m->cap; i++)
        if (m->slots[i].used) { *find(&bigger, m->slots[i].key) = m->slots[i]; bigger.len++; }
    free(m->slots);
    *m = bigger;
}

int main(void) {
    Map counts = { calloc(16, sizeof(Slot)), 16, 0 };
    for (int64_t i = 1; i <= 1000000; i++) {
        int64_t k = i % 1000;
        Slot *s = find(&counts, k);
        if (!s->used) {
            if ((counts.len + 1) * 2 > counts.cap) { grow(&counts); s = find(&counts, k); }
            s->used = 1; s->key = k; s->value = 0; counts.len++;
        }
        s->value += 1;
    }
    printf("%zu %lld\n", counts.len, (long long)find(&counts, 7)->value);
    free(counts.slots);
    return 0;
}
