package main

import (
	"siga-backend/go/internal/clientes"
	"siga-backend/go/internal/platform"
)

func main() {
	platform.Start(clientes.Get)
}
