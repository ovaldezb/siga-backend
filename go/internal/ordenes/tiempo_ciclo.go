package ordenes

import (
	"context"
	"fmt"
	"slices"
	"sort"
	"strings"
	"time"

	"go.mongodb.org/mongo-driver/v2/bson"
	"go.mongodb.org/mongo-driver/v2/mongo/options"

	"siga-backend/go/internal/platform"
)

// KPI "Tiempo de ciclo y entregas a tiempo": cuánto tardan los autos en cada
// etapa y de punta a punta, y cuántos estuvieron listos el día prometido.
//
// Todo sale de `bitacora_estados` (un estado dura hasta la siguiente entrada),
// igual que las horas típicas del tablero. Una OS cuenta en el mes en que quedó
// lista (su último FINALIZADO), no en el que ingresó ni en el que se la llevaron.

const (
	// Tope de OS leídas por consulta (mes pedido + anterior). Un taller que lo
	// rebase necesita resúmenes mensuales precalculados, no esta lectura directa.
	maxOrdenesCiclo = 5000
	topLentas       = 10
)

// Centro de México, sin horario de verano desde 2022. Los límites del mes y la
// fecha prometida (que se guarda como día, sin hora) se leen en hora del taller:
// en UTC, un auto terminado a las 7 pm contaría como entregado al día siguiente.
// Fijo y no time.LoadLocation: provided.al2023 no trae la base de zonas.
var zonaTaller = time.FixedZone("America/Mexico_City", -6*60*60)

var proyeccionCiclo = bson.D{
	{Key: "folio", Value: 1}, {Key: "estado", Value: 1}, {Key: "createdAt", Value: 1},
	{Key: "bitacora_estados", Value: 1}, {Key: "fechaEstimadaEntrega", Value: 1},
	{Key: "mecanico_id", Value: 1}, {Key: "mecanico_nombre", Value: 1},
	{Key: "cliente_snapshot.nombre", Value: 1}, {Key: "cliente_snapshot.apellido_paterno", Value: 1},
	{Key: "vehiculo_snapshot.marca", Value: 1}, {Key: "vehiculo_snapshot.modelo", Value: 1},
	{Key: "vehiculo_snapshot.anio", Value: 1}, {Key: "vehiculo_snapshot.placas", Value: 1},
}

// cicloOrden es lo que se mide de una OS ya terminada.
type cicloOrden struct {
	doc        bson.M
	lista      time.Time // último FINALIZADO (o ENTREGADO si se saltó ese estado)
	trabajo    float64   // horas de ingreso a lista
	estancia   float64   // horas de ingreso a entregada; 0 si sigue en el taller
	etapas     map[string]float64
	conPromesa bool
	aTiempo    bool
}

