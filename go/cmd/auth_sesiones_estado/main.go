package main

import (
	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/sesiones"
)

func main() {
	platform.Start(sesiones.Estado)
}
