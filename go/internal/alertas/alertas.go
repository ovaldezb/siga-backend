// Package alertas junta lo que el taller tiene pendiente y suele olvidar: cobrar
// los créditos, costear las piezas vendidas, cerrar las órdenes terminadas y
// llamar a los clientes a los que les toca servicio. Es lo que muestra la campana.
package alertas

import (
	"context"
	"math"
	"slices"
	"sort"
	"strings"
	"time"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo"
	"go.mongodb.org/mongo-driver/v2/mongo/options"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/recordatorios"
)

const (
	msgSinCobros = "Tu usuario no tiene acceso a la informacion de cobros."
	// Un crédito con más de estos días ya se considera vencido.
	diasVencido   = 30
	topClientes   = 5
	limiteVentas  = 2000
	limiteOrdenes = 500
)

var ventaNoVigente = bson.A{"CANCELADA", "ANULADA"}

func redondear(n float64) float64 { return math.Round(n*100) / 100 }

// contexto común: tenant, scope de sucursal y rechazo a mecánicos (no ven importes).
func preparar(ctx context.Context, req platform.Request) (*mongo.Database, []string, *platform.Response, error) {
	claims := platform.Claims(req)
	tenantID := platform.ClaimString(claims, "custom:tenant_id")
	if tenantID == "" {
		r := platform.JSON(req, 403, "No autorizado", nil)
		return nil, nil, &r, nil
	}
	if platform.EsMecanico(claims) {
		r := platform.JSON(req, 403, msgSinCobros, nil)
		return nil, nil, &r, nil
	}
	db, err := platform.TenantDB(tenantID)
	if err != nil {
		return nil, nil, nil, err
	}
	scope, violacion, err := platform.SucursalScope(ctx, claims, db, req.QueryStringParameters["sucursal_id"])
	if err != nil {
		return nil, nil, nil, err
	}
	if violacion != "" {
		r := platform.JSON(req, 403, violacion, nil)
		return nil, nil, &r, nil
	}
	return db, scope, nil, nil
}

func conScope(filtro bson.D, scope []string) bson.D {
	if scope != nil {
		filtro = append(filtro, bson.E{Key: "sucursal_id", Value: platform.FiltroSucursal(scope)})
	}
	return filtro
}

// Resumen atiende GET /alertas[?sucursal_id=…].
func Resumen(ctx context.Context, req platform.Request) (platform.Response, error) {
	db, scope, corto, err := preparar(ctx, req)
	if err != nil || corto != nil {
		return deref(corto), err
	}
	ahora := time.Now()

	cobranza, err := resumenCobranza(ctx, db, scope, ahora)
	if err != nil {
		return platform.Response{}, err
	}
	costos, err := resumenCostos(ctx, db, scope)
	if err != nil {
		return platform.Response{}, err
	}
	ordenes, err := ordenesPorCerrar(ctx, db, scope)
	if err != nil {
		return platform.Response{}, err
	}
	pendientes, err := recordatorios.Pendientes(ctx, db, scope, false, ahora)
	if err != nil {
		return platform.Response{}, err
	}
	vencidos := 0
	for _, r := range pendientes {
		if r["estado"] == recordatorios.EstadoVencido {
			vencidos++
		}
	}

	aCredito, sinVenta := 0, 0
	saldoCredito, totalSinVenta := 0.0, 0.0
	for _, o := range ordenes {
		if o["tipo"] == tipoCredito {
			aCredito++
			saldoCredito += platform.Numero(o["saldo_pendiente"])
		} else {
			sinVenta++
			totalSinVenta += platform.Numero(o["total"])
		}
	}

	return platform.JSON(req, 200, "Alertas del taller", map[string]any{
		"cobranza":          cobranza,
		"costos_pendientes": costos,
		"ordenes_por_cerrar": map[string]any{
			"total":     len(ordenes),
			"a_credito": map[string]any{"count": aCredito, "saldo": redondear(saldoCredito)},
			"sin_venta": map[string]any{"count": sinVenta, "total": redondear(totalSinVenta)},
		},
		"recordatorios": map[string]any{"total": len(pendientes), "vencidos": vencidos},
	}), nil
}

func deref(r *platform.Response) platform.Response {
	if r == nil {
		return platform.Response{}
	}
	return *r
}