// TiempoCiclo atiende GET /kpis/tiempo-ciclo[?mes=AAAA-MM&sucursal_id=…].
func TiempoCiclo(ctx context.Context, req platform.Request) (platform.Response, error) {
	claims := platform.Claims(req)
	tenantID := platform.ClaimString(claims, "custom:tenant_id")
	if tenantID == "" {
		return platform.JSON(req, 403, "No se encontró un tenantId asociado.", nil), nil
	}
	if platform.EsMecanico(claims) {
		return platform.JSON(req, 403, "Tu usuario no tiene acceso a los indicadores del taller.", nil), nil
	}
	qp := req.QueryStringParameters
	desde, ok := inicioDeMes(qp["mes"], ahora())
	if !ok {
		return platform.JSON(req, 400, "El mes debe tener el formato AAAA-MM.", nil), nil
	}
	hasta := desde.AddDate(0, 1, 0)
	antDesde := desde.AddDate(0, -1, 0)

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

	// updatedAt nunca es anterior al cierre de la OS, así que acota la lectura sin
	// dejar fuera ninguna del periodo; el mes exacto se decide con la bitácora.
	filtro := bson.D{
		{Key: "estado", Value: bson.D{{Key: "$in", Value: bson.A{"FINALIZADO", "ENTREGADO"}}}},
		// updatedAt existe como Date y como texto ISO según quién escribió la OS.
		{Key: "$or", Value: bson.A{
			bson.D{{Key: "updatedAt", Value: bson.D{{Key: "$gte", Value: antDesde}}}},
			bson.D{{Key: "updatedAt", Value: bson.D{{Key: "$gte", Value: isoformatPython(antDesde)}}}},
		}},
	}
	if scope != nil {
		filtro = append(filtro, bson.E{Key: "sucursal_id", Value: platform.FiltroSucursal(scope)})
	}
	cur, err := db.Collection("ordenes_servicio").Find(ctx, filtro, options.Find().
		SetProjection(proyeccionCiclo).
		SetSort(bson.D{{Key: "updatedAt", Value: -1}}).SetLimit(maxOrdenesCiclo))
	if err != nil {
		return platform.Response{}, err
	}
	var docs []bson.M
	if err := cur.All(ctx, &docs); err != nil {
		return platform.Response{}, err
	}

	var actuales, anteriores []cicloOrden
	for _, d := range docs {
		c, ok := medirCiclo(d)
		if !ok {
			continue
		}
		switch {
		case !c.lista.Before(desde) && c.lista.Before(hasta):
			actuales = append(actuales, c)
		case !c.lista.Before(antDesde) && c.lista.Before(desde):
			anteriores = append(anteriores, c)
		}
	}

	return platform.JSON(req, 200, "Tiempo de ciclo recuperado", map[string]any{
		"mes":            desde.Format("2006-01"),
		"mes_anterior":   antDesde.Format("2006-01"),
		"resumen":        resumenCiclo(actuales),
		"anterior":       resumenCiclo(anteriores),
		"etapas":         etapasCiclo(actuales, anteriores),
		"mecanicos":      mecanicosCiclo(actuales),
		"ordenes_lentas": lentasCiclo(actuales),
		"truncado":       len(docs) >= maxOrdenesCiclo,
	}), nil
}

// inicioDeMes: primer instante del mes pedido en hora del taller; sin `mes`, el
// mes en curso.
func inicioDeMes(mes string, now time.Time) (time.Time, bool) {
	if mes = strings.TrimSpace(mes); mes == "" {
		local := now.In(zonaTaller)
		return time.Date(local.Year(), local.Month(), 1, 0, 0, 0, 0, zonaTaller), true
	}
	t, err := time.ParseInLocation("2006-01", mes, zonaTaller)
	return t, err == nil
}

// medirCiclo saca de la bitácora los tiempos de una OS. false si no se puede
// saber cuándo ingresó o cuándo quedó lista.
func medirCiclo(d bson.M) (cicloOrden, bool) {
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
	sort.SliceStable(tramos, func(a, b int) bool { return tramos[a].fecha.Before(tramos[b].fecha) })

	c := cicloOrden{doc: d, etapas: map[string]float64{}}
	var entregada time.Time
	for _, t := range tramos {
		switch t.estado {
		case "FINALIZADO":
			c.lista = t.fecha
		case "ENTREGADO":
			entregada = t.fecha
		}
	}
	// Entregada sin pasar por FINALIZADO: quedó lista cuando se la llevaron.
	if c.lista.IsZero() {
		c.lista = entregada
	}
	if c.lista.IsZero() {
		return c, false
	}
	// Reabierta y vuelta a cerrar después de entregada: el auto ya no estaba.
	if d["estado"] == "ENTREGADO" && !entregada.IsZero() && c.lista.After(entregada) {
		c.lista = entregada
	}
	// El ingreso es la primera entrada de la bitácora: es la que corrige el ADMIN
	// cuando ajusta fechas. OS viejas sin esa entrada caen a createdAt.
	ingreso, ok := fechaUTC(d["createdAt"])
	if len(tramos) > 0 && (!ok || tramos[0].fecha.Before(ingreso)) {
		ingreso, ok = tramos[0].fecha, true
	}
	if !ok || !c.lista.After(ingreso) {
		return c, false
	}
	c.trabajo = c.lista.Sub(ingreso).Hours()
	if entregada.After(ingreso) && d["estado"] == "ENTREGADO" {
		c.estancia = entregada.Sub(ingreso).Hours()
	}

	// Una OS que vuelve a una etapa suma sus pasadas: cuenta una vez por etapa.
	for i := 0; i+1 < len(tramos); i++ {
		ini, fin := tramos[i], tramos[i+1]
		if slices.Contains(estadosTablero, ini.estado) && fin.fecha.After(ini.fecha) {
			c.etapas[ini.estado] += fin.fecha.Sub(ini.fecha).Hours()
		}
	}

	// La promesa es un día, sin hora: a tiempo si quedó lista ese día o antes.
	if s, _ := d["fechaEstimadaEntrega"].(string); len(s) >= 10 {
		if promesa, err := time.ParseInLocation("2006-01-02", s[:10], zonaTaller); err == nil {
			c.conPromesa = true
			c.aTiempo = c.lista.Before(promesa.AddDate(0, 0, 1))
		}
	}
	return c, true
}

