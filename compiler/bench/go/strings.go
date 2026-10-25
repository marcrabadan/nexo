package main

import (
	"fmt"
	"strings"
)

func main() {
	var parts []string
	for i := 1; i <= 300_000; i++ {
		parts = append(parts, fmt.Sprintf("w%d", i%500))
	}
	total := 0
	for _, p := range parts {
		total += len(p)
	}
	line := "a b c d e f g h"
	n := 0
	for r := 0; r < 100_000; r++ {
		n += len(strings.Split(line, " "))
	}
	fmt.Println(total, n)
}
