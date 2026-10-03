package platform_test

import (
	"context"
	"reflect"
	"testing"

	"go.mongodb.org/mongo-driver/v2/bson"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/testmongo"
)

func TestSucursalRef(t *testing.T) {
	casos := map[string]any{
		"s1": map[string]any{"sucursal": "s1"},
		"s2": map[string]any{"id": "s2"},
		"s3": map[string]any{"sucursal_id": "s3"},
		"s4": "s4",
		"":   42,
	}
	for want, item := range casos {
		if got := platform.SucursalRef(item); got != want {
			t.Fatalf("%v: got %q", item, got)
		}
	}
}

func TestFiltroSucursal(t *testing.T) {
	if got := platform.FiltroSucursal([]string{"a"}); got != "a" {
		t.Fatalf("una: %v", got)
	}
	want := bson.D{{Key: "$in", Value: []string{"a", "b"}}}
	if got := platform.FiltroSucursal([]string{"a", "b"}); !reflect.DeepEqual(got, want) {
		t.Fatalf("varias: %v", got)
	}
}

func TestSucursalScopeContraMongo(t *testing.T) {
	const dbName = "t_scope"
	c := testmongo.Conectar(t, dbName)
	db := c.Database(dbName)
	ctx := context.Background()
	if _, err := db.Collection("usuarios").InsertMany(ctx, []any{
		bson.D{{Key: "email", Value: "cajero@t.mx"}, {Key: "sucursales", Value: bson.A{
			bson.D{{Key: "sucursal", Value: "s1"}}, "s2", bson.D{{Key: "sucursal", Value: "s1"}},
		}}},
		bson.D{{Key: "email", Value: "sin@t.mx"}, {Key: "sucursales", Value: bson.A{}}},
	}); err != nil {
		t.Fatal(err)
	}
	claims := func(email string, grupos ...any) map[string]any {
		return map[string]any{"email": email, "custom:tenant_id": "t", "cognito:groups": grupos}
	}

	casos := []struct {
		nombre    string
		claims    map[string]any
		pedida    string
		scope     []string
		violacion string
	}{
		{"admin sin pedir", claims("a@t.mx", "ADMIN"), "", nil, ""},
		{"admin pide", claims("a@t.mx", "SUPER_ADMIN"), "sx", []string{"sx"}, ""},
		{"cajero sin pedir: todas, sin duplicados", claims("cajero@t.mx", "CAJERO"), "", []string{"s1", "s2"}, ""},
		{"cajero pide propia", claims("cajero@t.mx", "CAJERO"), "s2", []string{"s2"}, ""},
		{"cajero pide ajena", claims("cajero@t.mx", "CAJERO"), "s9", nil, platform.MsgSucursalAjena},
		{"sin sucursales", claims("sin@t.mx", "ASESOR"), "", nil, platform.MsgSinSucursales},
		{"usuario inexistente", claims("nadie@t.mx", "ASESOR"), "s1", nil, platform.MsgSinSucursales},
		{"sin email", map[string]any{"custom:tenant_id": "t"}, "", nil, platform.MsgSinSucursales},
	}
	for _, c := range casos {
		scope, violacion, err := platform.SucursalScope(ctx, c.claims, db, c.pedida)
		if err != nil {
			t.Fatalf("%s: %v", c.nombre, err)
		}
		if !reflect.DeepEqual(scope, c.scope) || violacion != c.violacion {
			t.Fatalf("%s: scope=%v violacion=%q", c.nombre, scope, violacion)
		}
	}
}