// resumenCobranza: ventas con saldo, sin las anuladas ni las de OS canceladas
// (mismo criterio que la cartera de Contabilidad).
func resumenCobranza(ctx context.Context, db *mongo.Database, scope []string, ahora time.Time) (map[string]any, error) {
	filtro := conScope(bson.D{
		{Key: "saldo_pendiente", Value: bson.D{{Key: "$gt", Value: 0}}},
		{Key: "estado", Value: bson.D{{Key: "$nin", Value: ventaNoVigente}}},
	}, scope)
	cur, err := db.Collection("ventas").Find(ctx, filtro, options.Find().
		SetProjection(bson.D{{Key: "cliente_id", Value: 1}, {Key: "cliente_nombre", Value: 1},
			{Key: "saldo_pendiente", Value: 1}, {Key: "createdAt", Value: 1}, {Key: "orden_id", Value: 1}}).
		SetLimit(limiteVentas))
	if err != nil {
		return nil, err
	}
	var ventas []bson.M
	if err := cur.All(ctx, &ventas); err != nil {
		return nil, err
	}
	canceladas, err := ordenesCanceladas(ctx, db, ventas)
	if err != nil {
		return nil, err
	}

	type cliente struct {
		id, nombre    string
		saldo         float64
		docs, diasMax int
	}
	porCliente := map[string]*cliente{}
	total, vencido := 0.0, 0.0
	docs, docsVencidos := 0, 0
	for _, v := range ventas {
		if oid, _ := v["orden_id"].(string); oid != "" && canceladas[oid] {
			continue
		}
		saldo := platform.Numero(v["saldo_pendiente"])
		dias := 0
		if f, ok := v["createdAt"].(bson.DateTime); ok {
			dias = int(ahora.Sub(f.Time()).Hours() / 24)
		}
		total += saldo
		docs++
		if dias > diasVencido {
			vencido += saldo
			docsVencidos++
		}
		cid, _ := v["cliente_id"].(string)
		if cid == "" {
			cid = "PUBLICO_GENERAL"
		}
		c := porCliente[cid]
		if c == nil {
			nombre, _ := v["cliente_nombre"].(string)
			if nombre == "" {
				nombre = "Público General"
			}
			c = &cliente{id: cid, nombre: nombre}
			porCliente[cid] = c
		}
		c.saldo += saldo
		c.docs++
		if dias > c.diasMax {
			c.diasMax = dias
		}
	}

	lista := make([]*cliente, 0, len(porCliente))
	for _, c := range porCliente {
		lista = append(lista, c)
	}
	sort.Slice(lista, func(i, j int) bool { return lista[i].saldo > lista[j].saldo })
	if len(lista) > topClientes {
		lista = lista[:topClientes]
	}
	clientes := make([]map[string]any, 0, len(lista))
	for _, c := range lista {
		clientes = append(clientes, map[string]any{
			"cliente_id": c.id, "cliente_nombre": c.nombre, "saldo": redondear(c.saldo),
			"documentos": c.docs, "dias_max": c.diasMax,
		})
	}
	return map[string]any{
		"saldo_total": redondear(total),
		"documentos":  docs,
		"vencido":     map[string]any{"saldo": redondear(vencido), "documentos": docsVencidos, "dias": diasVencido},
		"clientes":    clientes,
	}, nil
}

func ordenesCanceladas(ctx context.Context, db *mongo.Database, ventas []bson.M) (map[string]bool, error) {
	var ids bson.A
	for _, v := range ventas {
		if s, _ := v["orden_id"].(string); s != "" {
			if oid, err := bson.ObjectIDFromHex(s); err == nil {
				ids = append(ids, oid)
			}
		}
	}
	out := map[string]bool{}
	if len(ids) == 0 {
		return out, nil
	}
	cur, err := db.Collection("ordenes_servicio").Find(ctx, bson.D{
		{Key: "_id", Value: bson.D{{Key: "$in", Value: ids}}},
		{Key: "estado", Value: "CANCELADO"},
	}, options.Find().SetProjection(bson.D{{Key: "_id", Value: 1}}))
	if err != nil {
		return nil, err
	}
	var docs []bson.M
	if err := cur.All(ctx, &docs); err != nil {
		return nil, err
	}
	for _, d := range docs {
		if oid, ok := d["_id"].(bson.ObjectID); ok {
			out[oid.Hex()] = true
		}
	}
	return out, nil
}

// resumenCostos: líneas vendidas sin precio de entrada (mismo criterio que
// GET /ventas/costos-pendientes). Mientras existan, la utilidad sale inflada.
func resumenCostos(ctx context.Context, db *mongo.Database, scope []string) (map[string]any, error) {
	filtro := conScope(bson.D{
		{Key: "items.costo_pendiente", Value: true},
		{Key: "estado", Value: bson.D{{Key: "$nin", Value: ventaNoVigente}}},
	}, scope)
	cur, err := db.Collection("ventas").Aggregate(ctx, mongo.Pipeline{
		{{Key: "$match", Value: filtro}},
		{{Key: "$unwind", Value: "$items"}},
		{{Key: "$match", Value: bson.D{{Key: "items.costo_pendiente", Value: true}}}},
		{{Key: "$group", Value: bson.D{
			{Key: "_id", Value: nil},
			{Key: "lineas", Value: bson.D{{Key: "$sum", Value: 1}}},
			{Key: "importe", Value: bson.D{{Key: "$sum", Value: bson.D{{Key: "$multiply", Value: bson.A{
				bson.D{{Key: "$convert", Value: bson.D{{Key: "input", Value: "$items.precio_unitario"}, {Key: "to", Value: "double"}, {Key: "onError", Value: 0}, {Key: "onNull", Value: 0}}}},
				bson.D{{Key: "$convert", Value: bson.D{{Key: "input", Value: bson.D{{Key: "$ifNull", Value: bson.A{"$items.cantidad", 1}}}}, {Key: "to", Value: "double"}, {Key: "onError", Value: 1}, {Key: "onNull", Value: 1}}}},
			}}}}}},
		}}},
	})
	if err != nil {
		return nil, err
	}
	var filas []bson.M
	if err := cur.All(ctx, &filas); err != nil {
		return nil, err
	}
	lineas, importe := 0.0, 0.0
	if len(filas) > 0 {
		lineas = platform.Numero(filas[0]["lineas"])
		importe = platform.Numero(filas[0]["importe"])
	}
	return map[string]any{"lineas": int(lineas), "importe_venta": redondear(importe)}, nil
}

