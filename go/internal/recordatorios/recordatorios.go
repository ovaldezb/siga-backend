// Package recordatorios arma la lista de vehículos a los que les toca servicio
// (cambio de aceite o bujías por km o por fecha, o mucho tiempo sin venir) para
// que el taller llame o mande WhatsApp al dueño. El seguimiento (contactado,
// pospuesto, descartado) lo escribe Python en `vehiculos.recordatorio_servicio`.
package recordatorios

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"time"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/vehiculos"
)

const (
	// Mismos umbrales por default que el filtro de mantenimiento de Vehículos.
	diasVencido = 365
	kmUmbral    = 500
	diasPronto  = 180
	limite      = 300

	EstadoVencido = "vencido"
	EstadoPronto  = "pronto"
)

// Pendientes devuelve los vehículos del scope a los que les toca servicio, sin
// los que están en el taller (OS abierta) ni los que tienen un seguimiento
// vigente, salvo que se pidan con incluirSeguimiento. `ahora` se inyecta para
// las pruebas.
func Pendientes(ctx context.Context, db *mongo.Database, scope []string, incluirSeguimiento bool, ahora time.Time) ([]map[string]any, error) {
	etapas, err := vehiculos.EtapasMantenimiento(diasVencido, kmUmbral, diasPronto)
	if err != nil {
		return nil, err
	}
	filtro := bson.D{}
	if scope != nil {
		filtro = append(filtro, bson.E{Key: "sucursal_id", Value: platform.FiltroSucursal(scope)})
	}
	pipeline := append(mongo.Pipeline{{{Key: "$match", Value: filtro}}}, etapas...)
	cur, err := db.Collection("vehiculos").Aggregate(ctx, pipeline)
	if err != nil {
		return nil, err
	}
	var docs []bson.M
	if err := cur.All(ctx, &docs); err != nil {
		return nil, err
	}

	enTaller, err := vehiculosEnTaller(ctx, db)
	if err != nil {
		return nil, err
	}

	mes := ahora.UTC().Format("2006-01")
	mesSiguiente := ahora.UTC().AddDate(0, 1, 0).Format("2006-01")
	items := make([]map[string]any, 0)
	for _, d := range docs {
		v := platform.Doc(d)
		id, _ := v["id"].(string)
		if enTaller[id] {
			continue
		}
		seguimiento, _ := v["recordatorio_servicio"].(map[string]any)
		if !incluirSeguimiento && seguimientoVigente(d["recordatorio_servicio"], ahora) {
			continue
		}
		estado, motivos := evaluar(v, mes, mesSiguiente)
		if estado == "" {
			continue
		}
		items = append(items, map[string]any{
			"id":                          id,
			"placas":                      v["placas"],
			"marca":                       v["marca"],
			"modelo":                      v["modelo"],
			"anio":                        primero(v["anio"], v["año"]),
			"color":                       v["color"],
			"numero_economico":            v["numero_economico"],
			"sucursal_id":                 v["sucursal_id"],
			"cliente_id":                  v["cliente_id"],
			"cliente_nombre":              strings.Join(strings.Fields(fmt.Sprint(primero(v["cliente_nombre"], ""))), " "),
			"cliente_telefono":            v["cliente_telefono"],
			"kilometraje":                 v["kilometraje"],
			"proximo_cambio_aceite":       v["proximo_cambio_aceite"],
			"proximo_cambio_aceite_fecha": v["proximo_cambio_aceite_fecha"],
			"proximo_cambio_bujias":       v["proximo_cambio_bujias"],
			"proximo_cambio_bujias_fecha": v["proximo_cambio_bujias_fecha"],
			"km_para_aceite":              v["km_para_aceite"],
			"dias_desde_ultima_visita":    v["dias_desde_ultima_visita"],
			"estado":                      estado,
			"motivos":                     motivos,
			"recordatorio_servicio":       seguimiento,
		})
	}

	// Primero lo vencido; dentro de cada grupo, lo más urgente (menos km para el
	// aceite y, si no hay km, más días sin venir).
	sort.SliceStable(items, func(i, j int) bool {
		a, b := items[i], items[j]
		if a["estado"] != b["estado"] {
			return a["estado"] == EstadoVencido
		}
		ka, okA := kmRestante(a)
		kb, okB := kmRestante(b)
		if okA != okB {
			return okA
		}
		if okA && ka != kb {
			return ka < kb
		}
		return platform.Numero(a["dias_desde_ultima_visita"]) > platform.Numero(b["dias_desde_ultima_visita"])
	})
	if len(items) > limite {
		items = items[:limite]
	}
	return items, nil
}

