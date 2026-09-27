package cotizaciones

import (
	"context"
	"encoding/json"
	"slices"
	"testing"
	"time"

	"go.mongodb.org/mongo-driver/v2/bson"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/testmongo"
)

const (
	tenant = "aaaa-1234"
	dbName = "t_aaaa1234"
)

func req(claims map[string]any, qp map[string]string) platform.Request {
	r := platform.Request{QueryStringParameters: qp}
	r.RequestContext.Authorizer = map[string]any{"jwt": map[string]any{"claims": claims}}
	return r
}

func claims(email string, grupos ...any) map[string]any {
	return map[string]any{"email": email, "custom:tenant_id": tenant, "cognito:groups": grupos}
}

func llamar(t *testing.T, r platform.Request) (int, string, []map[string]any) {
	t.Helper()
	resp, err := List(context.Background(), r)
	if err != nil {
		if _, ok := err.(*platform.ClientError); ok {
			return 400, err.Error(), nil
		}
		t.Fatalf("handler devolvió error: %v", err)
	}
	var s struct {
		Message string
		Data    []map[string]any
	}
	if err := json.Unmarshal([]byte(resp.Body), &s); err != nil {
		t.Fatalf("body no es JSON: %v (%s)", err, resp.Body)
	}
	return resp.StatusCode, s.Message, s.Data
}

func folios(docs []map[string]any) []string {
	out := make([]string, len(docs))
	for i, d := range docs {
		out[i], _ = d["folio"].(string)
	}
	return out
}

func mismos(got []string, want ...string) bool {
	return slices.Equal(slices.Sorted(slices.Values(got)), slices.Sorted(slices.Values(want)))
}

func TestSinTenant(t *testing.T) {
	if s, _, _ := llamar(t, req(map[string]any{}, nil)); s != 403 {
		t.Fatalf("sin tenant: %d", s)
	}
}

func TestListContraMongo(t *testing.T) {
	c := testmongo.Conectar(t, dbName)
	db := c.Database(dbName)
	ctx := context.Background()
	base := time.Date(2026, 9, 1, 10, 0, 0, 0, time.UTC)
	cot := func(folio, tipo, sucursal, status string, hora int) bson.D {
		d := bson.D{{Key: "tenant_id", Value: tenant}, {Key: "folio", Value: folio}, {Key: "tipo", Value: tipo},
			{Key: "status", Value: status}, {Key: "createdAt", Value: base.Add(time.Duration(hora) * time.Hour)},
			{Key: "vigencia_hasta", Value: base.Add(30 * 24 * time.Hour)}}
		if sucursal != "" {
			d = append(d, bson.E{Key: "sucursal_id", Value: sucursal})
		}
		return d
	}
	if _, err := db.Collection("cotizaciones").InsertMany(ctx, []any{
		cot("P1", "PLANTILLA", "", "ACTIVA", 1),
		cot("C1", "CLIENTE", "s1", "BORRADOR", 2),
		cot("C2", "CLIENTE", "s2", "ENVIADA", 3),
		cot("C3", "CLIENTE", "s1", "CONVERTIDA", 4),
		bson.D{{Key: "tenant_id", Value: "otro"}, {Key: "folio", Value: "X"}, {Key: "tipo", Value: "CLIENTE"}},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Collection("usuarios").InsertOne(ctx, bson.D{
		{Key: "email", Value: "asesor@t.mx"}, {Key: "sucursales", Value: bson.A{bson.D{{Key: "sucursal", Value: "s1"}}}},
	}); err != nil {
		t.Fatal(err)
	}
	admin := claims("admin@t.mx", "ADMIN")
	asesor := claims("asesor@t.mx", "ASESOR")

	s, m, docs := llamar(t, req(admin, nil))
	if s != 200 || m != "Cotizaciones obtenidas" || len(docs) != 4 || folios(docs)[0] != "C3" || folios(docs)[3] != "P1" {
		t.Fatalf("admin todo: %d %q %v", s, m, folios(docs))
	}
	if docs[0]["id"] == nil || docs[0]["createdAt"] != "2026-09-01T14:00:00Z" || docs[0]["vigencia_hasta"] != "2026-10-01T10:00:00Z" {
		t.Fatalf("serialización: %v", docs[0])
	}

	casos := []struct {
		nombre string
		claims map[string]any
		qp     map[string]string
		want   []string
	}{
		{"asesor sin tipo: plantillas + cliente de su sucursal", asesor, nil, []string{"P1", "C1", "C3"}},
		{"asesor tipo cliente", asesor, map[string]string{"tipo": "cliente"}, []string{"C1", "C3"}},
		{"asesor plantillas sin scope", asesor, map[string]string{"tipo": "PLANTILLA"}, []string{"P1"}},
		{"asesor status en minúsculas", asesor, map[string]string{"status": "convertida"}, []string{"C3"}},
		{"admin pide s2", admin, map[string]string{"sucursalId": "s2"}, []string{"P1", "C2"}},
		{"admin pide s2 tipo cliente", admin, map[string]string{"sucursalId": "s2", "tipo": "CLIENTE"}, []string{"C2"}},
		{"limit", admin, map[string]string{"limit": " 2 "}, []string{"C3", "C2"}},
	}
	for _, c := range casos {
		s, _, docs := llamar(t, req(c.claims, c.qp))
		if s != 200 || !mismos(folios(docs), c.want...) {
			t.Fatalf("%s: %d %v", c.nombre, s, folios(docs))
		}
	}

	if s, m, _ := llamar(t, req(asesor, map[string]string{"sucursalId": "s2"})); s != 403 || m != platform.MsgSucursalAjena {
		t.Fatalf("sucursal ajena: %d %q", s, m)
	}
	if s, m, _ := llamar(t, req(admin, map[string]string{"tipo": "otro"})); s != 400 || m != "tipo inválido. Permitidos: ['CLIENTE', 'PLANTILLA']" {
		t.Fatalf("tipo inválido: %d %q", s, m)
	}
	if s, _, _ := llamar(t, req(admin, map[string]string{"limit": "abc"})); s != 400 {
		t.Fatalf("limit inválido: %d", s)
	}
	if s, _, docs := llamar(t, req(admin, map[string]string{"status": "NADA"})); s != 200 || docs == nil || len(docs) != 0 {
		t.Fatalf("vacío debe ser lista: %d %v", s, docs)
	}
}
