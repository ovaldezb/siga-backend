// Package inventario atiende lecturas de inventario (port de get_item_handler
// en items_manager.py y list_traspasos_handler en traspasos_manager.py). Altas,
// ajustes, listados de items y traspasos siguen en Python.
package inventario

import (
	"context"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo"
	"go.mongodb.org/mongo-driver/v2/mongo/options"

	"siga-backend/go/internal/platform"
)

const limiteTraspasos = 50

// Item atiende GET /items/{id}[?sucursalId=…&ignoreScope=1]. Un ADMIN con
// ignoreScope=1 lo busca en cualquier sucursal (búsqueda cruzada); los demás
// solo dentro de sus sucursales.
func Item(ctx context.Context, req platform.Request) (platform.Response, error) {
	claims := platform.Claims(req)
	tenantID := platform.ClaimString(claims, "custom:tenant_id")
	if tenantID == "" {
		return platform.JSON(req, 403, "No se encontró un tenantId asociado.", nil), nil
	}
	oid, err := platform.ParseObjectID(req.PathParameters["id"], "id")
	if err != nil {
		return platform.Response{}, err
	}
	db, err := platform.TenantDB(tenantID)
	if err != nil {
		return platform.Response{}, err
	}

	qp := req.QueryStringParameters
	pedida := qp["sucursalId"]
	if pedida == "" {
		pedida = qp["sucursal_id"]
	}
	filtro := bson.D{{Key: "_id", Value: oid}}
	if !(qp["ignoreScope"] == "1" && platform.IsAdmin(claims)) {
		scope, violacion, err := platform.SucursalScope(ctx, claims, db, pedida)
		if err != nil {
			return platform.Response{}, err
		}
		if violacion != "" {
			return platform.JSON(req, 403, violacion, nil), nil
		}
		if scope != nil {
			filtro = append(filtro, bson.E{Key: "sucursal_id", Value: platform.FiltroSucursal(scope)})
		}
	}

	var doc bson.M
	err = db.Collection("items").FindOne(ctx, filtro).Decode(&doc)
	if err == mongo.ErrNoDocuments {
		return platform.JSON(req, 404, "Item no encontrado.", nil), nil
	}
	if err != nil {
		return platform.Response{}, err
	}
	item := platform.Doc(doc)
	if v, ok := item["sucursal_id"]; ok {
		item["sucursalId"] = v
		delete(item, "sucursal_id")
	}
	return platform.JSON(req, 200, "Detalle del item", item), nil
}

// Traspasos atiende GET /inventario/traspasos?sucursal_id=…&tipo=…&estado=…:
// los 50 más recientes. tipo es entrantes (default), salientes o todos.
func Traspasos(ctx context.Context, req platform.Request) (platform.Response, error) {
	tenantID := platform.ClaimString(platform.Claims(req), "custom:tenant_id")
	if tenantID == "" {
		// Python no lo validaba y get_tenant_db respondía 400.
		return platform.JSON(req, 403, "No se encontró un tenantId asociado.", nil), nil
	}
	qp := req.QueryStringParameters
	filtro := bson.D{{Key: "tenant_id", Value: tenantID}}
	if sid := qp["sucursal_id"]; sid != "" {
		switch tipo := qp["tipo"]; tipo {
		case "", "entrantes":
			filtro = append(filtro, bson.E{Key: "destino_id", Value: sid})
		case "salientes":
			filtro = append(filtro, bson.E{Key: "origen_id", Value: sid})
		default:
			filtro = append(filtro, bson.E{Key: "$or", Value: bson.A{
				bson.D{{Key: "destino_id", Value: sid}},
				bson.D{{Key: "origen_id", Value: sid}},
			}})
		}
	}
	if estado := qp["estado"]; estado != "" {
		filtro = append(filtro, bson.E{Key: "estado", Value: estado})
	}

	db, err := platform.TenantDB(tenantID)
	if err != nil {
		return platform.Response{}, err
	}
	cur, err := db.Collection("traspasos").Find(ctx, filtro,
		options.Find().SetSort(bson.D{{Key: "createdAt", Value: -1}}).SetLimit(limiteTraspasos))
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
	return platform.JSON(req, 200, "Traspasos", map[string]any{"items": items}), nil
}
