// Package clientes atiende el detalle de un cliente (port de get_cliente_handler
// en src/handlers/clientes/clientes_manager.py). El listado y la edición siguen
// en Python.
package clientes

import (
	"context"
	"math"
	"strconv"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo"

	"siga-backend/go/internal/platform"
)

// Get atiende GET /clientes/{id}. El POS lo consulta al cargar una OS para
// conocer el crédito disponible real (`limite_credito - saldo_credito`).
func Get(ctx context.Context, req platform.Request) (platform.Response, error) {
	tenantID := platform.ClaimString(platform.Claims(req), "custom:tenant_id")
	if tenantID == "" {
		// Python no lo validaba y get_tenant_db tronaba con ValueError.
		return platform.JSON(req, 403, "No se encontró un tenantId asociado.", nil), nil
	}
	oid, err := bson.ObjectIDFromHex(req.PathParameters["id"])
	if err != nil {
		return platform.JSON(req, 400, "ID inválido.", nil), nil
	}
	db, err := platform.TenantDB(tenantID)
	if err != nil {
		return platform.Response{}, err
	}

	var doc bson.M
	err = db.Collection("clientes").FindOne(ctx, bson.D{{Key: "_id", Value: oid}}).Decode(&doc)
	if err == mongo.ErrNoDocuments {
		return platform.JSON(req, 404, "Cliente no encontrado.", nil), nil
	}
	if err != nil {
		return platform.Response{}, err
	}

	cliente := platform.Doc(doc)
	if v, ok := cliente["sucursal_id"]; ok {
		cliente["sucursalId"] = v
		delete(cliente, "sucursal_id")
	}

	saldo, err := saldoCredito(ctx, db, oid.Hex())
	if err != nil {
		return platform.Response{}, err
	}
	cliente["saldo_credito"] = saldo
	return platform.JSON(req, 200, "Detalle del cliente", cliente), nil
}

// saldoCredito suma el saldo pendiente de sus ventas: mismo criterio que el
// listado y que ventas_manager al autorizar crédito.
func saldoCredito(ctx context.Context, db *mongo.Database, clienteID string) (float64, error) {
	cur, err := db.Collection("ventas").Aggregate(ctx, mongo.Pipeline{
		{{Key: "$match", Value: bson.D{
			{Key: "cliente_id", Value: clienteID},
			{Key: "saldo_pendiente", Value: bson.D{{Key: "$gt", Value: 0}}},
		}}},
		{{Key: "$group", Value: bson.D{
			{Key: "_id", Value: nil},
			{Key: "saldo", Value: bson.D{{Key: "$sum", Value: "$saldo_pendiente"}}},
		}}},
	})
	if err != nil {
		return 0, err
	}
	var filas []bson.M
	if err := cur.All(ctx, &filas); err != nil {
		return 0, err
	}
	if len(filas) == 0 {
		return 0, nil
	}
	return math.Round(numero(filas[0]["saldo"])*100) / 100, nil
}

// numero lee el resultado de $sum, que Mongo devuelve como int32, int64,
// double o decimal según los valores sumados.
func numero(v any) float64 {
	switch x := v.(type) {
	case float64:
		return x
	case int32:
		return float64(x)
	case int64:
		return float64(x)
	case bson.Decimal128:
		r, _ := strconv.ParseFloat(x.String(), 64)
		return r
	}
	return 0
}
