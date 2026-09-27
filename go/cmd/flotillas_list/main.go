package main

import (
	"siga-backend/go/internal/flotillas"
	"siga-backend/go/internal/platform"
)

func main() {
	platform.Start(flotillas.List)
}
