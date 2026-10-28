package main

import (
	"siga-backend/go/internal/alertas"
	"siga-backend/go/internal/platform"
)

func main() {
	platform.Start(alertas.PorCerrar)
}
