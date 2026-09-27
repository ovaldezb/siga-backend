package catalogos

import (
	"context"
	"encoding/json"
	"testing"

	"go.mongodb.org/mongo-driver/v2/bson"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/testmongo"
)

func TestListModulosContraMongo(t *testing.T) {
	c := testmongo.Conectar(t, "_platform")
	col := c.Database("_platform").Collection("modulos")
	ctx := context.Background()

	resp, err := ListModulos(ctx, platform.Request{})
	if err != nil || resp.StatusCode != 200 || string(leer(t, resp).Data) != "[]" {
		t.Fatalf("vacío: %v %d %s", err, resp.StatusCode, resp.Body)
	}

	if _, err := col.InsertMany(ctx, []any{
		bson.D{{Key: "modulo", Value: "ordenes"}, {Key: "nombre", Value: "Órdenes"}},
		bson.D{{Key: "modulo", Value: "pos"}},
	}); err != nil {
		t.Fatal(err)
	}
	resp, err = ListModulos(ctx, platform.Request{})
	if err != nil {
		t.Fatal(err)
	}
	s := leer(t, resp)
	var modulos []map[string]any
	if err := json.Unmarshal(s.Data, &modulos); err != nil {
		t.Fatal(err)
	}
	if s.Message != "Módulos recuperados" || len(modulos) != 2 || modulos[0]["modulo"] != "ordenes" ||
		modulos[0]["nombre"] != "Órdenes" {
		t.Fatalf("modulos = %v", modulos)
	}
	for _, m := range modulos {
		if _, ok := m["id"]; ok {
			t.Fatalf("no debe exponer _id: %v", m)
		}
	}
}
