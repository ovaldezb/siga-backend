package ordenes

import (
	"context"
	"math"
	"regexp"
	"slices"
	"sort"
	"strings"
	"time"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo"
	"go.mongodb.org/mongo-driver/v2/mongo/options"

	"siga-backend/go/internal/platform"
)

// Tablero del taller (kanban): una columna por estado vivo de la OS. Port de
// _tablero_response en ordenes_manager.py, que sigue atendiendo
// GET /ordenes?vista=tablero para fronts viejos en caché.

// ENTREGADO y CANCELADO no son columnas: el auto ya salió del taller.
var estadosTablero = []string{"RECEPCION", "COTIZADO", "APROBADO", "EN_PROCESO", "FINALIZADO"}

const (
	// Un taller con más autos vivos que esto necesita filtros, no un tablero.
	maxTarjetas = 300
	// Ventana y tope del histórico con el que se calcula el tiempo típico por estado.
	diasHistorico = 90
	maxHistorico  = 1000
)

var proyeccionTablero = bson.D{
	{Key: "folio", Value: 1}, {Key: "estado", Value: 1}, {Key: "createdAt", Value: 1},
	{Key: "updatedAt", Value: 1}, {Key: "bitacora_estados", Value: 1},
	{Key: "cliente_snapshot.nombre", Value: 1}, {Key: "cliente_snapshot.apellido_paterno", Value: 1},
	{Key: "cliente_snapshot.telefono", Value: 1},
	{Key: "vehiculo_snapshot.marca", Value: 1}, {Key: "vehiculo_snapshot.modelo", Value: 1},
	{Key: "vehiculo_snapshot.anio", Value: 1}, {Key: "vehiculo_snapshot.placas", Value: 1},
	{Key: "vehiculo_snapshot.color", Value: 1},
	{Key: "mecanico_id", Value: 1}, {Key: "mecanico_nombre", Value: 1}, {Key: "fechaEstimadaEntrega", Value: 1},
	{Key: "total", Value: 1}, {Key: "pagada", Value: 1}, {Key: "saldo_pendiente", Value: 1},
	{Key: "falla_reportada", Value: 1},
}

// Campos con importe a nivel de la orden (_ORDEN_CAMPOS_DINERO).
var camposDinero = []string{"total", "subtotal", "iva", "anticipo", "costo_revision",
	"saldo_pendiente", "monto_credito", "pago_info"}

var camposBusqueda = []string{"folio", "cliente_snapshot.nombre", "cliente_snapshot.apellido_paterno",
	"vehiculo_snapshot.marca", "vehiculo_snapshot.modelo", "vehiculo_snapshot.placas"}

// ahora es reemplazable en pruebas.
var ahora = func() time.Time { return time.Now().UTC() }

// Tablero atiende GET /ordenes/tablero[?sucursal_id=…&mecanico_id=…&q=…&estado=…]:
// tarjetas ligeras de las OS que siguen en el taller, cada una con cuánto lleva
// en su estado, más el tiempo típico por estado para marcar dónde se atoran.
//
// PENDIENTE (igual que en Python): no aplica el scope de sucursal del usuario,
// solo filtra por la sucursal_id que manda el front. Ver la nota en
// list_ordenes_handler antes de cambiarlo.
func Tablero(ctx context.Context, req platform.Request) (platform.Response, error) {
	claims := platform.Claims(req)
	tenantID := platform.ClaimString(claims, "custom:tenant_id")
	if tenantID == "" {
		return platform.JSON(req, 403, "No se encontró un tenantId asociado.", nil), nil
	}
	db, err := platform.TenantDB(tenantID)
	if err != nil {
		return platform.Response{}, err
	}
	qp := req.QueryStringParameters
	now := ahora()

	col := db.Collection("ordenes_servicio")
	filtro := filtroTablero(qp)
	total, err := col.CountDocuments(ctx, filtro)
	if err != nil {
		return platform.Response{}, err
	}
	cur, err := col.Find(ctx, filtro, options.Find().SetProjection(proyeccionTablero).
		SetSort(bson.D{{Key: "createdAt", Value: 1}}).SetLimit(maxTarjetas))
	if err != nil {
		return platform.Response{}, err
	}
	var docs []bson.M
	if err := cur.All(ctx, &docs); err != nil {
		return platform.Response{}, err
	}

	sinImportes := platform.EsMecanico(claims)
	items := make([]map[string]any, 0, len(docs))
	for _, d := range docs {
		items = append(items, tarjeta(d, now, sinImportes))
	}

	tipicas, err := horasTipicas(ctx, col, qp["sucursal_id"], now)
	if err != nil {
		return platform.Response{}, err
	}
	return platform.JSON(req, 200, "Tablero recuperado", map[string]any{
		"items":         items,
		"total":         total,
		"truncado":      total > int64(len(items)),
		"horas_tipicas": tipicas,
	}), nil
}

