package talleres

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"go.mongodb.org/mongo-driver/v2/bson"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/testmongo"
)

func TestParseFechaPython(t *testing.T) {
	want := time.Date(2026, 10, 1, 12, 30, 0, 0, time.UTC)
	for _, s := range []string{
		"2026-10-01T12:30:00Z",
		"2026-10-01T12:30:00.000Z",
		"2026-10-01T12:30:00.123456",
		"2026-10-01T12:30:00+00:00",
		"2026-10-01T12:30:00-06:00", // el offset se descarta, como .replace(tzinfo=None)
		"2026-10-01T12:30",
		"2026-10-01 12:30:00",
	} {
		got, ok := parseFechaPython(s)
		if !ok || !got.Truncate(time.Minute).Equal(want) {
			t.Fatalf("%q → %v %v", s, got, ok)
		}
	}
	if got, ok := parseFechaPython("2026-10-01"); !ok || !got.Equal(time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)) {
		t.Fatalf("solo fecha: %v %v", got, ok)
	}
	for _, s := range []string{"", "mañana", "01/10/2026"} {
		if _, ok := parseFechaPython(s); ok {
			t.Fatalf("%q no debía reconocerse", s)
		}
	}
}

func TestVencido(t *testing.T) {
	now := time.Date(2026, 10, 1, 12, 0, 0, 0, time.UTC)
	casos := []struct {
		taller bson.M
		want   bool
	}{
		{bson.M{"proximaFechaPago": "2026-10-01T12:00:00Z"}, true}, // estado ausente = ACTIVO; igual cuenta
		{bson.M{"estado": "ACTIVO", "proximaFechaPago": "2026-10-01T12:00:01Z"}, false},
		{bson.M{"estado": "ACTIVO", "proximaFechaPago": bson.NewDateTimeFromTime(now.Add(-time.Hour))}, true},
		{bson.M{"estado": "INACTIVO", "proximaFechaPago": "2020-01-01"}, false},
		{bson.M{"estado": "ACTIVO", "proximaFechaPago": "no es fecha"}, false},
		{bson.M{"estado": "ACTIVO", "proximaFechaPago": nil}, false},
		{bson.M{"estado": "ACTIVO"}, false},
	}
	for _, c := range casos {
		if got := vencido(c.taller, now); got != c.want {
			t.Fatalf("%v: vencido=%v", c.taller, got)
		}
	}
}

func req(tenant string) platform.Request {
	r := platform.Request{}
	claims := map[string]any{"email": "admin@taller.com"}
	if tenant != "" {
		claims["custom:tenant_id"] = tenant
	}
	r.RequestContext.Authorizer = map[string]any{"jwt": map[string]any{"claims": claims}}
	return r
}

func llamar(t *testing.T, r platform.Request) (int, map[string]any) {
	t.Helper()
	resp, err := Modulos(context.Background(), r)
	if err != nil {
		t.Fatalf("handler devolvió error: %v", err)
	}
	var s struct{ Data map[string]any }
	if err := json.Unmarshal([]byte(resp.Body), &s); err != nil {
		t.Fatalf("body no es JSON: %v", err)
	}
	return resp.StatusCode, s.Data
}

func TestSuperAdminGlobal(t *testing.T) {
	status, data := llamar(t, req(""))
	if status != 200 || data["modulos"].([]any)[0] != "*" {
		t.Fatalf("status %d data %v", status, data)
	}
}

func TestModulosContraMongo(t *testing.T) {
	c := testmongo.Conectar(t, "_platform")
	col := c.Database("_platform").Collection("talleres")
	ctx := context.Background()
	now := time.Date(2026, 10, 1, 12, 0, 0, 0, time.UTC)
	ahora = func() time.Time { return now }
	t.Cleanup(func() { ahora = func() time.Time { return time.Now().UTC() } })

	_, err := col.InsertMany(ctx, []any{
		bson.D{
			{Key: "tenantId", Value: "vencido"}, {Key: "estado", Value: "ACTIVO"},
			{Key: "modulos", Value: bson.A{"ordenes", "pos"}}, {Key: "nombreComercial", Value: "Taller Express"},
			{Key: "proximaFechaPago", Value: bson.NewDateTimeFromTime(now.Add(-time.Minute))},
			{Key: "fechaSuscripcion", Value: bson.NewDateTimeFromTime(time.Date(2026, 1, 15, 0, 0, 0, 0, time.UTC))},
			{Key: "precioSuscripcion", Value: 499.0}, {Key: "usuarios", Value: 5}, {Key: "openpayClabe", Value: nil},
		},
		bson.D{
			{Key: "tenantId", Value: "al-corriente"}, {Key: "estado", Value: "ACTIVO"},
			{Key: "proximaFechaPago", Value: "2026-11-01T00:00:00Z"},
		},
	})
	if err != nil {
		t.Fatal(err)
	}

	status, data := llamar(t, req("vencido"))
	if status != 200 || data["estado"] != "INACTIVO" {
		t.Fatalf("vencido: %d %v", status, data)
	}
	var doc bson.M
	if err := col.FindOne(ctx, bson.D{{Key: "tenantId", Value: "vencido"}}).Decode(&doc); err != nil {
		t.Fatal(err)
	}
	if doc["estado"] != "INACTIVO" || doc["updatedAt"] == nil {
		t.Fatalf("no se persistió INACTIVO: %v", doc)
	}
	if data["nombreTaller"] != "Taller Express" || data["fechaSuscripcion"] != "2026-01-15T00:00:00Z" ||
		data["proximaFechaPago"] != "2026-10-01T11:59:00Z" || data["precioSuscripcion"] != 499.0 ||
		data["usuarios"] != 5.0 || data["openpayClabe"] != nil || data["openpayBank"] != "BBVA Bancomer" ||
		data["direccion"] != "Dirección no especificada" {
		t.Fatalf("respuesta = %v", data)
	}
	if m := data["modulos"].([]any); len(m) != 2 || m[0] != "ordenes" {
		t.Fatalf("modulos = %v", data["modulos"])
	}
	if _, ok := data["logoUrl"]; !ok {
		t.Fatal("logoUrl debe venir aunque sea null")
	}

	status, data = llamar(t, req("al-corriente"))
	if status != 200 || data["estado"] != "ACTIVO" || data["proximaFechaPago"] != "2026-11-01T00:00:00Z" ||
		data["nombreTaller"] != "SIGA" || len(data["modulos"].([]any)) != 0 {
		t.Fatalf("al corriente: %d %v", status, data)
	}

	if status, _ := llamar(t, req("no-existe")); status != 404 {
		t.Fatalf("inexistente: %d", status)
	}
}
