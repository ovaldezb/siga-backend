package main

import (
	"siga-backend/go/internal/compras"
	"siga-backend/go/internal/platform"
)

func main() {
	platform.Start(compras.CxP)
}
