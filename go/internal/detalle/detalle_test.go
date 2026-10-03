package detalle

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
	tenant = "aaaa-bbbb"
	dbName = "t_aaaabbbb"
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

func llamar(t *testing.T, r Recurso, rq platform.Request) (int, string, map[string]any) {
	t.Helper()
	resp, err := r.Handler()(context.Background(), rq)
	if err != nil {
		t.Fatalf("handler devolvió error: %v", err)
	}
	var s struct {
		Message string
		Data    map[string]any
	}
	if err := json.Unmarshal([]byte(resp.Body), &s); err != nil {
		t.Fatalf("body no es JSON: %v", err)
	}
	return resp.StatusCode, s.Message, s.Data
}

func TestValidacionesSinBaseDeDatos(t *testing.T) {
	for _, r := range []Recurso{Proveedor, Cita, Compra, Cotizacion} {
		if s, m, _ := llamar(t, r, req("", bson.NewObjectID().Hex())); s != 403 || m != r.SinTenant {
			t.Fatalf("%s sin tenant: %d %q", r.Coleccion, s, m)
		}
		for _, id := range []string{"", "xyz"} {
			if s, m, _ := llamar(t, r, req(tenant, id)); s != 400 || m != r.IDInvalido {
				t.Fatalf("%s id %q: %d %q", r.Coleccion, id, s, m)
			}
		}
	}
}

func TestDetalleContraMongo(t *testing.T) {
	c := testmongo.Conectar(t, dbName)
	db := c.Database(dbName)
	ctx := context.Background()
	fecha := time.Date(2026, 9, 26, 18, 30, 0, 0, time.UTC)

	for _, r := range []Recurso{Proveedor, Cita, Compra, Cotizacion} {
		oid := bson.NewObjectID()
		ref := bson.NewObjectID()
		_, err := db.Collection(r.Coleccion).InsertOne(ctx, bson.D{
			{Key: "_id", Value: oid}, {Key: "tenant_id", Value: tenant},
			{Key: "nombre", Value: "Ñandú"}, {Key: "createdAt", Value: fecha},
			{Key: "items", Value: bson.A{bson.D{{Key: "ref", Value: ref}, {Key: "cantidad", Value: 2}}}},
		})
		if err != nil {
			t.Fatal(err)
		}

		s, m, data := llamar(t, r, req(tenant, oid.Hex()))
		if s != 200 || m != r.Encontrado {
			t.Fatalf("%s: %d %q", r.Coleccion, s, m)
		}
		if data["id"] != oid.Hex() || data["_id"] != nil || data["nombre"] != "Ñandú" ||
			data["createdAt"] != "2026-09-26T18:30:00Z" {
			t.Fatalf("%s data = %v", r.Coleccion, data)
		}
		item := data["items"].([]any)[0].(map[string]any)
		if item["ref"] != ref.Hex() || item["cantidad"] != 2.0 {
			t.Fatalf("%s items = %v", r.Coleccion, data["items"])
		}

		if s, m, _ := llamar(t, r, req(tenant, bson.NewObjectID().Hex())); s != 404 || m != r.NoEncontrado {
			t.Fatalf("%s inexistente: %d %q", r.Coleccion, s, m)
		}
	}

	// Una cotización de otro tenant_id guardada en la misma base no se devuelve.
	ajena := bson.NewObjectID()
	if _, err := db.Collection("cotizaciones").InsertOne(ctx, bson.D{
		{Key: "_id", Value: ajena}, {Key: "tenant_id", Value: "otro"},
	}); err != nil {
		t.Fatal(err)
	}
	if s, _, _ := llamar(t, Cotizacion, req(tenant, ajena.Hex())); s != 404 {
		t.Fatalf("cotización de otro tenant: %d", s)
	}
}
