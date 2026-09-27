package main

import (
	"siga-backend/go/internal/cotizaciones"
	"siga-backend/go/internal/platform"
)

func main() {
	platform.Start(cotizaciones.List)
}
