package inventario

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"go.mongodb.org/mongo-driver/v2/bson"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/testmongo"
)

const (
	tenant = "aaaa-eeee"
	dbName = "t_aaaaeeee"
)

func req(claims map[string]any, id string, qp map[string]string) platform.Request {
	r := platform.Request{PathParameters: map[string]string{"id": id}, QueryStringParameters: qp}
	r.RequestContext.Authorizer = map[string]any{"jwt": map[string]any{"claims": claims}}
	return r
}

func claims(email string, grupos ...any) map[string]any {
	return map[string]any{"email": email, "custom:tenant_id": tenant, "cognito:groups": grupos}
}

func llamar(t *testing.T, h platform.Handler, r platform.Request) (int, string, json.RawMessage) {
	t.Helper()
	resp, err := h(context.Background(), r)
	if err != nil {
		t.Fatalf("handler devolvió error: %v", err)
	}
	var s struct {
		Message string
		Data    json.RawMessage
	}
	if err := json.Unmarshal([]byte(resp.Body), &s); err != nil {
		t.Fatalf("body no es JSON: %v", err)
	}
	return resp.StatusCode, s.Message, s.Data
}

func TestValidacionesSinBaseDeDatos(t *testing.T) {
	if s, _, _ := llamar(t, Item, req(map[string]any{}, bson.NewObjectID().Hex(), nil)); s != 403 {
		t.Fatalf("item sin tenant: %d", s)
	}
	if _, err := Item(context.Background(), req(claims("a@t.mx"), "nope", nil)); err == nil {
		t.Fatal("item con id inválido debía dar ClientError (400)")
	}
	if s, _, _ := llamar(t, Traspasos, req(map[string]any{}, "", nil)); s != 403 {
		t.Fatalf("traspasos sin tenant: %d", s)
	}
}

