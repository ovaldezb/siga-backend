package clientes

import (
	"context"
	"encoding/json"
	"testing"

	"go.mongodb.org/mongo-driver/v2/bson"

	"siga-backend/go/internal/platform"
	"siga-backend/go/internal/testmongo"
)

func TestClavesNormalizadas(t *testing.T) {
	casos := []struct{ got, want string }{
		{telefonoClave("+52 (55) 1234-5678"), "5512345678"},
		{telefonoClave("5512345678"), "5512345678"},
		{telefonoClave("12345"), ""},
		{telefonoClave("0000000000"), ""},
		{emailClave("  Juan@Mail.COM "), "juan@mail.com"},
		{emailClave("sin correo"), ""},
		{rfcClave("pepj800101 ab1"), "PEPJ800101AB1"},
		{rfcClave("XAXX010101000"), ""},
		{placasClave("abc-12-34"), "ABC1234"},
		{placasClave("S/P"), ""},
		{nombreClave(map[string]any{"nombre": "José", "apellido_paterno": "Pérez "}), "jose perez"},
		{nombreClave(map[string]any{"nombre": "Juan"}), ""},
		{nombreClave(map[string]any{"nombre": "Público", "apellido_paterno": "General"}), ""},
	}
	for i, c := range casos {
		if c.got != c.want {
			t.Errorf("caso %d: %q, quería %q", i, c.got, c.want)
		}
	}
}

func llamarDuplicados(t *testing.T, claims map[string]any, qp map[string]string) (int, map[string]any) {
	t.Helper()
	r := platform.Request{QueryStringParameters: qp}
	r.RequestContext.Authorizer = map[string]any{"jwt": map[string]any{"claims": claims}}
	resp, err := Duplicados(context.Background(), r)
	if err != nil {
		t.Fatalf("handler devolvió error: %v", err)
	}
	var s struct{ Data map[string]any }
	if err := json.Unmarshal([]byte(resp.Body), &s); err != nil {
		t.Fatalf("body: %v %s", err, resp.Body)
	}
	return resp.StatusCode, s.Data
}

