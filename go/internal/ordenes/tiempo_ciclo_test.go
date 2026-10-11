package ordenes

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"go.mongodb.org/mongo-driver/v2/bson"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/testmongo"
)

type estadisticaCiclo struct {
	MedianaHoras  float64 `json:"mediana_horas"`
	PromedioHoras float64 `json:"promedio_horas"`
	Muestras      int
}

type puntualidadCiclo struct {
	Porcentaje *float64
	ATiempo    int `json:"a_tiempo"`
	ConPromesa int `json:"con_promesa"`
	SinPromesa int `json:"sin_promesa"`
}

type resumenTiempoCiclo struct {
	Ordenes  int
	Trabajo  *estadisticaCiclo
	Estancia *estadisticaCiclo
	ATiempo  puntualidadCiclo `json:"a_tiempo"`
}

type tiempoCiclo struct {
	Mes         string
	MesAnterior string `json:"mes_anterior"`
	Resumen     resumenTiempoCiclo
	Anterior    resumenTiempoCiclo
	Etapas      []struct {
		Estado   string
		Actual   *estadisticaCiclo
		Anterior *estadisticaCiclo
	}
	Mecanicos []struct {
		MecanicoID *string `json:"mecanico_id"`
		Nombre     string
		Ordenes    int
		Trabajo    *estadisticaCiclo
		ATiempo    puntualidadCiclo `json:"a_tiempo"`
	}
	OrdenesLentas []map[string]any `json:"ordenes_lentas"`
	Truncado      bool
}

func pedirTiempoCiclo(t *testing.T, claims map[string]any, qp map[string]string) (int, tiempoCiclo) {
	t.Helper()
	r := platform.Request{QueryStringParameters: qp}
	r.RequestContext.Authorizer = map[string]any{"jwt": map[string]any{"claims": claims}}
	resp, err := TiempoCiclo(context.Background(), r)
	if err != nil {
		t.Fatalf("handler devolvió error: %v", err)
	}
	var s struct{ Data tiempoCiclo }
	if err := json.Unmarshal([]byte(resp.Body), &s); err != nil {
		t.Fatalf("body no es JSON: %v (%s)", err, resp.Body)
	}
	return resp.StatusCode, s.Data
}

// bitacoraCiclo arma una bitácora con pares estado, fecha.
func bitacoraCiclo(pares ...any) bson.A {
	a := bson.A{}
	for i := 0; i < len(pares); i += 2 {
		a = append(a, bson.D{{Key: "estado", Value: pares[i]}, {Key: "fecha", Value: pares[i+1]}})
	}
	return a
}

// mx: fecha y hora en el centro de México, como la ve el taller.
func mx(dia, hora int) time.Time {
	return time.Date(2026, 9, dia, hora, 0, 0, 0, zonaTaller)
}

func TestInicioDeMes(t *testing.T) {
	// 1 de octubre a las 03:00 UTC todavía es 30 de septiembre en el taller.
	now := time.Date(2026, 10, 1, 3, 0, 0, 0, time.UTC)
	if d, ok := inicioDeMes("", now); !ok || !d.Equal(time.Date(2026, 9, 1, 6, 0, 0, 0, time.UTC)) {
		t.Fatalf("mes en curso: %v %v", d, ok)
	}
	if d, ok := inicioDeMes(" 2026-02 ", now); !ok || !d.Equal(time.Date(2026, 2, 1, 6, 0, 0, 0, time.UTC)) {
		t.Fatalf("mes pedido: %v %v", d, ok)
	}
	for _, s := range []string{"2026", "2026-13", "09-2026", "2026-09-01"} {
		if _, ok := inicioDeMes(s, now); ok {
			t.Fatalf("%q no debía aceptarse", s)
		}
	}
}