func TestItemContraMongo(t *testing.T) {
	c := testmongo.Conectar(t, dbName)
	db := c.Database(dbName)
	ctx := context.Background()

	enS1, enS2 := bson.NewObjectID(), bson.NewObjectID()
	if _, err := db.Collection("items").InsertMany(ctx, []any{
		bson.D{{Key: "_id", Value: enS1}, {Key: "sucursal_id", Value: "s1"}, {Key: "nombre", Value: "Balata"}},
		bson.D{{Key: "_id", Value: enS2}, {Key: "sucursal_id", Value: "s2"}, {Key: "nombre", Value: "Filtro"}},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Collection("usuarios").InsertOne(ctx, bson.D{
		{Key: "email", Value: "cajero@t.mx"}, {Key: "sucursales", Value: bson.A{bson.D{{Key: "sucursal", Value: "s1"}}}},
	}); err != nil {
		t.Fatal(err)
	}
	cajero := claims("cajero@t.mx", "CAJERO")
	admin := claims("admin@t.mx", "ADMIN")

	s, _, data := llamar(t, Item, req(cajero, enS1.Hex(), nil))
	var item map[string]any
	_ = json.Unmarshal(data, &item)
	if s != 200 || item["id"] != enS1.Hex() || item["sucursalId"] != "s1" || item["sucursal_id"] != nil {
		t.Fatalf("cajero en su sucursal: %d %s", s, data)
	}
	if s, _, _ := llamar(t, Item, req(cajero, enS2.Hex(), nil)); s != 404 {
		t.Fatalf("cajero, item de otra sucursal: %d", s)
	}
	if s, m, _ := llamar(t, Item, req(cajero, enS2.Hex(), map[string]string{"sucursalId": "s2"})); s != 403 || m != platform.MsgSucursalAjena {
		t.Fatalf("cajero pide sucursal ajena: %d %q", s, m)
	}
	if s, _, _ := llamar(t, Item, req(cajero, enS2.Hex(), map[string]string{"ignoreScope": "1"})); s != 404 {
		t.Fatalf("ignoreScope no aplica a no-admin: %d", s)
	}
	if s, _, _ := llamar(t, Item, req(admin, enS2.Hex(), map[string]string{"sucursal_id": "s1"})); s != 404 {
		t.Fatalf("admin filtrando por s1: %d", s)
	}
	if s, _, _ := llamar(t, Item, req(admin, enS2.Hex(), map[string]string{"sucursalId": "s1", "ignoreScope": "1"})); s != 200 {
		t.Fatalf("admin con ignoreScope: %d", s)
	}
	if s, _, _ := llamar(t, Item, req(admin, enS2.Hex(), nil)); s != 200 {
		t.Fatalf("admin sin filtro: %d", s)
	}
}

func TestTraspasosContraMongo(t *testing.T) {
	c := testmongo.Conectar(t, dbName)
	db := c.Database(dbName)
	ctx := context.Background()
	base := time.Date(2026, 9, 1, 10, 0, 0, 0, time.UTC)
	docs := []any{
		bson.D{{Key: "tenant_id", Value: tenant}, {Key: "folio", Value: "T1"}, {Key: "origen_id", Value: "s1"}, {Key: "destino_id", Value: "s2"}, {Key: "estado", Value: "PENDIENTE"}, {Key: "createdAt", Value: base}},
		bson.D{{Key: "tenant_id", Value: tenant}, {Key: "folio", Value: "T2"}, {Key: "origen_id", Value: "s2"}, {Key: "destino_id", Value: "s1"}, {Key: "estado", Value: "RECIBIDO"}, {Key: "createdAt", Value: base.Add(time.Hour)}},
		bson.D{{Key: "tenant_id", Value: "otro"}, {Key: "folio", Value: "X"}, {Key: "origen_id", Value: "s1"}, {Key: "destino_id", Value: "s2"}, {Key: "createdAt", Value: base}},
	}
	for i := range 55 {
		docs = append(docs, bson.D{{Key: "tenant_id", Value: tenant}, {Key: "folio", Value: "V"}, {Key: "origen_id", Value: "s8"},
			{Key: "destino_id", Value: "s9"}, {Key: "createdAt", Value: base.Add(-time.Duration(i+1) * time.Hour)}})
	}
	if _, err := db.Collection("traspasos").InsertMany(ctx, docs); err != nil {
		t.Fatal(err)
	}

	folios := func(qp map[string]string) []string {
		t.Helper()
		s, m, data := llamar(t, Traspasos, req(claims("a@t.mx"), "", qp))
		var out struct{ Items []map[string]any }
		if err := json.Unmarshal(data, &out); err != nil || s != 200 || m != "Traspasos" {
			t.Fatalf("%v: %d %q %s", qp, s, m, data)
		}
		r := make([]string, len(out.Items))
		for i, it := range out.Items {
			r[i], _ = it["folio"].(string)
		}
		return r
	}
	eq := func(got []string, want ...string) {
		t.Helper()
		if len(got) != len(want) {
			t.Fatalf("got %v want %v", got, want)
		}
		for i := range want {
			if got[i] != want[i] {
				t.Fatalf("got %v want %v", got, want)
			}
		}
	}

	eq(folios(map[string]string{"sucursal_id": "s2"}), "T1")
	eq(folios(map[string]string{"sucursal_id": "s2", "tipo": "salientes"}), "T2")
	eq(folios(map[string]string{"sucursal_id": "s2", "tipo": "todos"}), "T2", "T1")
	eq(folios(map[string]string{"sucursal_id": "s1", "tipo": "todos", "estado": "PENDIENTE"}), "T1")
	if todos := folios(nil); len(todos) != limiteTraspasos || todos[0] != "T2" || todos[1] != "T1" {
		t.Fatalf("sin filtro: %d %v", len(todos), todos[:3])
	}

	_, _, data := llamar(t, Traspasos, req(claims("a@t.mx"), "", map[string]string{"sucursal_id": "s2"}))
	var out struct{ Items []map[string]any }
	_ = json.Unmarshal(data, &out)
	if out.Items[0]["createdAt"] != "2026-09-01T10:00:00Z" || out.Items[0]["id"] == nil {
		t.Fatalf("serialización: %v", out.Items[0])
	}
	if _, _, data := llamar(t, Traspasos, req(claims("a@t.mx"), "", map[string]string{"sucursal_id": "nada"})); string(data) != `{"items":[]}` {
		t.Fatalf("vacío: %s", data)
	}
}
