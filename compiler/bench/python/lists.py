xs = []
for i in range(1, 2_000_001):
    xs.append(i * 2)
evens = sum(x + 1 for x in xs if x % 4 == 0)
print(evens)
print(len([x for x in xs if x % 3 == 0]))
