// Package compras atiende el listado y las cuentas por pagar (port de
// list_compras_handler y list_cxp_handler en src/handlers/compras/compras_manager.py).
// El detalle vive en internal/detalle; alta, pagos y cancelación siguen en Python.
package compras

import (
	"context"
	"math"
	"regexp"
	"strings"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo/options"

	"siga-backend/go/internal/platform"
)

const (
	limiteCxP         = 200
	limiteListado     = 50
	limiteListadoTope = 200
)

// List atiende GET /compras[?proveedor_id=…&sucursal_id=…&estado=…&search=…&page=…&limit=…]:
// paginado de la más reciente a la más vieja, con un tope de 200 por página.
func List(ctx context.Context, req platform.Request) (platform.Response, error) {
	tenantID := platform.ClaimString(platform.Claims(req), "custom:tenant_id")
	if tenantID == "" {
		return platform.JSON(req, 403, "No autorizado", nil), nil
	}
	qp := req.QueryStringParameters
	page, limit, _, err := platform.Paginacion(qp, limiteListado)
	if err != nil {
		return platform.Response{}, err
	}
	limit = min(limit, limiteListadoTope)
	skip := (page - 1) * limit // Paginacion ya rechazó las páginas que dan skip negativo

	filtro := bson.D{}
	if v := qp["proveedor_id"]; v != "" {
		filtro = append(filtro, bson.E{Key: "proveedor_id", Value: v})
	}
	if v := qp["sucursal_id"]; v != "" {
		filtro = append(filtro, bson.E{Key: "sucursal_id", Value: v})
	}
	if v := qp["estado"]; v != "" {
		filtro = append(filtro, bson.E{Key: "estado", Value: strings.ToUpper(v)})
	}
	if s := qp["search"]; s != "" {
		// Python pasaba el texto crudo como regex: un "(" o un "+" del folio
		// reventaba la consulta con 500. Aquí se busca literal.
		re := bson.Regex{Pattern: regexp.QuoteMeta(s), Options: "i"}
		filtro = append(filtro, bson.E{Key: "$or", Value: bson.A{
			bson.D{{Key: "folio", Value: re}},
			bson.D{{Key: "referencia_proveedor", Value: re}},
			bson.D{{Key: "proveedor_snapshot.nombre", Value: re}},
		}})
	}

	db, err := platform.TenantDB(tenantID)
	if err != nil {
		return platform.Response{}, err
	}
	col := db.Collection("compras")
	total, err := col.CountDocuments(ctx, filtro)
	if err != nil {
		return platform.Response{}, err
	}
	cur, err := col.Find(ctx, filtro, options.Find().
		SetSort(bson.D{{Key: "createdAt", Value: -1}}).SetSkip(skip).SetLimit(limit))
	if err != nil {
		return platform.Response{}, err
	}
	var docs []bson.M
	if err := cur.All(ctx, &docs); err != nil {
		return platform.Response{}, err
	}
	items := make([]map[string]any, 0, len(docs))
	for _, d := range docs {
		items = append(items, platform.Doc(d))
	}
	return platform.JSON(req, 200, "Compras obtenidas", map[string]any{
		"items": items,
		"total": total,
		"page":  page,
		"limit": limit,
	}), nil
}

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