// estadistica resume una lista de horas; nil si está vacía.
func estadistica(horas []float64) map[string]any {
	if len(horas) == 0 {
		return nil
	}
	horas = slices.Clone(horas)
	sort.Float64s(horas)
	mitad := len(horas) / 2
	mediana := horas[mitad]
	if len(horas)%2 == 0 {
		mediana = (horas[mitad-1] + horas[mitad]) / 2
	}
	suma := 0.0
	for _, h := range horas {
		suma += h
	}
	return map[string]any{
		"mediana_horas":  redondear1(mediana),
		"promedio_horas": redondear1(suma / float64(len(horas))),
		"muestras":       len(horas),
	}
}

// puntualidad: de las OS con fecha prometida, cuántas estuvieron listas a tiempo.
func puntualidad(ciclos []cicloOrden) map[string]any {
	con, aTiempo := 0, 0
	for _, c := range ciclos {
		if c.conPromesa {
			con++
			if c.aTiempo {
				aTiempo++
			}
		}
	}
	out := map[string]any{"porcentaje": nil, "a_tiempo": aTiempo, "con_promesa": con, "sin_promesa": len(ciclos) - con}
	if con > 0 {
		out["porcentaje"] = redondear1(float64(aTiempo) * 100 / float64(con))
	}
	return out
}

func resumenCiclo(ciclos []cicloOrden) map[string]any {
	var trabajo, estancia []float64
	for _, c := range ciclos {
		trabajo = append(trabajo, c.trabajo)
		if c.estancia > 0 {
			estancia = append(estancia, c.estancia)
		}
	}
	return map[string]any{
		"ordenes":  len(ciclos),
		"trabajo":  estadistica(trabajo),
		"estancia": estadistica(estancia),
		"a_tiempo": puntualidad(ciclos),
	}
}

// etapasCiclo: una fila por etapa, en el orden del tablero, con su valor del mes
// anterior. Solo promedia las OS que pasaron por la etapa: muchas se saltan pasos.
func etapasCiclo(actuales, anteriores []cicloOrden) []map[string]any {
	horas := func(ciclos []cicloOrden, estado string) []float64 {
		var out []float64
		for _, c := range ciclos {
			if h, ok := c.etapas[estado]; ok {
				out = append(out, h)
			}
		}
		return out
	}
	out := make([]map[string]any, 0, len(estadosTablero))
	for _, estado := range estadosTablero {
		fila := map[string]any{"estado": estado, "actual": estadistica(horas(actuales, estado)),
			"anterior": estadistica(horas(anteriores, estado))}
		out = append(out, fila)
	}
	return out
}

