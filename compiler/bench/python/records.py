class P:
    __slots__ = ("x", "y")

    def __init__(self, x, y):
        self.x = x
        self.y = y

    def norm(self):
        return self.x * self.x + self.y * self.y


acc = 0
for i in range(1, 2_000_001):
    p = P(i, i + 1)
    acc += p.norm() % 10
print(acc)