// filtroTablero arma el mismo $and que list_ordenes_handler con los filtros que
// usa el tablero; sin estado explícito, solo los estados que son columnas.
func filtroTablero(qp map[string]string) bson.D {
	var y bson.A
	if s := qp["sucursal_id"]; s != "" {
		y = append(y, bson.D{{Key: "sucursal_id", Value: s}})
	}
	if m := qp["mecanico_id"]; m != "" {
		y = append(y, bson.D{{Key: "mecanico_id", Value: m}})
	}
	var estados []string
	for _, e := range strings.Split(qp["estado"], ",") {
		if e = strings.TrimSpace(e); e != "" {
			estados = append(estados, e)
		}
	}
	switch {
	case len(estados) == 1:
		y = append(y, bson.D{{Key: "estado", Value: estados[0]}})
	case len(estados) > 1:
		y = append(y, bson.D{{Key: "estado", Value: bson.D{{Key: "$in", Value: estados}}}})
	}
	if q := qp["q"]; q != "" {
		re := bson.Regex{Pattern: regexp.QuoteMeta(q), Options: "i"}
		or := make(bson.A, 0, len(camposBusqueda))
		for _, c := range camposBusqueda {
			or = append(or, bson.D{{Key: c, Value: re}})
		}
		y = append(y, bson.D{{Key: "$or", Value: or}})
	}
	if qp["estado"] == "" {
		y = append(y, bson.D{{Key: "estado", Value: bson.D{{Key: "$in", Value: estadosTablero}}}})
	}
	return bson.D{{Key: "$and", Value: y}}
}

func tarjeta(d bson.M, now time.Time, sinImportes bool) map[string]any {
	desde, hayDesde := entradaAlEstado(d)
	ingreso, hayIngreso := fechaUTC(d["createdAt"])
	entrega, hayEntrega := fechaUTC(d["fechaEstimadaEntrega"])

	o := platform.Doc(d)
	delete(o, "bitacora_estados")
	o["en_estado_desde"] = nil
	o["horas_en_estado"] = nil
	o["dias_en_taller"] = nil
	if hayDesde {
		o["en_estado_desde"] = platform.IsoUTC(desde)
		o["horas_en_estado"] = redondear1(now.Sub(desde).Hours())
	}
	if hayIngreso {
		o["dias_en_taller"] = diasCompletos(now.Sub(ingreso))
	}
	o["atrasada"] = hayEntrega && entrega.Before(now) && o["estado"] != "FINALIZADO"
	if sinImportes {
		for _, c := range camposDinero {
			delete(o, c)
		}
	}
	return o
}

// entradaAlEstado: la última entrada de la bitácora con el estado actual; las OS
// viejas sin bitácora caen a createdAt.
func entradaAlEstado(d bson.M) (time.Time, bool) {
	estado := d["estado"]
	bitacora := lista(d["bitacora_estados"])
	for i := len(bitacora) - 1; i >= 0; i-- {
		e := documento(bitacora[i])
		if e != nil && e["estado"] == estado {
			if f, ok := fechaUTC(e["fecha"]); ok {
				return f, true
			}
		}
	}
	return fechaUTC(d["createdAt"])
}

type tramo struct {
	fecha  time.Time
	estado string
}

