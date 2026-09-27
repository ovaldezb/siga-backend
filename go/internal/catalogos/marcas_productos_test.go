package catalogos

import (
	"context"
	"encoding/json"
	"testing"

	"go.mongodb.org/mongo-driver/v2/bson"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/testmongo"
)

func TestActiva(t *testing.T) {
	casos := map[bool][]any{
		true:  {map[string]any{}, map[string]any{"activa": true}, map[string]any{"activa": 1.0}, map[string]any{"activa": "si"}},
		false: {map[string]any{"activa": false}, map[string]any{"activa": nil}, map[string]any{"activa": 0.0}, map[string]any{"activa": ""}, "suelta"},
	}
	for want, ms := range casos {
		for _, m := range ms {
			if got := activa(m); got != want {
				t.Fatalf("%v: activa=%v", m, got)
			}
		}
	}
}

func TestMarcasProductosContraMongo(t *testing.T) {
	const tenant, dbName = "aaaa-9999", "t_aaaa9999"
	c := testmongo.Conectar(t, dbName)
	ctx := context.Background()
	llamar := func(tenantID, todas string) (int, []map[string]any) {
		t.Helper()
		r := platform.Request{QueryStringParameters: map[string]string{"todas": todas}}
		claims := map[string]any{}
		if tenantID != "" {
			claims["custom:tenant_id"] = tenantID
		}
		r.RequestContext.Authorizer = map[string]any{"jwt": map[string]any{"claims": claims}}
		resp, err := MarcasProductos(ctx, r)
		if err != nil {
			t.Fatal(err)
		}
		var s struct {
			Data struct{ Marcas []map[string]any }
		}
		if err := json.Unmarshal([]byte(resp.Body), &s); err != nil {
			t.Fatalf("body: %v %s", err, resp.Body)
		}
		return resp.StatusCode, s.Data.Marcas
	}

	if s, _ := llamar("", ""); s != 403 {
		t.Fatalf("sin tenant: %d", s)
	}
	if s, m := llamar(tenant, ""); s != 200 || m == nil || len(m) != 0 {
		t.Fatalf("sin configuración: %d %v", s, m)
	}

	if _, err := c.Database(dbName).Collection("configuracion").InsertOne(ctx, bson.D{
		{Key: "tenant_id", Value: tenant},
		{Key: "marcas", Value: bson.A{
			bson.D{{Key: "nombre", Value: "Bosch"}, {Key: "activa", Value: true}},
			bson.D{{Key: "nombre", Value: "Gonher"}},
			bson.D{{Key: "nombre", Value: "Vieja"}, {Key: "activa", Value: false}},
		}},
	}); err != nil {
		t.Fatal(err)
	}
	if _, m := llamar(tenant, ""); len(m) != 2 || m[0]["nombre"] != "Bosch" || m[1]["nombre"] != "Gonher" {
		t.Fatalf("activas: %v", m)
	}
	for _, todas := range []string{"true", "TRUE", "1", "yes"} {
		if _, m := llamar(tenant, todas); len(m) != 3 {
			t.Fatalf("todas=%s: %v", todas, m)
		}
	}
	if _, m := llamar(tenant, "no"); len(m) != 2 {
		t.Fatalf("todas=no: %v", m)
	}
}
