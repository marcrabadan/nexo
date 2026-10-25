package main

import (
	"fmt"
	"sync"
)

func work(n int64) int64 {
	var t int64
	for i := int64(0); i < 5_000_000; i++ {
		t += (i * n) % 13
	}
	return t
}

func main() {
	sums := make([]int64, 16)
	var wg sync.WaitGroup
	for n := int64(1); n <= 16; n++ {
		wg.Add(1)
		go func(n int64) {
			defer wg.Done()
			sums[n-1] = work(n)
		}(n)
	}
	wg.Wait()
	var total int64
	for _, s := range sums {
		total += s
	}
	fmt.Println(total)
}
