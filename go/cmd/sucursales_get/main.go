package main

import (
	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/sucursales"
)

func main() {
	platform.Start(sucursales.Get)
}
