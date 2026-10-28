package main

import (
	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/recordatorios"
)

func main() {
	platform.Start(recordatorios.List)
}
