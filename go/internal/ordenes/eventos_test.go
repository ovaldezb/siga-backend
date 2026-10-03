package ordenes

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"go.mongodb.org/mongo-driver/v2/bson"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/testmongo"
)

const (
	tenant = "aaaa-dddd"
	dbName = "t_aaaadddd"
)

func req(tenantID, id string) platform.Request {
	r := platform.Request{PathParameters: map[string]string{"id": id}}
	claims := map[string]any{}
	if tenantID != "" {
		claims["custom:tenant_id"] = tenantID
	}
	r.RequestContext.Authorizer = map[string]any{"jwt": map[string]any{"claims": claims}}
	return r
}

func llamar(t *testing.T, rq platform.Request) (int, []map[string]any) {
	t.Helper()
	resp, err := Eventos(context.Background(), rq)
	if err != nil {
		t.Fatalf("handler devolvió error: %v", err)
	}
	var s struct{ Data []map[string]any }
	if err := json.Unmarshal([]byte(resp.Body), &s); err != nil {
		t.Fatalf("body no es JSON: %v (%s)", err, resp.Body)
	}
	return resp.StatusCode, s.Data
}

func TestValidacionesSinBaseDeDatos(t *testing.T) {
	if s, _ := llamar(t, req("", bson.NewObjectID().Hex())); s != 403 {
		t.Fatalf("sin tenant: %d", s)
	}
	if s, _ := llamar(t, req(tenant, "nope")); s != 404 {
		t.Fatalf("id inválido: %d", s)
	}
}

func TestEventosContraMongo(t *testing.T) {
	c := testmongo.Conectar(t, dbName)
	db := c.Database(dbName)
	ctx := context.Background()

	os1 := bson.NewObjectID()
	vacia := bson.NewObjectID()
	if _, err := db.Collection("ordenes_servicio").InsertMany(ctx, []any{
		bson.D{{Key: "_id", Value: os1}}, bson.D{{Key: "_id", Value: vacia}},
	}); err != nil {
		t.Fatal(err)
	}
	base := time.Date(2026, 9, 1, 10, 0, 0, 0, time.UTC)
	if _, err := db.Collection("os_events").InsertMany(ctx, []any{
		bson.D{{Key: "orden_id", Value: os1.Hex()}, {Key: "tipo", Value: "os.estado_changed"}, {Key: "ts", Value: base.Add(time.Hour)}},
		bson.D{{Key: "orden_id", Value: os1.Hex()}, {Key: "tipo", Value: "os.created"}, {Key: "ts", Value: base},
			{Key: "meta", Value: bson.D{{Key: "ip", Value: "1.2.3.4"}}}},
		bson.D{{Key: "orden_id", Value: "otra"}, {Key: "tipo", Value: "os.created"}, {Key: "ts", Value: base}},
	}); err != nil {
		t.Fatal(err)
	}

	s, evs := llamar(t, req(tenant, os1.Hex()))
	if s != 200 || len(evs) != 2 {
		t.Fatalf("eventos: %d %v", s, evs)
	}
	if evs[0]["tipo"] != "os.created" || evs[0]["ts"] != "2026-09-01T10:00:00Z" || evs[1]["tipo"] != "os.estado_changed" {
		t.Fatalf("orden cronológico: %v", evs)
	}
	if evs[0]["id"] == nil || evs[0]["meta"].(map[string]any)["ip"] != "1.2.3.4" {
		t.Fatalf("serialización: %v", evs[0])
	}

	// Una OS sin eventos devuelve lista vacía, no null.
	resp, _ := Eventos(ctx, req(tenant, vacia.Hex()))
	if resp.StatusCode != 200 || !json.Valid([]byte(resp.Body)) || !strings.Contains(resp.Body, `"data":[]`) {
		t.Fatalf("sin eventos: %d %s", resp.StatusCode, resp.Body)
	}

	if s, _ := llamar(t, req(tenant, bson.NewObjectID().Hex())); s != 404 {
		t.Fatalf("OS inexistente: %d", s)
	}
}
