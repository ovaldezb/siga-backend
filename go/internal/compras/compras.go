// Package compras atiende las cuentas por pagar (port de list_cxp_handler en
// src/handlers/compras/compras_manager.py). El resto de compras sigue en Python.
package compras

import (
	"context"
	"math"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo/options"

	"siga-backend/go/internal/platform"
)

const limiteCxP = 200

// CxP atiende GET /compras/cxp[?proveedor_id=…&sucursal_id=…]: compras no
// canceladas con saldo pendiente, las más recientes primero, con el total.
func CxP(ctx context.Context, req platform.Request) (platform.Response, error) {
	tenantID := platform.ClaimString(platform.Claims(req), "custom:tenant_id")
	if tenantID == "" {
		return platform.JSON(req, 403, "No autorizado", nil), nil
	}
	qp := req.QueryStringParameters
	filtro := bson.D{
		{Key: "saldo_pendiente", Value: bson.D{{Key: "$gt", Value: 0}}},
		{Key: "estado", Value: bson.D{{Key: "$ne", Value: "CANCELADA"}}},
	}
	if v := qp["proveedor_id"]; v != "" {
		filtro = append(filtro, bson.E{Key: "proveedor_id", Value: v})
	}
	if v := qp["sucursal_id"]; v != "" {
		filtro = append(filtro, bson.E{Key: "sucursal_id", Value: v})
	}

	db, err := platform.TenantDB(tenantID)
	if err != nil {
		return platform.Response{}, err
	}
	cur, err := db.Collection("compras").Find(ctx, filtro,
		options.Find().SetSort(bson.D{{Key: "createdAt", Value: -1}}).SetLimit(limiteCxP))
	if err != nil {
		return platform.Response{}, err
	}
	var docs []bson.M
	if err := cur.All(ctx, &docs); err != nil {
		return platform.Response{}, err
	}

	items := make([]map[string]any, 0, len(docs))
	total := 0.0
	for _, d := range docs {
		total += platform.Numero(d["saldo_pendiente"])
		items = append(items, platform.Doc(d))
	}
	return platform.JSON(req, 200, "Cuentas por pagar", map[string]any{
		"items":       items,
		"total_saldo": math.Round(total*100) / 100,
		"count":       len(items),
	}), nil
}
