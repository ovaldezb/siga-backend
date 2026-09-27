package main

import (
	"siga-backend/go/internal/autorizaciones"
	"siga-backend/go/internal/platform"
)

func main() {
	platform.Start(autorizaciones.List)
}
