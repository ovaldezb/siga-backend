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

type tablero struct {
	Items        []map[string]any
	Total        int64
	Truncado     bool
	HorasTipicas map[string]struct {
		Horas    float64
		Muestras int
	} `json:"horas_tipicas"`
}

func pedirTablero(t *testing.T, claims map[string]any, qp map[string]string) (int, tablero) {
	t.Helper()
	r := platform.Request{QueryStringParameters: qp}
	r.RequestContext.Authorizer = map[string]any{"jwt": map[string]any{"claims": claims}}
	resp, err := Tablero(context.Background(), r)
	if err != nil {
		t.Fatalf("handler devolvió error: %v", err)
	}
	var s struct{ Data tablero }
	if err := json.Unmarshal([]byte(resp.Body), &s); err != nil {
		t.Fatalf("body no es JSON: %v (%s)", err, resp.Body)
	}
	return resp.StatusCode, s.Data
}

func TestParseISOComoPython(t *testing.T) {
	want := time.Date(2026, 9, 1, 16, 0, 0, 0, time.UTC)
	for _, s := range []string{
		"2026-09-01T16:00:00Z", "2026-09-01T16:00:00.000Z", "2026-09-01T16:00:00+00:00",
		"2026-09-01T10:00:00-06:00", "2026-09-01T10:00:00-0600", "2026-09-01T10:00-06",
		"2026-09-01 16:00:00", "2026-09-01T16:00",
	} {
		if got, ok := parseISO(s); !ok || !got.Equal(want) {
			t.Fatalf("%q → %v %v", s, got, ok)
		}
	}
	if got, ok := parseISO("2026-09-01"); !ok || !got.Equal(time.Date(2026, 9, 1, 0, 0, 0, 0, time.UTC)) {
		t.Fatalf("solo fecha: %v", got)
	}
	for _, s := range []string{"", "ayer", "01/09/2026"} {
		if _, ok := parseISO(s); ok {
			t.Fatalf("%q no debía reconocerse", s)
		}
	}
}

func TestAuxiliares(t *testing.T) {
	if redondear1(0.25) != 0.2 || redondear1(0.35) != 0.4 || redondear1(12.36) != 12.4 {
		t.Fatal("redondeo a 1 decimal con mitades al par")
	}
	if diasCompletos(47*time.Hour) != 1 || diasCompletos(-time.Hour) != -1 {
		t.Fatal("días hacia abajo como timedelta.days")
	}
	if isoformatPython(time.Date(2026, 6, 29, 4, 30, 0, 0, time.UTC)) != "2026-06-29T04:30:00" ||
		isoformatPython(time.Date(2026, 6, 29, 4, 30, 0, 123_000_000, time.UTC)) != "2026-06-29T04:30:00.123000" {
		t.Fatal("isoformat de Python")
	}
}