func TestMedirCiclo(t *testing.T) {
	// Vuelve a EN_PROCESO: las dos pasadas se suman. Lista el día prometido a las 7 pm
	// (ya es el día siguiente en UTC) y recogida dos días después.
	c, ok := medirCiclo(bson.M{
		"estado": "ENTREGADO", "createdAt": mx(1, 9), "fechaEstimadaEntrega": "2026-09-03",
		"bitacora_estados": bitacoraCiclo(
			"RECEPCION", mx(1, 9), "EN_PROCESO", mx(1, 12), "COTIZADO", mx(2, 12),
			"EN_PROCESO", mx(2, 18), "FINALIZADO", platform.IsoUTC(mx(3, 19).UTC()), "ENTREGADO", mx(5, 19)),
	})
	if !ok || c.trabajo != 58 || c.estancia != 106 || !c.conPromesa || !c.aTiempo {
		t.Fatalf("ciclo: %v %+v", ok, c)
	}
	if c.etapas["RECEPCION"] != 3 || c.etapas["EN_PROCESO"] != 49 || c.etapas["COTIZADO"] != 6 ||
		c.etapas["FINALIZADO"] != 48 || len(c.etapas) != 4 {
		t.Fatalf("etapas: %v", c.etapas)
	}

	// Lista al día siguiente de lo prometido; sigue en el taller, sin estancia.
	c, ok = medirCiclo(bson.M{
		"estado": "FINALIZADO", "createdAt": mx(1, 9), "fechaEstimadaEntrega": "2026-09-01",
		"bitacora_estados": bitacoraCiclo("RECEPCION", mx(1, 9), "FINALIZADO", mx(2, 1)),
	})
	if !ok || c.trabajo != 16 || c.estancia != 0 || !c.conPromesa || c.aTiempo {
		t.Fatalf("tarde: %v %+v", ok, c)
	}

	// Entregada sin pasar por FINALIZADO y sin promesa: lista cuando se la llevaron.
	// La bitácora (corregida por el ADMIN) manda sobre createdAt para el ingreso.
	c, ok = medirCiclo(bson.M{
		"estado": "ENTREGADO", "createdAt": mx(2, 9), "fechaEstimadaEntrega": "",
		"bitacora_estados": bitacoraCiclo("RECEPCION", mx(1, 9), "ENTREGADO", mx(1, 14)),
	})
	if !ok || c.trabajo != 5 || c.estancia != 5 || c.conPromesa || !c.lista.Equal(mx(1, 14)) {
		t.Fatalf("sin finalizado: %v %+v", ok, c)
	}

	// Un FINALIZADO posterior a la entrega no alarga el trabajo más allá de la estancia.
	c, ok = medirCiclo(bson.M{
		"estado": "ENTREGADO", "createdAt": mx(1, 9),
		"bitacora_estados": bitacoraCiclo("RECEPCION", mx(1, 9), "FINALIZADO", mx(1, 12), "ENTREGADO", mx(1, 15), "FINALIZADO", mx(1, 20)),
	})
	if !ok || c.trabajo != 6 || c.estancia != 6 {
		t.Fatalf("finalizado tras la entrega: %v %+v", ok, c)
	}

	// Sin cierre en la bitácora, o cerrada antes de ingresar: no se puede medir.
	for _, d := range []bson.M{
		{"estado": "ENTREGADO", "createdAt": mx(1, 9)},
		{"estado": "FINALIZADO", "createdAt": mx(1, 9), "bitacora_estados": bitacoraCiclo("RECEPCION", mx(1, 9))},
		{"estado": "FINALIZADO", "bitacora_estados": bitacoraCiclo("FINALIZADO", mx(1, 9))},
	} {
		if _, ok := medirCiclo(d); ok {
			t.Fatalf("no debía medirse: %v", d)
		}
	}
}

func TestEstadisticaYPuntualidad(t *testing.T) {
	if estadistica(nil) != nil {
		t.Fatal("sin muestras debe ser nil")
	}
	horas := []float64{10, 2, 4, 100}
	e := estadistica(horas)
	if e["mediana_horas"] != 7.0 || e["promedio_horas"] != 29.0 || e["muestras"] != 4 || horas[0] != 10 {
		t.Fatalf("estadística: %v (entrada %v)", e, horas)
	}
	if e := estadistica([]float64{3, 1, 2}); e["mediana_horas"] != 2.0 {
		t.Fatalf("mediana impar: %v", e)
	}

	p := puntualidad([]cicloOrden{{conPromesa: true, aTiempo: true}, {conPromesa: true}, {conPromesa: true, aTiempo: true}, {}})
	if p["porcentaje"] != 66.7 || p["a_tiempo"] != 2 || p["con_promesa"] != 3 || p["sin_promesa"] != 1 {
		t.Fatalf("puntualidad: %v", p)
	}
	if p := puntualidad([]cicloOrden{{}}); p["porcentaje"] != nil {
		t.Fatalf("sin promesas no hay porcentaje: %v", p)
	}
}

