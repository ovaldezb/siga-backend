package catalogos

import (
	"context"
	"strings"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo"
	"go.mongodb.org/mongo-driver/v2/mongo/options"

	"siga-backend/go/internal/platform"
)

// MarcasProductos atiende GET /catalogos/marcas-productos[?todas=true]: el
// catálogo de marcas de productos del taller (configuracion.marcas). Por defecto
// solo las activas; con todas=true también las inactivas, para administrarlas.
// El alta y la edición siguen en configuracion_manager.py.
func MarcasProductos(ctx context.Context, req platform.Request) (platform.Response, error) {
	tenantID := platform.ClaimString(platform.Claims(req), "custom:tenant_id")
	if tenantID == "" {
		return platform.JSON(req, 403, "No autorizado", nil), nil
	}
	var todas bool
	switch strings.ToLower(req.QueryStringParameters["todas"]) {
	case "1", "true", "yes":
		todas = true
	}

	db, err := platform.TenantDB(tenantID)
	if err != nil {
		return platform.Response{}, err
	}
	var config bson.M
	err = db.Collection("configuracion").FindOne(ctx, bson.D{{Key: "tenant_id", Value: tenantID}},
		options.FindOne().SetProjection(bson.D{{Key: "marcas", Value: 1}})).Decode(&config)
	if err != nil && err != mongo.ErrNoDocuments {
		return platform.Response{}, err
	}

	crudas, _ := platform.Doc(config)["marcas"].([]any)
	marcas := make([]any, 0, len(crudas))
	for _, m := range crudas {
		if todas || activa(m) {
			marcas = append(marcas, m)
		}
	}
	return platform.JSON(req, 200, "Marcas de productos", map[string]any{"marcas": marcas}), nil
}

// activa replica m.get('activa', True): sin la clave cuenta como activa y
// cualquier valor "falsy" de Python (false, null, 0, "") la desactiva.
func activa(m any) bool {
	marca, ok := m.(map[string]any)
	if !ok {
		return false // Python tronaba con .get sobre algo que no es dict
	}
	v, ok := marca["activa"]
	if !ok {
		return true
	}
	switch x := v.(type) {
	case bool:
		return x
	case nil:
		return false
	case string:
		return x != ""
	}
	return platform.Numero(v) != 0
}
