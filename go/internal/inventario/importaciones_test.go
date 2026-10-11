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

func TestImportacionesValidaciones(t *testing.T) {
	if s, _, _ := llamar(t, Importaciones, req(map[string]any{}, "", nil)); s != 403 {
		t.Fatalf("sin tenant: %d", s)
	}
	if s, _, _ := llamar(t, Importaciones, req(claims("a@t.mx", "ASESOR"), "", map[string]string{"sucursal_id": "s1"})); s != 403 {
		t.Fatalf("asesor: %d", s)
	}
	if s, m, _ := llamar(t, Importaciones, req(claims("a@t.mx", "ADMIN"), "", nil)); s != 400 || m != "Indique la sucursal a la que se importa." {
		t.Fatalf("sin sucursal: %d %q", s, m)
	}
}

func TestImportacionesContraMongo(t *testing.T) {
	c := testmongo.Conectar(t, dbName)
	db := c.Database(dbName)
	ctx := context.Background()

	var docs []any
	for i, estado := range []string{"APLICADA", "DESHECHA", "INCOMPLETA", "APLICANDO"} {
		docs = append(docs, bson.D{
			{Key: "sucursal_id", Value: "s1"}, {Key: "estado", Value: estado}, {Key: "archivo", Value: estado},
			{Key: "createdAt", Value: platform.IsoUTC(time.Date(2026, 10, 1+i, 0, 0, 0, 0, time.UTC))},
			{Key: "resumen", Value: bson.D{{Key: "nuevos", Value: 2}}},
		})
	}
	for i := range 25 {
		docs = append(docs, bson.D{{Key: "sucursal_id", Value: "s2"}, {Key: "estado", Value: "APLICADA"},
			{Key: "createdAt", Value: platform.IsoUTC(time.Date(2026, 9, 1, i, 0, 0, 0, time.UTC))}})
	}
	if _, err := db.Collection("importaciones_inventario").InsertMany(ctx, docs); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Collection("usuarios").InsertOne(ctx, bson.D{
		{Key: "email", Value: "gerente@t.mx"}, {Key: "sucursales", Value: bson.A{bson.D{{Key: "sucursal", Value: "s1"}}}},
	}); err != nil {
		t.Fatal(err)
	}

	lotes := func(cl map[string]any, sucursal string) (int, []map[string]any) {
		t.Helper()
		s, _, data := llamar(t, Importaciones, req(cl, "", map[string]string{"sucursal_id": sucursal}))
		var out []map[string]any
		_ = json.Unmarshal(data, &out)
		return s, out
	}

	s, l := lotes(claims("gerente@t.mx", "GERENTE"), "s1")
	if s != 200 || len(l) != 4 {
		t.Fatalf("gerente en su sucursal: %d %v", s, l)
	}
	// Más reciente primero; puede_deshacer sólo en APLICADA e INCOMPLETA.
	esperado := []struct {
		estado string
		puede  bool
	}{{"APLICANDO", false}, {"INCOMPLETA", true}, {"DESHECHA", false}, {"APLICADA", true}}
	for i, e := range esperado {
		if l[i]["estado"] != e.estado || l[i]["puede_deshacer"] != e.puede {
			t.Fatalf("lote %d: %v", i, l[i])
		}
	}
	if id, _ := l[0]["id"].(string); len(id) != 24 || l[0]["_id"] != nil {
		t.Fatalf("id: %v", l[0])
	}
	if r, _ := l[0]["resumen"].(map[string]any); r["nuevos"] != float64(2) {
		t.Fatalf("resumen: %v", l[0]["resumen"])
	}

	if s, _ := lotes(claims("gerente@t.mx", "GERENTE"), "s2"); s != 403 {
		t.Fatalf("gerente en sucursal ajena: %d", s)
	}
	if s, l := lotes(claims("admin@t.mx", "ADMIN"), "s2"); s != 200 || len(l) != limiteImportaciones {
		t.Fatalf("límite: %d %d", s, len(l))
	}
}
