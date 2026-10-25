package main

import "fmt"

func main() {
	var total int64
	for i := int64(0); i < 50_000_000; i++ {
		total += i % 7
	}
	fmt.Println(total)
}
