package main

import (
	"siga-backend/go/internal/citas"
	"siga-backend/go/internal/platform"
)

func main() {
	platform.Start(citas.List)
}
