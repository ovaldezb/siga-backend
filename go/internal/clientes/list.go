package clientes

import (
	"context"
	"math"
	"regexp"
	"strings"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo"
	"go.mongodb.org/mongo-driver/v2/mongo/options"

	"siga-backend/go/internal/platform"
)

// List atiende GET /clientes[?q=…&sucursalId=…&page=…&limit=…] (port de
// list_clientes_handler). La cartera es del taller, no de la sucursal: solo se
// filtra por sucursal si se pide una, y ésa se valida contra las del usuario.
// Cada cliente trae sus vehículos, cotizaciones pendientes y saldo de crédito.
// No llama ensure_indexes: los siguen asegurando los handlers Python del tenant.
func List(ctx context.Context, req platform.Request) (platform.Response, error) {
	claims := platform.Claims(req)
	tenantID := platform.ClaimString(claims, "custom:tenant_id")
	if tenantID == "" {
		return platform.JSON(req, 403, "No se encontró un tenantId asociado.", nil), nil
	}
	qp := req.QueryStringParameters
	q := strings.TrimSpace(qp["q"])
	pedida := qp["sucursalId"]
	if pedida == "" {
		pedida = qp["sucursal_id"]
	}
	page, limit, skip, err := platform.Paginacion(qp, 20)
	if err != nil {
		return platform.Response{}, err
	}
	db, err := platform.TenantDB(tenantID)
	if err != nil {
		return platform.Response{}, err
	}

	filtro := bson.D{}
	if pedida != "" {
		scope, violacion, err := platform.SucursalScope(ctx, claims, db, pedida)
		if err != nil {
			return platform.Response{}, err
		}
		if violacion != "" {
			return platform.JSON(req, 403, violacion, nil), nil
		}
		if len(scope) > 0 {
			filtro = bson.D{{Key: "sucursal_id", Value: platform.FiltroSucursal(scope)}}
		}
	}
	if q != "" {
		re := bson.Regex{Pattern: regexp.QuoteMeta(q), Options: "i"}
		busqueda := bson.D{{Key: "$or", Value: bson.A{
			bson.D{{Key: "nombre", Value: re}},
			bson.D{{Key: "apellido_paterno", Value: re}},
			bson.D{{Key: "telefono", Value: re}},
		}}}
		if len(filtro) > 0 {
			filtro = bson.D{{Key: "$and", Value: bson.A{filtro, busqueda}}}
		} else {
			filtro = busqueda
		}
	}

	col := db.Collection("clientes")
	total, err := col.CountDocuments(ctx, filtro)
	if err != nil {
		return platform.Response{}, err
	}
	cur, err := col.Find(ctx, filtro, options.Find().SetSkip(skip).SetLimit(limit))
	if err != nil {
		return platform.Response{}, err
	}
	var docs []bson.M
	if err := cur.All(ctx, &docs); err != nil {
		return platform.Response{}, err
	}
	clientes := make([]map[string]any, 0, len(docs))
	ids := make([]string, 0, len(docs))
	for _, d := range docs {
		c := platform.Doc(d)
		if v, ok := c["sucursal_id"]; ok {
			c["sucursalId"] = v
			delete(c, "sucursal_id")
		}
		id, _ := c["id"].(string)
		ids = append(ids, id)
		clientes = append(clientes, c)
	}

	vehiculos, cotizaciones := map[string]float64{}, map[string]float64{}
	saldos := map[string]float64{}
	if len(ids) > 0 {
		if vehiculos, err = contarPor(ctx, db.Collection("vehiculos"),
			bson.D{{Key: "cliente_id", Value: bson.D{{Key: "$in", Value: ids}}}}, "$cliente_id", 1); err != nil {
			return platform.Response{}, err
		}
		// Cotizaciones pendientes: OS en RECEPCION o COTIZADO, antes de APROBADO.
		if cotizaciones, err = contarPor(ctx, db.Collection("ordenes_servicio"), bson.D{
			{Key: "cliente_snapshot.id", Value: bson.D{{Key: "$in", Value: ids}}},
			{Key: "estado", Value: bson.D{{Key: "$in", Value: bson.A{"RECEPCION", "COTIZADO"}}}},
		}, "$cliente_snapshot.id", 1); err != nil {
			return platform.Response{}, err
		}
		// Saldo deudor de CxC: mismo criterio que ventas_manager al validar crédito.
		if saldos, err = contarPor(ctx, db.Collection("ventas"), bson.D{
			{Key: "cliente_id", Value: bson.D{{Key: "$in", Value: ids}}},
			{Key: "saldo_pendiente", Value: bson.D{{Key: "$gt", Value: 0}}},
		}, "$cliente_id", "$saldo_pendiente"); err != nil {
			return platform.Response{}, err
		}
	}
	for i, c := range clientes {
		c["num_vehiculos"] = int64(vehiculos[ids[i]])
		c["cotizaciones_pendientes"] = int64(cotizaciones[ids[i]])
		c["saldo_credito"] = math.Round(saldos[ids[i]]*100) / 100
	}

	totalPages := int64(0)
	if limit > 0 {
		totalPages = (total + limit - 1) / limit
	}
	return platform.JSON(req, 200, "Clientes obtenidos", map[string]any{
		"items":      clientes,
		"total":      total,
		"page":       page,
		"limit":      limit,
		"totalPages": totalPages,
	}), nil
}

// contarPor agrupa por `campo` sumando `suma` (1 para contar, o un campo) en
// una sola agregación para toda la página.
func contarPor(ctx context.Context, col *mongo.Collection, match bson.D, campo string, suma any) (map[string]float64, error) {
	cur, err := col.Aggregate(ctx, mongo.Pipeline{
		{{Key: "$match", Value: match}},
		{{Key: "$group", Value: bson.D{{Key: "_id", Value: campo}, {Key: "n", Value: bson.D{{Key: "$sum", Value: suma}}}}}},
	})
	if err != nil {
		return nil, err
	}
	var filas []bson.M
	if err := cur.All(ctx, &filas); err != nil {
		return nil, err
	}
	out := make(map[string]float64, len(filas))
	for _, f := range filas {
		if id, ok := f["_id"].(string); ok {
			out[id] = platform.Numero(f["n"])
		}
	}
	return out, nil
}