// horasTipicas: mediana de horas que las OS cerradas en los últimos 90 días
// pasaron en cada estado, sacada de la bitácora (un estado dura hasta la
// siguiente entrada). Mediana y no promedio: un auto olvidado un mes en el patio
// arrastraría el promedio y pintaría todo el tablero de verde.
func horasTipicas(ctx context.Context, col *mongo.Collection, sucursalID string, now time.Time) (map[string]any, error) {
	desde := now.Add(-diasHistorico * 24 * time.Hour)
	filtro := bson.D{
		{Key: "estado", Value: bson.D{{Key: "$in", Value: bson.A{"FINALIZADO", "ENTREGADO"}}}},
		// updatedAt existe como Date y como texto ISO según quién escribió la OS.
		{Key: "$or", Value: bson.A{
			bson.D{{Key: "updatedAt", Value: bson.D{{Key: "$gte", Value: desde}}}},
			bson.D{{Key: "updatedAt", Value: bson.D{{Key: "$gte", Value: isoformatPython(desde)}}}},
		}},
	}
	if sucursalID != "" {
		filtro = append(filtro, bson.E{Key: "sucursal_id", Value: sucursalID})
	}
	cur, err := col.Find(ctx, filtro, options.Find().
		SetProjection(bson.D{{Key: "bitacora_estados", Value: 1}, {Key: "_id", Value: 0}}).
		SetSort(bson.D{{Key: "updatedAt", Value: -1}}).SetLimit(maxHistorico))
	if err != nil {
		return nil, err
	}
	var docs []bson.M
	if err := cur.All(ctx, &docs); err != nil {
		return nil, err
	}

	duraciones := map[string][]float64{}
	for _, d := range docs {
		var tramos []tramo
		for _, raw := range lista(d["bitacora_estados"]) {
			e := documento(raw)
			if e == nil {
				continue
			}
			if f, ok := fechaUTC(e["fecha"]); ok {
				estado, _ := e["estado"].(string)
				tramos = append(tramos, tramo{f, estado})
			}
		}
		sort.SliceStable(tramos, func(a, b int) bool {
			if !tramos[a].fecha.Equal(tramos[b].fecha) {
				return tramos[a].fecha.Before(tramos[b].fecha)
			}
			return tramos[a].estado < tramos[b].estado
		})
		for i := 0; i+1 < len(tramos); i++ {
			ini, fin := tramos[i], tramos[i+1]
			if slices.Contains(estadosTablero, ini.estado) && fin.fecha.After(ini.fecha) {
				duraciones[ini.estado] = append(duraciones[ini.estado], fin.fecha.Sub(ini.fecha).Hours())
			}
		}
	}

	tipicas := map[string]any{}
	for estado, horas := range duraciones {
		sort.Float64s(horas)
		mitad := len(horas) / 2
		mediana := horas[mitad]
		if len(horas)%2 == 0 {
			mediana = (horas[mitad-1] + horas[mitad]) / 2
		}
		tipicas[estado] = map[string]any{"horas": redondear1(mediana), "muestras": len(horas)}
	}
	return tipicas, nil
}

// redondear1 replica round(x, 1) de Python (mitades al par).
func redondear1(x float64) float64 { return math.RoundToEven(x*10) / 10 }

// diasCompletos replica timedelta.days: días enteros hacia abajo (también si es negativo).
func diasCompletos(d time.Duration) int64 { return int64(math.Floor(d.Hours() / 24)) }

func lista(v any) []any {
	switch x := v.(type) {
	case bson.A:
		return x
	case []any:
		return x
	}
	return nil
}

func documento(v any) map[string]any {
	switch x := v.(type) {
	case bson.M:
		return x
	case map[string]any:
		return x
	case bson.D:
		m := make(map[string]any, len(x))
		for _, e := range x {
			m[e.Key] = e.Value
		}
		return m
	}
	return nil
}

// fechaUTC replica _a_utc_naive: fecha de Mongo o texto ISO (con o sin Z u
// offset, que sí se aplica) llevado a UTC. false si no se reconoce.
func fechaUTC(v any) (time.Time, bool) {
	switch x := v.(type) {
	case bson.DateTime:
		return x.Time().UTC(), true
	case time.Time:
		return x.UTC(), true
	case string:
		return parseISO(x)
	}
	return time.Time{}, false
}

// Formatos que acepta datetime.fromisoformat (Python 3.13) en los datos reales:
// separador T o espacio, fracción opcional y offset Z, ±HH:MM, ±HHMM o ±HH.
var formatosISO = func() []string {
	var out []string
	for _, sep := range []string{"T", " "} {
		for _, hora := range []string{"15:04:05.999999999", "15:04", "15"} {
			for _, off := range []string{"Z07:00", "-0700", "-07", ""} {
				out = append(out, "2006-01-02"+sep+hora+off)
			}
		}
	}
	return append(out, "2006-01-02")
}()

func parseISO(s string) (time.Time, bool) {
	s = strings.TrimSpace(s)
	if s == "" {
		return time.Time{}, false
	}
	for _, f := range formatosISO {
		if t, err := time.Parse(f, s); err == nil {
			return t.UTC(), true
		}
	}
	return time.Time{}, false
}

// isoformatPython replica datetime.isoformat() de un datetime sin zona: sin
// fracción si los microsegundos son 0, con seis dígitos si no, y sin "Z".
func isoformatPython(t time.Time) string {
	t = t.UTC()
	if t.Nanosecond()/1000 == 0 {
		return t.Format("2006-01-02T15:04:05")
	}
	return t.Format("2006-01-02T15:04:05.000000")
}