func TestTiempoCicloContraMongo(t *testing.T) {
	c := testmongo.Conectar(t, dbName)
	col := c.Database(dbName).Collection("ordenes_servicio")
	ctx := context.Background()
	now := time.Date(2026, 9, 27, 12, 0, 0, 0, time.UTC)
	ahora = func() time.Time { return now }
	t.Cleanup(func() { ahora = func() time.Time { return time.Now().UTC() } })
	agosto := func(dia, hora int) time.Time { return time.Date(2026, 8, dia, hora, 0, 0, 0, zonaTaller) }

	if _, err := col.InsertMany(ctx, []any{
		// Septiembre, m1, a tiempo: 30 h de trabajo, 54 h de estancia.
		bson.D{{Key: "folio", Value: "OS-1"}, {Key: "estado", Value: "ENTREGADO"}, {Key: "sucursal_id", Value: "s1"},
			{Key: "createdAt", Value: mx(1, 9)}, {Key: "updatedAt", Value: mx(3, 15)},
			{Key: "mecanico_id", Value: "m1"}, {Key: "mecanico_nombre", Value: "Ana"},
			{Key: "fechaEstimadaEntrega", Value: "2026-09-02"},
			{Key: "cliente_snapshot", Value: bson.D{{Key: "nombre", Value: "Juan"}, {Key: "apellido_paterno", Value: "Pérez"}}},
			{Key: "vehiculo_snapshot", Value: bson.D{{Key: "marca", Value: "Nissan"}, {Key: "modelo", Value: "Versa"},
				{Key: "anio", Value: 2020}, {Key: "placas", Value: "ABC-1"}}},
			{Key: "bitacora_estados", Value: bitacoraCiclo("RECEPCION", mx(1, 9), "EN_PROCESO", mx(1, 13),
				"FINALIZADO", mx(2, 15), "ENTREGADO", mx(3, 15))}},
		// Septiembre, m1, tarde: 100 h de trabajo, sigue en el taller. updatedAt como texto.
		bson.D{{Key: "folio", Value: "OS-2"}, {Key: "estado", Value: "FINALIZADO"}, {Key: "sucursal_id", Value: "s1"},
			{Key: "createdAt", Value: mx(10, 8)}, {Key: "updatedAt", Value: isoformatPython(mx(14, 12))},
			{Key: "mecanico_id", Value: "m1"}, {Key: "mecanico_nombre", Value: "Ana"},
			{Key: "fechaEstimadaEntrega", Value: "2026-09-12"},
			{Key: "bitacora_estados", Value: bitacoraCiclo("RECEPCION", mx(10, 8), "COTIZADO", mx(10, 18),
				"EN_PROCESO", mx(13, 18), "FINALIZADO", mx(14, 12))}},
		// Septiembre, sin mecánico ni promesa, otra sucursal: 2 h.
		bson.D{{Key: "folio", Value: "OS-3"}, {Key: "estado", Value: "ENTREGADO"}, {Key: "sucursal_id", Value: "s2"},
			{Key: "createdAt", Value: mx(20, 10)}, {Key: "updatedAt", Value: mx(20, 13)},
			{Key: "bitacora_estados", Value: bitacoraCiclo("RECEPCION", mx(20, 10), "FINALIZADO", mx(20, 12), "ENTREGADO", mx(20, 13))}},
		// Agosto (mes anterior), tocada en septiembre: 10 h, a tiempo.
		bson.D{{Key: "folio", Value: "OS-4"}, {Key: "estado", Value: "ENTREGADO"}, {Key: "sucursal_id", Value: "s1"},
			{Key: "createdAt", Value: agosto(30, 8)}, {Key: "updatedAt", Value: mx(1, 10)},
			{Key: "fechaEstimadaEntrega", Value: "2026-08-31"},
			{Key: "bitacora_estados", Value: bitacoraCiclo("RECEPCION", agosto(30, 8), "FINALIZADO", agosto(30, 18), "ENTREGADO", mx(1, 10))}},
		// Fuera: cancelada, viva, y cerrada antes del mes anterior.
		bson.D{{Key: "folio", Value: "OS-5"}, {Key: "estado", Value: "CANCELADO"}, {Key: "updatedAt", Value: mx(5, 10)},
			{Key: "bitacora_estados", Value: bitacoraCiclo("RECEPCION", mx(4, 10), "FINALIZADO", mx(5, 9), "CANCELADO", mx(5, 10))}},
		bson.D{{Key: "folio", Value: "OS-6"}, {Key: "estado", Value: "EN_PROCESO"}, {Key: "updatedAt", Value: mx(5, 10)},
			{Key: "bitacora_estados", Value: bitacoraCiclo("RECEPCION", mx(4, 10), "EN_PROCESO", mx(5, 10))}},
		bson.D{{Key: "folio", Value: "OS-7"}, {Key: "estado", Value: "ENTREGADO"},
			{Key: "updatedAt", Value: time.Date(2026, 6, 20, 12, 0, 0, 0, time.UTC)},
			{Key: "bitacora_estados", Value: bitacoraCiclo("RECEPCION", time.Date(2026, 6, 19, 12, 0, 0, 0, time.UTC),
				"FINALIZADO", time.Date(2026, 6, 20, 12, 0, 0, 0, time.UTC))}},
	}); err != nil {
		t.Fatal(err)
	}
	admin := map[string]any{"custom:tenant_id": tenant, "cognito:groups": "[ADMIN]"}

	if s, _ := pedirTiempoCiclo(t, map[string]any{}, nil); s != 403 {
		t.Fatalf("sin tenant: %d", s)
	}
	if s, _ := pedirTiempoCiclo(t, map[string]any{"custom:tenant_id": tenant, "cognito:groups": "[MECANICO]"}, nil); s != 403 {
		t.Fatalf("mecánico: %d", s)
	}
	if s, _ := pedirTiempoCiclo(t, admin, map[string]string{"mes": "septiembre"}); s != 400 {
		t.Fatalf("mes inválido: %d", s)
	}

	s, k := pedirTiempoCiclo(t, admin, nil)
	if s != 200 || k.Mes != "2026-09" || k.MesAnterior != "2026-08" || k.Truncado {
		t.Fatalf("respuesta: %d %+v", s, k)
	}
	r := k.Resumen
	if r.Ordenes != 3 || r.Trabajo == nil || r.Trabajo.MedianaHoras != 30 || r.Trabajo.PromedioHoras != 44 ||
		r.Estancia == nil || r.Estancia.Muestras != 2 || r.Estancia.MedianaHoras != 28.5 {
		t.Fatalf("resumen: %+v %+v %+v", r, r.Trabajo, r.Estancia)
	}
	if r.ATiempo.Porcentaje == nil || *r.ATiempo.Porcentaje != 50 || r.ATiempo.ConPromesa != 2 || r.ATiempo.SinPromesa != 1 {
		t.Fatalf("a tiempo: %+v", r.ATiempo)
	}
	if a := k.Anterior; a.Ordenes != 1 || a.Trabajo.MedianaHoras != 10 || *a.ATiempo.Porcentaje != 100 {
		t.Fatalf("anterior: %+v", a)
	}

	if len(k.Etapas) != len(estadosTablero) || k.Etapas[0].Estado != "RECEPCION" {
		t.Fatalf("etapas: %+v", k.Etapas)
	}
	recepcion, aprobado, enProceso := k.Etapas[0], k.Etapas[2], k.Etapas[3]
	if recepcion.Actual.Muestras != 3 || recepcion.Actual.MedianaHoras != 4 || recepcion.Anterior.MedianaHoras != 10 ||
		aprobado.Actual != nil || aprobado.Anterior != nil ||
		enProceso.Actual.Muestras != 2 || enProceso.Actual.MedianaHoras != 22 || enProceso.Anterior != nil {
		t.Fatalf("etapas: %+v %+v %+v", recepcion.Actual, aprobado, enProceso.Actual)
	}

	if len(k.Mecanicos) != 2 || k.Mecanicos[0].Nombre != "Ana" || k.Mecanicos[0].Ordenes != 2 ||
		*k.Mecanicos[0].ATiempo.Porcentaje != 50 || k.Mecanicos[0].Trabajo.MedianaHoras != 65 ||
		k.Mecanicos[1].Nombre != "Sin asignar" || k.Mecanicos[1].MecanicoID != nil {
		t.Fatalf("mecánicos: %+v", k.Mecanicos)
	}

	if len(k.OrdenesLentas) != 3 {
		t.Fatalf("lentas: %v", k.OrdenesLentas)
	}
	lenta, rapida := k.OrdenesLentas[0], k.OrdenesLentas[2]
	if lenta["folio"] != "OS-2" || lenta["horas_trabajo"] != 100.0 || lenta["etapa_mas_lenta"] != "COTIZADO" ||
		lenta["horas_etapa"] != 72.0 || lenta["a_tiempo"] != false || lenta["horas_estancia"] != nil || lenta["id"] == "" {
		t.Fatalf("más lenta: %v", lenta)
	}
	if rapida["folio"] != "OS-3" || rapida["a_tiempo"] != nil || rapida["horas_estancia"] != 3.0 {
		t.Fatalf("más rápida: %v", rapida)
	}
	if os1 := k.OrdenesLentas[1]; os1["vehiculo"] != "Nissan Versa 2020 · ABC-1" || os1["cliente"] != "Juan Pérez" || os1["mecanico"] != "Ana" {
		t.Fatalf("textos: %v", os1)
	}

	// Mes explícito y filtro de sucursal.
	if _, k := pedirTiempoCiclo(t, admin, map[string]string{"mes": "2026-08"}); k.Resumen.Ordenes != 1 || k.Anterior.Ordenes != 0 || k.Anterior.Trabajo != nil {
		t.Fatalf("agosto: %+v", k.Resumen)
	}
	if _, k := pedirTiempoCiclo(t, admin, map[string]string{"sucursal_id": "s2"}); k.Resumen.Ordenes != 1 || k.Resumen.Trabajo.MedianaHoras != 2 {
		t.Fatalf("sucursal: %+v", k.Resumen)
	}
}
