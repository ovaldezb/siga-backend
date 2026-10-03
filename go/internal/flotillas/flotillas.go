// Package flotillas atiende el listado de flotillas (port de
// list_flotillas_handler en src/handlers/flotillas/flotillas_manager.py). El
// detalle, el CRUD y el portal siguen en Python.
package flotillas

import (
	"context"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo"
	"go.mongodb.org/mongo-driver/v2/mongo/options"

	"siga-backend/go/internal/platform"
)

// List atiende GET /flotillas: cada flotilla con cuántos clientes tiene y
// cuántos vehículos suman, para que la tarjeta muestre el parque vehicular sin
// abrir el detalle. No crea índices (ensure_indexes): los siguen asegurando
// los handlers Python del tenant.
func List(ctx context.Context, req platform.Request) (platform.Response, error) {
	tenantID := platform.ClaimString(platform.Claims(req), "custom:tenant_id")
	if tenantID == "" {
		return platform.JSON(req, 403, "No tenantId", nil), nil
	}
	db, err := platform.TenantDB(tenantID)
	if err != nil {
		return platform.Response{}, err
	}

	cur, err := db.Collection("flotillas").Find(ctx, bson.D{})
	if err != nil {
		return platform.Response{}, err
	}
	var docs []bson.M
	if err := cur.All(ctx, &docs); err != nil {
		return platform.Response{}, err
	}
	flotillas := make([]map[string]any, 0, len(docs))
	ids := make([]string, 0, len(docs))
	for _, d := range docs {
		f := platform.Doc(d)
		flotillas = append(flotillas, f)
		if id, ok := f["id"].(string); ok {
			ids = append(ids, id)
		}
	}
	if len(flotillas) == 0 {
		return platform.JSON(req, 200, "Flotillas obtenidas", flotillas), nil
	}

	miembros, err := miembrosPorFlotilla(ctx, db, ids)
	if err != nil {
		return platform.Response{}, err
	}
	var clientes []string
	for _, cs := range miembros {
		clientes = append(clientes, cs...)
	}
	vehiculos, err := vehiculosPorCliente(ctx, db, clientes)
	if err != nil {
		return platform.Response{}, err
	}

	for _, f := range flotillas {
		id, _ := f["id"].(string)
		var unidades int64
		for _, c := range miembros[id] {
			unidades += vehiculos[c]
		}
		f["num_clientes"] = len(miembros[id])
		f["num_vehiculos"] = unidades
	}
	return platform.JSON(req, 200, "Flotillas obtenidas", flotillas), nil
}

// miembrosPorFlotilla: ids de cliente agrupados por flotilla_id.
func miembrosPorFlotilla(ctx context.Context, db *mongo.Database, ids []string) (map[string][]string, error) {
	cur, err := db.Collection("clientes").Find(ctx,
		bson.D{{Key: "flotilla_id", Value: bson.D{{Key: "$in", Value: ids}}}},
		options.Find().SetProjection(bson.D{{Key: "flotilla_id", Value: 1}}))
	if err != nil {
		return nil, err
	}
	var docs []bson.M
	if err := cur.All(ctx, &docs); err != nil {
		return nil, err
	}
	out := make(map[string][]string)
	for _, d := range docs {
		flot, _ := d["flotilla_id"].(string)
		if oid, ok := d["_id"].(bson.ObjectID); ok {
			out[flot] = append(out[flot], oid.Hex())
		}
	}
	return out, nil
}

// vehiculosPorCliente: una sola agregación para todos los clientes.
func vehiculosPorCliente(ctx context.Context, db *mongo.Database, clientes []string) (map[string]int64, error) {
	out := make(map[string]int64)
	if len(clientes) == 0 {
		return out, nil
	}
	cur, err := db.Collection("vehiculos").Aggregate(ctx, mongo.Pipeline{
		{{Key: "$match", Value: bson.D{{Key: "cliente_id", Value: bson.D{{Key: "$in", Value: clientes}}}}}},
		{{Key: "$group", Value: bson.D{
			{Key: "_id", Value: "$cliente_id"},
			{Key: "count", Value: bson.D{{Key: "$sum", Value: 1}}},
		}}},
	})
	if err != nil {
		return nil, err
	}
	var filas []bson.M
	if err := cur.All(ctx, &filas); err != nil {
		return nil, err
	}
	for _, f := range filas {
		if c, ok := f["_id"].(string); ok {
			out[c] = int64(platform.Numero(f["count"]))
		}
	}
	return out, nil
}
