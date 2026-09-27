package main

import (
	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/usuarios"
)

func main() {
	platform.Start(usuarios.Me)
}
