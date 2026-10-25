parts = []
for i in range(1, 300_001):
    parts.append(f"w{i % 500}")
total = 0
for p in parts:
    total += len(p)
line = "a b c d e f g h"
n = 0
for _ in range(100_000):
    n += len(line.split(" "))
print(total, n)
