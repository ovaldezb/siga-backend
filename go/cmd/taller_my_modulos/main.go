package main

import (
	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/talleres"
)

func main() {
	platform.Start(talleres.Modulos)
}
