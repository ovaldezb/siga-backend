// Package proveedores atiende el listado de proveedores (port de
// list_proveedores_handler en src/handlers/proveedores/proveedores_manager.py).
// El detalle vive en internal/detalle; alta, edición y borrado siguen en Python.
package proveedores

import (
	"context"
	"regexp"
	"strings"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo/options"

	"siga-backend/go/internal/platform"
)

var camposBusqueda = []string{"nombre", "contacto", "categoria", "telefono", "rfc"}

// List atiende GET /proveedores[?q=…&page=…&limit=…]: paginado por nombre, con
// búsqueda literal sin distinguir mayúsculas en nombre, contacto, categoría,
// teléfono y RFC.
func List(ctx context.Context, req platform.Request) (platform.Response, error) {
	tenantID := platform.ClaimString(platform.Claims(req), "custom:tenant_id")
	if tenantID == "" {
		return platform.JSON(req, 403, "No se encontró un tenantId asociado.", nil), nil
	}
	qp := req.QueryStringParameters
	page, limit, skip, err := platform.Paginacion(qp, 20)
	if err != nil {
		return platform.Response{}, err
	}

	filtro := bson.D{}
	if q := strings.TrimSpace(qp["q"]); q != "" {
		re := bson.Regex{Pattern: regexp.QuoteMeta(q), Options: "i"}
		or := make(bson.A, 0, len(camposBusqueda))
		for _, campo := range camposBusqueda {
			or = append(or, bson.D{{Key: campo, Value: re}})
		}
		filtro = bson.D{{Key: "$or", Value: or}}
	}

	db, err := platform.TenantDB(tenantID)
	if err != nil {
		return platform.Response{}, err
	}
	col := db.Collection("proveedores")
	total, err := col.CountDocuments(ctx, filtro)
	if err != nil {
		return platform.Response{}, err
	}
	cur, err := col.Find(ctx, filtro, options.Find().
		SetSort(bson.D{{Key: "nombre", Value: 1}}).SetSkip(skip).SetLimit(limit))
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
	return platform.JSON(req, 200, "Proveedores obtenidos", map[string]any{
		"items": items,
		"total": total,
		"page":  page,
		"limit": limit,
	}), nil
}
