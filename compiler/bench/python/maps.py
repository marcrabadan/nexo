counts = {}
for i in range(1, 1_000_001):
    k = i % 1000
    counts[k] = counts.get(k, 0) + 1
print(len(counts), counts[7])
