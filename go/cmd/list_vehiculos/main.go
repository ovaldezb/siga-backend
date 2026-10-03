package main

import (
	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/vehiculos"
)

func main() {
	platform.Start(vehiculos.List)
}