// evaluar decide si al vehículo le toca servicio y por qué. Coincide con
// mantenimiento_status de Vehículos y además mira las fechas de próximo cambio
// (YYYY-MM), que ese filtro no considera.
func evaluar(v map[string]any, mes, mesSiguiente string) (string, []string) {
	var vencidos, prontos []string
	km := platform.Numero(v["kilometraje"])

	porKm := func(etiqueta string, proximo any) {
		p := platform.Numero(proximo)
		if p <= 0 || km <= 0 {
			return
		}
		switch falta := p - km; {
		case falta <= 0:
			vencidos = append(vencidos, fmt.Sprintf("%s vencido por kilometraje (tocaba a los %s km)", etiqueta, miles(p)))
		case falta <= kmUmbral:
			prontos = append(prontos, fmt.Sprintf("%s en %s km", etiqueta, miles(falta)))
		}
	}
	porFecha := func(etiqueta string, fecha any) {
		f, _ := fecha.(string)
		if len(f) < 7 {
			return
		}
		switch f = f[:7]; {
		case f <= mes:
			vencidos = append(vencidos, fmt.Sprintf("%s programado para %s", etiqueta, f))
		case f == mesSiguiente:
			prontos = append(prontos, fmt.Sprintf("%s programado para el próximo mes", etiqueta))
		}
	}
	porKm("Cambio de aceite", v["proximo_cambio_aceite"])
	porFecha("Cambio de aceite", v["proximo_cambio_aceite_fecha"])
	porKm("Cambio de bujías", v["proximo_cambio_bujias"])
	porFecha("Cambio de bujías", v["proximo_cambio_bujias_fecha"])

	if dias, ok := v["dias_desde_ultima_visita"]; ok && dias != nil {
		switch d := platform.Numero(dias); {
		case d >= diasVencido:
			vencidos = append(vencidos, fmt.Sprintf("Sin venir hace %d días", int(d)))
		case d >= diasPronto:
			prontos = append(prontos, fmt.Sprintf("Sin venir hace %d días", int(d)))
		}
	}

	if len(vencidos) > 0 {
		return EstadoVencido, append(vencidos, prontos...)
	}
	if len(prontos) > 0 {
		return EstadoPronto, prontos
	}
	return "", nil
}

// seguimientoVigente: el taller ya lo contactó, lo pospuso o lo descartó y la
// fecha `hasta` aún no llega. Sin `hasta` (descartado) no vuelve a aparecer.
func seguimientoVigente(raw any, ahora time.Time) bool {
	seg := aMapa(raw)
	if seg == nil || seg["estado"] == nil {
		return false
	}
	switch h := seg["hasta"].(type) {
	case bson.DateTime:
		return h.Time().After(ahora)
	case time.Time:
		return h.After(ahora)
	}
	return seg["estado"] == "DESCARTADO"
}

func aMapa(raw any) bson.M {
	switch x := raw.(type) {
	case bson.M:
		return x
	case bson.D:
		m := bson.M{}
		for _, e := range x {
			m[e.Key] = e.Value
		}
		return m
	}
	return nil
}

// vehiculosEnTaller: los que tienen una OS abierta ya están siendo atendidos.
func vehiculosEnTaller(ctx context.Context, db *mongo.Database) (map[string]bool, error) {
	res := db.Collection("ordenes_servicio").Distinct(ctx, "vehiculo_id", bson.D{
		{Key: "estado", Value: bson.D{{Key: "$nin", Value: bson.A{"ENTREGADO", "CANCELADO"}}}},
	})
	var ids []any
	if err := res.Decode(&ids); err != nil {
		return nil, err
	}
	out := make(map[string]bool, len(ids))
	for _, id := range ids {
		if s, ok := id.(string); ok && s != "" {
			out[s] = true
		}
	}
	return out, nil
}

func kmRestante(item map[string]any) (float64, bool) {
	v, ok := item["km_para_aceite"]
	if !ok || v == nil {
		return 0, false
	}
	return platform.Numero(v), true
}

func primero(vals ...any) any {
	for _, v := range vals {
		if v != nil {
			return v
		}
	}
	return nil
}

func miles(n float64) string {
	s := fmt.Sprintf("%d", int64(n))
	var b strings.Builder
	for i, r := range s {
		if i > 0 && (len(s)-i)%3 == 0 {
			b.WriteByte(',')
		}
		b.WriteRune(r)
	}
	return b.String()
}

// List atiende GET /recordatorios-servicio[?sucursal_id=…&todos=1]: vehículos a
// los que les toca servicio. `todos=1` incluye los que ya tienen seguimiento.
func List(ctx context.Context, req platform.Request) (platform.Response, error) {
	claims := platform.Claims(req)
	tenantID := platform.ClaimString(claims, "custom:tenant_id")
	if tenantID == "" {
		return platform.JSON(req, 403, "No autorizado", nil), nil
	}
	db, err := platform.TenantDB(tenantID)
	if err != nil {
		return platform.Response{}, err
	}
	qp := req.QueryStringParameters
	scope, violacion, err := platform.SucursalScope(ctx, claims, db, qp["sucursal_id"])
	if err != nil {
		return platform.Response{}, err
	}
	if violacion != "" {
		return platform.JSON(req, 403, violacion, nil), nil
	}
	items, err := Pendientes(ctx, db, scope, platform.Verdadero(qp["todos"]), time.Now())
	if err != nil {
		return platform.Response{}, err
	}
	vencidos := 0
	for _, it := range items {
		if it["estado"] == EstadoVencido {
			vencidos++
		}
	}
	return platform.JSON(req, 200, "Recordatorios de servicio", map[string]any{
		"items":    items,
		"total":    len(items),
		"vencidos": vencidos,
	}), nil
}
