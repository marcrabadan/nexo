package main

import "fmt"

func main() {
	counts := map[int64]int64{}
	for i := int64(1); i <= 1_000_000; i++ {
		counts[i%1000]++
	}
	fmt.Println(len(counts), counts[7])
}
