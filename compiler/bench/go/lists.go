package main

import "fmt"

func main() {
	var xs []int64
	for i := int64(1); i <= 2_000_000; i++ {
		xs = append(xs, i*2)
	}
	var evens int64
	for _, x := range xs {
		if x%4 == 0 {
			evens += x + 1
		}
	}
	var thirds []int64
	for _, x := range xs {
		if x%3 == 0 {
			thirds = append(thirds, x)
		}
	}
	fmt.Println(evens)
	fmt.Println(len(thirds))
}
