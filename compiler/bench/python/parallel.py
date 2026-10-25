# 16 tasks on 4 processes: threads would share one interpreter lock.
from multiprocessing import Pool


def work(n):
    t = 0
    i = 0
    while i < 5_000_000:
        t += (i * n) % 13
        i += 1
    return t


if __name__ == "__main__":
    with Pool() as pool:
        print(sum(pool.map(work, range(1, 17))))
