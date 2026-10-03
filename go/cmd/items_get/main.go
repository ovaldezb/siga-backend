package main

import (
	"siga-backend/go/internal/inventario"
	"siga-backend/go/internal/platform"
)

func main() {
	platform.Start(inventario.Item)
}