func TestTableroContraMongo(t *testing.T) {
	c := testmongo.Conectar(t, dbName)
	col := c.Database(dbName).Collection("ordenes_servicio")
	ctx := context.Background()
	now := time.Date(2026, 9, 27, 12, 0, 0, 0, time.UTC)
	ahora = func() time.Time { return now }
	t.Cleanup(func() { ahora = func() time.Time { return time.Now().UTC() } })
	hace := func(h float64) time.Time { return now.Add(-time.Duration(h * float64(time.Hour))) }
	bit := func(pares ...any) bson.A {
		a := bson.A{}
		for i := 0; i < len(pares); i += 2 {
			a = append(a, bson.D{{Key: "estado", Value: pares[i]}, {Key: "fecha", Value: pares[i+1]}})
		}
		return a
	}

	if _, err := col.InsertMany(ctx, []any{
		// En proceso desde hace 5 h (bitácora con Date), ingresó hace 50 h, entrega vencida.
		bson.D{{Key: "folio", Value: "OS-1"}, {Key: "estado", Value: "EN_PROCESO"}, {Key: "sucursal_id", Value: "s1"},
			{Key: "createdAt", Value: hace(50)}, {Key: "mecanico_id", Value: "m1"},
			{Key: "fechaEstimadaEntrega", Value: "2026-09-26T10:00:00Z"},
			{Key: "total", Value: 1500.5}, {Key: "saldo_pendiente", Value: 500}, {Key: "pagada", Value: false},
			{Key: "cliente_snapshot", Value: bson.D{{Key: "nombre", Value: "Juan"}, {Key: "rfc", Value: "NOPROYECTAR"}}},
			{Key: "vehiculo_snapshot", Value: bson.D{{Key: "marca", Value: "Nissan"}, {Key: "placas", Value: "ABC-1"}}},
			{Key: "bitacora_estados", Value: bit("RECEPCION", hace(50), "EN_PROCESO", hace(20), "APROBADO", hace(10), "EN_PROCESO", hace(5))}},
		// Sin bitácora y con fechas como texto: cae a createdAt.
		bson.D{{Key: "folio", Value: "OS-2"}, {Key: "estado", Value: "RECEPCION"}, {Key: "sucursal_id", Value: "s2"},
			{Key: "createdAt", Value: "2026-09-27T06:00:00-06:00"}},
		// FINALIZADO con entrega vencida no cuenta como atrasada.
		bson.D{{Key: "folio", Value: "OS-3"}, {Key: "estado", Value: "FINALIZADO"}, {Key: "sucursal_id", Value: "s1"},
			{Key: "createdAt", Value: hace(30)}, {Key: "fechaEstimadaEntrega", Value: hace(1)}},
		// Fuera del tablero y con histórico para horas típicas.
		bson.D{{Key: "folio", Value: "OS-4"}, {Key: "estado", Value: "ENTREGADO"}, {Key: "sucursal_id", Value: "s1"},
			{Key: "createdAt", Value: hace(100)}, {Key: "updatedAt", Value: hace(24)},
			{Key: "bitacora_estados", Value: bit("RECEPCION", hace(100), "EN_PROCESO", hace(98), "FINALIZADO", hace(90), "ENTREGADO", hace(24))}},
		bson.D{{Key: "folio", Value: "OS-5"}, {Key: "estado", Value: "ENTREGADO"}, {Key: "sucursal_id", Value: "s2"},
			{Key: "updatedAt", Value: isoformatPython(hace(48))},
			{Key: "bitacora_estados", Value: bit("EN_PROCESO", isoformatPython(hace(60))+"Z", "RECEPCION", hace(64), "FINALIZADO", hace(56))}},
		// Cerrada hace más de 90 días: no entra al histórico.
		bson.D{{Key: "folio", Value: "OS-6"}, {Key: "estado", Value: "ENTREGADO"}, {Key: "updatedAt", Value: hace(24 * 100)},
			{Key: "bitacora_estados", Value: bit("EN_PROCESO", hace(24*105), "FINALIZADO", hace(24*101))}},
		bson.D{{Key: "folio", Value: "OS-7"}, {Key: "estado", Value: "CANCELADO"}, {Key: "createdAt", Value: hace(3)}},
	}); err != nil {
		t.Fatal(err)
	}
	admin := map[string]any{"custom:tenant_id": tenant, "cognito:groups": "[ADMIN]"}
	mecanico := map[string]any{"custom:tenant_id": tenant, "cognito:groups": "[MECANICO]"}

	if s, _ := pedirTablero(t, map[string]any{}, nil); s != 403 {
		t.Fatalf("sin tenant: %d", s)
	}

	s, tb := pedirTablero(t, admin, nil)
	if s != 200 || tb.Total != 3 || tb.Truncado || len(tb.Items) != 3 {
		t.Fatalf("tablero: %d %+v", s, tb)
	}
	por := map[string]map[string]any{}
	for _, it := range tb.Items {
		por[it["folio"].(string)] = it
	}
	// Orden por createdAt ascendente, como lo ordena Mongo: el texto va antes que los Date.
	if tb.Items[0]["folio"] != "OS-2" || tb.Items[1]["folio"] != "OS-1" {
		t.Fatalf("orden: %v", tb.Items[0]["folio"])
	}
	os1 := por["OS-1"]
	if os1["en_estado_desde"] != platform.IsoUTC(hace(5)) || os1["horas_en_estado"] != 5.0 ||
		os1["dias_en_taller"] != 2.0 || os1["atrasada"] != true || os1["bitacora_estados"] != nil ||
		os1["total"] != 1500.5 || os1["saldo_pendiente"] != 500.0 || os1["pagada"] != false {
		t.Fatalf("OS-1: %v", os1)
	}
	if cs := os1["cliente_snapshot"].(map[string]any); cs["nombre"] != "Juan" || cs["rfc"] != nil {
		t.Fatalf("proyección de cliente_snapshot: %v", cs)
	}
	if os2 := por["OS-2"]; os2["horas_en_estado"] != 0.0 || os2["en_estado_desde"] != "2026-09-27T12:00:00Z" ||
		os2["dias_en_taller"] != 0.0 || os2["atrasada"] != false || os2["createdAt"] != "2026-09-27T06:00:00-06:00" {
		t.Fatalf("OS-2 (texto, sin bitácora): %v", os2)
	}
	if os3 := por["OS-3"]; os3["atrasada"] != false || os3["horas_en_estado"] != 30.0 {
		t.Fatalf("OS-3 (finalizada): %v", os3)
	}

	// EN_PROCESO: OS-4 8 h, OS-5 4 h → mediana 6. RECEPCION: 2 y 4 → 3. FINALIZADO: solo OS-4 (66 h).
	ht := tb.HorasTipicas
	if len(ht) != 3 || ht["EN_PROCESO"].Horas != 6 || ht["EN_PROCESO"].Muestras != 2 ||
		ht["RECEPCION"].Horas != 3 || ht["FINALIZADO"].Horas != 66 || ht["FINALIZADO"].Muestras != 1 {
		t.Fatalf("horas típicas: %+v", ht)
	}

	_, tb = pedirTablero(t, admin, map[string]string{"sucursal_id": "s1"})
	if tb.Total != 2 || len(tb.HorasTipicas) != 3 || tb.HorasTipicas["EN_PROCESO"].Muestras != 1 {
		t.Fatalf("sucursal s1: %+v", tb)
	}
	if _, tb = pedirTablero(t, admin, map[string]string{"mecanico_id": "m1"}); tb.Total != 1 {
		t.Fatalf("mecánico: %+v", tb)
	}
	if _, tb = pedirTablero(t, admin, map[string]string{"q": "abc-1"}); tb.Total != 1 || tb.Items[0]["folio"] != "OS-1" {
		t.Fatalf("búsqueda por placas: %+v", tb)
	}
	if _, tb = pedirTablero(t, admin, map[string]string{"estado": "ENTREGADO, CANCELADO"}); tb.Total != 4 {
		t.Fatalf("estado explícito reemplaza las columnas: %+v", tb)
	}

	_, tb = pedirTablero(t, mecanico, nil)
	for _, it := range tb.Items {
		if _, ok := it["total"]; ok {
			t.Fatalf("mecánico ve importes: %v", it)
		}
		if _, ok := it["saldo_pendiente"]; ok {
			t.Fatalf("mecánico ve saldo: %v", it)
		}
	}
	if tb.Items[1]["pagada"] != false {
		t.Fatalf("pagada no es un importe: %v", tb.Items[1])
	}
}
