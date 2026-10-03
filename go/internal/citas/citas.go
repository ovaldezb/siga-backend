// Package citas atiende el listado de citas (port de list_citas_handler en
// src/handlers/citas/citas_manager.py). El detalle vive en internal/detalle;
// alta, edición y borrado siguen en Python.
package citas

import (
	"context"
	"regexp"
	"strings"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo"
	"go.mongodb.org/mongo-driver/v2/mongo/options"

	"siga-backend/go/internal/platform"
)

// List atiende GET /citas. Filtros: sucursal_id (con el scope del usuario), q,
// estado (o "todos"), estado_in y estado_ne (CSV, para las pestañas),
// fecha_desde y fecha_hasta (texto YYYY-MM-DD), page y limit. Ordena de la cita
// más reciente a la más vieja y agrega datos del cliente para la fila.
func List(ctx context.Context, req platform.Request) (platform.Response, error) {
	claims := platform.Claims(req)
	tenantID := platform.ClaimString(claims, "custom:tenant_id")
	if tenantID == "" {
		return platform.JSON(req, 403, "No se encontró un tenantId asociado.", nil), nil
	}
	qp := req.QueryStringParameters
	page, limit, skip, err := platform.Paginacion(qp, 100)
	if err != nil {
		return platform.Response{}, err
	}
	db, err := platform.TenantDB(tenantID)
	if err != nil {
		return platform.Response{}, err
	}
	scope, violacion, err := platform.SucursalScope(ctx, claims, db, qp["sucursal_id"])
	if err != nil {
		return platform.Response{}, err
	}
	if violacion != "" {
		return platform.JSON(req, 403, violacion, nil), nil
	}

	filtro := filtroCitas(qp, scope)
	col := db.Collection("citas")
	total, err := col.CountDocuments(ctx, filtro)
	if err != nil {
		return platform.Response{}, err
	}
	cur, err := col.Find(ctx, filtro, options.Find().
		SetSort(bson.D{{Key: "fecha", Value: -1}, {Key: "horaInicio", Value: -1}, {Key: "createdAt", Value: -1}}).
		SetSkip(skip).SetLimit(limit))
	if err != nil {
		return platform.Response{}, err
	}
	var docs []bson.M
	if err := cur.All(ctx, &docs); err != nil {
		return platform.Response{}, err
	}
	citas := make([]map[string]any, 0, len(docs))
	for _, d := range docs {
		citas = append(citas, platform.Doc(d))
	}

	if err := enriquecer(ctx, db, citas); err != nil {
		return platform.Response{}, err
	}
	return platform.JSON(req, 200, "Citas obtenidas", map[string]any{
		"items": citas,
		"total": total,
		"page":  page,
		"limit": limit,
	}), nil
}

func filtroCitas(qp map[string]string, scope []string) bson.D {
	filtro := bson.D{}
	var y bson.A
	if scope != nil {
		y = append(y, bson.D{{Key: "sucursal_id", Value: platform.FiltroSucursal(scope)}})
	}
	if q := strings.TrimSpace(qp["q"]); q != "" {
		re := bson.Regex{Pattern: regexp.QuoteMeta(q), Options: "i"}
		y = append(y, bson.D{{Key: "$or", Value: bson.A{
			bson.D{{Key: "clienteNombre", Value: re}},
			bson.D{{Key: "servicio", Value: re}},
			bson.D{{Key: "tecnicoNombre", Value: re}},
		}}})
	}
	if len(y) > 0 {
		filtro = append(filtro, bson.E{Key: "$and", Value: y})
	}

	// estado, estado_in y estado_ne se combinan igual que en Python: estado_in
	// reemplaza al estado exacto, y estado_ne se agrega a un $in o reemplaza al
	// estado exacto.
	var estado any
	if e := strings.TrimSpace(qp["estado"]); e != "" && e != "todos" {
		estado = e
	}
	if in := csv(qp["estado_in"]); len(in) > 0 {
		estado = bson.D{{Key: "$in", Value: in}}
	}
	if ne := csv(qp["estado_ne"]); len(ne) > 0 {
		if d, ok := estado.(bson.D); ok {
			estado = append(d, bson.E{Key: "$nin", Value: ne})
		} else {
			estado = bson.D{{Key: "$nin", Value: ne}}
		}
	}
	if estado != nil {
		filtro = append(filtro, bson.E{Key: "estado", Value: estado})
	}

	desde, hasta := strings.TrimSpace(qp["fecha_desde"]), strings.TrimSpace(qp["fecha_hasta"])
	if desde != "" || hasta != "" {
		rango := bson.D{}
		if desde != "" {
			rango = append(rango, bson.E{Key: "$gte", Value: desde})
		}
		if hasta != "" {
			rango = append(rango, bson.E{Key: "$lte", Value: hasta})
		}
		filtro = append(filtro, bson.E{Key: "fecha", Value: rango})
	}
	return filtro
}

