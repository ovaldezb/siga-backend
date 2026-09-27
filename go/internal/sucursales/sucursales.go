// Package sucursales atiende la lectura de sucursales del taller (port de
// list_sucursales_handler en src/handlers/sucursales/sucursales_manager.py).
// El alta, edición y borrado siguen en Python.
package sucursales

import (
	"context"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo"

	"siga-backend/go/internal/platform"
)

// Todas devuelve las sucursales del tenant ya serializadas (id, fechas ISO), en
// el orden natural de la colección como hacía find() en Python.
func Todas(ctx context.Context, db *mongo.Database) ([]map[string]any, error) {
	cur, err := db.Collection("sucursales").Find(ctx, bson.D{})
	if err != nil {
		return nil, err
	}
	var docs []bson.M
	if err := cur.All(ctx, &docs); err != nil {
		return nil, err
	}
	out := make([]map[string]any, 0, len(docs))
	for _, d := range docs {
		out = append(out, platform.Doc(d))
	}
	return out, nil
}

// List atiende GET /sucursales. Lo llama el login de todo ADMIN en paralelo
// con /usuarios/me y /talleres/me/modulos.
func List(ctx context.Context, req platform.Request) (platform.Response, error) {
	tenantID := platform.ClaimString(platform.Claims(req), "custom:tenant_id")
	if tenantID == "" {
		return platform.JSON(req, 403, "No se encontró un tenantId asociado.", nil), nil
	}
	db, err := platform.TenantDB(tenantID)
	if err != nil {
		return platform.Response{}, err
	}
	todas, err := Todas(ctx, db)
	if err != nil {
		return platform.Response{}, err
	}
	return platform.JSON(req, 200, "Sucursales obtenidas", todas), nil
}
