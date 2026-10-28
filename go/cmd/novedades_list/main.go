package main

import (
	"siga-backend/go/internal/novedades"
	"siga-backend/go/internal/platform"
)

func main() {
	platform.Start(novedades.List)
}