func csv(s string) []string {
	var out []string
	for _, p := range strings.Split(s, ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

// enriquecer agrega a cada cita el teléfono del cliente (botón de WhatsApp),
// cuántos vehículos tiene, sus OS en taller y sus cotizaciones pendientes. Sin
// cliente, num_vehiculos vale 1 como en Python.
func enriquecer(ctx context.Context, db *mongo.Database, citas []map[string]any) error {
	vistos := map[string]bool{}
	var ids []string
	var oids []bson.ObjectID
	for _, c := range citas {
		id, _ := c["clienteId"].(string)
		if id == "" || vistos[id] {
			continue
		}
		vistos[id] = true
		ids = append(ids, id)
		if oid, err := bson.ObjectIDFromHex(id); err == nil {
			oids = append(oids, oid)
		}
	}

	telefonos := map[string]string{}
	vehiculos, enTaller, cotizaciones := map[string]int64{}, map[string]int64{}, map[string]int64{}
	if len(ids) > 0 {
		if len(oids) > 0 {
			cur, err := db.Collection("clientes").Find(ctx, bson.D{{Key: "_id", Value: bson.D{{Key: "$in", Value: oids}}}},
				options.Find().SetProjection(bson.D{{Key: "telefono", Value: 1}}))
			if err != nil {
				return err
			}
			var docs []bson.M
			if err := cur.All(ctx, &docs); err != nil {
				return err
			}
			for _, d := range docs {
				if oid, ok := d["_id"].(bson.ObjectID); ok {
					t, _ := d["telefono"].(string)
					telefonos[oid.Hex()] = t
				}
			}
		}
		var err error
		if vehiculos, err = contar(ctx, db.Collection("vehiculos"),
			bson.D{{Key: "cliente_id", Value: bson.D{{Key: "$in", Value: ids}}}}, "$cliente_id"); err != nil {
			return err
		}
		porEstados := func(estados ...string) (map[string]int64, error) {
			return contar(ctx, db.Collection("ordenes_servicio"), bson.D{
				{Key: "cliente_snapshot.id", Value: bson.D{{Key: "$in", Value: ids}}},
				{Key: "estado", Value: bson.D{{Key: "$in", Value: estados}}},
			}, "$cliente_snapshot.id")
		}
		if enTaller, err = porEstados("APROBADO", "EN_PROCESO"); err != nil {
			return err
		}
		if cotizaciones, err = porEstados("RECEPCION", "COTIZADO"); err != nil {
			return err
		}
	}

	for _, c := range citas {
		id, _ := c["clienteId"].(string)
		if id == "" {
			c["num_vehiculos"], c["os_pendientes"], c["cotizaciones_pendientes"], c["cliente_telefono"] = 1, 0, 0, ""
			continue
		}
		n, ok := vehiculos[id]
		if !ok {
			n = 1
		}
		c["num_vehiculos"] = n
		c["os_pendientes"] = enTaller[id]
		c["cotizaciones_pendientes"] = cotizaciones[id]
		c["cliente_telefono"] = telefonos[id]
	}
	return nil
}

func contar(ctx context.Context, col *mongo.Collection, match bson.D, campo string) (map[string]int64, error) {
	cur, err := col.Aggregate(ctx, mongo.Pipeline{
		{{Key: "$match", Value: match}},
		{{Key: "$group", Value: bson.D{{Key: "_id", Value: campo}, {Key: "n", Value: bson.D{{Key: "$sum", Value: 1}}}}}},
	})
	if err != nil {
		return nil, err
	}
	var filas []bson.M
	if err := cur.All(ctx, &filas); err != nil {
		return nil, err
	}
	out := make(map[string]int64, len(filas))
	for _, f := range filas {
		if id, ok := f["_id"].(string); ok {
			out[id] = int64(platform.Numero(f["n"]))
		}
	}
	return out, nil
}
