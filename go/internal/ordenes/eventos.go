// Package ordenes atiende la bitácora de eventos de una OS (port de
// list_orden_events_handler y list_os_events). El resto de órdenes sigue en
// Python.
package ordenes

import (
	"context"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo"
	"go.mongodb.org/mongo-driver/v2/mongo/options"

	"siga-backend/go/internal/platform"
)

const limiteEventos = 200

// Eventos atiende GET /ordenes/{id}/events: la colección os_events en orden
// cronológico (append-only; este endpoint solo lee).
func Eventos(ctx context.Context, req platform.Request) (platform.Response, error) {
	tenantID := platform.ClaimString(platform.Claims(req), "custom:tenant_id")
	if tenantID == "" {
		return platform.JSON(req, 403, "No se encontró un tenantId asociado.", nil), nil
	}
	oid, err := bson.ObjectIDFromHex(req.PathParameters["id"])
	if err != nil {
		// Python también respondía 404 con un id mal formado.
		return platform.JSON(req, 404, "Orden no encontrada", nil), nil
	}
	db, err := platform.TenantDB(tenantID)
	if err != nil {
		return platform.Response{}, err
	}

	// Validar que la OS exista en el tenant evita filtrar eventos cruzados.
	err = db.Collection("ordenes_servicio").FindOne(ctx, bson.D{{Key: "_id", Value: oid}},
		options.FindOne().SetProjection(bson.D{{Key: "_id", Value: 1}})).Err()
	if err == mongo.ErrNoDocuments {
		return platform.JSON(req, 404, "Orden no encontrada", nil), nil
	}
	if err != nil {
		return platform.Response{}, err
	}

	cur, err := db.Collection("os_events").Find(ctx, bson.D{{Key: "orden_id", Value: oid.Hex()}},
		options.Find().SetSort(bson.D{{Key: "ts", Value: 1}}).SetLimit(limiteEventos))
	if err != nil {
		return platform.Response{}, err
	}
	var docs []bson.M
	if err := cur.All(ctx, &docs); err != nil {
		return platform.Response{}, err
	}
	eventos := make([]map[string]any, 0, len(docs))
	for _, d := range docs {
		eventos = append(eventos, platform.Doc(d))
	}
	return platform.JSON(req, 200, "Eventos de la orden", eventos), nil
}
