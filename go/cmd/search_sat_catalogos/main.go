package main

import (
	"siga-backend/go/internal/catalogos"
	"siga-backend/go/internal/platform"
)

func main() {
	platform.Start(catalogos.SearchSAT)
}