package main

import (
	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/proveedores"
)

func main() {
	platform.Start(proveedores.List)
}