func TestDuplicadosContraMongo(t *testing.T) {
	c := testmongo.Conectar(t, dbName)
	db := c.Database(dbName)
	ctx := context.Background()
	norte, sur := bson.NewObjectID(), bson.NewObjectID()
	juanN, juanS, ana, pedro, luis := bson.NewObjectID(), bson.NewObjectID(), bson.NewObjectID(), bson.NewObjectID(), bson.NewObjectID()
	if _, err := db.Collection("sucursales").InsertMany(ctx, []any{
		bson.D{{Key: "_id", Value: norte}, {Key: "nombre", Value: "Norte"}},
		bson.D{{Key: "_id", Value: sur}, {Key: "nombre", Value: "Sur"}},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Collection("clientes").InsertMany(ctx, []any{
		bson.D{{Key: "_id", Value: juanN}, {Key: "nombre", Value: "Juan"}, {Key: "apellido_paterno", Value: "Pérez"},
			{Key: "telefono", Value: "55 1234 5678"}, {Key: "sucursal_id", Value: norte.Hex()}},
		bson.D{{Key: "_id", Value: juanS}, {Key: "nombre", Value: "JUAN"}, {Key: "apellido_paterno", Value: "Perez"},
			{Key: "telefono", Value: "+525512345678"}, {Key: "sucursal_id", Value: sur.Hex()}},
		bson.D{{Key: "_id", Value: ana}, {Key: "nombre", Value: "Ana"}, {Key: "apellido_paterno", Value: "López"},
			{Key: "email", Value: "ana@x.com"}, {Key: "rfc", Value: "XAXX010101000"}},
		// Pedro y Luis no comparten datos personales, pero sí el vehículo.
		bson.D{{Key: "_id", Value: pedro}, {Key: "nombre", Value: "Pedro"}, {Key: "apellido_paterno", Value: "Ruiz"}, {Key: "rfc", Value: "XAXX010101000"}},
		bson.D{{Key: "_id", Value: luis}, {Key: "nombre", Value: "Luis"}, {Key: "apellido_paterno", Value: "Gómez"}},
		// Teléfono del taller capturado en tres clientes distintos: es de relleno.
		bson.D{{Key: "nombre", Value: "Omnibus"}, {Key: "apellido_paterno", Value: "SA"}, {Key: "telefono", Value: "4491382401"}},
		bson.D{{Key: "nombre", Value: "Rugo"}, {Key: "apellido_paterno", Value: "Transportes"}, {Key: "telefono", Value: "4491382401"}},
		bson.D{{Key: "nombre", Value: "Prueba"}, {Key: "apellido_paterno", Value: "Prueba"}, {Key: "telefono", Value: "449 138 2401"}},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Collection("vehiculos").InsertMany(ctx, []any{
		bson.D{{Key: "cliente_id", Value: pedro.Hex()}, {Key: "placas", Value: "ABC-123-A"}},
		bson.D{{Key: "cliente_id", Value: luis.Hex()}, {Key: "placas", Value: "abc123a"}},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Collection("ordenes_servicio").InsertOne(ctx,
		bson.D{{Key: "cliente_snapshot", Value: bson.D{{Key: "id", Value: juanN.Hex()}}}}); err != nil {
		t.Fatal(err)
	}
	asesor := map[string]any{"custom:tenant_id": tenant}
	admin := map[string]any{"custom:tenant_id": tenant, "cognito:groups": "ADMIN"}

	// Alta en la sucursal Sur de alguien que ya existe en Norte.
	st, data := llamarDuplicados(t, asesor, map[string]string{"telefono": "5512345678", "nombre": "juan", "apellido_paterno": "pérez"})
	items, _ := data["items"].([]any)
	if st != 200 || len(items) != 2 {
		t.Fatalf("coincidencias: %d %v", st, data)
	}
	primero := items[0].(map[string]any)
	if motivos := primero["coincide_por"].([]any); len(motivos) != 2 || motivos[0] != PorTelefono || motivos[1] != PorNombre {
		t.Errorf("motivos: %v", motivos)
	}
	if primero["sucursalNombre"] == nil {
		t.Errorf("sin sucursalNombre: %v", primero)
	}

	// `excluir` deja fuera al propio cliente (al editarlo).
	_, data = llamarDuplicados(t, asesor, map[string]string{"telefono": "5512345678", "excluir": juanN.Hex()})
	if items, _ := data["items"].([]any); len(items) != 1 || items[0].(map[string]any)["id"] != juanS.Hex() {
		t.Errorf("excluir: %v", data)
	}

	// Por placas, sin importar guiones ni mayúsculas.
	_, data = llamarDuplicados(t, asesor, map[string]string{"placas": "ABC 123 A"})
	if items, _ := data["items"].([]any); len(items) != 2 {
		t.Errorf("placas: %v", data)
	}

	// Datos que no identifican a nadie (RFC genérico) no traen a toda la cartera.
	_, data = llamarDuplicados(t, asesor, map[string]string{"rfc": "XAXX010101000"})
	if items, _ := data["items"].([]any); len(items) != 0 {
		t.Errorf("rfc genérico: %v", data)
	}

	// Un teléfono de relleno no trae a todos los que lo comparten.
	_, data = llamarDuplicados(t, asesor, map[string]string{"telefono": "4491382401"})
	if items, _ := data["items"].([]any); len(items) != 0 {
		t.Errorf("teléfono de relleno: %v", data)
	}

	// Los grupos son sólo para ADMIN.
	if st, _ := llamarDuplicados(t, asesor, nil); st != 403 {
		t.Errorf("asesor viendo grupos: %d", st)
	}
	st, data = llamarDuplicados(t, admin, nil)
	grupos, _ := data["grupos"].([]any)
	if st != 200 || len(grupos) != 2 {
		t.Fatalf("grupos: %d %v", st, data)
	}
	g0 := grupos[0].(map[string]any)
	if g0["coincide_por"].([]any)[0] != PorTelefono || len(g0["clientes"].([]any)) != 2 {
		t.Errorf("grupo teléfono: %v", g0)
	}
	for _, cl := range g0["clientes"].([]any) {
		m := cl.(map[string]any)
		if m["id"] == juanN.Hex() && m["num_ordenes"] != float64(1) {
			t.Errorf("num_ordenes: %v", m)
		}
	}
	if g1 := grupos[1].(map[string]any); g1["coincide_por"].([]any)[0] != PorPlacas {
		t.Errorf("grupo placas: %v", g1)
	}
}
