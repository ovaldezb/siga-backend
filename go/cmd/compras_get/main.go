package main

import (
	"siga-backend/go/internal/detalle"
	"siga-backend/go/internal/platform"
)

func main() {
	platform.Start(detalle.Compra.Handler())
}