func mecanicosCiclo(ciclos []cicloOrden) []map[string]any {
	type grupo struct {
		nombre string
		ciclos []cicloOrden
	}
	grupos := map[string]*grupo{}
	var orden []string
	for _, c := range ciclos {
		id, _ := c.doc["mecanico_id"].(string)
		g := grupos[id]
		if g == nil {
			g = &grupo{nombre: "Sin asignar"}
			grupos[id] = g
			orden = append(orden, id)
		}
		if n, _ := c.doc["mecanico_nombre"].(string); n != "" && id != "" {
			g.nombre = n
		}
		g.ciclos = append(g.ciclos, c)
	}
	out := make([]map[string]any, 0, len(orden))
	for _, id := range orden {
		g := grupos[id]
		var trabajo []float64
		for _, c := range g.ciclos {
			trabajo = append(trabajo, c.trabajo)
		}
		var mecanicoID any
		if id != "" {
			mecanicoID = id
		}
		out = append(out, map[string]any{
			"mecanico_id": mecanicoID,
			"nombre":      g.nombre,
			"ordenes":     len(g.ciclos),
			"trabajo":     estadistica(trabajo),
			"a_tiempo":    puntualidad(g.ciclos),
		})
	}
	sort.SliceStable(out, func(a, b int) bool { return out[a]["ordenes"].(int) > out[b]["ordenes"].(int) })
	return out
}

// lentasCiclo: las OS que más tardaron en quedar listas, con la etapa donde más
// tiempo se quedaron.
func lentasCiclo(ciclos []cicloOrden) []map[string]any {
	ciclos = slices.Clone(ciclos)
	sort.SliceStable(ciclos, func(a, b int) bool { return ciclos[a].trabajo > ciclos[b].trabajo })
	if len(ciclos) > topLentas {
		ciclos = ciclos[:topLentas]
	}
	out := make([]map[string]any, 0, len(ciclos))
	for _, c := range ciclos {
		var etapa any
		var horasEtapa any
		max := 0.0
		for _, estado := range estadosTablero {
			// FINALIZADO es la espera del cliente, no dónde se atoró el trabajo.
			if h := c.etapas[estado]; estado != "FINALIZADO" && h > max {
				max, etapa, horasEtapa = h, estado, redondear1(h)
			}
		}
		var estancia, aTiempo any
		if c.estancia > 0 {
			estancia = redondear1(c.estancia)
		}
		if c.conPromesa {
			aTiempo = c.aTiempo
		}
		id := ""
		if oid, ok := c.doc["_id"].(bson.ObjectID); ok {
			id = oid.Hex()
		}
		out = append(out, map[string]any{
			"id":              id,
			"folio":           c.doc["folio"],
			"vehiculo":        textoVehiculo(documento(c.doc["vehiculo_snapshot"])),
			"cliente":         textoCliente(documento(c.doc["cliente_snapshot"])),
			"mecanico":        c.doc["mecanico_nombre"],
			"horas_trabajo":   redondear1(c.trabajo),
			"horas_estancia":  estancia,
			"etapa_mas_lenta": etapa,
			"horas_etapa":     horasEtapa,
			"a_tiempo":        aTiempo,
			"lista_en":        platform.IsoUTC(c.lista),
		})
	}
	return out
}

func textoVehiculo(v map[string]any) string {
	var partes []string
	for _, campo := range []string{"marca", "modelo", "anio"} {
		if s := strings.TrimSpace(fmt.Sprint(v[campo])); v[campo] != nil && s != "" {
			partes = append(partes, s)
		}
	}
	texto := strings.Join(partes, " ")
	if placas, _ := v["placas"].(string); placas != "" {
		texto = strings.TrimSpace(texto + " · " + placas)
	}
	return texto
}

func textoCliente(c map[string]any) string {
	nombre, _ := c["nombre"].(string)
	apellido, _ := c["apellido_paterno"].(string)
	return strings.TrimSpace(nombre + " " + apellido)
}