const (
	tipoCredito  = "CREDITO"
	tipoSinVenta = "SIN_VENTA"
)

// ordenesPorCerrar: OS en FINALIZADO. Con venta son créditos que esperan su
// liquidación; sin venta nunca pasaron por caja.
func ordenesPorCerrar(ctx context.Context, db *mongo.Database, scope []string) ([]map[string]any, error) {
	cur, err := db.Collection("ordenes_servicio").Find(ctx, conScope(bson.D{{Key: "estado", Value: "FINALIZADO"}}, scope),
		options.Find().SetProjection(bson.D{
			{Key: "folio", Value: 1}, {Key: "total", Value: 1}, {Key: "venta_id", Value: 1}, {Key: "venta_folio", Value: 1},
			{Key: "saldo_pendiente", Value: 1}, {Key: "cliente_id", Value: 1}, {Key: "cliente_snapshot", Value: 1},
			{Key: "vehiculo_snapshot", Value: 1}, {Key: "bitacora_estados", Value: 1}, {Key: "createdAt", Value: 1},
			{Key: "updatedAt", Value: 1}, {Key: "sucursal_id", Value: 1},
		}).SetSort(bson.D{{Key: "updatedAt", Value: 1}}).SetLimit(limiteOrdenes))
	if err != nil {
		return nil, err
	}
	var docs []bson.M
	if err := cur.All(ctx, &docs); err != nil {
		return nil, err
	}
	out := make([]map[string]any, 0, len(docs))
	for _, d := range docs {
		o := platform.Doc(d)
		tipo := tipoSinVenta
		if s, _ := o["venta_id"].(string); s != "" {
			tipo = tipoCredito
		}
		cli, _ := o["cliente_snapshot"].(map[string]any)
		veh, _ := o["vehiculo_snapshot"].(map[string]any)
		out = append(out, map[string]any{
			"id":               o["id"],
			"folio":            o["folio"],
			"tipo":             tipo,
			"total":            redondear(platform.Numero(o["total"])),
			"saldo_pendiente":  redondear(platform.Numero(o["saldo_pendiente"])),
			"venta_id":         o["venta_id"],
			"venta_folio":      o["venta_folio"],
			"sucursal_id":      o["sucursal_id"],
			"cliente_id":       o["cliente_id"],
			"cliente_nombre":   nombreCliente(cli),
			"vehiculo":         descripcionVehiculo(veh),
			"fecha_finalizado": fechaFinalizado(o),
		})
	}
	return out, nil
}

func nombreCliente(c map[string]any) string {
	return unir(c, "nombre", "apellido_paterno", "apellido_materno")
}

func descripcionVehiculo(v map[string]any) string {
	return unir(v, "marca", "modelo", "placas")
}

// unir junta los campos de texto presentes, sin espacios repetidos.
func unir(m map[string]any, claves ...string) string {
	partes := make([]string, 0, len(claves))
	for _, k := range claves {
		if s, _ := m[k].(string); s != "" {
			partes = append(partes, s)
		}
	}
	return strings.Join(strings.Fields(strings.Join(partes, " ")), " ")
}

// fechaFinalizado: la última transición a FINALIZADO de la bitácora; si no hay,
// la última modificación de la OS.
func fechaFinalizado(o map[string]any) any {
	if b, ok := o["bitacora_estados"].([]any); ok {
		for _, raw := range slices.Backward(b) {
			if e, ok := raw.(map[string]any); ok && e["estado"] == "FINALIZADO" && e["fecha"] != nil {
				return e["fecha"]
			}
		}
	}
	if o["updatedAt"] != nil {
		return o["updatedAt"]
	}
	return o["createdAt"]
}

// PorCerrar atiende GET /ordenes/por-cerrar[?sucursal_id=…]: las OS en FINALIZADO
// para cerrarlas en bloque (liquidar créditos o cobrar las que no tienen venta).
func PorCerrar(ctx context.Context, req platform.Request) (platform.Response, error) {
	db, scope, corto, err := preparar(ctx, req)
	if err != nil || corto != nil {
		return deref(corto), err
	}
	ordenes, err := ordenesPorCerrar(ctx, db, scope)
	if err != nil {
		return platform.Response{}, err
	}
	return platform.JSON(req, 200, "Órdenes por cerrar", map[string]any{
		"items": ordenes,
		"count": len(ordenes),
	}), nil
}
