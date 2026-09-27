package main

import (
	"siga-backend/go/internal/ordenes"
	"siga-backend/go/internal/platform"
)

func main() {
	platform.Start(ordenes.Eventos)
}
