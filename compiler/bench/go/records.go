package main

import "fmt"

type P struct{ x, y int64 }

func (p P) norm() int64 { return p.x*p.x + p.y*p.y }

func main() {
	var acc int64
	for i := int64(1); i <= 2_000_000; i++ {
		p := P{i, i + 1}
		acc += p.norm() % 10
	}
	fmt.Println(acc)
}
